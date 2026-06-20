"""分層 watchlist 管理 + 動態交易層 refresh。

三層：
    指標 (Indicator):  BTC / ETH / SOL  → 規範 regime，不交易
    現貨 (Spot):       SUI / WLFI       → 監控不交易
    交易 (Trading):    動態 Top 7-10    → Setup A/B 在此 FIRE

refresh 策略：
    - 啟動時：立即 refresh trading tier
    - 每週一 00:00 UTC：完整重排
    - 每日 00:00 UTC：補位（top 30 中跌出的踢掉）
"""
from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field

from market_intel_mcp.symbol_mapping import (
    TIER_INDICATOR,
    TIER_SPOT,
    TRADING_CANDIDATES,
    HOT_SYMBOLS,
)


def _market_candidates(min_vol_usd: float = 20_000_000, cap: int = 120) -> list[str]:
    """v31: 從掃描器即時全市場快照取候選池（取代固定 29 檔策展清單）。
    回 vol>=門檻、依量排序的 base symbols；失敗回空（呼叫端 fallback）。"""
    import sqlite3
    from botpaths import db_path
    try:
        conn = sqlite3.connect(db_path("scanner.db"))
        try:
            mx = conn.execute("SELECT MAX(ts) FROM snapshots").fetchone()[0]
            if not mx:
                return []
            rows = conn.execute(
                "SELECT inst, vol24h_usd FROM snapshots WHERE ts=? AND vol24h_usd>=? "
                "ORDER BY vol24h_usd DESC LIMIT ?", (mx, min_vol_usd, cap)).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()
    except Exception:
        return []


@dataclass
class WatchlistManager:
    indicator: tuple[str, ...] = TIER_INDICATOR     # 固定
    spot: tuple[str, ...] = TIER_SPOT               # 固定
    trading: list[str] = field(default_factory=list)  # 動態
    last_refresh: dt.datetime | None = None
    trading_size: int = 8                            # 7-10 範圍中選 8
    candidate_pool: tuple[str, ...] = TRADING_CANDIDATES
    # v54-3: #35 陰陽共生「做空候選」影子層。預設 short_tier_size=0 ＝ 永遠休眠：
    #   short_tier 一律空、永不進 fire_tier()/all_symbols/scheduler，純觀測用。
    short_tier: list[str] = field(default_factory=list)
    short_tier_size: int = 0

    @property
    def all_symbols(self) -> list[str]:
        """全部需要 fetch snapshot 的 symbol（去重）"""
        seen: list[str] = []
        for s in list(self.indicator) + list(self.spot) + self.trading:
            if s not in seen:
                seen.append(s)
        return seen

    def fire_tier(self) -> list[str]:
        """FIRE-scan 只跑交易層；指標+現貨不會觸發訊號"""
        # 從 trading 中排除 spot（避免和現貨倉位衝突）
        return [s for s in self.trading if s not in self.spot]

    def is_spot(self, sym: str) -> bool:
        return sym in self.spot

    def is_indicator(self, sym: str) -> bool:
        return sym in self.indicator

    async def refresh(self, source) -> dict:
        """委派給 source.get_strength_universe，取回排名後挑 Top N。"""
        from market_intel_mcp.strength import compute_strength_scores

        before = set(self.trading)
        t0 = asyncio.get_event_loop().time()

        # v31: 候選池改用掃描器全市場即時清單（union 固定 candidates 確保主流幣在內）；
        #      掃描器未就緒（冷啟動）時 fallback 固定清單
        market = _market_candidates()
        if market:
            pool = list(dict.fromkeys(list(self.candidate_pool) + market))
        else:
            pool = list(self.candidate_pool)
        n_pool = len(pool)   # task#64：請求進宇宙的候選檔數（截斷遙測基準）

        # 走公開介面，不依賴 source 的私有方法
        universe = await source.get_strength_universe(
            limit=len(pool),
            candidate_symbols=pool,
        )
        if isinstance(universe, dict) and universe.get("error"):
            elapsed = asyncio.get_event_loop().time() - t0
            return {"chosen": list(self.trading), "dropped": [], "added": [],
                    "scored": [], "elapsed_sec": round(elapsed, 2),
                    "ts": self.last_refresh, "error": universe.get("message"),
                    "universe_telemetry": {"n_pool": n_pool, "n_universe": 0,
                                           "n_dropped": 0, "errored": True}}

        items = universe.get("items", [])

        # task#64：宇宙截斷遙測（純觀測，零訊號數學影響）。
        # get_strength_universe 對 pool 內每檔各打一次 /pairs-markets；遇 429/錯誤會
        # silently `continue`（見 coinglass.py），使回傳 items 少於請求數＝模型對市場
        # 部分失明。這裡用「請求數 vs 回傳數」算出被丟幾檔，先量測截斷率再決定是否治本。
        # items / chosen 照常往下走 → 不改候選覆蓋、不過回測閘、不碰任何下單數學。
        n_universe = len(items)
        n_dropped = max(0, n_pool - n_universe)
        universe_telemetry = {"n_pool": n_pool, "n_universe": n_universe,
                              "n_dropped": n_dropped, "errored": False}

        # 硬性過濾（低流動性、極端漲跌、過熱費率）
        # 註：ret_7d_pct 由 mi_get_strength_universe 用 ret_24h × 5 估算
        filtered = []
        skipped_reasons: dict[str, int] = {"vol": 0, "ret": 0, "funding": 0}
        for it in items:
            vol = it.get("vol_24h_usd", 0) or 0
            ret_24h_est = (it.get("return_7d_pct", 0) or 0) / 5  # 還原 24h 漲幅
            funding = it.get("funding", 0) or 0
            if vol < 20_000_000:    # 20M（之前 30M 過嚴）
                skipped_reasons["vol"] += 1
                continue
            if abs(ret_24h_est) > 30:   # 24h 移動 > 30% 視為極端
                skipped_reasons["ret"] += 1
                continue
            if abs(funding) > 0.0025:   # 0.25%/8h（之前 0.15% 過嚴）
                skipped_reasons["funding"] += 1
                continue
            filtered.append(it)

        scored = compute_strength_scores(filtered)
        chosen = [it["symbol"] for it in scored[:self.trading_size]]

        # 守護：若篩出 0 個，保留現有 trading 不清空（避免空名單 = 0 掃描）
        if not chosen and self.trading:
            elapsed = asyncio.get_event_loop().time() - t0
            return {
                "chosen": list(self.trading),
                "dropped": [], "added": [], "scored": scored,
                "elapsed_sec": round(elapsed, 2),
                "ts": self.last_refresh,
                "universe_telemetry": universe_telemetry,
                "warn": f"strict filters left 0 candidates (vol={skipped_reasons['vol']} "
                        f"ret={skipped_reasons['ret']} funding={skipped_reasons['funding']}); "
                        f"keeping previous list",
            }

        # 更新狀態
        self.trading = chosen
        self.last_refresh = dt.datetime.now(tz=dt.timezone.utc)

        # 同步 HOT_SYMBOLS（給 mi_get_snapshot 的 is_hot 用）
        HOT_SYMBOLS.clear()
        HOT_SYMBOLS.update(chosen)

        # v54-3: #35 陰陽共生影子層 — 在同一份 filtered universe 上算「弱勢做空候選」排名。
        #   純觀測：short_tier_size 預設 0 → self.short_tier 永遠空 → 永不進 fire_tier()。
        #   絕不影響 trading/chosen/HOT_SYMBOLS/掃描/下單；任何錯誤吞掉不影響做多 refresh。
        short_scored: list[dict] = []
        try:
            from market_intel_mcp.weakness import (
                compute_weakness_scores, passes_short_liquidity)
            short_pool = [it for it in filtered
                          if passes_short_liquidity(it, 20_000_000)]
            short_scored = compute_weakness_scores(short_pool)
            self.short_tier = [it["symbol"]
                               for it in short_scored[:self.short_tier_size]]
        except Exception:
            self.short_tier = []

        # task#68：免費 OKX 大宗源強度宇宙「影子比對」（純觀測，零訊號/候選變更）。
        #   量測 scanner.db 免費源 vs 現行 CoinGlass per-coin 路徑的「覆蓋率 + top-N
        #   一致度」，為日後「改用免費源解 task#64 冷 burst 截斷（須過回測閘）」累積
        #   決策證據。重用同一輪 CoinGlass items（不另 burst）、零網路（讀 scanner.db）。
        #   絕不影響 chosen；任何錯誤吞掉續跑。
        try:
            from l3_dispatcher.free_strength_universe import (
                append_shadow, compare_universes)
            append_shadow(compare_universes(pool, items, chosen, self.trading_size))
        except Exception:
            pass

        elapsed = asyncio.get_event_loop().time() - t0
        return {
            "chosen": chosen,
            "dropped": list(before - set(chosen)),
            "added": list(set(chosen) - before),
            "scored": scored,    # 全部排序後的清單，給報告用
            "short_scored": short_scored,   # v54-3: #35 弱勢做空排名（觀測；未啟用）
            "short_tier": list(self.short_tier),
            "elapsed_sec": round(elapsed, 2),
            "ts": self.last_refresh,
            "universe_telemetry": universe_telemetry,   # task#64 截斷遙測（純觀測）
        }


async def run_refresh_loop(manager: WatchlistManager, source,
                           daily_at_hour_utc: int = 0,
                           callback=None):
    """每日 00:00 UTC（可配置）refresh 交易層。
    啟動時也立即跑一次。
    """
    # 啟動立即 refresh
    print(f"[refresh] initial refresh starting...")
    result = await manager.refresh(source)
    _tel = result.get("universe_telemetry") or {}
    _drop = (f", universe {_tel['n_universe']}/{_tel['n_pool']} (dropped {_tel['n_dropped']})"
             if _tel.get("n_dropped") else "")
    print(f"[refresh] chose {len(result['chosen'])} symbols, elapsed={result['elapsed_sec']}s{_drop}")
    if callback:
        await callback(result)

    while True:
        # 算到下一個 00:00 UTC 的秒數
        now = dt.datetime.now(tz=dt.timezone.utc)
        next_run = now.replace(hour=daily_at_hour_utc, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run = next_run + dt.timedelta(days=1)
        wait = (next_run - now).total_seconds()
        print(f"[refresh] next at {next_run.strftime('%Y-%m-%d %H:%M UTC')} (in {wait/3600:.1f}h)")
        await asyncio.sleep(wait)

        result = await manager.refresh(source)
        _tel = result.get("universe_telemetry") or {}
        _drop = (f"  universe_dropped={_tel['n_dropped']}/{_tel['n_pool']}"
                 if _tel.get("n_dropped") else "")
        print(f"[refresh] re-ranked: added={result['added']}  dropped={result['dropped']}{_drop}")
        if callback:
            await callback(result)
