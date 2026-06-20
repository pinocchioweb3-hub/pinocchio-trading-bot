"""復盤引擎 step7（task#52）── champion / challenger 離線回放（零真錢）。

定位（使用者 INTENT #1）：自動優化器要比較「現行參數（champion）」與「候選參數
（challenger）」孰優，但把關不是逐次人工點頭、而是**統計嚴謹度**。本檔在「同一份
歷史前向樣本」上，對兩組參數各自離線回放出逐筆 realized_r，再丟進 L2 四關閘
（backtest.l2_stat_gates.evaluate_candidate）——唯有 challenger 在統計上顯著、不過擬合、
且**實際上更好**，才回 promote=True。零網路、零下單、純讀本地帳本。

可忠實離線回放的兩個軸（**不需要進場後的價格路徑**）：
  1. 分批比例（tp allocation）：TP/SL 觸及的價位由市場決定、與分批大小無關；改變每段的
     size 只是線性重組已實現 R。用 paper_audit.recompute_r 拿到「與配置無關」的每段
     leg_r，再套新配置即可。timeout 段的出場價也由市場決定（與配置無關），用 champion
     配置從帳本 realized_r 回推一次該價的 R，再套 challenger 配置——忠實。
       → 這是「成對、逐筆對齊」的嚴謹 champion/challenger（同筆同列進 PBO 矩陣）。
  2. 進場過濾（selection）：候選若收緊進場條件，只是「少打一些單」——對歷史樣本做子集
     篩選即可忠實重放（每筆帶 plan_snapshot context，可套任意謂詞）。
       → 這是「子集 vs 全集」的描述式比較 + 子集自身顯著性（**非**成對檢定，已標明）。

明確不在範圍（紅線③：不臆造做不到的事）：
  改變 stop 距離 / TP 價位 / 持倉時限會改變「哪些價位被觸及、何時出場」，那需要進場後
  的逐根 K 線重放（candle-replay backtest engine）——**非本檔能力**，列為後續任務，
  本檔不假裝能算（會在文件與報告明說）。

self-check（防回放模型有 bug／不誠實）：champion 配置回放必須重現帳本記錄的 realized_r
  （非 timeout 單為獨立驗證；timeout 單驗證非 timeout 段與回推算術）。對不上 = 回放模型
  有 bug → 該筆標 unverifiable 並排除，且 self_check_ok=False 時 promote 一律 False。

用法：
    python -m l3_dispatcher.champion_challenger --selftest   # 合成資料離線自測（無網路/DB）
    python -m l3_dispatcher.champion_challenger --demo       # 對真帳本跑一次示範比較
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field

from botconfig import CONFIG

# v49：Windows 主控台預設 cp950，印 emoji/繁中會 UnicodeEncodeError → 強制 UTF-8
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# realized_r 自洽容差（純算術，留一點 rounding 餘裕；與 paper_audit.R_TOL 同口徑）
R_TOL = 0.02
_ALLOC_SUM_EPS = 1e-6


# ════════════════════════════════════════════════════════════════════════
#  參數集（policy）
# ════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class AllocPolicy:
    """分批比例參數集。tp_alloc=(a1,a2,a3) 為 TP1/TP2/TP3 各段平倉比例，
    終結段（stop/timeout）吃剩餘 size。總和必須 = 1.0（與 botconfig 同約束）。"""
    name: str
    tp_alloc: tuple[float, float, float]

    def __post_init__(self):
        a = self.tp_alloc
        if len(a) != 3:
            raise ValueError(f"tp_alloc 需三段，得 {a}")
        if any(x < 0 for x in a):
            raise ValueError(f"tp_alloc 不得有負值：{a}")
        if abs(sum(a) - 1.0) > 1e-3:
            raise ValueError(f"tp_alloc 總和須=1.0，得 {sum(a)}（{a}）")


def champion_alloc(name: str = "champion(現行)") -> AllocPolicy:
    """以現行 botconfig 的 tp_size_split 建 champion 配置（前三段）。"""
    s = CONFIG.tp_size_split
    return AllocPolicy(name, (float(s[0]), float(s[1]), float(s[2])))


# ════════════════════════════════════════════════════════════════════════
#  忠實回放核心（與配置無關的逐段 leg_r → 套任意配置）
# ════════════════════════════════════════════════════════════════════════
def _decompose(trade: dict) -> dict | None:
    """把一筆已平倉單拆成「與配置無關」的逐段 leg_r。回 {legs:[(leg,leg_r)], filled}
    或 None（不可忠實回放：sl 壞、缺 TP/SL 價、或 timeout 無法回推、或無腿）。

    reuse paper_audit.recompute_r：它已給出 tp/stop 段的 leg_r（與 size 無關），
    以及 timeout 段回推所需的 nontimeout_r / timeout_size / filled。
    """
    from l3_dispatcher.paper_audit import recompute_r
    rec = recompute_r(trade)
    if rec.get("bad_sl_dist"):
        return None
    used = rec.get("legs") or []
    if not used:
        return None
    filled = rec.get("filled") or 1.0
    legs: list[tuple[str, float]] = []
    for (leg, _size, leg_r) in used:
        if leg in ("tp1", "tp2", "tp3", "stop"):
            if leg_r is None:        # 缺價 → 不可忠實回放
                return None
            legs.append((leg, float(leg_r)))
        elif leg == "timeout":
            # timeout 出場價未存帳本 → 從記錄的 realized_r 回推（與配置無關的價的 R）
            denom = rec.get("timeout_size", 0.0) * (filled or 1.0)
            if denom <= 0:
                return None
            residual_r = float(trade["realized_r"]) - float(rec.get("nontimeout_r", 0.0))
            leg_r_t = residual_r / denom
            legs.append(("timeout", leg_r_t))
        # 其他 leg 標籤忽略（理論上不會出現）
    if not legs:
        return None
    return {"legs": legs, "filled": filled}


def replay_trade_r(trade: dict, policy: AllocPolicy) -> float | None:
    """以 policy 的分批比例重放此筆 realized_r（與帳本同口徑：×entry_filled_pct）。
    TP 段吃 alloc[i]；終結段（stop/timeout）吃剩餘 size。回 float 或 None（不可回放）。
    """
    d = _decompose(trade)
    if d is None:
        return None
    alloc = {"tp1": policy.tp_alloc[0], "tp2": policy.tp_alloc[1], "tp3": policy.tp_alloc[2]}
    r_sum = 0.0
    remaining = 1.0
    terminal_r: float | None = None
    for (leg, leg_r) in d["legs"]:
        if leg in alloc:
            size = alloc[leg]
            r_sum += size * leg_r
            remaining -= size
        else:  # stop / timeout = 終結段，吃剩餘
            terminal_r = leg_r
    if terminal_r is not None:
        size = remaining if remaining > 0 else 0.0
        r_sum += size * terminal_r
    return round(r_sum * d["filled"], 6)


def replay_series(trades: list[dict], policy: AllocPolicy) -> tuple[list[float], int]:
    """對整份樣本回放 policy。回 (returns, n_unverifiable)。順序與 trades 對齊（None 略過）。"""
    out: list[float] = []
    bad = 0
    for t in trades:
        r = replay_trade_r(t, policy)
        if r is None:
            bad += 1
        else:
            out.append(r)
    return out, bad


def _self_check(trades: list[dict]) -> tuple[int, int, list[int]]:
    """champion 回放是否重現帳本 realized_r。回 (n_checked, n_mismatch, mismatch_ids)。"""
    champ = champion_alloc()
    n_checked = 0
    mismatch_ids: list[int] = []
    for t in trades:
        r = replay_trade_r(t, champ)
        if r is None:
            continue
        n_checked += 1
        if abs(r - float(t["realized_r"])) > R_TOL:
            mismatch_ids.append(int(t.get("id", -1)))
    return n_checked, len(mismatch_ids), mismatch_ids


# ════════════════════════════════════════════════════════════════════════
#  champion / challenger 成對比較（分批比例軸；L2 四關把關）
# ════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class ChallengerVerdict:
    bucket_key: str
    champion: str
    challenger: str
    n_aligned: int                 # 兩配置皆可回放的筆數（進 PBO 矩陣）
    n_unverifiable: int
    champ_mean_r: float | None
    chal_mean_r: float | None
    self_check_ok: bool
    self_check_detail: str
    l2_passed: bool
    l2_summary: str
    promote: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


def compare_allocation(trades: list[dict], challenger: AllocPolicy, *,
                       bucket_key: str, ledger=None,
                       champion: AllocPolicy | None = None,
                       hypothesis: str = "", append_ledger: bool = True,
                       registered_at_ms: int | None = None) -> ChallengerVerdict:
    """成對回放 champion vs challenger（同份樣本、逐筆對齊），過 L2 四關才 promote。

    promote = self_check_ok AND L2.passed AND chal_mean_r > champ_mean_r。
    （L2 的 minTRL fail-closed：對齊樣本 <30 筆 → 一律擋下，這正是現況下的誠實答案。）
    """
    from backtest.l2_stat_gates import TrialLedger, evaluate_candidate

    champ = champion or champion_alloc()
    reasons: list[str] = []

    # 1) self-check：champion 必須重現帳本
    n_chk, n_mis, mis_ids = _self_check(trades)
    self_ok = (n_mis == 0)
    sc_detail = (f"champion 回放對帳：{n_chk} 筆查核、{n_mis} 筆不符"
                 + (f"（id={mis_ids[:8]}…）" if mis_ids else ""))
    if not self_ok:
        reasons.append("self-check 失敗（回放對不上帳本）→ promote 強制 False")

    # 2) 逐筆對齊回放（兩配置皆可回放者才進矩陣，確保 PBO 同列同筆）
    aligned_champ: list[float] = []
    aligned_chal: list[float] = []
    matrix: list[list[float]] = []
    n_unver = 0
    for t in trades:
        rc = replay_trade_r(t, champ)
        rh = replay_trade_r(t, challenger)
        if rc is None or rh is None:
            n_unver += 1
            continue
        aligned_champ.append(rc)
        aligned_chal.append(rh)
        matrix.append([rc, rh])

    champ_mean = (sum(aligned_champ) / len(aligned_champ)) if aligned_champ else None
    chal_mean = (sum(aligned_chal) / len(aligned_chal)) if aligned_chal else None

    # 3) L2 四關（候選 = challenger 的對齊 R 序列；matrix 給 PBO/CSCV）
    led = ledger if ledger is not None else TrialLedger()
    v = evaluate_candidate(
        led, bucket_key=bucket_key, candidate_returns=aligned_chal,
        matrix=matrix if len(matrix) >= 2 and len(matrix[0]) >= 2 else None,
        hypothesis=hypothesis or f"alloc:{challenger.tp_alloc} vs {champ.tp_alloc}",
        registered_at_ms=registered_at_ms, append=append_ledger)

    better = (champ_mean is not None and chal_mean is not None and chal_mean > champ_mean)
    if not better:
        reasons.append("challenger 平均 R 未優於 champion → 不晉升")
    if not v.passed:
        reasons.append("L2 四關未全過（統計上未證實／樣本不足／過擬合）→ 不晉升")

    promote = bool(self_ok and v.passed and better)
    if promote:
        reasons.append("✅ self-check 過 + L2 四關全過 + 實際更好 → 允許寫入此參數變更")

    return ChallengerVerdict(
        bucket_key=bucket_key, champion=champ.name, challenger=challenger.name,
        n_aligned=len(matrix), n_unverifiable=n_unver,
        champ_mean_r=(round(champ_mean, 4) if champ_mean is not None else None),
        chal_mean_r=(round(chal_mean, 4) if chal_mean is not None else None),
        self_check_ok=self_ok, self_check_detail=sc_detail,
        l2_passed=v.passed, l2_summary=v.summary, promote=promote,
        reasons=tuple(reasons))


# ════════════════════════════════════════════════════════════════════════
#  進場過濾軸（子集 vs 全集；描述式 + 子集顯著性，非成對檢定）
# ════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class SelectionVerdict:
    bucket_key: str
    n_full: int
    n_subset: int
    full_mean_r: float | None
    subset_mean_r: float | None
    l2_passed: bool
    l2_summary: str
    note: str = ("⚠️ 進場過濾軸＝子集 vs 全集的『描述式比較』＋子集自身顯著性，"
                 "非逐筆成對檢定（不同樣本）；勿宣稱為配對結果。")


def evaluate_selection(trades: list[dict], predicate, *, bucket_key: str,
                       ledger=None, hypothesis: str = "",
                       policy: AllocPolicy | None = None,
                       append_ledger: bool = True,
                       registered_at_ms: int | None = None) -> SelectionVerdict:
    """進場過濾候選：predicate(trade)->bool 選出子集，比較子集 vs 全集平均 R，
    並對子集 R 序列跑 L2（子集的 edge 是否統計上為真）。

    predicate 拿到的是「整筆 trade dict」（含 plan_snapshot 衍生欄，呼叫端可自備
    取 context 的 helper）。本軸不改變出場、純做樣本選擇 → 忠實。
    """
    from backtest.l2_stat_gates import TrialLedger, evaluate_candidate

    pol = policy or champion_alloc()
    full_r, _ = replay_series(trades, pol)
    subset = [t for t in trades if _safe_pred(predicate, t)]
    subset_r, _ = replay_series(subset, pol)

    full_mean = (sum(full_r) / len(full_r)) if full_r else None
    subset_mean = (sum(subset_r) / len(subset_r)) if subset_r else None

    led = ledger if ledger is not None else TrialLedger()
    v = evaluate_candidate(
        led, bucket_key=bucket_key, candidate_returns=subset_r, matrix=None,
        hypothesis=hypothesis or "selection-filter",
        registered_at_ms=registered_at_ms, append=append_ledger)

    return SelectionVerdict(
        bucket_key=bucket_key, n_full=len(full_r), n_subset=len(subset_r),
        full_mean_r=(round(full_mean, 4) if full_mean is not None else None),
        subset_mean_r=(round(subset_mean, 4) if subset_mean is not None else None),
        l2_passed=v.passed, l2_summary=v.summary)


def _safe_pred(predicate, trade) -> bool:
    try:
        return bool(predicate(trade))
    except Exception:
        return False


# ════════════════════════════════════════════════════════════════════════
#  渲染（繁中，含誠實橫幅）
# ════════════════════════════════════════════════════════════════════════
def render_challenger(v: ChallengerVerdict) -> str:
    L = ["═" * 66,
         "champion / challenger 離線回放（復盤引擎 step7｜分批比例軸）",
         "═" * 66,
         "⚠️ 純離線回放、零真錢；TP/SL 價位由市場決定，本軸只重組分批 size（忠實）。",
         f"   bucket={v.bucket_key}",
         f"   champion ＝ {v.champion}",
         f"   challenger＝ {v.challenger}",
         "",
         f"  對齊樣本：{v.n_aligned} 筆（不可回放略過 {v.n_unverifiable} 筆）",
         f"  champion 平均 R ＝ {v.champ_mean_r}",
         f"  challenger平均 R ＝ {v.chal_mean_r}",
         f"  self-check：{'✅' if v.self_check_ok else '❌'} {v.self_check_detail}",
         f"  L2 四關：{'✅' if v.l2_passed else '❌'} {v.l2_summary}",
         "",
         "【晉升判定（self-check ∧ L2 四關 ∧ 實際更好）】",
         f"  → {'✅ 允許晉升此參數' if v.promote else '❌ 不晉升'}"]
    for r in v.reasons:
        L.append(f"     • {r}")
    L.append("═" * 66)
    return "\n".join(L)


# ════════════════════════════════════════════════════════════════════════
#  離線自測（合成資料，無需網路/DB）
# ════════════════════════════════════════════════════════════════════════
def _mk_trade(tid, direction, entry, stop, tp1, tp2, tp3, legs, realized_r,
              filled=1.0):
    return {"id": tid, "symbol": "TST", "setup": "intraday", "direction": direction,
            "entry_price": entry, "stop_price": stop, "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "entry_at": 0, "exit_at": 10, "legs_hit": legs, "exit_reason": legs.split(",")[-1],
            "realized_r": realized_r, "pnl_usd": realized_r * 100, "entry_filled_pct": filled}


def _selftest() -> bool:
    ok_all = True

    def chk(name, cond):
        nonlocal ok_all
        mark = "✅" if cond else "❌"
        print(f"  {mark} {name}")
        ok_all = ok_all and cond

    champ = champion_alloc()
    a1, a2, a3 = champ.tp_alloc  # 現行配置（測試以此造帳本 realized_r → self-check 必過）

    # — 乾淨多單：tp1+tp2+stop（entry=100, stop=90, sl=10; tp1=110(+1R) tp2=120(+2R)）—
    # champion realized_r = a1*1 + a2*2 + (剩餘=1-a1-a2)*(-1)
    rem = 1.0 - a1 - a2
    r_clean = round(a1 * 1.0 + a2 * 2.0 + rem * (-1.0), 6)
    t_clean = _mk_trade(1, "bull", 100, 90, 110, 120, 140, "tp1,tp2,stop", r_clean)

    # champion 回放重現帳本
    chk("champion 回放重現帳本(tp1,tp2,stop)",
        abs(replay_trade_r(t_clean, champ) - r_clean) < 1e-6)

    # challenger 改配置 → 重組 R（同價、不同 size）
    chal = AllocPolicy("challenger(0.4/0.3/0.3)", (0.4, 0.3, 0.3))
    expect_chal = round(0.4 * 1.0 + 0.3 * 2.0 + (1 - 0.4 - 0.3) * (-1.0), 6)
    chk("challenger 配置忠實重組 R", abs(replay_trade_r(t_clean, chal) - expect_chal) < 1e-6)

    # — 即時止損單：legs=stop → R=-1（任何配置都 -1）—
    t_stop = _mk_trade(2, "bull", 100, 90, 110, 120, 140, "stop", -1.0)
    chk("即時止損 champion 回放 = -1", abs(replay_trade_r(t_stop, champ) + 1.0) < 1e-6)
    chk("即時止損 challenger 回放 = -1", abs(replay_trade_r(t_stop, chal) + 1.0) < 1e-6)

    # — 全 TP 單：tp1,tp2,tp3（無終結段）filled=0.7 → R×0.7 —
    r_full = round((a1 * 1.0 + a2 * 2.0 + a3 * 4.0) * 0.7, 6)
    t_full = _mk_trade(3, "bull", 100, 90, 110, 120, 140, "tp1,tp2,tp3", r_full, filled=0.7)
    chk("全TP單 filled=0.7 champion 回放重現帳本",
        abs(replay_trade_r(t_full, champ) - r_full) < 1e-6)

    # — timeout 單：tp1 後 timeout，帳本 realized_r 隱含 timeout 出場價 —
    #   champion: tp1 段 a1*(+1R); timeout 段 (1-a1)*leg_r_t；設 leg_r_t=+0.5
    leg_r_t = 0.5
    r_to = round(a1 * 1.0 + (1 - a1) * leg_r_t, 6)
    t_to = _mk_trade(4, "bull", 100, 90, 110, 120, 140, "tp1,timeout", r_to)
    chk("timeout 單 champion 回放重現帳本(回推自洽)",
        abs(replay_trade_r(t_to, champ) - r_to) < 1e-6)
    # challenger 對 timeout 單：tp1 取 0.4、timeout 取 0.6 同價(leg_r_t=0.5)
    expect_to_chal = round(0.4 * 1.0 + 0.6 * leg_r_t, 6)
    chk("timeout 單 challenger 套同出場價忠實重組",
        abs(replay_trade_r(t_to, chal) - expect_to_chal) < 1e-6)

    # — 不可回放：sl_dist=0 → None —
    t_bad = _mk_trade(5, "bull", 100, 100, 110, 120, 140, "tp1,stop", 0.0)
    chk("sl_dist=0 不可回放→None", replay_trade_r(t_bad, champ) is None)

    # — self-check 偵測竄改：把帳本 realized_r 改錯 → mismatch —
    t_tamper = dict(t_clean, id=9, realized_r=r_clean + 1.0)
    n_chk, n_mis, _ = _self_check([t_clean, t_tamper])
    chk("self-check 抓到竄改的 realized_r", n_chk == 2 and n_mis == 1)

    # — compare_allocation：合成 40 筆對齊樣本 → 跑得動、回 verdict、self_check_ok —
    import tempfile
    from pathlib import Path
    from backtest.l2_stat_gates import TrialLedger
    sample = []
    # 造一批 champion-自洽的單（含多種腿型），確保 self-check 過
    for i in range(40):
        rem_i = 1.0 - a1 - a2
        rr = round(a1 * 1.0 + a2 * 2.0 + rem_i * (-1.0), 6)
        sample.append(_mk_trade(100 + i, "bull", 100, 90, 110, 120, 140, "tp1,tp2,stop", rr))
    with tempfile.TemporaryDirectory() as td:
        led = TrialLedger(Path(td) / "trial_ledger.jsonl")
        v = compare_allocation(sample, chal, bucket_key="TST|bull", ledger=led,
                               hypothesis="selftest", append_ledger=True)
        chk("compare_allocation self_check_ok", v.self_check_ok)
        chk("compare_allocation 對齊 40 筆", v.n_aligned == 40)
        # 同質樣本（每筆 R 相同）→ 離散=0 → minTRL/DSR 應擋（promote False，誠實）
        chk("同質樣本 L2 擋下（不誤晉升）", v.promote is False)
        ok_chain, _ = led.verify_chain()
        chk("L2 ledger 鏈完整", ok_chain)

    print("  自測通過：忠實回放（tp/stop/timeout/filled）+ self-check 防竄改 + "
          "compare_allocation 不誤晉升 ✅" if ok_all else "  ❌ 有失敗項")
    return ok_all


def _demo() -> None:
    """對真帳本跑一次示範（現況樣本多半 <30 → L2 fail-closed，這正是誠實答案）。"""
    from l3_dispatcher.paper_audit import load_closed
    trades = load_closed(limit=500, days=365)
    if not trades:
        print("（帳本無已平倉單，無法示範）")
        return
    chal = AllocPolicy("challenger(0.4/0.3/0.3)", (0.4, 0.3, 0.3))
    v = compare_allocation(trades, chal, bucket_key="ALL|demo",
                           hypothesis="demo: 0.4/0.3/0.3", append_ledger=False)
    print(render_challenger(v))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if _selftest() else 1)
    if "--demo" in sys.argv:
        _demo()
        sys.exit(0)
    print(__doc__)
    print("用法：--selftest（合成自測） | --demo（對真帳本示範比較）")
