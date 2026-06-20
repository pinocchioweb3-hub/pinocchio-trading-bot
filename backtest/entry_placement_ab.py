"""入場區深度 A/B 回測（task#59 ── 診斷 task#58「entry_expired 81% 區間設太深」的拍板閘門）。

對應記憶 trading-bot-entry-expired-verdict：deepdive 限價單 entry_expired（已平倉樣本中 ~45%
從未成交、餓死 L2 學習桶）的「真因」是**入場區設太深**（16 筆加密實證：81% 連最低點都沒回踩到
近端限價，價格趨勢跑走），而**非**成交偵測 bug（那只佔 19%＝task#60 的低衝擊保真修）。

本檔回答的問題（與 stop_placement_ab 完全同構的「先唯讀量化再決定」紀律）：
  **同一批歷史進場訊號、同一個方向論點、同一個結構止損與同一個目標價**，只換「入場積極度」
  （多積極地相對於回踩區進場），比較各組的『涵蓋率 × 進場品質』權衡，看到底哪種入場策略
  在「每個被提出的訊號」維度（per-proposed，含沒成交＝0）期望值最高。

────────────────────────────────────────────────────────────────────────
為什麼是 per-proposed（每提出一筆）而非 per-filled（每成交一筆）：
  深回踩若成交，因進場價更好、止損距離更短 → R 倍數更大（固定金額風險、倉位隨止損距離縮放）；
  但代價是**很多時候根本不成交**（涵蓋率低、樣本餓死）。真正該被優化的是兩者的權衡 →
  把「沒成交」誠實計為 0R 攤進每一筆被提出的訊號，才看得到「淺 vs 深」的真實淨效益。
  （per-filled 只看成交那些，會系統性高估深回踩、隱藏餓死樣本的代價 = 正是 task#58 的陷阱。）

紅線③（不臆造）：本檔不寫死任何勝率／報酬；數字全部由回測即時算出，報告開頭印「簡化輸入」橫幅。
  門檻嚴格：唯有某組 per-proposed EV 在統計上（PSR≥95% 的配對差異）顯著優於 A(市價即進)，
  才認定「該入場積極度對我們有效」。否則誠實記『未證實』。
紅線①（真錢）：本檔全離線、唯讀快取 OHLCV，零下單、零訊號數學變更、零 daemon 接線。
  產出的方向（淺/深 × 牛熊 regime）是要『餵給模擬盤 auto-optimizer 的證據』，真錢永遠人工。
────────────────────────────────────────────────────────────────────────

實驗設計（每個決策都為了「公平比較」與「不前視」）：
  1. 進場訊號集對各組完全相同 → apples-to-apples（只有入場策略變）。
  2. 訊號 = N 根區間突破（趨勢進場，COOLDOWN 降自相關），零前視（同 stop_placement_ab）。
  3. 方向論點固定：止損價(stop_abs) 與目標價(tp_abs) 是『同一個絕對價位』，由 A 的結構決定，
     各組共用 → 完美隔離「入場策略」這單一變因。
  4. 入場深度用『結構止損距離 dist_a 的比例』表示（非裸 ATR 倍數），確保回踩價恆在 entry 與
     stop 之間（不會荒謬地掛在止損之下），且與 deepdive LLM『相對結構提回踩區』的尺度一致：
       A 市價即進     = 訊號收盤即進（pullback=0；恆成交）
       B 淺回踩       = 限價掛在 entry − 0.33×dist_a（淺；成交率高）
       C 深回踩       = 限價掛在 entry − 0.66×dist_a（深；模擬 deepdive 過深的區，成交率低）
       D 深回踩轉市價 = 同 C，但掛單 FILL_EXPIRY 根內未成交 → 到期改市價追（追過頭超過目標價則放棄）
  5. 限價成交＝理想化『盤中觸價』（bull 用未來 low≤限價、bear 用 high≥限價）＝教科書限價語意。
     （這刻意不模擬 live monitor 只看收盤的取樣 bug＝那是 task#60 保真修，與『入場策略』正交。）
  6. 成交後逐根 bar 用盤中 high/low 走 SL/TP；同根同時觸 SL/TP 保守先判 SL（避免回測過度樂觀）。
  7. R 正規化：固定金額風險、倉位隨止損距離縮放 → 每組 R 用『自己的進場價到 stop_abs 距離』為分母。
     （深回踩成交時止損距離更短 → R 倍數天生更大；EV 任何改善必須在攤進『沒成交=0』後仍成立才算數。）
  8. 來回手續費＋滑點換算成 R 扣除（同 simulator/stop_ab 公式；深回踩止損距離小 → cost_r 較大，誠實反映）。
  9. 牛熊分段：日線 200MA 進場當下相對位置（無前視）→ 升/降 regime。這正是要餵給
     per-symbol×per-regime auto-optimizer 的切面（趨勢 regime 可能該靠市價、choppy 才值得等深回踩）。

執行：
    python -m backtest.entry_placement_ab                 # 預設 BTC/ETH/SOL，全部快取歷史
    python -m backtest.entry_placement_ab BTC ETH         # 指定幣
    python -m backtest.entry_placement_ab --selftest      # 離線合成資料自測（無需快取/網路）
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from statistics import mean

# Windows 主控台預設 cp950，印 emoji/繁中報告會 UnicodeEncodeError 崩潰 → 強制 UTF-8
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from backtest.validation import assess, psr
# 直接複用 stop_placement_ab 已自測過的零前視訊號/指標/regime 機制（單一真相來源，避免漂移）
from backtest.stop_placement_ab import (
    Signal, wilder_atr, sma_at, build_regime_lookup, regime_at, generate_signals,
    TF, DAYS, BREAKOUT_LB, HOLD_MAX, REGIME_MA, FEE, SLIP, TP_ATR,
)

# ─── 參數（全部具名、可改、有註解理由）──────────────────────────────────
FILL_EXPIRY = 12          # 限價掛單存活窗（根）；1h×12=12h，鏡像 live PENDING_MAX_HOURS=12
PULLBACK_SHALLOW = 0.33   # B 淺回踩：限價 = entry − 0.33×dist_a（淺，貼近市價，成交率高）
PULLBACK_DEEP = 0.66      # C 深回踩：限價 = entry − 0.66×dist_a（深，逼近止損，模擬 deepdive 過深的區）

VARIANTS = ("A_市價即進", "B_淺回踩", "C_深回踩", "D_深回踩轉市價")


# ─── 單筆訊號 → 單組入場策略結果 ───────────────────────────────────────
@dataclass
class Outcome:
    filled: bool
    realized_r: float       # per-proposed：未成交＝0.0
    exit_reason: str        # tp | stop | timeout | unfilled
    fill_offset: int | None  # 成交發生在訊號後第幾根（診斷用；未成交＝None）


def _simulate_exit(bars, start_idx: int, entry_px: float, stop_abs: float,
                   tp_abs: float, direction: str, risk_dist: float,
                   include_start_bar: bool) -> tuple[float, str]:
    """從 start_idx 起逐根走（最多 HOLD_MAX 根）SL/TP；同根先判 SL（保守）。回 (R, reason)。
    include_start_bar=True：限價盤中成交，當根剩餘行情仍算數（從 start_idx 起算）。
    include_start_bar=False：市價以收盤成交，從下一根起算。"""
    cost_r = 2 * (FEE + SLIP) * entry_px / risk_dist

    def to_r(px):
        return ((px - entry_px) if direction == "bull" else (entry_px - px)) / risk_dist

    begin = start_idx if include_start_bar else start_idx + 1
    fut = bars[begin:begin + HOLD_MAX]
    for b in fut:
        hi, lo = b["high"], b["low"]
        hit_stop = (lo <= stop_abs) if direction == "bull" else (hi >= stop_abs)
        hit_tp = (hi >= tp_abs) if direction == "bull" else (lo <= tp_abs)
        if hit_stop:                       # 保守：同根先判 stop
            return round(to_r(stop_abs) - cost_r, 4), "stop"
        if hit_tp:
            return round(to_r(tp_abs) - cost_r, 4), "tp"
    if fut:
        return round(to_r(fut[-1]["close"]) - cost_r, 4), "timeout"
    return round(-cost_r, 4), "timeout"


def _try_limit_fill(bars, sig_idx: int, limit_px: float, direction: str) -> int | None:
    """掛單從訊號下一根起 FILL_EXPIRY 根內，盤中是否觸價（bull:low≤限價／bear:high≥限價）。
    回首次觸價 bar 索引；皆未觸＝None。"""
    end = min(sig_idx + 1 + FILL_EXPIRY, len(bars))
    for k in range(sig_idx + 1, end):
        b = bars[k]
        touched = (b["low"] <= limit_px) if direction == "bull" else (b["high"] >= limit_px)
        if touched:
            return k
    return None


def _limit_variant(bars, sig: Signal, frac: float, stop_abs: float,
                   tp_abs: float, convert: bool) -> Outcome:
    """限價回踩組（B/C/D）。frac＝回踩深度佔 dist_a 比例。convert=True 則到期未成交改市價追。"""
    d, entry_a, dist_a = sig.direction, sig.entry, sig.dist_a
    limit_px = entry_a - frac * dist_a if d == "bull" else entry_a + frac * dist_a
    risk = abs(limit_px - stop_abs)
    if risk <= 0:                          # 退化保護（理論上 frac<1 不會發生）
        return Outcome(False, 0.0, "unfilled", None)

    fill_bar = _try_limit_fill(bars, sig.idx, limit_px, d)
    if fill_bar is not None:
        r, reason = _simulate_exit(bars, fill_bar, limit_px, stop_abs, tp_abs, d,
                                   risk, include_start_bar=True)
        return Outcome(True, r, reason, fill_bar - sig.idx)

    if not convert:                        # B/C：未成交＝不交易，per-proposed 計 0
        return Outcome(False, 0.0, "unfilled", None)

    # D：到期改市價追（價格沒回踩多半是趨勢跑走 → 追單進場價更差、R:R 更糟，誠實反映）
    conv_idx = min(sig.idx + FILL_EXPIRY, len(bars) - 1)
    conv_px = bars[conv_idx]["close"]
    # 理性追單閘：不追到已穿越自己目標價之外（那等於在目標之上買進，無意義）→ 放棄＝不交易
    if (d == "bull" and conv_px >= tp_abs) or (d == "bear" and conv_px <= tp_abs):
        return Outcome(False, 0.0, "unfilled", None)
    risk_c = abs(conv_px - stop_abs)
    if risk_c <= 0:                        # 追單時價已在止損之下（極端）→ 不交易
        return Outcome(False, 0.0, "unfilled", None)
    r, reason = _simulate_exit(bars, conv_idx, conv_px, stop_abs, tp_abs, d,
                              risk_c, include_start_bar=False)
    return Outcome(True, r, reason, conv_idx - sig.idx)


def run_signal(bars, sig: Signal) -> dict[str, Outcome]:
    """單筆訊號 → 四組入場策略結果（同 stop_abs / tp_abs，只差入場）。"""
    d, entry_a, dist_a = sig.direction, sig.entry, sig.dist_a
    if d == "bull":
        stop_abs = entry_a - dist_a
        tp_abs = entry_a + TP_ATR * sig.atr
    else:
        stop_abs = entry_a + dist_a
        tp_abs = entry_a - TP_ATR * sig.atr

    # A 市價即進：訊號收盤成交，R 分母＝結構止損距離 dist_a
    r_a, reason_a = _simulate_exit(bars, sig.idx, entry_a, stop_abs, tp_abs, d,
                                   abs(entry_a - stop_abs), include_start_bar=False)
    return {
        "A_市價即進":     Outcome(True, r_a, reason_a, 0),
        "B_淺回踩":       _limit_variant(bars, sig, PULLBACK_SHALLOW, stop_abs, tp_abs, convert=False),
        "C_深回踩":       _limit_variant(bars, sig, PULLBACK_DEEP, stop_abs, tp_abs, convert=False),
        "D_深回踩轉市價": _limit_variant(bars, sig, PULLBACK_DEEP, stop_abs, tp_abs, convert=True),
    }


# ─── 指標彙整 ──────────────────────────────────────────────────────────
def metrics(outcomes: list[Outcome]) -> dict:
    if not outcomes:
        return {"n": 0}
    proposed = len(outcomes)
    filled = [o for o in outcomes if o.filled]
    r_proposed = [o.realized_r for o in outcomes]      # 未成交＝0（涵蓋率×品質的真實淨值）
    r_filled = [o.realized_r for o in filled]
    # 最大回撤（R，per-proposed 權益曲線，依訊號序）
    cum = peak = mdd = 0.0
    for r in r_proposed:
        cum += r
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    val = assess(r_proposed)
    wins = [r for r in r_filled if r > 0]
    return {
        "n": proposed,
        "filled": len(filled),
        "fill_rate": round(len(filled) / proposed * 100, 1),
        "ev_proposed": round(mean(r_proposed), 4),
        "ev_filled": round(mean(r_filled), 4) if r_filled else None,
        "win_rate": round(len(wins) / len(filled) * 100, 1) if filled else None,
        "max_dd_r": round(mdd, 2),
        "psr": val.get("psr"), "dsr": val.get("dsr"), "min_trl": val.get("min_trl"),
    }


def paired_verdict(r_by_var: dict[str, list[float]]) -> dict[str, dict]:
    """配對差異檢定：每組 vs A(市價)（同訊號逐筆 per-proposed 相減）。psr(diff)=P(該組平均優於 A)。"""
    base = r_by_var["A_市價即進"]
    out = {}
    for v in ("B_淺回踩", "C_深回踩", "D_深回踩轉市價"):
        diff = [x - y for x, y in zip(r_by_var[v], base)]
        if len(diff) < 3:
            out[v] = {"delta_ev": None, "psr_better": None, "verdict": "樣本不足"}
            continue
        p = psr(diff, 0.0)
        delta = mean(diff)
        if p >= 0.95:
            verdict = "✅顯著優於 A(市價)"
        elif p <= 0.05:
            verdict = "❌顯著劣於 A(市價)"
        else:
            verdict = "持平（無顯著差異）"
        out[v] = {"delta_ev": round(delta, 4), "psr_better": round(p, 4), "verdict": verdict}
    return out


# ─── 報告渲染（繁中、含誠實橫幅）───────────────────────────────────────
def render(per_symbol_counts, overall, by_regime, verdict, n_total):
    L = []
    L.append("═" * 74)
    L.append("入場區深度 A/B 回測（task#59｜回踩多深才最划算：涵蓋率 × 進場品質）")
    L.append("═" * 74)
    L.append("⚠️ 簡化輸入：固定突破進場 + 同價 SL/TP(4×ATR) + 理想化限價觸價成交；用 1h K 線盤中高低近似；")
    L.append("   已扣來回手續費 0.1%+滑點 0.1%，未計資金費；『未成交』誠實計 0R 攤進每筆提出訊號。")
    L.append("   入場深度＝結構止損距離的比例（A=0／B=0.33／C=0.66）；數字僅供期望值錨定，非實盤保證。")
    L.append(f"   進場訊號：{', '.join(f'{s}={n}' for s, n in per_symbol_counts.items())}（共 {n_total} 筆，各組同一批）")
    L.append("")
    L.append("【全樣本｜四組對比（per-proposed＝含未成交的真實淨值）】")
    L.append(f"  {'組別':<16}{'提出':>5}{'成交率':>8}{'EV提出R':>10}{'EV成交R':>10}{'勝率':>8}{'最大回撤R':>11}{'PSR':>7}")
    for v in VARIANTS:
        m = overall[v]
        evf = f"{m['ev_filled']:+.3f}" if m.get("ev_filled") is not None else "—"
        wr = f"{m['win_rate']:.1f}%" if m.get("win_rate") is not None else "—"
        psr_s = f"{int(m['psr'] * 100)}%" if m.get("psr") is not None else "—"
        L.append(f"  {v:<16}{m['n']:>5}{m['fill_rate']:>7.1f}%{m['ev_proposed']:>+10.3f}"
                 f"{evf:>10}{wr:>8}{m['max_dd_r']:>+11.2f}{psr_s:>7}")
    L.append("")
    L.append("【配對差異檢定｜每組 vs A(市價)（同訊號逐筆 per-proposed 相減）】")
    for v, d in verdict.items():
        if d["delta_ev"] is None:
            L.append(f"  {v:<16}：{d['verdict']}")
        else:
            L.append(f"  {v:<16}：ΔEV {d['delta_ev']:+.3f}R／P(優於A) {int(d['psr_better'] * 100)}% → {d['verdict']}")
    L.append("")
    L.append("【牛熊分段（日線200MA 判，進場當下趨勢方向，無前視）｜餵 per-symbol×regime 優化器】")
    for reg in ("bull_regime", "bear_regime", "unknown"):
        rg = by_regime.get(reg)
        if not rg or rg["A_市價即進"]["n"] == 0:
            continue
        label = {"bull_regime": "升趨勢(價在200MA上)", "bear_regime": "降趨勢(價在200MA下)",
                 "unknown": "趨勢未定"}[reg]
        L.append(f"  ◆ {label}")
        L.append(f"    {'組別':<16}{'提出':>5}{'成交率':>8}{'EV提出R':>10}{'EV成交R':>10}")
        for v in VARIANTS:
            m = rg[v]
            if m["n"] == 0:
                continue
            evf = f"{m['ev_filled']:+.3f}" if m.get("ev_filled") is not None else "—"
            L.append(f"    {v:<16}{m['n']:>5}{m['fill_rate']:>7.1f}%{m['ev_proposed']:>+10.3f}{evf:>10}")
    L.append("")
    L.append("【拍板結論（嚴格門檻：須配對顯著才落地為入場積極度方向）】")
    L.append(_final_verdict(verdict, overall))
    L.append("═" * 74)
    return "\n".join(L)


def _final_verdict(verdict, overall):
    # 選 per-proposed EV 最高者
    ranked = sorted(VARIANTS, key=lambda v: overall[v].get("ev_proposed", -9.9), reverse=True)
    best = ranked[0]
    lines = []
    # C 深回踩 vs A 是核心問題（task#58 主因＝區太深）
    c = verdict.get("C_深回踩", {})
    b = verdict.get("B_淺回踩", {})
    d = verdict.get("D_深回踩轉市價", {})
    if c.get("verdict", "").startswith("❌"):
        lines.append("  → 確認 task#58 診斷：C(深回踩) per-proposed EV『顯著劣於』市價即進——回踩設太深的"
                     "涵蓋率損失（餓死樣本）壓過了進場品質提升。建議把 deepdive 入場區整體調淺/靠市價。")
    elif c.get("verdict", "").startswith("✅"):
        lines.append("  → 反直覺：C(深回踩) 即使攤進未成交仍顯著優於市價——進場品質紅利大於涵蓋率損失。"
                     "保留深回踩，但仍須以 D(到期轉市價) 救回被餓死的樣本。")
    else:
        lines.append("  → C(深回踩) 與市價即進無顯著差異：深回踩的『更好 R:R』恰被『更低成交率』抵消，"
                     "淨效益持平。深區唯一確定的壞處是餓死 L2 樣本（涵蓋率低）→ 仍應調淺以加速學習。")
    if b.get("verdict", "").startswith("✅"):
        lines.append("  → B(淺回踩) 顯著優於市價：存在『淺回踩甜蜜點』——略等一下拿到更好進場、又不犧牲太多成交。")
    elif b.get("verdict", "").startswith("❌"):
        lines.append("  → B(淺回踩) 顯著劣於市價：本策略連淺回踩都等不到，趨勢突破後直接市價進最划算。")
    if d.get("verdict", "").startswith("✅"):
        lines.append("  → D(深回踩轉市價) 顯著優於市價：到期改市價追能回收深回踩錯過的涵蓋率。")
    lines.append(f"  → 全樣本 per-proposed EV 最高＝【{best}】（{overall[best].get('ev_proposed'):+.3f}R/提出）。"
                 "此為『方向證據』，下一步餵入 champion/challenger 做 per-symbol×regime 調參，"
                 "過 L2 四閘（minTRL/DSR/PBO/FDR）才晉升活鍵；僅驅動模擬盤，真錢永遠人工（紅線①）。")
    lines.append("  → 牛熊分段若方向相反（趨勢靠市價、choppy 值得回踩），即為 regime-aware 入場積極度的依據。")
    return "\n".join(lines)


# ─── 主流程 ────────────────────────────────────────────────────────────
async def _load(symbol):
    from backtest.data_loader import get_ohlc
    bars_1h = await get_ohlc(symbol, TF, DAYS)
    daily = await get_ohlc(symbol, "1d", DAYS + REGIME_MA + 30)
    return bars_1h, daily


async def run(symbols):
    overall_out = {v: [] for v in VARIANTS}
    regime_out = {reg: {v: [] for v in VARIANTS}
                  for reg in ("bull_regime", "bear_regime", "unknown")}
    per_symbol_counts = {}
    for sym in symbols:
        bars, daily = await _load(sym)
        if not bars or len(bars) < BREAKOUT_LB + HOLD_MAX + 5:
            print(f"[entry_ab] {sym}: 資料不足（{len(bars) if bars else 0} 根），略過")
            per_symbol_counts[sym] = 0
            continue
        rts, rflag = build_regime_lookup(daily)
        sigs = generate_signals(sym, bars, rts, rflag)
        per_symbol_counts[sym] = len(sigs)
        for sig in sigs:
            res = run_signal(bars, sig)
            for v in VARIANTS:
                overall_out[v].append(res[v])
                regime_out[sig.regime][v].append(res[v])
    n_total = sum(per_symbol_counts.values())
    overall = {v: (metrics(overall_out[v]) if overall_out[v] else {"n": 0}) for v in VARIANTS}
    by_regime = {}
    for reg in ("bull_regime", "bear_regime", "unknown"):
        by_regime[reg] = {v: (metrics(regime_out[reg][v]) if regime_out[reg][v] else {"n": 0})
                          for v in VARIANTS}
    if n_total >= 3:
        r_by_var = {v: [o.realized_r for o in overall_out[v]] for v in VARIANTS}
        verdict = paired_verdict(r_by_var)
    else:
        verdict = {v: {"delta_ev": None, "psr_better": None, "verdict": "樣本不足"}
                   for v in ("B_淺回踩", "C_深回踩", "D_深回踩轉市價")}
    return render(per_symbol_counts, overall, by_regime, verdict, n_total)


# ─── 離線自測（合成資料，無需快取/網路）────────────────────────────────
def _selftest():
    """合成 K 線跑通整條管線；只驗『不爆、不變量正確、數值在合理域』，不臆造 edge。"""
    import math
    # 盤整→脈衝突破→回拉 反覆：確保有 fresh breakout、有回踩（讓限價成交/未成交都被走到）。
    bars = []
    px = 100.0
    ts0 = 1_700_000_000_000
    for i in range(2000):
        wobble = math.sin(i / 6.0) * 0.6
        impulse = 4.0 if (i % 60) in (30, 31) else 0.0     # 每 60 根一次向上脈衝（製造突破）
        pull = -2.5 if (i % 60) in (40, 41) else 0.0       # 隨後回拉（製造回踩/成交）
        px = max(1.0, px + 0.03 + wobble * 0.2 + impulse + pull)
        hi = px + abs(wobble) * 0.5 + 0.6
        lo = px - abs(wobble) * 0.5 - 0.6
        bars.append({"ts": ts0 + i * 3_600_000, "open": px, "high": hi,
                     "low": lo, "close": px})
    daily = []
    for dd in range(300):
        c = 80.0 + dd * 0.2
        daily.append({"ts": ts0 + dd * 86_400_000, "open": c, "high": c + 1,
                      "low": c - 1, "close": c})
    rts, rflag = build_regime_lookup(daily)
    sigs = generate_signals("SYN", bars, rts, rflag)
    assert len(sigs) > 0, "合成資料應產生至少一個突破訊號"

    out_by = {v: [] for v in VARIANTS}
    for sig in sigs:
        assert sig.dist_a > 0
        res = run_signal(bars, sig)
        for v in VARIANTS:
            o = res[v]
            assert math.isfinite(o.realized_r), f"{v} R 非有限數"
            assert (o.realized_r == 0.0) if (not o.filled) else True, "未成交 per-proposed 必為 0"
            out_by[v].append(o)

    # 不變量①：A 市價恆 100% 成交
    assert all(o.filled for o in out_by["A_市價即進"]), "A 應恆成交"
    # 不變量②：淺回踩成交數 ≥ 深回踩（bull 深限價更低 → low 觸深必觸淺）
    fill_b = sum(o.filled for o in out_by["B_淺回踩"])
    fill_c = sum(o.filled for o in out_by["C_深回踩"])
    assert fill_b >= fill_c, f"淺回踩成交({fill_b}) 應 ≥ 深回踩({fill_c})"
    # 不變量③：D(深+轉市價) 成交數 ≥ C(深)（C 成交 ⊆ D 成交，加上到期轉市價）
    fill_d = sum(o.filled for o in out_by["D_深回踩轉市價"])
    assert fill_d >= fill_c, f"D({fill_d}) 應 ≥ C({fill_c})"

    for v in VARIANTS:
        m = metrics(out_by[v])
        assert m["n"] == len(sigs)
        assert 0.0 <= m["fill_rate"] <= 100.0
        if m.get("win_rate") is not None:
            assert 0.0 <= m["win_rate"] <= 100.0

    r_by = {v: [o.realized_r for o in out_by[v]] for v in VARIANTS}
    ver = paired_verdict(r_by)
    assert set(ver) == {"B_淺回踩", "C_深回踩", "D_深回踩轉市價"}
    for v, d in ver.items():
        assert d["verdict"] in ("✅顯著優於 A(市價)", "❌顯著劣於 A(市價)",
                                "持平（無顯著差異）", "樣本不足")
    # render 不爆
    overall = {v: metrics(out_by[v]) for v in VARIANTS}
    _ = render({"SYN": len(sigs)}, overall,
               {r: {v: {"n": 0} for v in VARIANTS} for r in ("bull_regime", "bear_regime", "unknown")},
               ver, len(sigs))
    print(f"  自測通過：合成 {len(sigs)} 訊號 × 4 組入場策略，管線/不變量/檢定/渲染皆正常 ✅")
    print(f"    成交率：A=100% B(淺)={fill_b}/{len(sigs)} C(深)={fill_c}/{len(sigs)} D(深轉市價)={fill_d}/{len(sigs)}")
    return True


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--selftest" in sys.argv:
        ok = _selftest()
        sys.exit(0 if ok else 1)
    syms = [a.upper() for a in args] or ["BTC", "ETH", "SOL"]
    print(asyncio.run(run(syms)))
