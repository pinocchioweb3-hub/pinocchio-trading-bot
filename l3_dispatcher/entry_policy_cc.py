"""entry_policy_cc.py — 復盤引擎 step9（task#61）── 入場積極度 champion/challenger（重放式，零真錢）。

定位（承 task#58 診斷 + task#59 AB 定案）：
    task#58 實證 deepdive 限價單 entry_expired（已平倉樣本 ~45% 從未成交、餓死 L2 學習桶）
    主因＝入場區設太深（價格趨勢跑走、連最低點都沒回踩到限價）。task#59 離線 AB 進一步證明：
    對動量論點，市價即進 EV 最高、淺回踩顯著更差、深回踩持平，而 **D（深限價＋到期轉市價）
    能把成交率從 35%→94%、per-proposed EV 對市價無顯著差異** ＝「把死掉的 entry_expired
    非事件轉成有標籤的真實結果」的鑰匙（缺的數據回補）。

    但 task#59 的證據來自合成『突破訊號』代理，≠ deepdive 自己的 LLM 論點。正確治本不是把 D
    硬寫進 live deepdive，而是把『入場積極度』做成可調旋鈕，在**模擬盤層**對 deepdive 自身的
    真實凍結計畫跑 champion / challenger，用『真實後續 K 線』重放反事實成交結果，過 L2 四關
    才晉升。本檔就是那個重放式評估器。

本檔『雙重價值』（務必理解，影響判讀）：
    (1) 超越性檢定：某入場積極度（如市價即進）是否在 per-proposed EV 上**統計顯著優於**現行
        深限價 champion？→ 過 L2 四關才允許晉升活鍵（與 #52/#53 同等嚴謹）。
    (2) 涵蓋率回補：重放本身就替 champion 會放掉的 entry_expired 單算出『若用 D / 市價會怎樣』
        的真實標籤 → 餵飽餓死的 per-(symbol×regime) 桶，讓 (1) 的 L2 從『n<30 fail-closed』
        變成真的能跑。這是本檔對整個復盤引擎最直接的解鎖（不需 D 在 EV 上「贏」也成立）。

與 champion_challenger.py 的關係（為何需要新檔，而非沿用）：
    champion_challenger.py 明示其能力『不含』改變進場價／止損距離／持倉時限——那需要進場後的
    逐根 K 線重放（candle-replay backtest engine），並列為後續任務。**本檔就是那個後續任務**：
    入場積極度恰好需要 candle-replay（不同進場點 → 不同成交與否、不同進場價 → 不同 R）。

三紅線在本檔的落點：
    紅線①（真錢）：全離線、唯讀快取 OHLC、零下單、零訊號數學變更、零 daemon 接線。產出的方向
        只是『要餵給模擬盤 auto-optimizer 的證據』；真錢永遠人工。
    紅線③（不臆造）：未成交誠實計 0R 攤進每筆提出（per-proposed，與 AB 同口徑）；champion 重放
        必須能重現『現實中真的成交過』那些單的成交事實（self-check），否則 promote 強制 False；
        晉升門檻＝L2 四關 + 配對顯著更好，證不出就誠實記『未證實』。

可調旋鈕（入場積極度政策；**只改「如何成交」，不覆寫 LLM 提的結構區位**）：
    champion        ＝ limit_expire ：限價掛在計畫進場價，FILL_EXPIRY 根內未觸價＝放棄（現行行為）。
    challenger D     ＝ limit_convert：同上，但到期未成交→改市價追（救涵蓋率；過頭超過目標價則放棄）。
    challenger 市價  ＝ market       ：訊號當下即市價進（AB 的動量味贏家，但對 deepdive 論點待證）。

self-check（防重放模型不誠實）：對『現實中真的成交並平倉』的單，champion(limit_expire) 重放
    **必須**也判定成交（限價確實被觸及）；若重放判未成交＝重放資料/視窗有問題 → 該筆排除且
    self_check_ok=False → promote 一律 False。（反方向不罰：現實記為 expired 但重放判成交，多半
    是 task#60 的成交偵測取樣 bug＝重放更正確，非重放錯誤。）

簡化輸入（誠實揭露，與 AB 一致；本檔做的是**相對**比較，非絕對績效宣稱）：
    出場用單一代表目標價（計畫 tp1）＋計畫止損，跨政策完全相同以隔離『入場』單一變因；
    持倉窗 HOLD_MAX、限價存活窗 FILL_EXPIRY 與 AB／live 同口徑；同根同觸 SL/TP 保守先判 SL；
    來回手續費＋滑點已扣（沿用 AB 的 _simulate_exit）。不重放完整 tp1/tp2/tp3 分批階梯
    （那是 champion_challenger 的分配軸）——本檔只問『入場積極度』。

用法：
    python -m l3_dispatcher.entry_policy_cc --selftest   # 合成資料離線自測（無網路/DB）
    python -m l3_dispatcher.entry_policy_cc --demo       # 對真帳本＋快取 K 線跑一次示範
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from math import sqrt
from statistics import mean

# Windows 主控台預設 cp950，印 emoji/繁中報告會 UnicodeEncodeError → 強制 UTF-8
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 單一真相來源：直接複用 task#59 AB 已自測過的逐根重放原語（同 fill/exit 語意，避免漂移）。
#   _simulate_exit：從某根起逐根走 SL/TP（同根先判 SL、扣來回成本、固定金額風險正規化 R）。
#   _try_limit_fill：限價在訊號後 FILL_EXPIRY 根內是否盤中觸價（bull:low≤限價／bear:high≥限價）。
#   Outcome / metrics：per-proposed 結果容器與彙整（未成交計 0R）。
from backtest.entry_placement_ab import (
    _simulate_exit, _try_limit_fill, Outcome, metrics, FILL_EXPIRY,
)


# ════════════════════════════════════════════════════════════════════════
#  入場積極度政策（policy）＋ 進場計畫（plan）
# ════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class EntryPolicy:
    """入場積極度政策。kind 決定『如何成交』，不覆寫計畫的限價/止損/目標價位。
      market        ：訊號當下市價即進（恆成交）。
      limit_expire  ：限價掛在計畫進場價；FILL_EXPIRY 根內未觸價＝放棄（現行 champion）。
      limit_convert ：同 limit_expire，但到期未成交→改市價追（D，救涵蓋率）。
    """
    name: str
    kind: str   # "market" | "limit_expire" | "limit_convert"

    def __post_init__(self):
        if self.kind not in ("market", "limit_expire", "limit_convert"):
            raise ValueError(f"未知 entry policy kind：{self.kind}")


# 三個正名政策（champion 與兩個 challenger）
CHAMPION = EntryPolicy("champion(現行深限價可到期)", "limit_expire")
CHALLENGER_CONVERT = EntryPolicy("D_深限價到期轉市價", "limit_convert")
CHALLENGER_MARKET = EntryPolicy("市價即進", "market")


# ════════════════════════════════════════════════════════════════════════
#  涵蓋率非劣性晉升（task#63）── 解「D 永不晉升 → live 涵蓋率回補卡死」
# ════════════════════════════════════════════════════════════════════════
# 背景：promote 的舊路徑＝EV 超越性（chal_mean_r > champ_mean_r ∧ L2 四關）。但 task#59
#   （n=1350）已證入場積極度 **EV-neutral**——D 的價值是把成交率 35%→94%（餵飽餓死桶），
#   不是 EV。故 D 永遠過不了 EV 超越性 → 永不 promote → trade_monitor 的「到期轉市價」分支
#   在 production 永不觸發 → entry_expired 結構性卡死（即使 task#62 把桶填滿）。
#   治本＝為**可落地的回補臂 D(limit_convert)** 加第二條晉升路徑：當 D 的 EV 對 champion
#   **統計非劣**（配對單尾 95% 下界 > −margin）且**實質回補涵蓋率**且樣本足，即允許晉升。
#   （market 在 paper_journal record 層是 no-op＝幽靈晉升，故**不**走涵蓋率路徑，僅限 D。）
NONINF_MARGIN_R = 0.05      # 非劣性邊界：可容忍的每筆提出 EV 讓步上限（R）。task#59 觀測 D vs
                           #   champion 差異 ≈ −0.004R、四組 EV 全幅 ≈0.077R → 0.05R 為合理冷漠區。
COV_PROMOTE_MIN_PP = 20.0   # 涵蓋率回補晉升的最低成交率增益（百分點）。task#59 D 回補 +59pp。
COV_PROMOTE_MIN_N = 30      # 涵蓋率晉升所需最低對齊樣本（＝L2 minTRL n≥30 精神，防小樣本）。
_T95_ONESIDED = 1.70        # 保守單尾 95% t 臨界（df≥29 → t≈1.699；n≥30 保證 df≥29，略偏保守）。


def _noninferior_ev(champ_r: list[float], chal_r: list[float],
                    margin: float = NONINF_MARGIN_R) -> tuple[bool, float | None]:
    """配對單尾非劣性檢定：challenger 的 per-proposed EV 是否**非劣於** champion。

    H0（劣性）：mean(chal − champ) ≤ −margin    H1（非劣）：mean(chal − champ) > −margin
    用配對差異（同一批計畫逐筆相減 → 變異更小）的單尾 95% 下界 lower＝md − t·SE。
    lower > −margin → 判非劣（True）。回 (是否非劣, 下界 lower 四捨五入)；樣本不足 → (False, None)。
    """
    n = len(champ_r)
    if n < 2 or len(chal_r) != n:
        return (False, None)
    d = [chal_r[i] - champ_r[i] for i in range(n)]
    md = sum(d) / n
    var = sum((x - md) ** 2 for x in d) / (n - 1)
    se = sqrt(var / n) if var > 0 else 0.0
    lower = md - _T95_ONESIDED * se
    return (lower > -margin, round(lower, 4))


@dataclass(frozen=True)
class EntryPlan:
    """一份『進場當下凍結』的計畫（衍生自 plan_snapshot；非 look-ahead）。

    signal_idx     ＝訊號當下在 candles 內的索引（市價進＝該根收盤；限價從下一根起算成交）。
    limit_px       ＝計畫進場價（planned_entry；對 deepdive 限價單＝回踩區近端限價）。
    stop_px/tp_px  ＝計畫止損/目標價（planned_stop / tp1）；跨政策共用以隔離入場變因。
    reality_filled ＝現實中此單是否真的成交過（closed=True／entry_expired=False／未知=None）；
                     僅供 self-check 用（不影響重放數學）。
    pid            ＝join key（paper_id 或合成 id），對應 candles_by_pid。
    """
    pid: str
    symbol: str
    direction: str          # bull/bear（long/short 視同）
    signal_idx: int
    limit_px: float
    stop_px: float
    tp_px: float
    reality_filled: bool | None = None


def _is_bull(direction: str) -> bool:
    return direction in ("bull", "long")


# ════════════════════════════════════════════════════════════════════════
#  重放核心（單筆計畫 × 單一政策 → per-proposed Outcome）
# ════════════════════════════════════════════════════════════════════════
def replay_entry(candles: list[dict], plan: EntryPlan,
                 policy: EntryPolicy) -> Outcome | None:
    """以 policy 重放單筆計畫。回 Outcome（per-proposed：未成交＝0R）或 None（無法重放→略過）。

    None 的情形：signal_idx 越界、退化風險距離（|進場−止損|≤0）等資料問題。
    """
    n = len(candles)
    if not (0 <= plan.signal_idx < n):
        return None
    d = plan.direction
    bull = _is_bull(d)
    norm_d = "bull" if bull else "bear"   # _simulate_exit/_try_limit_fill 只認 bull/bear
    stop_abs, tp_abs, limit_px = plan.stop_px, plan.tp_px, plan.limit_px

    if policy.kind == "market":
        entry_px = candles[plan.signal_idx]["close"]
        risk = abs(entry_px - stop_abs)
        if risk <= 0:
            return None
        r, reason = _simulate_exit(candles, plan.signal_idx, entry_px, stop_abs, tp_abs,
                                   norm_d, risk, include_start_bar=False)
        return Outcome(True, r, reason, 0)

    # limit_expire / limit_convert：先試限價盤中觸價
    risk_lim = abs(limit_px - stop_abs)
    if risk_lim <= 0:
        return None
    fill_bar = _try_limit_fill(candles, plan.signal_idx, limit_px, norm_d)
    if fill_bar is not None:
        r, reason = _simulate_exit(candles, fill_bar, limit_px, stop_abs, tp_abs,
                                   norm_d, risk_lim, include_start_bar=True)
        return Outcome(True, r, reason, fill_bar - plan.signal_idx)

    if policy.kind == "limit_expire":      # champion：未成交＝放棄，per-proposed 計 0
        return Outcome(False, 0.0, "unfilled", None)

    # limit_convert（D）：到期改市價追（價沒回踩多半趨勢跑走 → 追單進場差、誠實反映）
    conv_idx = min(plan.signal_idx + FILL_EXPIRY, n - 1)
    conv_px = candles[conv_idx]["close"]
    # 理性追單閘：不追到已穿越目標價之外（在目標之上買進＝無意義）→ 放棄＝不交易
    if (bull and conv_px >= tp_abs) or (not bull and conv_px <= tp_abs):
        return Outcome(False, 0.0, "unfilled", None)
    risk_c = abs(conv_px - stop_abs)
    if risk_c <= 0:                        # 追單時價已在止損之外（極端）→ 不交易
        return Outcome(False, 0.0, "unfilled", None)
    r, reason = _simulate_exit(candles, conv_idx, conv_px, stop_abs, tp_abs, norm_d,
                              risk_c, include_start_bar=False)
    return Outcome(True, r, reason, conv_idx - plan.signal_idx)


# ════════════════════════════════════════════════════════════════════════
#  self-check：champion 重放必須重現『現實真成交』的成交事實（單向）
# ════════════════════════════════════════════════════════════════════════
def _self_check(plans: list[EntryPlan],
                candles_by_pid: dict[str, list[dict]]) -> tuple[int, int, list[str]]:
    """對 reality_filled=True 的單，champion(limit_expire) 重放必須也判成交。
    回 (n_checked, n_mismatch, mismatch_pids)。反方向（現實 expired／未知）不檢、不罰。
    """
    n_chk = 0
    mismatch: list[str] = []
    for p in plans:
        if p.reality_filled is not True:
            continue
        cs = candles_by_pid.get(p.pid)
        if not cs:
            continue
        out = replay_entry(cs, p, CHAMPION)
        if out is None:
            continue
        n_chk += 1
        if not out.filled:
            mismatch.append(p.pid)
    return n_chk, len(mismatch), mismatch


# ════════════════════════════════════════════════════════════════════════
#  champion / challenger 成對比較（入場積極度軸；L2 四關把關）
# ════════════════════════════════════════════════════════════════════════
def bucket_key(symbol: str, regime: str | None) -> str:
    """桶鍵＝symbol×regime（per-symbol × per-regime 自適應入場積極度）。
    regime 採 plan_snapshot.regime_at_entry.oi_price_quadrant（與 auto_param_store 同切面）。"""
    return f"{symbol}|{regime or 'unknown'}"


@dataclass(frozen=True)
class EntryPolicyVerdict:
    bucket_key: str
    champion: str
    challenger: str
    n_aligned: int                  # 兩政策皆可重放的筆數（進 PBO 矩陣 / 配對基礎）
    n_skipped: int                  # 無 K 線或退化而略過
    champ_mean_r: float | None      # per-proposed（含未成交 0）
    chal_mean_r: float | None
    champ_fill_rate: float | None   # %（涵蓋率；本軸的核心觀測）
    chal_fill_rate: float | None
    coverage_delta_pp: float | None  # challenger − champion 成交率（百分點）
    self_check_ok: bool
    self_check_detail: str
    l2_passed: bool
    l2_summary: str
    promote: bool                   # ＝超越性（self-check ∧ L2 四關 ∧ 配對平均更好）
    # ── task#63 涵蓋率非劣性晉升（僅 D／limit_convert）──
    ev_noninferior: bool = False    # D 的 per-proposed EV 是否統計非劣於 champion（配對單尾）
    ev_noninf_lo: float | None = None  # 配對差異(chal−champ) 單尾 95% 下界（>−margin 即非劣）
    coverage_promote: bool = False  # ＝涵蓋率非劣性晉升（self-check ∧ 非劣 ∧ 實質回補涵蓋率 ∧ n≥30）
    reasons: tuple[str, ...] = field(default_factory=tuple)


def compare_entry_policy(plans: list[EntryPlan],
                         candles_by_pid: dict[str, list[dict]],
                         challenger: EntryPolicy, *,
                         bucket_key: str, ledger=None,
                         champion: EntryPolicy = CHAMPION,
                         hypothesis: str = "", append_ledger: bool = True,
                         registered_at_ms: int | None = None) -> EntryPolicyVerdict:
    """成對重放 champion vs challenger（同一批計畫、逐筆 per-proposed 對齊），兩條晉升路徑。

    路徑①（promote，EV 超越性）＝ self_check_ok AND L2.passed AND chal_mean_r > champ_mean_r。
      （L2 的 minTRL fail-closed：對齊樣本 <30 → 一律擋下，正是現況的誠實答案。）
    路徑②（coverage_promote，涵蓋率非劣性；task#63，**僅 D／limit_convert**）＝
      self_check_ok AND (not promote) AND EV 對 champion 統計非劣（配對單尾 95% 下界 >−margin）
      AND coverage_delta_pp ≥ COV_PROMOTE_MIN_PP AND n_aligned ≥ COV_PROMOTE_MIN_N。
      存在理由：task#59 證入場積極度 EV-neutral → D 永遠過不了路徑①，但 D 的價值是把
      成交率 35%→94%（餵飽餓死桶＋讓 live trade_monitor 到期轉市價分支能真正啟用）。
      故對「可落地的回補臂 D」另開一條「EV 不變差、涵蓋率變好」的嚴謹晉升路徑。
      （market 在 record 層是 no-op＝幽靈晉升，刻意排除於路徑②。）
    兩條路徑互斥（coverage_promote 帶 not promote 條件）；即使皆不成立，coverage_delta_pp
    仍揭示 challenger 救回多少涵蓋率＝餵桶價值。
    """
    from backtest.l2_stat_gates import TrialLedger, evaluate_candidate

    reasons: list[str] = []

    # 1) self-check（單向：現實真成交者 champion 必須也判成交）
    n_chk, n_mis, mis = _self_check(plans, candles_by_pid)
    self_ok = (n_mis == 0)
    sc_detail = (f"champion 重放對『現實真成交』查核：{n_chk} 筆、{n_mis} 筆重放判未成交"
                 + (f"（pid={mis[:8]}…）" if mis else ""))
    if not self_ok:
        reasons.append("self-check 失敗（重放對不上現實成交事實）→ promote 強制 False")

    # 2) 逐筆對齊重放（兩政策皆可重放者才進矩陣，確保 PBO 同列同筆）
    champ_r: list[float] = []
    chal_r: list[float] = []
    matrix: list[list[float]] = []
    champ_fills = chal_fills = 0
    n_skip = 0
    for p in plans:
        cs = candles_by_pid.get(p.pid)
        if not cs:
            n_skip += 1
            continue
        oc = replay_entry(cs, p, champion)
        oh = replay_entry(cs, p, challenger)
        if oc is None or oh is None:
            n_skip += 1
            continue
        champ_r.append(oc.realized_r)
        chal_r.append(oh.realized_r)
        matrix.append([oc.realized_r, oh.realized_r])
        champ_fills += int(oc.filled)
        chal_fills += int(oh.filled)

    n_al = len(matrix)
    champ_mean = mean(champ_r) if champ_r else None
    chal_mean = mean(chal_r) if chal_r else None
    champ_fr = round(champ_fills / n_al * 100, 1) if n_al else None
    chal_fr = round(chal_fills / n_al * 100, 1) if n_al else None
    cov_delta = (round(chal_fr - champ_fr, 1)
                 if champ_fr is not None and chal_fr is not None else None)

    # 3) L2 四關（候選＝challenger 對齊 R 序列；matrix 給 PBO/CSCV）
    led = ledger if ledger is not None else TrialLedger()
    v = evaluate_candidate(
        led, bucket_key=bucket_key, candidate_returns=chal_r,
        matrix=matrix if (n_al >= 2 and len(matrix[0]) >= 2) else None,
        hypothesis=hypothesis or f"entry:{challenger.kind} vs {champion.kind}",
        registered_at_ms=registered_at_ms, append=append_ledger)

    better = (champ_mean is not None and chal_mean is not None and chal_mean > champ_mean)
    if not better:
        reasons.append("challenger per-proposed 平均 R 未優於 champion → 不晉升（超越性）")
    if not v.passed:
        reasons.append("L2 四關未全過（統計上未證實／樣本不足／過擬合）→ 不晉升")

    promote = bool(self_ok and v.passed and better)
    if promote:
        reasons.append("✅ self-check 過 + L2 四關全過 + per-proposed 配對更好 → 允許晉升入場積極度")
    if cov_delta is not None and cov_delta > 0:
        reasons.append(f"ℹ️ 涵蓋率回補：challenger 成交率 {champ_fr}%→{chal_fr}%"
                       f"（+{cov_delta}pp）＝餵飽餓死桶的價值（即使未 promote 仍成立）")

    # 4) task#63 涵蓋率非劣性晉升路徑（僅 D／limit_convert，唯一可落地的回補臂）。
    #    當 EV 超越性不成立（D 本就 EV-neutral）、但 D 的 EV 對 champion 統計非劣 ∧ 實質回補
    #    涵蓋率 ∧ 樣本足 → 允許晉升，讓 live trade_monitor 的到期轉市價分支能真正啟用。
    ev_ni, ev_ni_lo = _noninferior_ev(champ_r, chal_r)
    coverage_promote = bool(
        self_ok and not promote and challenger.kind == "limit_convert"
        and n_al >= COV_PROMOTE_MIN_N and ev_ni
        and cov_delta is not None and cov_delta >= COV_PROMOTE_MIN_PP)
    if coverage_promote:
        reasons.append(
            f"✅ 涵蓋率非劣性晉升（D）：EV 對 champion 統計非劣（配對單尾 95% 下界 {ev_ni_lo}R "
            f"> −{NONINF_MARGIN_R}R）＋實質回補涵蓋率（+{cov_delta}pp ≥ {COV_PROMOTE_MIN_PP}pp）"
            f"＋對齊樣本 {n_al} ≥ {COV_PROMOTE_MIN_N} → 啟用 D 以解 live entry_expired 卡死")
    elif (not promote and challenger.kind == "limit_convert"
          and cov_delta is not None and cov_delta >= COV_PROMOTE_MIN_PP):
        # 涵蓋率夠但非劣性/樣本未過 → 誠實說明為何「還不」晉升（紅線③：不捏造已達標）
        why = []
        if n_al < COV_PROMOTE_MIN_N:
            why.append(f"對齊樣本 {n_al} < {COV_PROMOTE_MIN_N}")
        if not ev_ni:
            why.append(f"EV 非劣未過（下界 {ev_ni_lo}R ≤ −{NONINF_MARGIN_R}R）")
        if not self_ok:
            why.append("self-check 失敗")
        reasons.append(f"⏸️ D 已實質回補涵蓋率(+{cov_delta}pp)，但尚未達涵蓋率晉升門檻："
                       + "、".join(why) + "（續累積樣本）")

    return EntryPolicyVerdict(
        bucket_key=bucket_key, champion=champion.name, challenger=challenger.name,
        n_aligned=n_al, n_skipped=n_skip,
        champ_mean_r=(round(champ_mean, 4) if champ_mean is not None else None),
        chal_mean_r=(round(chal_mean, 4) if chal_mean is not None else None),
        champ_fill_rate=champ_fr, chal_fill_rate=chal_fr, coverage_delta_pp=cov_delta,
        self_check_ok=self_ok, self_check_detail=sc_detail,
        l2_passed=v.passed, l2_summary=v.summary, promote=promote,
        ev_noninferior=ev_ni, ev_noninf_lo=ev_ni_lo, coverage_promote=coverage_promote,
        reasons=tuple(reasons))


# ════════════════════════════════════════════════════════════════════════
#  渲染（繁中，含誠實橫幅）
# ════════════════════════════════════════════════════════════════════════
def render_verdict(v: EntryPolicyVerdict) -> str:
    L = ["═" * 68,
         "入場積極度 champion / challenger 重放（復盤引擎 step9｜task#61）",
         "═" * 68,
         "⚠️ 純離線重放、零真錢；未成交誠實計 0R 攤進每筆提出（per-proposed）。",
         "   出場用單一代表目標(tp1)+計畫止損，跨政策相同以隔離『入場』單一變因（簡化）。",
         f"   bucket={v.bucket_key}",
         f"   champion ＝ {v.champion}",
         f"   challenger＝ {v.challenger}",
         "",
         f"  對齊樣本：{v.n_aligned} 筆（無K線/退化略過 {v.n_skipped} 筆）",
         f"  成交率（涵蓋率）：champion {v.champ_fill_rate}%  →  challenger {v.chal_fill_rate}%"
         + (f"（+{v.coverage_delta_pp}pp）" if v.coverage_delta_pp else ""),
         f"  per-proposed 平均 R：champion {v.champ_mean_r}  vs  challenger {v.chal_mean_r}",
         f"  self-check：{'✅' if v.self_check_ok else '❌'} {v.self_check_detail}",
         f"  L2 四關：{'✅' if v.l2_passed else '❌'} {v.l2_summary}",
         f"  EV 非劣性（配對單尾 95% 下界）：{'✅' if v.ev_noninferior else '❌'} "
         f"{v.ev_noninf_lo}R（門檻 >−{NONINF_MARGIN_R}R）",
         "",
         "【晉升判定（路徑①EV超越性 ∨ 路徑②涵蓋率非劣性[僅D]）】",
         f"  → {'✅ 路徑① EV 超越性晉升' if v.promote else ('✅ 路徑② 涵蓋率非劣性晉升(D)' if v.coverage_promote else '❌ 不晉升')}"]
    for r in v.reasons:
        L.append(f"     • {r}")
    L.append("═" * 68)
    return "\n".join(L)


# ════════════════════════════════════════════════════════════════════════
#  離線自測（合成 K 線，無需網路/DB）
# ════════════════════════════════════════════════════════════════════════
def _mk_candles(path: list[tuple[float, float, float]]) -> list[dict]:
    """由 (high, low, close) 序列造 candles（ts 遞增；open 取前收近似，不影響重放數學）。"""
    out = []
    ts = 1_700_000_000_000
    prev_close = path[0][2]
    for i, (hi, lo, cl) in enumerate(path):
        out.append({"ts": ts + i * 3_600_000, "open": prev_close,
                    "high": hi, "low": lo, "close": cl})
        prev_close = cl
    return out


def _selftest() -> bool:
    import math
    ok_all = True

    def chk(name, cond):
        nonlocal ok_all
        print(f"  {'✅' if cond else '❌'} {name}")
        ok_all = ok_all and cond

    # ── 場景①：bull，限價回踩成交後續漲打到 TP ──────────────────────────
    # signal_idx=0 收盤 100；限價 95；止損 90；目標 110。
    # 之後第 2 根回踩到 94（low）觸 95 限價成交，再漲到 112（high）打 110 目標。
    cs1 = _mk_candles([
        (101, 99, 100),   # 0 訊號根
        (100, 96, 98),    # 1
        (99, 94, 96),     # 2 ← low 94 ≤ 95 限價成交
        (105, 95, 104),   # 3
        (112, 103, 111),  # 4 ← high 112 ≥ 110 目標
    ] + [(111, 108, 110)] * 5)
    p1 = EntryPlan("p1", "BTC", "bull", 0, limit_px=95, stop_px=90, tp_px=110,
                   reality_filled=True)
    o_mkt = replay_entry(cs1, p1, CHALLENGER_MARKET)
    o_lim = replay_entry(cs1, p1, CHAMPION)
    o_cnv = replay_entry(cs1, p1, CHALLENGER_CONVERT)
    chk("市價政策恆成交", o_mkt is not None and o_mkt.filled)
    chk("限價回踩成交且打到 TP", o_lim is not None and o_lim.filled and o_lim.exit_reason == "tp")
    chk("D 與限價在『會回踩成交』時結果一致",
        o_cnv is not None and abs(o_cnv.realized_r - o_lim.realized_r) < 1e-9)
    # 限價成交 R 應 > 市價（進場更低、風險距離更短 → 固定金額風險 R 更大）
    chk("限價成交 R > 市價 R（更佳進場）", o_lim.realized_r > o_mkt.realized_r)

    # ── 場景②：bull，趨勢直接跑走永不回踩（entry_expired 的典型）──────────
    # 限價 95 永不被觸及；價格從 100 一路漲到 130。
    cs2 = _mk_candles([(100 + i, 99 + i, 100 + i) for i in range(15)])
    p2 = EntryPlan("p2", "ETH", "bull", 0, limit_px=95, stop_px=90, tp_px=120,
                   reality_filled=False)
    e_lim = replay_entry(cs2, p2, CHAMPION)
    e_cnv = replay_entry(cs2, p2, CHALLENGER_CONVERT)
    e_mkt = replay_entry(cs2, p2, CHALLENGER_MARKET)
    chk("champion 永不回踩＝未成交 per-proposed 0R",
        e_lim is not None and not e_lim.filled and e_lim.realized_r == 0.0)
    # D 到期市價追：conv_px=candles[12].close=112 < tp 120 → 應成交（救回涵蓋率）
    chk("D 到期轉市價救回涵蓋率（成交）", e_cnv is not None and e_cnv.filled)
    chk("市價政策當然成交", e_mkt is not None and e_mkt.filled)

    # ── 場景③：bear，限價回踩（向上回踩）成交後下跌打 TP ────────────────
    # signal 收盤 100；限價 105（向上回踩）；止損 110；目標 90。
    cs3 = _mk_candles([
        (101, 99, 100),    # 0 訊號根
        (106, 100, 105),   # 1 ← high 106 ≥ 105 限價成交（bear）
        (104, 95, 96),     # 2
        (92, 88, 89),      # 3 ← low 88 ≤ 90 目標
    ] + [(90, 86, 88)] * 5)
    p3 = EntryPlan("p3", "SOL", "bear", 0, limit_px=105, stop_px=110, tp_px=90,
                   reality_filled=True)
    b_lim = replay_entry(cs3, p3, CHAMPION)
    chk("bear 限價回踩成交且打到 TP",
        b_lim is not None and b_lim.filled and b_lim.exit_reason == "tp")

    # ── 場景④：D 理性追單閘——到期收盤已穿越目標價之外 → 放棄（不追） ────
    # bull；限價 95 永不觸；價格急漲，到期(第12根)收盤已 > 目標 105 → 不追。
    cs4 = _mk_candles([(100 + i * 2, 99 + i * 2, 100 + i * 2) for i in range(15)])
    p4 = EntryPlan("p4", "BTC", "bull", 0, limit_px=95, stop_px=90, tp_px=105,
                   reality_filled=False)
    d4 = replay_entry(cs4, p4, CHALLENGER_CONVERT)
    chk("D 追過頭超過目標價→放棄（不交易）", d4 is not None and not d4.filled)

    # ── 場景⑤：退化/越界保護 ────────────────────────────────────────────
    p_bad = EntryPlan("pb", "BTC", "bull", 99, limit_px=95, stop_px=90, tp_px=110)
    chk("signal_idx 越界 → None", replay_entry(cs1, p_bad, CHAMPION) is None)
    p_deg = EntryPlan("pd", "BTC", "bull", 0, limit_px=90, stop_px=90, tp_px=110)
    chk("限價=止損（風險距離0）→ None", replay_entry(cs1, p_deg, CHAMPION) is None)

    # ── self-check：現實真成交者 champion 必須也判成交 ─────────────────
    # p1 reality_filled=True 且重放會成交 → 不該 mismatch。
    cbp = {"p1": cs1, "p2": cs2, "p3": cs3, "p4": cs4}
    n_chk, n_mis, _ = _self_check([p1, p2, p3, p4], cbp)
    chk("self-check：真成交單無 mismatch", n_chk >= 1 and n_mis == 0)
    # 注入一筆『現實說成交但重放永不成交（限價設在永不觸及處）』→ 應抓到 mismatch
    p_ghost = EntryPlan("pg", "BTC", "bull", 0, limit_px=50, stop_px=40, tp_px=110,
                        reality_filled=True)
    cbp_g = dict(cbp, pg=cs2)   # cs2 一路漲，永不跌到 50
    _, n_mis_g, mis_g = _self_check([p_ghost], cbp_g)
    chk("self-check 抓到『現實成交但重放永不成交』", n_mis_g == 1 and "pg" in mis_g)

    # ── metrics 不變量 ──────────────────────────────────────────────────
    outs = [o for o in (o_mkt, o_lim, o_cnv) if o]
    m = metrics(outs)
    chk("metrics n 正確", m["n"] == len(outs))
    chk("metrics 成交率域", 0.0 <= m["fill_rate"] <= 100.0)

    # ── compare_entry_policy：合成 40 筆對齊樣本，跑得動、回 verdict ────
    import tempfile
    from pathlib import Path
    from backtest.l2_stat_gates import TrialLedger
    plans = []
    cbp_big: dict[str, list[dict]] = {}
    for i in range(40):
        # 交錯：一半會回踩成交、一半趨勢跑走（讓 champion 有未成交、D 有救回）
        pid = f"b{i}"
        if i % 2 == 0:
            plans.append(EntryPlan(pid, "BTC", "bull", 0, 95, 90, 110, reality_filled=True))
            cbp_big[pid] = cs1
        else:
            plans.append(EntryPlan(pid, "BTC", "bull", 0, 95, 90, 120, reality_filled=False))
            cbp_big[pid] = cs2
    with tempfile.TemporaryDirectory() as td:
        led = TrialLedger(Path(td) / "trial_ledger.jsonl")
        v = compare_entry_policy(plans, cbp_big, CHALLENGER_CONVERT,
                                 bucket_key="BTC|price_up_oi_up", ledger=led,
                                 hypothesis="selftest", append_ledger=True)
        chk("compare self_check_ok", v.self_check_ok)
        chk("compare 對齊 40 筆", v.n_aligned == 40)
        # D 成交率必 ≥ champion（救回涵蓋率）
        chk("D 成交率 ≥ champion（涵蓋率回補）",
            v.chal_fill_rate is not None and v.champ_fill_rate is not None
            and v.chal_fill_rate >= v.champ_fill_rate)
        chk("涵蓋率 delta > 0（D 確實救回）", v.coverage_delta_pp and v.coverage_delta_pp > 0)
        # 同質合成樣本 → L2 多半擋（離散低/PBO）；promote 必為 bool 不爆
        chk("verdict.promote 為 bool", isinstance(v.promote, bool))
        ok_chain, _ = led.verify_chain()
        chk("L2 ledger 鏈完整", ok_chain)
        # render 不爆
        chk("render 不爆", isinstance(render_verdict(v), str) and len(render_verdict(v)) > 0)

    # ── _noninferior_ev 純函式單元 ──────────────────────────────────────
    ni_eq, lo_eq = _noninferior_ev([0.0] * 30, [0.0] * 30)
    chk("非劣檢定：相同序列→非劣(下界≈0)", ni_eq and lo_eq is not None and abs(lo_eq) < 1e-9)
    ni_close, _ = _noninferior_ev([0.0] * 30, [-0.02] * 30)   # 讓步 0.02R < 邊界 0.05R
    chk("非劣檢定：小讓步(−0.02R)仍非劣", ni_close)
    ni_bad, lo_bad = _noninferior_ev([0.0] * 30, [-0.2] * 30)  # 讓步 0.2R > 邊界
    chk("非劣檢定：大讓步(−0.2R)非非劣", (not ni_bad) and lo_bad is not None and lo_bad < -0.05)
    chk("非劣檢定：樣本<2→(False,None)", _noninferior_ev([0.0], [0.0]) == (False, None))

    # ── 涵蓋率非劣性晉升（task#63）：30 筆趨勢跑走，champion 0% 成交、D 到期轉市價回補 ──
    def _trend_breakeven(n_bars=200):
        """bull；限價 95 永不觸（low 恆 >95）；到期(第12根)收盤 106<tp110 → D 轉市價；
        之後長期橫盤 106 → 逾時平倉≈進場價＝per-proposed R≈−成本（D 對 champion EV 非劣）。"""
        path = [(101.0, 99.0, 100.0)]
        for i in range(1, 13):
            base = 100.0 + i * 0.5            # i=12 → 106；low=base−0.2 恆 >95
            path.append((base + 0.4, base - 0.2, base))
        path += [(106.3, 105.7, 106.0)] * (n_bars - len(path))   # 橫盤，永不觸 110/80
        return _mk_candles(path)

    def _trend_crash(n_bars=200):
        """同上但 D 轉市價後崩到止損 80 → D 大幅 EV-劣（非劣性應擋下）。"""
        path = [(101.0, 99.0, 100.0)]
        for i in range(1, 13):
            base = 100.0 + i * 0.5
            path.append((base + 0.4, base - 0.2, base))
        path += [(80.5, 79.5, 80.0)] * (n_bars - len(path))      # 轉市價後即崩到止損
        return _mk_candles(path)

    def _cov_verdict(series, n_plans, kind_policy):
        plans_t = [EntryPlan(f"t{i}", "BTC", "bull", 0, 95, 80, 110, reality_filled=False)
                   for i in range(n_plans)]
        cbp_t = {f"t{i}": series for i in range(n_plans)}
        with tempfile.TemporaryDirectory() as td:
            led = TrialLedger(Path(td) / "trial_ledger.jsonl")
            return compare_entry_policy(plans_t, cbp_t, kind_policy,
                                        bucket_key="BTC|price_up_oi_up", ledger=led,
                                        hypothesis="cov-selftest")

    v_cov = _cov_verdict(_trend_breakeven(), 30, CHALLENGER_CONVERT)
    chk("涵蓋率晉升：champion 0% 成交", v_cov.champ_fill_rate == 0.0)
    chk("涵蓋率晉升：D 高成交率回補(≥20pp)",
        v_cov.coverage_delta_pp is not None and v_cov.coverage_delta_pp >= COV_PROMOTE_MIN_PP)
    chk("涵蓋率晉升：D EV 對 champion 非劣", v_cov.ev_noninferior)
    chk("涵蓋率晉升：EV 超越性 promote=False（D 本就 EV-neutral）", v_cov.promote is False)
    chk("涵蓋率晉升：coverage_promote=True（路徑②啟用 D）", v_cov.coverage_promote is True)

    v_bad = _cov_verdict(_trend_crash(), 30, CHALLENGER_CONVERT)
    chk("非劣擋下：D 大幅 EV-劣 → ev_noninferior=False", v_bad.ev_noninferior is False)
    chk("非劣擋下：coverage_promote=False（雖回補涵蓋率亦不晉升）", v_bad.coverage_promote is False)

    v_small = _cov_verdict(_trend_breakeven(), 20, CHALLENGER_CONVERT)
    chk("樣本<30：coverage_promote=False（minTRL 精神 fail-closed）",
        v_small.coverage_promote is False and v_small.n_aligned == 20)

    v_mkt = _cov_verdict(_trend_breakeven(), 30, CHALLENGER_MARKET)
    chk("market 不走涵蓋率路徑：coverage_promote=False（record 層 no-op）",
        v_mkt.coverage_promote is False)

    print("  自測通過：重放(市價/限價/D/bear/追單閘/退界) + self-check 單向防呆 + "
          "compare 涵蓋率回補 + 非劣檢定 + 涵蓋率非劣性晉升(D路徑②) + L2 鏈 ✅"
          if ok_all else "  ❌ 有失敗項")
    return ok_all


def _demo() -> None:
    """對真帳本＋快取 K 線跑一次示範（現況樣本多半 <30 → L2 fail-closed，正是誠實答案）。"""
    print("（demo：需接 paper_journal 計畫載入 + data_loader 取 K 線；step-2 接線後啟用）")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if _selftest() else 1)
    if "--demo" in sys.argv:
        _demo()
        sys.exit(0)
    print(__doc__)
    print("用法：--selftest（合成自測） | --demo（對真帳本示範）")
