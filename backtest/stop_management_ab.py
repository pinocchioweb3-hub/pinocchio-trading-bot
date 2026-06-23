"""止損『管理』A/B 回測（止損遷移：保本 / 移動止損）── 回應使用者「止損管理一定要做」。

與 stop_placement_ab.py（止損『佈局』＝一開始放哪）不同：本檔測『進場後止損怎麼動』——
TP1 落袋後把剩餘部位的止損如何遷移。四個政策同一批訊號、同一段未來 K 線、同一組三段 TP，
只換『止損遷移規則』，比較淨 R 期望值，看保本/移動止損對『我們的趨勢策略』到底加不加 EV。

研究綜整（背景工作流 wonlqpkir，7 來源）核心結論——必須以回測證明、不可預設：
  • 保本(breakeven-after-TP1)幾乎一致『抬勝率、降 EV』：砍掉跑到 TP3 的右尾大贏單，對順勢
    策略傷害最大；本系統 TP1 已落袋 50%，再對剩餘加保本＝雙重保守，預期壓低 R 期望。
  • 真正可能補回的是『移動止損(ATR/Chandelier)』讓贏單延伸——但同樣須驗證、非普世正 EV。
  ⇒ 裁判只能是『淨 R 期望值（配對 PSR）』，不是勝率（保本必抬勝率＝幻覺）。

────────────────────────────────────────────────────────────────────────
紅線③：本檔不寫死任何勝率/報酬；數字皆回測即時算出；報告開頭印『簡化輸入』橫幅。
零前視：ATR/極值只用到『當根(含)以前』已收盤資料；同根 SL/TP 皆觸→保守先判 SL。
門檻嚴格：唯有某政策淨 EV 配對 PSR≥95% 顯著優於 A_fixed 才認定「對我們有效」。
────────────────────────────────────────────────────────────────────────

執行：
    python -m backtest.stop_management_ab            # 預設 BTC/ETH/SOL
    python -m backtest.stop_management_ab BTC ETH
    python -m backtest.stop_management_ab --selftest # 合成資料自測（無需快取/網路）
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from statistics import mean

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 複用 stop_placement_ab 的零前視原語與訊號集（單一真相，確保與既有 AB 同框架）
from backtest.stop_placement_ab import (
    FEE, SLIP, HOLD_MAX, TF, DAYS, REGIME_MA,
    Signal, wilder_atr, build_regime_lookup, regime_at, generate_signals,
)
from backtest.validation import assess, psr

# ─── 止損管理參數（具名、有理由）─────────────────────────────────────────
TP_R = (1.0, 2.0, 4.0)        # TP1/TP2/TP3 = entry ± {1,2,4}×結構止損距離(1R)
TP_ALLOC = (0.5, 0.3, 0.2)    # 三段落袋比例（= botconfig.tp_size_split 預設）
BE_TRIGGER_LEG = 1            # 保本觸發：TP1 成交後
CHAND_N, CHAND_K = 22, 3.0    # Chandelier(22,3.0)：LeBeau 原始值
ATRTR_N, ATRTR_K = 14, 2.5    # ATR-trail(14,2.5)
MIN_BE_BUFFER_R = 0.1         # 保本緩衝下限（R）：切忌剛好成本價（隱性虧損＋噪音磁鐵）

VARIANTS = ("A_fixed", "B_breakeven", "C_chandelier", "D_atr_trail")


@dataclass
class Result:
    realized_r: float
    exit_reason: str          # tp | stop | timeout
    win: bool


def _rolling_atr(bars, n):
    """整段 bars 的 Wilder ATR 序列（與索引對齊；前 n 根 None）。"""
    return wilder_atr([b["high"] for b in bars], [b["low"] for b in bars],
                      [b["close"] for b in bars], n)


def _policy_stop(policy, *, bars, gi, entry, dist, bull, highest, lowest,
                 cost_r, atrN):
    """回該政策在『第 gi 根收盤後』算出的新止損價；fixed → None（不遷移）。
    只用到 gi(含)以前資料（highest/lowest 為進場後至 gi 的極值、atrN[gi]）＝零前視。"""
    if policy == "A_fixed":
        return None
    if policy == "B_breakeven":
        buf_r = max(2.0 * cost_r, MIN_BE_BUFFER_R)      # 緩衝≥成本，永不剛好成本價
        return entry + buf_r * dist if bull else entry - buf_r * dist
    atr = atrN[gi] if gi < len(atrN) else None
    if atr is None or atr <= 0:
        return None
    if policy == "C_chandelier":
        return (highest - CHAND_K * atr) if bull else (lowest + CHAND_K * atr)
    if policy == "D_atr_trail":
        cl = bars[gi]["close"]
        return (cl - ATRTR_K * atr) if bull else (cl + ATRTR_K * atr)
    return None


def simulate_policy(bars, sig: Signal, policy: str, atrN) -> Result:
    """三段分批 TP + 政策化止損遷移，逐根走『嚴格未來』K 線。零前視、單向棘輪。"""
    entry, d, dist = sig.entry, sig.direction, sig.dist_a
    bull = d == "bull"
    stop = entry - dist if bull else entry + dist
    tps = [entry + m * dist if bull else entry - m * dist for m in TP_R]
    cost_r = 2 * (FEE + SLIP) * entry / dist

    def to_r(px):
        return ((px - entry) if bull else (entry - px)) / dist

    remaining, realized, tps_hit = 1.0, 0.0, 0
    highest, lowest = entry, entry
    exit_reason = "timeout"
    fut_end = min(sig.idx + 1 + HOLD_MAX, len(bars))
    for gi in range(sig.idx + 1, fut_end):
        b = bars[gi]
        hi, lo = b["high"], b["low"]
        # ① 同根保守先判 stop（剩餘部位）
        if (lo <= stop) if bull else (hi >= stop):
            realized += remaining * to_r(stop)
            remaining, exit_reason = 0.0, ("stop" if tps_hit == 0 else "stop_after_tp")
            break
        # ② 依序判 TP1/2/3（單根可連觸多段）
        while tps_hit < 3:
            tp = tps[tps_hit]
            if (hi >= tp) if bull else (lo <= tp):
                realized += TP_ALLOC[tps_hit] * to_r(tp)
                remaining -= TP_ALLOC[tps_hit]
                tps_hit += 1
            else:
                break
        if remaining <= 1e-9:
            exit_reason = "tp"
            break
        # ③ 收盤後更新極值（含本根）→ 算政策新止損 → 單向棘輪（只收緊）
        highest, lowest = max(highest, hi), min(lowest, lo)
        if tps_hit >= BE_TRIGGER_LEG:
            ns = _policy_stop(policy, bars=bars, gi=gi, entry=entry, dist=dist,
                              bull=bull, highest=highest, lowest=lowest,
                              cost_r=cost_r, atrN=atrN)
            if ns is not None:
                stop = max(stop, ns) if bull else min(stop, ns)
    else:
        if remaining > 1e-9 and fut_end > sig.idx + 1:
            realized += remaining * to_r(bars[fut_end - 1]["close"])
    realized -= cost_r
    return Result(round(realized, 4), exit_reason, realized > 0)


def run_signal(bars, sig: Signal, atr_by_n) -> dict[str, Result]:
    return {v: simulate_policy(bars, sig, v,
                               atr_by_n[CHAND_N] if v == "C_chandelier"
                               else atr_by_n[ATRTR_N] if v == "D_atr_trail"
                               else atr_by_n[CHAND_N])
            for v in VARIANTS}


def metrics(rs: list[float]) -> dict:
    if not rs:
        return {"n": 0}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    cum = peak = mdd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    val = assess(rs)
    return {"n": len(rs), "ev": round(mean(rs), 4),
            "win_rate": round(len(wins) / len(rs) * 100, 1),
            "avg_win": round(mean(wins), 3) if wins else 0.0,
            "avg_loss": round(mean(losses), 3) if losses else 0.0,
            "max_dd_r": round(mdd, 2), "psr": val.get("psr")}


def paired_verdict(r_by_var: dict[str, list[float]]) -> dict[str, dict]:
    """每政策 vs A_fixed 的配對差異 PSR。"""
    base = r_by_var["A_fixed"]
    out = {}
    for v in VARIANTS[1:]:
        diff = [x - y for x, y in zip(r_by_var[v], base)]
        if len(diff) < 3:
            out[v] = {"delta_ev": None, "psr_better": None, "verdict": "樣本不足"}
            continue
        p = psr(diff, 0.0)
        verdict = ("✅顯著優於 A" if p >= 0.95 else "❌顯著劣於 A" if p <= 0.05
                   else "持平（無顯著差異）")
        out[v] = {"delta_ev": round(mean(diff), 4), "psr_better": round(p, 4),
                  "verdict": verdict}
    return out


def render(per_symbol_counts, overall, by_regime, verdict, n_total):
    L = ["═" * 72,
         "止損管理 A/B 回測（保本 / 移動止損 是否對我們的趨勢策略加 EV）",
         "═" * 72,
         "⚠️ 簡化輸入：突破進場＋三段 TP(1/2/4R, 0.5/0.3/0.2)＋政策化止損遷移；1h 盤中高低近似觸發；",
         "   已扣來回手續費 0.1%+滑點 0.1%，未計資金費；同根 SL/TP 皆觸保守先判 SL；數字僅供 EV 錨定。",
         f"   進場訊號：{', '.join(f'{s}={n}' for s, n in per_symbol_counts.items())}（共 {n_total} 筆，四政策同一批）",
         "   政策：A=固定原始止損(現行) / B=TP1後保本+緩衝 / C=TP1後Chandelier(22,3.0) / D=TP1後ATR-trail(14,2.5)",
         "",
         "【全樣本｜四政策對比】",
         f"  {'政策':<14}{'筆數':>5}{'期望R':>9}{'勝率':>8}{'均盈R':>8}{'均虧R':>8}{'最大回撤R':>11}{'PSR':>7}"]
    for v in VARIANTS:
        m = overall[v]
        psr_s = f"{int(m['psr']*100)}%" if m.get("psr") is not None else "—"
        L.append(f"  {v:<14}{m['n']:>5}{m['ev']:>+9.3f}{m['win_rate']:>7.1f}%"
                 f"{m['avg_win']:>+8.2f}{m['avg_loss']:>+8.2f}{m['max_dd_r']:>+11.2f}{psr_s:>7}")
    L += ["", "【配對差異檢定｜每政策 vs A_fixed（同訊號逐筆相減，裁判＝淨R期望非勝率）】"]
    for v, dd in verdict.items():
        if dd["delta_ev"] is None:
            L.append(f"  {v:<14}：{dd['verdict']}")
        else:
            L.append(f"  {v:<14}：ΔEV {dd['delta_ev']:+.3f}R／P(優於A) {int(dd['psr_better']*100)}% → {dd['verdict']}")
    L += ["", "【牛熊分段（日線200MA，進場當下，無前視）】"]
    for reg in ("bull_regime", "bear_regime", "unknown"):
        rg = by_regime.get(reg)
        if not rg or rg["A_fixed"]["n"] == 0:
            continue
        label = {"bull_regime": "升趨勢(價在200MA上)", "bear_regime": "降趨勢(價在200MA下)",
                 "unknown": "趨勢未定"}[reg]
        L.append(f"  ◆ {label}")
        L.append(f"    {'政策':<14}{'筆數':>5}{'期望R':>9}{'勝率':>8}")
        for v in VARIANTS:
            m = rg[v]
            if m["n"] == 0:
                continue
            L.append(f"    {v:<14}{m['n']:>5}{m['ev']:>+9.3f}{m['win_rate']:>7.1f}%")
    L += ["", "【拍板結論（嚴格門檻：須配對 PSR≥95% 顯著優於 A 才落地）】", _final(verdict, overall), "═" * 72]
    return "\n".join(L)


def _final(verdict, overall):
    winners = [v for v, d in verdict.items() if d.get("verdict", "").startswith("✅")]
    if winners:
        return (f"  → 通過：{', '.join(winners)} 配對顯著優於 A_fixed。建議把該政策納入 stop_policy "
                "champion/challenger，過 L2 四關(minTRL/DSR/PBO/FDR)再寫模擬盤覆寫表。")
    be = verdict.get("B_breakeven", {}).get("verdict", "")
    if be.startswith("❌"):
        return ("  → 不通過：保本(B)『顯著拖累』EV，與研究一致（砍右尾、抬勝率幻覺）。"
                "保本維持為對人軟性建議(display-only)，不寫進模擬盤回放當預設；移動止損(C/D)續觀察。")
    return ("  → 不通過（無顯著差異）：止損遷移對本策略未證實加 EV。維持 A_fixed 為預設，"
            "保本/移動止損僅作可測 challenger，未過 L2 不落地。誠實記錄『未證實』(紅線③)。")


# ─── 主流程 ────────────────────────────────────────────────────────────
async def _load(symbol):
    from backtest.data_loader import get_ohlc
    return await get_ohlc(symbol, TF, DAYS), await get_ohlc(symbol, "1d", DAYS + REGIME_MA + 30)


async def run(symbols):
    regs = ("bull_regime", "bear_regime", "unknown")
    overall = {v: [] for v in VARIANTS}
    by_reg = {reg: {v: [] for v in VARIANTS} for reg in regs}
    counts = {}
    for sym in symbols:
        bars, daily = await _load(sym)
        if not bars or len(bars) < 50 + HOLD_MAX:
            print(f"[stop_mgmt_ab] {sym}: 資料不足，略過")
            counts[sym] = 0
            continue
        atr_by_n = {CHAND_N: _rolling_atr(bars, CHAND_N), ATRTR_N: _rolling_atr(bars, ATRTR_N)}
        rts, rflag = build_regime_lookup(daily)
        sigs = generate_signals(sym, bars, rts, rflag)
        counts[sym] = len(sigs)
        for sig in sigs:
            res = run_signal(bars, sig, atr_by_n)
            for v in VARIANTS:
                overall[v].append(res[v].realized_r)
                by_reg[sig.regime][v].append(res[v].realized_r)
    n_total = sum(counts.values())
    overall_m = {v: metrics(overall[v]) for v in VARIANTS}
    by_reg_m = {reg: {v: metrics(by_reg[reg][v]) for v in VARIANTS} for reg in regs}
    verdict = paired_verdict(overall)
    print(render(counts, overall_m, by_reg_m, verdict, n_total))


# ─── 自測（合成 K 線，零網路；驗不變量）────────────────────────────────
def _selftest() -> bool:
    cases = []

    def chk(c, label):
        cases.append((bool(c), label))
        print(("  ✅" if c else "  ❌") + " " + label)

    # 合成一段穩定上漲：bull 訊號後一路漲 → TP 全中。dist=1。
    def mk(prices):
        return [{"ts": i * 3600000, "high": p + 0.2, "low": p - 0.2, "close": p}
                for i, p in enumerate(prices)]

    up = mk([100 + i * 0.5 for i in range(160)])
    sig = Signal("X", 30, up[30]["ts"], "bull", 100.0, 1.0, 1.0, "bull_regime")
    atr_by_n = {CHAND_N: _rolling_atr(up, CHAND_N), ATRTR_N: _rolling_atr(up, ATRTR_N)}
    res = run_signal(up, sig, atr_by_n)
    chk(res["A_fixed"].realized_r > 0, f"穩漲：A_fixed 正報酬({res['A_fixed'].realized_r})")
    # ratchet 不變量：政策止損永不放鬆——以一段先漲後回測，C/D 不應比 A 更早被原始止損掃
    #   （此處僅驗政策函式輸出單向：highest 增→chandelier stop 不減）
    a1 = _policy_stop("C_chandelier", bars=up, gi=60, entry=100.0, dist=1.0, bull=True,
                      highest=up[60]["high"], lowest=100.0, cost_r=0.002, atrN=atr_by_n[CHAND_N])
    a2 = _policy_stop("C_chandelier", bars=up, gi=90, entry=100.0, dist=1.0, bull=True,
                      highest=up[90]["high"], lowest=100.0, cost_r=0.002, atrN=atr_by_n[CHAND_N])
    chk(a2 >= a1, f"Chandelier 隨高點上移止損只升不降({a1:.2f}→{a2:.2f})")
    # 保本緩衝永不剛好等於 entry（避免隱性虧損）
    be = _policy_stop("B_breakeven", bars=up, gi=40, entry=100.0, dist=1.0, bull=True,
                      highest=101.0, lowest=100.0, cost_r=0.002, atrN=[None] * 200)
    chk(be > 100.0, f"保本止損>進場價(含緩衝，得 {be:.4f})")
    # 同質下跌：bear 訊號一路跌，A 應正報酬（驗方向對稱）
    dn = mk([100 - i * 0.5 for i in range(160)])
    sigb = Signal("X", 30, dn[30]["ts"], "bear", 100.0, 1.0, 1.0, "bear_regime")
    atr_b = {CHAND_N: _rolling_atr(dn, CHAND_N), ATRTR_N: _rolling_atr(dn, ATRTR_N)}
    rb = run_signal(dn, sigb, atr_b)
    chk(rb["A_fixed"].realized_r > 0, f"穩跌：bear A_fixed 正報酬({rb['A_fixed'].realized_r})")
    ok = all(c for c, _ in cases)
    print(("✅ stop_management_ab selftest PASS" if ok else "❌ FAIL"))
    return ok


if __name__ == "__main__":
    import asyncio
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--selftest" in sys.argv:
        sys.exit(0 if _selftest() else 1)
    syms = [a.upper() for a in args] or ["BTC", "ETH", "SOL"]
    asyncio.run(run(syms))
