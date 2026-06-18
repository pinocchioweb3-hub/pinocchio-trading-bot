"""跨源「存在度／流動性」索引（task#33 純函式核心）。

目的：在跨源共振判讀之前，先回答「這個幣到底在幾家所掛牌、總流動性多深、
三大獨立源（OKX∧Binance∧CoinGlass）是否齊全」。三源齊全是「共振可信」的
前置條件——只在一家所掛牌的冷門幣，任何跨源「一致」都沒有意義。

設計鐵則：
    - 本模組純函式區（liquidity_tier / presence_score / compute_presence）
      **零 I/O、零 API、零隨機**，給離線單元測試與 shadow 計算用。
    - I/O 薄層（collect_presence_universe）僅蒐集原料，不做任何訊號數學，
      失敗的源一律標 None、絕不 raise（與專案各 source「缺料回 None / error」一致）。
    - 任何輸出**永不**乘進 strength_score、**永不**回寫 snapshot / fire。
      這只是「盤面存在度」的描述層，供 shadow 觀測與後續 convergence 加權參考。

「三源」定義（triple_present）：OKX ∧ Binance ∧ CoinGlass 同時有該幣資料。
選這三家是因為它們是彼此獨立的撮合/聚合源（CEX 自家盤 + 第三方聚合），
HL（鏈上）為加分項但不列入 triple 硬條件，避免冷門幣因 HL 未上而過嚴。
"""
from __future__ import annotations

import math

# 流動性分層門檻（USD，24h 名目量）。可由呼叫端覆寫，預設值對齊掃描器量級直覺。
DEEP_TIER_USD = 500_000_000     # ≥$500M：深水區（BTC/ETH/主流）
MEDIUM_TIER_USD = 50_000_000    # ≥$50M：中等流動性

# presence_score 量正規化的對數上限（log10(2e9)≈9.3 → 視為滿格）。
_VOL_LOG_CEIL = 2_000_000_000.0

# 計入 triple 的三大獨立源（canonical 鍵名，與 collect 蒐集的 per_exchange key 一致）
_TRIPLE_SOURCES = ("okx", "binance", "coinglass")


def liquidity_tier(total_vol_usd: float | None) -> str:
    """24h 總名目量 → 流動性分層字串。

    deep   ≥ DEEP_TIER_USD（$500M）
    medium ≥ MEDIUM_TIER_USD（$50M）
    shallow 其餘（含 None / 0 / 負值 → 一律 shallow，缺料保守視為淺水）

    >>> liquidity_tier(600_000_000)
    'deep'
    >>> liquidity_tier(500_000_000)   # 邊界含等於
    'deep'
    >>> liquidity_tier(50_000_000)
    'medium'
    >>> liquidity_tier(49_999_999)
    'shallow'
    >>> liquidity_tier(None)
    'shallow'
    """
    v = total_vol_usd if isinstance(total_vol_usd, (int, float)) else None
    if v is None or v <= 0:
        return "shallow"
    if v >= DEEP_TIER_USD:
        return "deep"
    if v >= MEDIUM_TIER_USD:
        return "medium"
    return "shallow"


def presence_score(n_exchanges: int, total_vol_usd: float | None,
                   max_exchanges: int = 4) -> float:
    """跨源存在度分數 ∈ [0, 1]：0.6·掛牌覆蓋 + 0.4·量對數正規化。

    - 掛牌覆蓋 = min(n_exchanges, max_exchanges) / max_exchanges（夾 0–1）。
    - 量項 = log10(vol) 正規化到 [0,1]，下限 $1M（log10=6）、上限 _VOL_LOG_CEIL。
      vol 為 None / ≤0 → 量項 0（不除零、不取 log 負無窮）。
    - 單調性：n 增 → 不減；vol 增 → 不減。整體夾 [0,1]。

    >>> round(presence_score(4, 2_000_000_000), 3)
    1.0
    >>> presence_score(0, None)
    0.0
    >>> 0.0 <= presence_score(2, 100_000_000) <= 1.0
    True
    """
    n = max(0, int(n_exchanges))
    mx = max(1, int(max_exchanges))
    coverage = min(n, mx) / mx  # ∈ [0,1]

    v = total_vol_usd if isinstance(total_vol_usd, (int, float)) else None
    if v is None or v <= 0:
        vol_term = 0.0
    else:
        lo = 6.0                              # log10($1M)
        hi = math.log10(_VOL_LOG_CEIL)        # log10($2B) ≈ 9.301
        lv = math.log10(v)
        vol_term = (lv - lo) / (hi - lo)
        vol_term = max(0.0, min(1.0, vol_term))

    score = 0.6 * coverage + 0.4 * vol_term
    return max(0.0, min(1.0, round(score, 4)))


def compute_presence(per_exchange: dict[str, dict | None]) -> dict:
    """彙整某幣在各源的存在度。**純函式、不改輸入**。

    參數
    ----
    per_exchange: {source_name: market_dict | None}
        source_name 預期含 'okx'/'binance'/'coinglass'/'hyperliquid' 等。
        值為各源盤面 dict（至少可含量欄）或 None（該源缺料/未掛牌/抓取失敗）。
        量欄依序找：'vol24h_usd' / 'day_notional_volume_usd' / 'vol_usd' /
        'volume_usd' / 'turnover_usd'，找不到視為該源無量貢獻（不崩潰）。

    回傳
    ----
    dict（不含任何訊號數學，純描述）：
        exchanges_present:   有資料（非 None）的 source 名稱排序列表
        n_exchanges:         上者長度
        liquidity_depth_usd: 各源量加總（None 量略過；全無 → 0.0）
        liquidity_tier:      liquidity_tier(liquidity_depth_usd)
        triple_present:      OKX ∧ Binance ∧ CoinGlass 是否同時 present
        presence_score:      presence_score(n_exchanges, liquidity_depth_usd)
    """
    src = per_exchange or {}
    present = sorted(k for k, v in src.items() if v is not None)

    depth = 0.0
    for v in src.values():
        if not isinstance(v, dict):
            continue
        vol = _extract_vol(v)
        if vol is not None and vol > 0:
            depth += vol

    present_set = set(present)
    triple = all(s in present_set for s in _TRIPLE_SOURCES)

    return {
        "exchanges_present": present,
        "n_exchanges": len(present),
        "liquidity_depth_usd": round(depth, 2),
        "liquidity_tier": liquidity_tier(depth),
        "triple_present": triple,
        "presence_score": presence_score(len(present), depth),
    }


_VOL_KEYS = (
    "vol24h_usd", "day_notional_volume_usd", "vol_usd",
    "volume_usd", "turnover_usd",
)


def _extract_vol(market: dict) -> float | None:
    """從各源盤面 dict 找出 24h 名目量（USD）。找不到/非數 → None。"""
    for k in _VOL_KEYS:
        if k in market:
            v = market[k]
            if isinstance(v, (int, float)):
                return float(v)
    return None


# ===========================================================================
# I/O 薄層（可寫；測試不跑網路 — 只要求 import 乾淨 + py_compile 過）
# ===========================================================================
async def collect_presence_universe(
    symbols: list[str] | None = None,
    *,
    binance_source=None,
    hl_source=None,
    coinglass_data: dict[str, dict] | None = None,
) -> dict[str, dict]:
    """蒐集每個 canonical 幣在各源的存在度原料，回 {canonical: compute_presence(...)}。

    來源策略（每源失敗一律標 None，絕不 raise）：
        OKX:        零額外呼叫 — 直接讀 market_scanner.scanner.db 最新 snapshot。
        Binance:    binance_source.list_perp_symbols()（一次列出全 USDT 永續 base）。
        Hyperliquid: hl_source.get_overview()（一次 metaAndAssetCtxs 回全永續）。
        CoinGlass:  由呼叫端把既有 CoinGlass 資料以 {canonical: market_dict} 傳入
                    （本層不主動打 CoinGlass，避免重複呼叫/額度浪費）。

    這是骨架：聚合用既有零/低成本端點，純函式區（compute_presence）做實際彙整。
    """
    # 延遲匯入，避免純函式測試載入時牽連 I/O 依賴
    from market_intel_mcp.symbol_mapping import to_canonical_aliased

    coinglass_data = coinglass_data or {}

    # --- OKX：讀 scanner.db 最新 snapshot（零呼叫）---
    okx_by_canon: dict[str, dict] = {}
    try:
        okx_by_canon = _load_okx_snapshot()
    except Exception:
        okx_by_canon = {}

    # --- Binance：list_perp_symbols（僅掛牌存在，量未必有 → present 但無量貢獻）---
    binance_set: set[str] = set()
    if binance_source is not None:
        try:
            r = await binance_source.list_perp_symbols()
            if isinstance(r, dict) and not r.get("error"):
                for s in (r.get("symbols") or []):
                    binance_set.add(to_canonical_aliased(s))
        except Exception:
            binance_set = set()

    # --- Hyperliquid：get_overview（top_by_oi 含量/盤面）---
    hl_by_canon: dict[str, dict] = {}
    if hl_source is not None:
        try:
            ov = await hl_source.get_overview(top_n=50)
            if isinstance(ov, dict) and not ov.get("error"):
                for row in (ov.get("top_by_oi") or []):
                    coin = row.get("coin")
                    if not coin:
                        continue
                    canon = to_canonical_aliased(coin)
                    hl_by_canon[canon] = {
                        "vol_usd": row.get("day_notional_volume_usd"),
                        "raw": row,
                    }
        except Exception:
            hl_by_canon = {}

    # --- CoinGlass：呼叫端傳入，正規化鍵 ---
    cg_by_canon: dict[str, dict] = {}
    for k, v in coinglass_data.items():
        try:
            cg_by_canon[to_canonical_aliased(k)] = v
        except Exception:
            continue

    # --- 決定要算的 universe ---
    if symbols:
        universe = {to_canonical_aliased(s) for s in symbols}
    else:
        universe = (set(okx_by_canon) | binance_set
                    | set(hl_by_canon) | set(cg_by_canon))

    out: dict[str, dict] = {}
    for canon in sorted(universe):
        per_exchange: dict[str, dict | None] = {
            "okx": okx_by_canon.get(canon),
            "binance": ({"present": True} if canon in binance_set else None),
            "coinglass": cg_by_canon.get(canon),
            "hyperliquid": hl_by_canon.get(canon),
        }
        out[canon] = compute_presence(per_exchange)
    return out


def _load_okx_snapshot() -> dict[str, dict]:
    """讀 scanner.db 最新一輪 snapshot → {canonical: {vol24h_usd, ...}}（零網路）。"""
    import sqlite3

    from botpaths import db_path as _db_path
    from market_intel_mcp.symbol_mapping import to_canonical_aliased

    db = _db_path("scanner.db")
    if not db.exists():
        return {}
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        row = conn.execute("SELECT MAX(ts) FROM snapshots").fetchone()
        if not row or row[0] is None:
            return {}
        ts = row[0]
        rows = conn.execute(
            "SELECT inst, last, vol24h_usd, oi_usd, funding, chg24h_pct "
            "FROM snapshots WHERE ts=?", (ts,)).fetchall()
    finally:
        conn.close()
    out: dict[str, dict] = {}
    for inst, last, vol, oi, funding, chg in rows:
        canon = to_canonical_aliased(inst)
        out[canon] = {
            "vol24h_usd": vol,
            "last": last,
            "oi_usd": oi,
            "funding": funding,
            "chg24h_pct": chg,
        }
    return out
