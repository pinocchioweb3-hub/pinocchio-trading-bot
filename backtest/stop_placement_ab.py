"""止損佈局 A/B 回測原型（task#12 ── v44 分支①「止損掃單規避」的拍板閘門）。

對應研究報告 `docs/research/v44-止損掃單規避研究.md` 第 6.7 節的「誠實驗證設計」：
**同一批歷史進場訊號**，只換止損佈局，比較四組的真實期望值，看「抗掃單」到底有沒有
對『我們的策略』加期望值（EV）。報告第 6 節已誠實指出：抗掃單提升 EV 是『有條件假設』
（Kaminski & Lo：只有報酬具正序列相關／趨勢時止損才產生正溢價），**必須回測證明、不能假設**。
本檔就是那個證明工具。

────────────────────────────────────────────────────────────────────────
紅線③（不臆造）聲明：本檔不寫死任何勝率／報酬；所有數字都由回測即時算出，
且報告開頭印明「簡化輸入」橫幅（固定 SL/TP 結構、近似觸發、未計資金費）。
門檻嚴格：唯有 B/C 組淨 EV 在統計上（PSR≥95% 的配對差異）顯著優於 A，才認定「抗掃單對我們有效」。
────────────────────────────────────────────────────────────────────────

實驗設計（每個決策都為了「公平比較」與「不前視」）：
  1. 進場訊號集對四組完全相同 → 真正的 apples-to-apples（只有止損變）。
  2. 進場 = N 根區間突破（趨勢／動量進場）。刻意選趨勢型進場，因為那正是
     理論上止損『可能』幫得到 EV 的唯一情境；在這裡都證不出 edge，就更該停手。
  3. 零前視：swing / ATR / 趨勢方向全部只用「進場那根（含）以前」的 K 線算；
     模擬只走『嚴格未來』的 K 線。
  4. 訊號間冷卻（cooldown）降低 R 序列自相關（PSR 假設近似 iid；殘餘相關會讓 PSR 偏樂觀，已於報告誠實標註）。
  5. 四組止損：
       A 結構止損        = 近期微結構 swing（最典型、最常被掃的「明顯極值」）
       B 結構 + 1.5×ATR  = 把止損推到「磁鐵之外」一層波動緩衝
       C 寬 3×ATR        = 純波動寬止損（忽略結構）
       D 無止損          = 只靠 TP / 逾時出場（R 以 A 的結構風險距離為基準計，已標明）
  6. 停利同價：四組 TP 都在 entry ± T×ATR 的『同一個絕對價位』→ 完美隔離「止損佈局」這單一變因。
  7. R 正規化（對齊報告 6.4）：固定金額風險、倉位隨止損距離縮放 → 每組 R 用『自己的止損距離』為分母。
     （所以寬止損天生壓縮每筆 R 倍數；EV 任何改善都必須來自更高勝率／更大平均獲利才算數。）
  8. 來回手續費＋滑點換算成 R 扣除（與 simulator.py 同公式；寬止損 cost_r 較小，已誠實反映）。
  9. 逐根 bar 用盤中 high/low（看得到插針掃單）；同一根 K 同時觸 SL/TP 時保守先判 SL（避免回測過度樂觀）。
 10. 被掃率：止損命中（虧損出場）後 K 根內，價格是否反向回到『進場價』→ 量化「被掃出場後它就往你的方向走了」。
 11. 牛熊分段：用『日線 200MA』在進場當日的相對位置（趨勢／無前視）分為 升prendregime/降regime。

執行：
    python -m backtest.stop_placement_ab                 # 預設 BTC/ETH/SOL，全部快取歷史
    python -m backtest.stop_placement_ab BTC ETH         # 指定幣
    python -m backtest.stop_placement_ab --selftest      # 離線合成資料自測（無需快取/網路）
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from statistics import mean

# v49: Windows 主控台預設 cp950，印 emoji/繁中報告會 UnicodeEncodeError 崩潰 → 強制 UTF-8
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from backtest.validation import assess, psr

# ─── 參數（全部具名、可改、有註解理由）──────────────────────────────────
TF = "1h"
DAYS = 760                  # 取滿快取（約 2 年，跨牛熊）
BREAKOUT_LB = 24           # 突破訊號回看（1h × 24 = 1 天的區間高/低）
STRUCT_LB = 8              # 結構止損用的「近期微 swing」回看（較短＝貼近進場的典型掃單目標）
ATR_LEN = 14              # Wilder ATR
ATR_BUFFER = 1.5          # B 組：結構 + 1.5×ATR
WIDE_ATR = 3.0            # C 組：純 3×ATR 寬止損
TP_ATR = 4.0             # 停利同價：entry ± 4×ATR（四組共用此絕對價位）
HOLD_MAX = 120           # 逾時：120 根（1h × 120 = 5 天）
COOLDOWN = 24            # 訊號間最少間隔（降自相關）
SWEEP_K = 12            # 被掃判定：止損後 K 根內回到進場價
FEE = 0.0005           # 單邊手續費（0.05%）
SLIP = 0.0005          # 單邊滑點（0.05%）
REGIME_MA = 200       # 日線 200MA 判趨勢方向（牛熊代理）

VARIANTS = ("A_結構", "B_結構+1.5ATR", "C_寬3ATR", "D_無止損")


# ─── 技術指標（純函式、零前視）─────────────────────────────────────────
def wilder_atr(highs, lows, closes, n=ATR_LEN):
    """Wilder ATR；回傳與輸入等長的 list，前 n 根為 None（資料不足）。"""
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    trs = [0.0] * len(closes)
    for i in range(1, len(closes)):
        trs[i] = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i - 1]),
                     abs(lows[i] - closes[i - 1]))
    atr = sum(trs[1:n + 1]) / n         # 第一個 ATR = 前 n 根 TR 均值
    out[n] = atr
    for i in range(n + 1, len(closes)):
        atr = (atr * (n - 1) + trs[i]) / n
        out[i] = atr
    return out


def sma_at(values, period):
    """回傳 list：out[i] = values[i-period+1 .. i] 的均值（不足為 None）。"""
    out = [None] * len(values)
    if len(values) < period:
        return out
    run = sum(values[:period])
    out[period - 1] = run / period
    for i in range(period, len(values)):
        run += values[i] - values[i - period]
        out[i] = run / period
    return out


# ─── 訊號 ──────────────────────────────────────────────────────────────
@dataclass
class Signal:
    symbol: str
    idx: int            # 進場 bar 在 1h 序列的索引
    ts: int
    direction: str      # bull | bear
    entry: float
    dist_a: float       # 結構止損距離（>0）
    atr: float
    regime: str         # bull_regime | bear_regime | unknown


def build_regime_lookup(daily_bars):
    """日線 200MA 趨勢：回 (ts_list, flag_list)，flag = 'bull'/'bear'/None（無前視）。"""
    if not daily_bars:
        return [], []
    closes = [b["close"] for b in daily_bars]
    ma = sma_at(closes, REGIME_MA)
    ts = [b["ts"] for b in daily_bars]
    flag = [None if ma[i] is None else ("bull" if closes[i] >= ma[i] else "bear")
            for i in range(len(closes))]
    return ts, flag


def regime_at(ts_list, flag_list, ts):
    """找 ts 當下（含之前）最近一根已成形日線的趨勢旗標。二分搜尋；無則 unknown。"""
    if not ts_list:
        return "unknown"
    lo, hi, best = 0, len(ts_list) - 1, -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if ts_list[mid] <= ts:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if best < 0 or flag_list[best] is None:
        return "unknown"
    return f"{flag_list[best]}_regime"


def generate_signals(symbol, bars, regime_ts, regime_flag):
    """N 根區間突破進場（零前視）。回 list[Signal]。"""
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    closes = [b["close"] for b in bars]
    atr = wilder_atr(highs, lows, closes)
    sigs: list[Signal] = []
    last_entry = -10**9
    start = max(BREAKOUT_LB, STRUCT_LB, ATR_LEN) + 1
    for i in range(start, len(bars) - 1):     # 留至少 1 根未來
        if atr[i] is None or atr[i] <= 0:
            continue
        if i - last_entry < COOLDOWN:
            continue
        prior_high = max(highs[i - BREAKOUT_LB:i])     # 不含 i（無前視）
        prior_low = min(lows[i - BREAKOUT_LB:i])
        struct_low = min(lows[i - STRUCT_LB:i])        # 近期微 swing low
        struct_high = max(highs[i - STRUCT_LB:i])
        c, cp = closes[i], closes[i - 1]
        direction = None
        if c > prior_high and cp <= prior_high:        # 向上突破（新鮮）
            direction, entry, dist_a = "bull", c, c - struct_low
        elif c < prior_low and cp >= prior_low:        # 向下突破
            direction, entry, dist_a = "bear", c, struct_high - c
        if direction is None or dist_a <= 0:
            continue
        sigs.append(Signal(symbol, i, bars[i]["ts"], direction, entry, dist_a,
                           atr[i], regime_at(regime_ts, regime_flag, bars[i]["ts"])))
        last_entry = i
    return sigs


# ─── 單組止損模擬（單 SL / 單 TP；同價 TP 隔離止損變因）─────────────────
@dataclass
class Result:
    realized_r: float
    exit_reason: str       # tp | stop | timeout
    swept: bool | None     # 止損後是否被掃回進場價（無止損組為 None）


def simulate_variant(bars, sig: Signal, dist: float, r_ref: float,
                     no_stop: bool) -> Result:
    """從 sig.idx 之後逐根走未來 K 線。
    dist   = 本組止損距離（>0）；no_stop=True 時忽略。
    r_ref  = R 正規化分母（A/B/C = 自己的 dist；D = A 的 dist）。
    """
    entry, d = sig.entry, sig.direction
    if no_stop:
        stop = None
    else:
        stop = entry - dist if d == "bull" else entry + dist
    tp_dist = TP_ATR * sig.atr
    tp = entry + tp_dist if d == "bull" else entry - tp_dist
    cost_r = 2 * (FEE + SLIP) * entry / r_ref

    def to_r(px):
        return ((px - entry) if d == "bull" else (entry - px)) / r_ref

    fut = bars[sig.idx + 1:sig.idx + 1 + HOLD_MAX]
    for j, b in enumerate(fut):
        hi, lo, cl = b["high"], b["low"], b["close"]
        hit_stop = (not no_stop) and (lo <= stop if d == "bull" else hi >= stop)
        hit_tp = (hi >= tp) if d == "bull" else (lo <= tp)
        if hit_stop:                       # 保守：同根先判 stop
            swept = _check_sweep(bars, sig, sig.idx + 1 + j)
            return Result(round(to_r(stop) - cost_r, 4), "stop", swept)
        if hit_tp:
            return Result(round(to_r(tp) - cost_r, 4), "tp", None if no_stop else False)
    # 逾時：以最後一根 close 平
    if fut:
        return Result(round(to_r(fut[-1]["close"]) - cost_r, 4), "timeout",
                      None if no_stop else False)
    return Result(0.0, "timeout", None if no_stop else False)


def _check_sweep(bars, sig: Signal, stop_bar_idx: int) -> bool:
    """止損命中後 SWEEP_K 根內，價格是否反向回到進場價（被掃 → 接著就往你的方向走）。"""
    entry, d = sig.entry, sig.direction
    look = bars[stop_bar_idx + 1:stop_bar_idx + 1 + SWEEP_K]
    for b in look:
        if d == "bull" and b["high"] >= entry:
            return True
        if d == "bear" and b["low"] <= entry:
            return True
    return False


# ─── 一個訊號 → 四組結果 ───────────────────────────────────────────────
def run_signal(bars, sig: Signal) -> dict[str, Result]:
    dist_a = sig.dist_a
    dist_b = dist_a + ATR_BUFFER * sig.atr
    dist_c = WIDE_ATR * sig.atr
    return {
        "A_結構":        simulate_variant(bars, sig, dist_a, dist_a, no_stop=False),
        "B_結構+1.5ATR": simulate_variant(bars, sig, dist_b, dist_b, no_stop=False),
        "C_寬3ATR":      simulate_variant(bars, sig, dist_c, dist_c, no_stop=False),
        "D_無止損":      simulate_variant(bars, sig, dist_a, dist_a, no_stop=True),
    }


# ─── 指標彙整 ──────────────────────────────────────────────────────────
def metrics(rs: list[float], swept_flags: list) -> dict:
    if not rs:
        return {"n": 0}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    # 最大回撤（R，累積權益曲線）
    cum = peak = mdd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    val = assess(rs)
    n_stop = sum(1 for f in swept_flags if f is not None)
    n_swept = sum(1 for f in swept_flags if f is True)
    return {
        "n": len(rs),
        "ev": round(mean(rs), 4),
        "win_rate": round(len(wins) / len(rs) * 100, 1),
        "avg_win": round(mean(wins), 3) if wins else 0.0,
        "avg_loss": round(mean(losses), 3) if losses else 0.0,
        "max_dd_r": round(mdd, 2),
        "sweep_rate": (round(n_swept / n_stop * 100, 1) if n_stop else None),
        "psr": val.get("psr"), "dsr": val.get("dsr"),
        "min_trl": val.get("min_trl"),
    }


def paired_verdict(r_by_var: dict[str, list[float]]) -> dict[str, dict]:
    """配對差異檢定：每組 vs A（同訊號逐筆相減）。psr(diff)=P(該組平均優於 A)。"""
    base = r_by_var["A_結構"]
    out = {}
    for v in ("B_結構+1.5ATR", "C_寬3ATR", "D_無止損"):
        diff = [x - y for x, y in zip(r_by_var[v], base)]
        if len(diff) < 3:
            out[v] = {"delta_ev": None, "psr_better": None, "verdict": "樣本不足"}
            continue
        p = psr(diff, 0.0)
        delta = mean(diff)
        if p >= 0.95:
            verdict = "✅顯著優於 A"
        elif p <= 0.05:
            verdict = "❌顯著劣於 A"
        else:
            verdict = "持平（無顯著差異）"
        out[v] = {"delta_ev": round(delta, 4), "psr_better": round(p, 4),
                  "verdict": verdict}
    return out


# ─── 報告渲染（繁中、含誠實橫幅）───────────────────────────────────────
def render(per_symbol_sig_counts, overall, by_regime, verdict, n_total):
    L = []
    L.append("═" * 70)
    L.append("止損佈局 A/B 回測（task#12｜抗掃單是否真的加期望值）")
    L.append("═" * 70)
    L.append("⚠️ 簡化輸入：固定突破進場 + 同價 TP(4×ATR) + 單 SL/TP；用 1h K 線盤中高低近似觸發；")
    L.append("   已扣來回手續費 0.1%+滑點 0.1%，未計資金費；數字僅供期望值錨定，非實盤保證。")
    L.append(f"   進場訊號：{', '.join(f'{s}={n}' for s, n in per_symbol_sig_counts.items())}（共 {n_total} 筆，四組同一批）")
    L.append("")
    L.append("【全樣本｜四組對比】")
    L.append(f"  {'組別':<14}{'筆數':>5}{'期望R':>9}{'勝率':>8}{'均盈R':>8}{'均虧R':>8}{'最大回撤R':>11}{'被掃率':>8}{'PSR':>7}")
    for v in VARIANTS:
        m = overall[v]
        sweep = f"{m['sweep_rate']}%" if m.get("sweep_rate") is not None else "—"
        psr_s = f"{int(m['psr']*100)}%" if m.get("psr") is not None else "—"
        L.append(f"  {v:<14}{m['n']:>5}{m['ev']:>+9.3f}{m['win_rate']:>7.1f}%"
                 f"{m['avg_win']:>+8.2f}{m['avg_loss']:>+8.2f}{m['max_dd_r']:>+11.2f}"
                 f"{sweep:>8}{psr_s:>7}")
    L.append("")
    L.append("【配對差異檢定｜每組 vs A（同訊號逐筆相減）】")
    for v, d in verdict.items():
        if d["delta_ev"] is None:
            L.append(f"  {v:<14}：{d['verdict']}")
        else:
            L.append(f"  {v:<14}：ΔEV {d['delta_ev']:+.3f}R／P(優於A) {int(d['psr_better']*100)}% → {d['verdict']}")
    L.append("")
    L.append("【牛熊分段（日線200MA 判，進場當下趨勢方向，無前視）】")
    for reg in ("bull_regime", "bear_regime", "unknown"):
        rg = by_regime.get(reg)
        if not rg or rg["A_結構"]["n"] == 0:
            continue
        label = {"bull_regime": "升pr" + "趨勢(價在200MA上)", "bear_regime": "降趨勢(價在200MA下)",
                 "unknown": "趨勢未定"}[reg]
        L.append(f"  ◆ {label}")
        L.append(f"    {'組別':<14}{'筆數':>5}{'期望R':>9}{'勝率':>8}{'被掃率':>8}")
        for v in VARIANTS:
            m = rg[v]
            if m["n"] == 0:
                continue
            sweep = f"{m['sweep_rate']}%" if m.get("sweep_rate") is not None else "—"
            L.append(f"    {v:<14}{m['n']:>5}{m['ev']:>+9.3f}{m['win_rate']:>7.1f}%{sweep:>8}")
    L.append("")
    L.append("【拍板結論（嚴格門檻：B/C 須配對顯著優於 A 才落地）】")
    L.append(_final_verdict(verdict, overall))
    L.append("═" * 70)
    return "\n".join(L)


def _final_verdict(verdict, overall):
    b = verdict.get("B_結構+1.5ATR", {})
    c = verdict.get("C_寬3ATR", {})
    b_win = b.get("verdict", "").startswith("✅")
    c_win = c.get("verdict", "").startswith("✅")
    if b_win or c_win:
        who = " 與 ".join([x for x, ok in (("B(結構+1.5ATR)", b_win), ("C(寬3ATR)", c_win)) if ok])
        return (f"  → 通過：{who} 在統計上顯著優於結構止損 A。建議落地 ATR 緩衝止損"
                f"（研究報告 5.2 #1/#3），並把『掃單後止損改放新結構』納入規則。")
    # 看 A 是否反而顯著最好
    a_best = all(verdict.get(v, {}).get("verdict", "").startswith("❌")
                 for v in ("B_結構+1.5ATR", "C_寬3ATR"))
    if a_best:
        return ("  → 不通過（且方向相反）：加寬止損在本策略上『顯著拖累』EV，與報告 6.1/6.4"
                "（隨機/均值回歸下止損傷 EV、寬止損壓縮 R）一致。停手：只保留軟性提示層，"
                "不落地 ATR 緩衝、不買 CoinGlass $299。")
    return ("  → 不通過（無顯著差異）：抗掃單對本策略未證實加 EV。依報告第 7 節，"
            "只保留第一層 LLM 軟性提示（止損別貼整數/別貼明顯極值），"
            "不落地硬性 ATR 緩衝、不買 $299 熱力圖。誠實記錄『未證實』。")


# ─── 主流程 ────────────────────────────────────────────────────────────
async def _load(symbol):
    from backtest.data_loader import get_ohlc
    bars_1h = await get_ohlc(symbol, TF, DAYS)
    daily = await get_ohlc(symbol, "1d", DAYS + REGIME_MA + 30)
    return bars_1h, daily


def _empty_metrics_set():
    return {v: {"n": 0} for v in VARIANTS}


async def run(symbols):
    overall_r = {v: [] for v in VARIANTS}
    overall_swept = {v: [] for v in VARIANTS}
    regime_r = {reg: {v: [] for v in VARIANTS} for reg in ("bull_regime", "bear_regime", "unknown")}
    regime_swept = {reg: {v: [] for v in VARIANTS} for reg in ("bull_regime", "bear_regime", "unknown")}
    per_symbol_counts = {}
    for sym in symbols:
        bars, daily = await _load(sym)
        if not bars or len(bars) < BREAKOUT_LB + HOLD_MAX + 5:
            print(f"[stop_ab] {sym}: 資料不足（{len(bars) if bars else 0} 根），略過")
            per_symbol_counts[sym] = 0
            continue
        rts, rflag = build_regime_lookup(daily)
        sigs = generate_signals(sym, bars, rts, rflag)
        per_symbol_counts[sym] = len(sigs)
        for sig in sigs:
            res = run_signal(bars, sig)
            for v in VARIANTS:
                overall_r[v].append(res[v].realized_r)
                overall_swept[v].append(res[v].swept)
                regime_r[sig.regime][v].append(res[v].realized_r)
                regime_swept[sig.regime][v].append(res[v].swept)
    n_total = sum(per_symbol_counts.values())
    overall = {v: (metrics(overall_r[v], overall_swept[v]) if overall_r[v] else {"n": 0})
               for v in VARIANTS}
    by_regime = {}
    for reg in ("bull_regime", "bear_regime", "unknown"):
        by_regime[reg] = {v: (metrics(regime_r[reg][v], regime_swept[reg][v])
                              if regime_r[reg][v] else {"n": 0}) for v in VARIANTS}
    verdict = paired_verdict(overall_r) if n_total >= 3 else {
        v: {"delta_ev": None, "psr_better": None, "verdict": "樣本不足"}
        for v in ("B_結構+1.5ATR", "C_寬3ATR", "D_無止損")}
    return render(per_symbol_counts, overall, by_regime, verdict, n_total)


# ─── 離線自測（合成資料，無需快取/網路）────────────────────────────────
def _selftest():
    """合成 K 線跑通整條管線；只驗『不爆、結構正確、數值在合理域』，不臆造 edge。"""
    import math
    # 造一段「盤整→脈衝突破→回測」反覆的合成 1h 序列：確保會有 fresh breakout、
    # 也會有止損命中與被掃（讓 sweep/stop/tp/timeout 各分支都被走到）。
    bars = []
    px = 100.0
    ts0 = 1_700_000_000_000
    for i in range(2000):
        wobble = math.sin(i / 6.0) * 0.6                  # 盤整噪音
        impulse = 4.0 if (i % 60) in (30, 31) else 0.0    # 每 60 根一次向上脈衝（製造突破）
        pull = -2.5 if (i % 60) in (40, 41) else 0.0      # 隨後回拉（製造掃單/止損）
        px = max(1.0, px + 0.03 + wobble * 0.2 + impulse + pull)
        hi = px + abs(wobble) * 0.5 + 0.6
        lo = px - abs(wobble) * 0.5 - 0.6
        bars.append({"ts": ts0 + i * 3_600_000, "open": px, "high": hi,
                     "low": lo, "close": px})
    # 合成日線（給 regime）
    daily = []
    for d in range(300):
        c = 80.0 + d * 0.2
        daily.append({"ts": ts0 + d * 86_400_000, "open": c, "high": c + 1,
                      "low": c - 1, "close": c})
    rts, rflag = build_regime_lookup(daily)
    sigs = generate_signals("SYN", bars, rts, rflag)
    assert len(sigs) > 0, "合成資料應產生至少一個突破訊號"
    r_by = {v: [] for v in VARIANTS}
    swept_by = {v: [] for v in VARIANTS}
    for sig in sigs:
        assert sig.dist_a > 0
        res = run_signal(bars, sig)
        for v in VARIANTS:
            r = res[v].realized_r
            assert isinstance(r, float) and math.isfinite(r), f"{v} R 非有限數"
            r_by[v].append(r)
            swept_by[v].append(res[v].swept)
    for v in VARIANTS:
        m = metrics(r_by[v], swept_by[v])
        assert m["n"] == len(sigs)
        assert -1.0 <= m["win_rate"] / 100 <= 1.0
        if m.get("sweep_rate") is not None:
            assert 0.0 <= m["sweep_rate"] <= 100.0
    # D 無止損：swept 必為 None
    assert all(s is None for s in swept_by["D_無止損"])
    # A/B/C：stop 出場才有 True/False，TP/timeout 為 False
    assert all(s in (True, False) for s in swept_by["A_結構"])
    ver = paired_verdict(r_by)
    assert set(ver) == {"B_結構+1.5ATR", "C_寬3ATR", "D_無止損"}
    for v, d in ver.items():
        assert d["verdict"] in ("✅顯著優於 A", "❌顯著劣於 A", "持平（無顯著差異）", "樣本不足")
    print(f"  自測通過：合成 {len(sigs)} 訊號 × 4 組止損，管線/指標/檢定皆正常 ✅")
    return True


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--selftest" in sys.argv:
        ok = _selftest()
        sys.exit(0 if ok else 1)
    syms = [a.upper() for a in args] or ["BTC", "ETH", "SOL"]
    print(asyncio.run(run(syms)))
