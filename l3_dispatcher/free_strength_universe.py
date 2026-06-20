"""task#68：免費 OKX 大宗源「強度宇宙」建構器 + 影子比對（shadow-first 半）。

背景（task#64 量測定案，見記憶 trading-bot-universe-truncation-verdict）：
live get_strength_universe 走 CoinGlass per-coin /pairs-markets 對 ~60 檔候選池
逐檔 burst，$79 Startup tier 冷快取下撞 429 → 靜默 `continue` 略過 → 截斷 30–92%
（實測 5–57/60）。根因＝CoinGlass 全市場端點 coins-markets 在 $79 被鎖（需 $299，
使用者定不升），被迫 per-coin。

本模組用「已在手、零網路」的 scanner.db（market_scanner 每 300s 入庫、3 日滾動、
~372 檔全 OKX 永續）建出與 CoinGlass get_strength_universe **同 schema** 的
strength-input items，作為免費替代源。對照 CoinGlass per-coin 路徑
（coinglass.py:660-670）逐欄忠實度：
    - return_7d_pct : chg24h × 5        ← 與 CoinGlass 同方法（coinglass.py:662）
    - vol_24h_usd   : scanner vol24h_usd（real，OKX 大宗）
    - funding       : scanner funding（real）
    - oi_delta_7d_pct : scanner OI 24h **實測** delta × 3
                        （CoinGlass 用 API 回報的 open_interest_change_percent_24h × 3；
                         本源直接量「現在 OI / 24h 前 OI」更原生，缺史料時退 0.0）
    - vol_24h_vs_30d  : current / 近期(≤3d retained)均量（surge proxy；
                        CoinGlass 自己也是粗估反推 × 0.85，見 coinglass.py:654-658）
    - cvd_slope_7d / top_trader_dev / btc_corr_30d : 與 CoinGlass path **同 stub**
      （0.0 / 0.05 / 0.70；coinglass.py:666-668）→ 對排名零相對差異
      （這 35% 權重兩路徑給相同常數 → z-score 同為 0 / btc_corr 同為 +1）。

影子鐵則（絕不可違反）：
    * 本模組只「建 items / 算比對」，**永不** 寫 fire_queue / snapshot / symbol_gate /
      任何下單路徑；**永不** 改 strength.py；**永不** 改 live chosen。
    * 唯一輸出 = 自己的 free_universe_shadow.jsonl（觀測用，給日後回測閘決策）。
    * 「改用此源取代 CoinGlass」＝改 live 候選覆蓋＝改 FIRE → **必過回測閘**
      （task#68 gated 半，本檔不做 flip）。
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

# 與 CoinGlass per-coin 路徑相同的進階因子 stub（coinglass.py:666-668）。
# 兩路徑給相同常數 → 對 compute_strength_scores 的相對排名零影響。
# test_free_strength_universe 鎖死這三值，防 CoinGlass 端漂移後本源失準。
_STUB_CVD = 0.0
_STUB_TOP_TRADER = 0.05
_STUB_BTC_CORR = 0.70

# OI 24h delta 對齊 CoinGlass「7d proxy」尺度（coinglass.py:665 oi_change × 3）
_OI_7D_SCALE = 3.0
# return 24h → 7d proxy（coinglass.py:662 ret_24h × 5）
_RET_7D_SCALE = 5.0

_SINK_MAX_BYTES = 5_000_000


def _sink_path():
    from botpaths import data_dir
    return data_dir() / "free_universe_shadow.jsonl"


def append_shadow(record: dict) -> None:
    """把一輪影子比對寫一行 JSONL（純本地檔；超過軟上限就輪替一次，寫失敗吞掉）。"""
    path = _sink_path()
    try:
        if path.exists() and path.stat().st_size > _SINK_MAX_BYTES:
            backup = path.with_suffix(".jsonl.1")
            try:
                if backup.exists():
                    backup.unlink()
                path.rename(backup)
            except OSError:
                pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load_scanner_inputs() -> tuple[dict, dict, dict]:
    """讀 scanner.db（零網路）→ (snap_map, oi_past_map, vol_avg_map)。

    snap_map  : {inst: {last, vol24h_usd, oi_usd, funding, chg24h_pct}}（最新一輪）
    oi_past_map: {inst: oi_usd}（最接近 24h 前的那一輪；用於 OI 實測 delta）
    vol_avg_map: {inst: AVG(vol24h_usd)}（retained ≤3d 全史；vol surge 分母）

    任一缺料 → 回空 dict（呼叫端退中性值，不崩潰）。三次查詢、皆走索引、每日跑。
    """
    from botpaths import db_path
    db = db_path("scanner.db")
    if not db.exists():
        return {}, {}, {}
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        row = conn.execute("SELECT MAX(ts) FROM snapshots").fetchone()
        if not row or row[0] is None:
            return {}, {}, {}
        ts = row[0]
        cur_rows = conn.execute(
            "SELECT inst, last, vol24h_usd, oi_usd, funding, chg24h_pct "
            "FROM snapshots WHERE ts=?", (ts,)).fetchall()
        # 最接近 24h 前的一輪（±1h 容差）
        target = ts - 86400
        prow = conn.execute(
            "SELECT ts FROM snapshots WHERE ts BETWEEN ? AND ? "
            "ORDER BY ABS(ts - ?) LIMIT 1",
            (target - 3600, target + 3600, target)).fetchone()
        oi_past: dict[str, float] = {}
        if prow:
            for inst, oi in conn.execute(
                    "SELECT inst, oi_usd FROM snapshots WHERE ts=?",
                    (prow[0],)).fetchall():
                oi_past[inst] = oi
        vol_avg: dict[str, float] = {}
        for inst, avgv in conn.execute(
                "SELECT inst, AVG(vol24h_usd) FROM snapshots GROUP BY inst").fetchall():
            vol_avg[inst] = avgv
    finally:
        conn.close()

    snap_map: dict[str, dict] = {}
    for inst, last, vol, oi, funding, chg in cur_rows:
        snap_map[inst] = {"last": last, "vol24h_usd": vol, "oi_usd": oi,
                          "funding": funding, "chg24h_pct": chg}
    return snap_map, oi_past, vol_avg


def build_items(pool: list[str], snap_map: dict, oi_past: dict,
                vol_avg: dict) -> list[dict]:
    """為 pool 內每檔組出與 CoinGlass get_strength_universe 同 schema 的 item。

    缺料的幣（不在 snap_map / vol≤0）一律略過（不入 items）——與 CoinGlass path
    「缺料 continue」對齊；不入 items ＝ 此免費源未覆蓋該幣（誠實，不捏造）。
    純函式：不讀檔不打網路，所有原料由參數傳入（離線可測）。
    """
    items: list[dict] = []
    for sym in pool:
        snap = snap_map.get(sym)
        if not snap:
            continue
        vol = snap.get("vol24h_usd")
        if not isinstance(vol, (int, float)) or vol <= 0:
            continue
        chg = snap.get("chg24h_pct") or 0.0
        funding = snap.get("funding") or 0.0

        # OI 24h 實測 delta（× 3 對齊 CoinGlass 7d proxy 尺度）；缺 24h 前 OI → 0.0 中性
        oi = snap.get("oi_usd")
        past_oi = oi_past.get(sym)
        if (isinstance(oi, (int, float)) and isinstance(past_oi, (int, float))
                and past_oi > 0):
            oi_delta_7d = (oi / past_oi - 1.0) * 100.0 * _OI_7D_SCALE
        else:
            oi_delta_7d = 0.0

        # vol surge：current / 近期均量；無均量 → 1.0 中性
        avgv = vol_avg.get(sym)
        vol_vs = vol / avgv if isinstance(avgv, (int, float)) and avgv > 0 else 1.0

        items.append({
            "symbol": sym,
            "return_7d_pct": chg * _RET_7D_SCALE,
            "vol_24h_usd": float(vol),
            "vol_24h_vs_30d": round(vol_vs, 3),
            "oi_delta_7d_pct": round(oi_delta_7d, 3),
            "cvd_slope_7d": _STUB_CVD,
            "top_trader_dev": _STUB_TOP_TRADER,
            "btc_corr_30d": 1.0 if sym == "BTC" else _STUB_BTC_CORR,
            "funding": float(funding),
        })
    return items


def build_free_universe(pool: list[str]) -> dict:
    """drop-in 替代 source.get_strength_universe：回 {source, ts, items}（零網路）。
    這是日後（過回測閘後）可直接替換 CoinGlass path 的免費源入口。"""
    snap_map, oi_past, vol_avg = load_scanner_inputs()
    return {"source": "free_okx_scanner", "ts": 0,
            "items": build_items(list(pool), snap_map, oi_past, vol_avg)}


def _passes_strength_filter(it: dict) -> bool:
    """MIRRORS watchlist.refresh 硬性過濾（watchlist.py:126-139）。
    為「免費源 top-N」與「live chosen（已過此濾）」做 apples-to-apples 比對。
    若兩處門檻日後漂移，比對會輕微偏差（非致命，僅影響觀測 agreement 精度）。"""
    vol = it.get("vol_24h_usd", 0) or 0
    ret_24h_est = (it.get("return_7d_pct", 0) or 0) / 5
    funding = it.get("funding", 0) or 0
    if vol < 20_000_000:
        return False
    if abs(ret_24h_est) > 30:
        return False
    if abs(funding) > 0.0025:
        return False
    return True


def compare_universes(pool: list[str], cg_items: list[dict] | None,
                      cg_chosen: list[str] | None, trading_size: int) -> dict:
    """量測「免費 OKX 源 vs 現行 CoinGlass per-coin 路徑」的覆蓋率 + top-N 一致度。

    純觀測：cg_chosen 是 live 已落地名單（傳入，不重算）；free_chosen 走同樣的
    硬性過濾 + compute_strength_scores → top trading_size。回可序列化摘要 dict。
    不改任何 live 狀態、不影響 chosen。
    """
    from market_intel_mcp.strength import compute_strength_scores

    free = build_free_universe(pool)
    free_items = free["items"]
    free_filtered = [it for it in free_items if _passes_strength_filter(it)]
    free_scored = compute_strength_scores(free_filtered)
    free_chosen = [it["symbol"] for it in free_scored[:trading_size]]

    cg_syms = {it.get("symbol") for it in (cg_items or []) if it.get("symbol")}
    free_syms = {it["symbol"] for it in free_items}
    cg_set = set(cg_chosen or [])
    free_set = set(free_chosen)
    overlap = sorted(cg_set & free_set)

    return {
        "ts": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "n_pool": len(pool),
        "cg_coverage": len(cg_syms),       # CoinGlass 實際回傳幾檔（截斷後）
        "free_coverage": len(free_syms),   # 免費源覆蓋幾檔
        # 免費源多覆蓋（即 CoinGlass burst 截斷掉的）— task#64 截斷的正面證據
        "free_extra_covered": sorted(free_syms - cg_syms)[:50],
        "free_extra_n": len(free_syms - cg_syms),
        # CoinGlass 有、免費源缺（須關注的覆蓋缺口；理想為空或極少）
        "cg_only_covered": sorted(cg_syms - free_syms)[:50],
        "cg_only_n": len(cg_syms - free_syms),
        "trading_size": trading_size,
        "cg_chosen": list(cg_chosen or []),
        "free_chosen": free_chosen,
        "topN_overlap": overlap,
        "topN_overlap_n": len(overlap),
        "topN_agreement": round(len(overlap) / max(1, trading_size), 3),
        "note": "shadow-only：免費源從不取代 CoinGlass live chosen；flip 須過回測閘(task#68)",
    }
