"""task#20 因子回補純函式：universe 排名路徑的「死權重 / 反符號」因子治本（離線、唯讀）。

背景（紅線判定）：strength.py 6 因子 z-score 加權，但 coinglass.py get_strength_universe
（唯一 live universe 構造器）對其中三個因子塞死值，導致截面 z-score 退化：
    * cvd_slope_7d  (20%) ＝ 常數 0.0   → z 恆 0（已由 binance_cvd_validate 治，v61）
    * top_trader_dev(10%) ＝ 常數 0.05  → z 恆 0（**死權重**）
    * btc_corr_30d  ( 5%) ＝ 常數 0.70  → 全 alt 同值 → _btc_corr_score 退化成常數偏移
另有一個「符號弄反」的 bug：
    * vol_24h_vs_30d(15%) ＝ 估算式 (1−vol_change/100)/0.85 → **量增反而讓因子變小**
      （與「量增=強勢」相反）。

本檔＝這三個因子的「正確值」純函式推導層（cvd 那道已在 binance_cvd_validate）。設計同 v61：
- 不接任何 live 路徑、不改任何 daemon 模組（純新增離線純函式）。
- 不下單（紅線①）、不對外（紅線②）、不捏造（紅線③：資料不足誠實回 None，不塞假值）。
- 全部用**免 API key** 的 Binance 公開端點當數據源（守住「秘鑰不入聊天室」）：
    top_trader_dev ← get_positioning（topLongShortAccountRatio，大戶帳戶多空比）
    btc_corr_30d   ← get_candles 1d 收盤 → 日報酬 → 與 BTC 日報酬的 Pearson
    vol_24h_vs_30d ← get_candles 1d quote 量 → 過去 window 日均量

⚠️ off-limits（與 cvd 同一套）：本檔只「算出正確值」供日後**離線 EV 閘**反事實重排序；
   是否真把值填進 live get_strength_universe＝改訊號路徑的幣覆蓋（→ trading tier → FIRE），
   **必過回測閘**（task#64 路徑），絕不在此自動晉升。不准碰 strength.py / signals.py。

語意對齊 strength.py：
- top_trader_dev＝「大戶帳戶多空比 偏離 1」＝ ratio − 1.0（ratio>1 大戶偏多、<1 偏空）。
- btc_corr_30d ＝與 BTC 的 30 日相關性。**必須用日報酬算，不可用價位本身**——價位高度自相關，
  全幣對 BTC 的價位 Pearson 幾乎都 >0.95 → _btc_corr_score 全部打成 −1.0（只是把「全 +」
  換成「全 −」的另一個常數，沒治到死權重）。報酬序列才有真正的截面差異。
- vol_24h_vs_30d＝24h 量 / 過去 30 日「完整日」均量（>1 放量、<1 縮量）。

用法：
  python -m backtest.factor_backfill                 # 用預設流動性樣本 live 試算（唯讀）
  python -m backtest.factor_backfill BTC ETH SOL ... # 指定樣本
  python -m backtest.factor_backfill --selftest      # 純函式單元自測（離線、零網路）
"""
from __future__ import annotations

import asyncio
import sys
from statistics import mean

# Pearson 唯一來源＝v61 回測閘已認證的純函式（避免兩份 Pearson 數學各自漂移）。
from backtest.binance_cvd_validate import _pearson

DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX"]

_EPS = 1e-12


def _tail(seq, n: int) -> list:
    """安全取尾端 n 筆（seq 為 None 或非序列 → []）。"""
    if not seq:
        return []
    try:
        return list(seq)[-n:]
    except TypeError:
        return []


# ════════════════════════════════════════════════════════════════════════
#  純函式：top_trader_dev（大戶帳戶多空比偏離 1）
# ════════════════════════════════════════════════════════════════════════
def top_trader_dev_from_ratio(latest_ratio) -> float | None:
    """大戶帳戶多空比 → 偏離 1（strength.py top_trader_dev，10% 權重）。

    latest_ratio＝get_positioning(...)['latest']＝topLongShortAccountRatio 最新值。
    回 ratio − 1.0（ratio>1 大戶淨多 → 正；<1 淨空 → 負）。None/非數 → None（誠實，紅線③）。
    """
    if latest_ratio is None:
        return None
    try:
        return float(latest_ratio) - 1.0
    except (TypeError, ValueError):
        return None


# ════════════════════════════════════════════════════════════════════════
#  純函式：日報酬 + btc_corr_30d（與 BTC 的日報酬相關性）
# ════════════════════════════════════════════════════════════════════════
def daily_returns(closes) -> list[float | None]:
    """升序收盤序列 → 逐筆報酬率 [c[i]/c[i-1] − 1]。

    非正收盤（0/負，資料髒）那一步報酬記 None（誠實略過，不捏造）；長度 = len(closes)−1。
    """
    out: list[float | None] = []
    cl = list(closes or [])
    for i in range(1, len(cl)):
        try:
            p0, p1 = float(cl[i - 1]), float(cl[i])
        except (TypeError, ValueError):
            out.append(None)
            continue
        if p0 <= 0:
            out.append(None)
            continue
        out.append(p1 / p0 - 1.0)
    return out


def btc_corr_from_closes(sym_closes, btc_closes, window: int = 30,
                         min_points: int = 20) -> float | None:
    """30d BTC 相關性（strength.py btc_corr_30d，5% 權重）——**用日報酬算 Pearson**。

    sym_closes / btc_closes＝日線收盤升序（最新在末；建議各 ≥ window+1 筆）。
    取各自最後 window+1 筆 → window 個日報酬 → 尾端對齊（新上市幣歷史短 → 取較短者長度）
    → 丟掉任一邊 None 的配對 → Pearson。可解析配對 < min_points 或零變異 → None（誠實不捏造）。

    為何不用價位：價位高度自相關，任兩幣的價位 Pearson 幾乎都 >0.95，會讓 _btc_corr_score
    全部退化成 −1.0（換個常數而已）。日報酬才能反映「同漲同跌」的真實連動強度。
    """
    rs = daily_returns(_tail(sym_closes, window + 1))
    rb = daily_returns(_tail(btc_closes, window + 1))
    m = min(len(rs), len(rb))
    if m < 2:
        return None
    rs, rb = rs[-m:], rb[-m:]   # 尾端對齊（保留最近的同窗報酬）
    xs: list[float] = []
    ys: list[float] = []
    for a, b in zip(rs, rb):
        if a is None or b is None:
            continue
        xs.append(a)
        ys.append(b)
    if len(xs) < min_points:
        return None
    return _pearson(xs, ys)


# ════════════════════════════════════════════════════════════════════════
#  純函式：vol_24h_vs_30d（正確算法；修正 universe stub 把符號弄反的 bug）
# ════════════════════════════════════════════════════════════════════════
def vol_ratio_24h_vs_30d(vol_24h_usd, prior_daily_vols_usd,
                         window: int = 30, min_days: int = 7) -> float | None:
    """24h 量 / 過去 window 個完整日均量（strength.py vol_24h_vs_30d，15% 權重）。

    vol_24h_usd＝最近 24h quote 成交額（universe item 已有此欄，直接餵）。
    prior_daily_vols_usd＝**過去完整日**的日線 quote 量序列（升序；不含今天，避免重複計入分子）。
    取最後 window 筆求均；可用日數 < min_days 或均量 ≤ 0 → None（誠實不捏造）。
    回 >1＝放量、<1＝縮量（與「量增=強勢」同向；修正 stub 的反符號 bug）。
    """
    if vol_24h_usd is None:
        return None
    try:
        v24 = float(vol_24h_usd)
    except (TypeError, ValueError):
        return None
    vols: list[float] = []
    for x in _tail(prior_daily_vols_usd, window):
        try:
            fx = float(x)
        except (TypeError, ValueError):
            continue
        if fx >= 0:
            vols.append(fx)
    if len(vols) < min_days:
        return None
    avg = mean(vols)
    if avg <= _EPS:
        return None
    return v24 / avg


# ════════════════════════════════════════════════════════════════════════
#  Live 試算（唯讀；Binance 免 key；只印推導值供 eyeball，不下任何判定/不接 live）
# ════════════════════════════════════════════════════════════════════════
async def _eval_symbol(bn, symbol: str, btc_closes: list[float]) -> dict:
    out: dict = {"symbol": symbol}
    # top_trader_dev ← 大戶多空比
    pos = await bn.get_positioning(symbol, "1d", 30)
    latest = pos.get("latest") if isinstance(pos, dict) and not pos.get("error") else None
    out["top_trader_ratio"] = latest
    out["top_trader_dev"] = top_trader_dev_from_ratio(latest)
    # btc_corr_30d + vol_24h_vs_30d ← 日線
    kl = await bn.get_candles(symbol, "1d", 35)
    if isinstance(kl, dict) and not kl.get("error"):
        candles = kl.get("candles") or []
        closes = [c["close"] for c in candles]
        vols = [c["volume_usd"] for c in candles]
        out["btc_corr_30d"] = (1.0 if symbol == "BTC"
                               else btc_corr_from_closes(closes, btc_closes))
        # 分子＝最近完整日量；分母＝其之前的日量（不含分子那天）
        vol_24h = vols[-1] if vols else None
        out["vol_24h_vs_30d"] = vol_ratio_24h_vs_30d(vol_24h, vols[:-1])
    else:
        out["btc_corr_30d"] = None
        out["vol_24h_vs_30d"] = None
    return out


async def main(symbols: list[str]) -> int:
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    from market_intel_mcp.sources.binance_perp import BinancePerpSource

    bn = BinancePerpSource()
    print(f"task#20 因子回補 live 試算（n={len(symbols)}；Binance 免 key、唯讀、不下判定）")
    print("（top_trader_dev=大戶比−1；btc_corr_30d=日報酬 Pearson；vol=24h/30d日均）\n")
    try:
        btc_kl = await bn.get_candles("BTC", "1d", 35)
        btc_closes = ([c["close"] for c in (btc_kl.get("candles") or [])]
                      if isinstance(btc_kl, dict) and not btc_kl.get("error") else [])
        rows = []
        for sym in symbols:
            rows.append(await _eval_symbol(bn, sym, btc_closes))
    finally:
        await bn.close()

    print(f"{'幣':<6}{'大戶比':>9}{'dev':>9}{'btc_corr':>10}{'vol比':>9}")
    print("-" * 43)
    for r in rows:
        ratio = r.get("top_trader_ratio")
        dev = r.get("top_trader_dev")
        corr = r.get("btc_corr_30d")
        vr = r.get("vol_24h_vs_30d")
        print(f"{r['symbol']:<6}"
              f"{(f'{ratio:.3f}' if ratio is not None else 'n/a'):>9}"
              f"{(f'{dev:+.3f}' if dev is not None else 'n/a'):>9}"
              f"{(f'{corr:+.3f}' if corr is not None else 'n/a'):>10}"
              f"{(f'{vr:.2f}' if vr is not None else 'n/a'):>9}")
    print("\n（此為唯讀試算，僅供 eyeball 數值合理性；是否填進 live universe 須過 EV 閘 + 回測閘）")
    return 0


# ════════════════════════════════════════════════════════════════════════
#  純函式自測（離線、零網路）
# ════════════════════════════════════════════════════════════════════════
def _selftest() -> int:
    # ── top_trader_dev ──
    assert top_trader_dev_from_ratio(1.5) == 0.5
    assert top_trader_dev_from_ratio(0.7) == -0.30000000000000004 or \
        abs(top_trader_dev_from_ratio(0.7) - (-0.3)) < 1e-9
    assert top_trader_dev_from_ratio(1.0) == 0.0
    assert top_trader_dev_from_ratio(None) is None
    assert top_trader_dev_from_ratio("bad") is None
    print("  ✅ top_trader_dev＝ratio−1（含 None/壞值誠實 None）")

    # ── daily_returns ──
    dr = daily_returns([100, 110, 99])
    assert len(dr) == 2 and abs(dr[0] - 0.1) < 1e-9 and abs(dr[1] - (-0.1)) < 1e-9
    assert daily_returns([100]) == []          # <2 筆 → 空
    assert daily_returns([]) == []
    r = daily_returns([100, 0, 50])            # p0=0 那步 → None
    assert r[0] == -1.0 and r[1] is None
    r2 = daily_returns([100, "x", 120])        # 壞值步 → None（不丟例外）
    assert r2[0] is None and r2[1] is None
    print("  ✅ daily_returns（含零/壞收盤誠實 None、長度 = n−1）")

    # ── btc_corr_from_closes ──
    # 完全同步漲跌（同一序列）→ 報酬相同 → Pearson = 1.0
    closes = [100 * (1.01 ** i) for i in range(31)]   # 30 個固定 +1% 報酬
    # 固定報酬序列零變異 → Pearson 未定義 → None（誠實，不假裝 1.0）
    assert btc_corr_from_closes(closes, closes) is None
    # 有變異且完全同步 → +1.0
    import math as _m
    sym_v = [100.0]
    btc_v = [100.0]
    for i in range(30):
        ret = 0.02 * _m.sin(i)        # 有變異的報酬
        sym_v.append(sym_v[-1] * (1 + ret))
        btc_v.append(btc_v[-1] * (1 + ret))
    c_same = btc_corr_from_closes(sym_v, btc_v)
    assert c_same is not None and abs(c_same - 1.0) < 1e-6, c_same
    # 完全反向 → −1.0
    inv_v = [100.0]
    for i in range(30):
        ret = -0.02 * _m.sin(i)
        inv_v.append(inv_v[-1] * (1 + ret))
    c_inv = btc_corr_from_closes(inv_v, btc_v)
    assert c_inv is not None and abs(c_inv - (-1.0)) < 1e-6, c_inv
    # 資料不足（< min_points）→ None
    assert btc_corr_from_closes([100, 101, 102], [100, 99, 98]) is None
    # 新上市幣歷史短於 BTC → 尾端對齊後仍可算（給足 min_points）
    short_sym = sym_v[-25:]   # 24 報酬
    c_short = btc_corr_from_closes(short_sym, btc_v)
    assert c_short is not None, "尾端對齊後 24 配對 ≥ min_points=20 應可算"
    print("  ✅ btc_corr：日報酬 Pearson（同向+1/反向−1/零變異 None/不足 None/尾端對齊）")

    # 證明「用價位 vs 用報酬」的差異（治本核心）：兩條都帶共同上行漂移(drift)的序列，
    # 價位都單調往上 → 價位 Pearson 偽高；但報酬的漂移常數會被去均吸收 → 只剩相位差的雜訊
    # → 報酬相關趨近 0。這正是「全幣對 BTC 價位 Pearson 幾乎都 >0.95」的成因。
    _drift = 0.02
    walk_a = [100.0]
    walk_b = [100.0]
    for i in range(30):
        walk_a.append(walk_a[-1] * (1 + _drift + 0.03 * _m.sin(i)))
        walk_b.append(walk_b[-1] * (1 + _drift + 0.03 * _m.cos(i)))   # 相位差 → 報酬雜訊不同步
    corr_ret = btc_corr_from_closes(walk_a, walk_b)
    corr_price = _pearson(walk_a[-31:], walk_b[-31:])
    assert corr_ret is not None and corr_price is not None
    assert corr_price > 0.9, f"帶漂移的價位應偽高相關，實得 {corr_price:.3f}"
    assert abs(corr_ret) < abs(corr_price), \
        f"報酬相關({corr_ret:.3f}) 應比價位相關({corr_price:.3f}) 更能區分"
    print(f"  ✅ 治本佐證：報酬相關 {corr_ret:+.3f} ≪ 價位相關 {corr_price:+.3f}（價位偽高相關）")

    # ── vol_ratio_24h_vs_30d（正確符號）──
    # 放量：24h=200、過去 30 日均=100 → 比 2.0（>1）
    assert vol_ratio_24h_vs_30d(200.0, [100.0] * 30) == 2.0
    # 縮量：24h=50、均=100 → 0.5（<1）→ 證明「量增→比值增」方向正確（修好反符號 bug）
    assert vol_ratio_24h_vs_30d(50.0, [100.0] * 30) == 0.5
    # 方向性對比：同樣的 30d 基準，較大的 24h 量 → 較大的比值（stub 是反的）
    hi = vol_ratio_24h_vs_30d(300.0, [100.0] * 30)
    lo = vol_ratio_24h_vs_30d(80.0, [100.0] * 30)
    assert hi > lo, "量增必須讓因子變大（修正 stub 反符號）"
    # 資料不足（< min_days）→ None
    assert vol_ratio_24h_vs_30d(200.0, [100.0] * 5) is None
    # 均量 0 → None（不除以零）
    assert vol_ratio_24h_vs_30d(200.0, [0.0] * 30) is None
    # None / 壞值 → None
    assert vol_ratio_24h_vs_30d(None, [100.0] * 30) is None
    assert vol_ratio_24h_vs_30d("x", [100.0] * 30) is None
    print("  ✅ vol_24h_vs_30d：量增→比值增（修正反符號）、不足/零均/壞值誠實 None")

    print("自測通過：top_trader_dev / btc_corr(日報酬) / vol(正確符號) 三純函式 ✅")
    return 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] == "--selftest":
        raise SystemExit(_selftest())
    syms = [s.upper() for s in argv] if argv else DEFAULT_SYMBOLS
    raise SystemExit(asyncio.run(main(syms)))
