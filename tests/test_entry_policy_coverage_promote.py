"""task#63：涵蓋率非劣性晉升路徑 契約測試。

鎖住「D（limit_convert）即使 EV-neutral 也能因『涵蓋率非劣性』晉升」這條路徑，避免日後
把 live 涵蓋率回補重新埋回結構性卡死（D 永過不了 EV 超越性 → trade_monitor 到期轉市價分支
永不啟用）。三層覆蓋：

  1. **評估層**（entry_policy_cc.compare_entry_policy）：
     - 30 筆趨勢跑走、橫盤 → champion 0% 成交、D 到期轉市價回補 → EV 對 champion 非劣
       ∧ 涵蓋率 +pp ≥ 門檻 ∧ n≥30 → coverage_promote=True、promote=False（EV-neutral）。
     - 崩盤（D 轉市價後打止損）→ EV 大幅劣 → ev_noninferior=False → coverage_promote=False。
     - n<30 → fail-closed → coverage_promote=False。
     - market 挑戰者（record 層 no-op＝幽靈晉升）→ 一律排除於涵蓋率路徑。
  2. **落地層**（entry_policy_store.apply_verdict）：coverage_promote=True/promote=False →
     action=promote、resolve 回 limit_convert、promote_basis="coverage_noninferiority" 留痕。
     EV 超越性同時成立時 promote_basis="ev_superiority"（① 優先）。
  3. **編排層**（entry_policy_optimizer.optimize_entry）：≥30 筆橫盤樣本 → 全域池 D 走涵蓋率
     路徑晉升 → n_promoted≥1、resolve 回 limit_convert；崩盤樣本 → 0 晉升、resolve 回 None。

全離線：覆寫表/帳本寫進 tmp_path；零網路、零真錢、零訊號數學變更（紅線①/③）。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from l3_dispatcher import entry_policy_store as eps
from l3_dispatcher import entry_policy_optimizer as epo
from l3_dispatcher.entry_policy_cc import (
    CHAMPION, CHALLENGER_CONVERT, CHALLENGER_MARKET,
    EntryPlan, _mk_candles, _noninferior_ev, compare_entry_policy,
    COV_PROMOTE_MIN_PP, COV_PROMOTE_MIN_N, NONINF_MARGIN_R,
)
from backtest.l2_stat_gates import TrialLedger


# ── 合成 K 線：訊號 close=100、限價 95 永不觸（low 恆 >95）──────────────────
def _flat_series(n: int | None = None) -> list[dict]:
    """橫盤：champion 限價 95 永不成交；D 到期(第12根 close=100)轉市價，續橫盤 →
    逾時平倉≈進場價＝per-proposed R≈−成本（D 對 champion EV 非劣）。"""
    n = n or (epo._FORWARD_BARS + 80)
    return _mk_candles([(100.3, 99.7, 100.0)] * n)


def _crash_after_conv(n: int | None = None) -> list[dict]:
    """前 13 根（含成交窗 1..12 與轉換根 12）橫盤 100；第 13 根起崩到止損 80 →
    D 轉市價後即打止損（≈ −1R），EV 大幅劣 → 非劣性應擋下。champion 仍 0% 成交。"""
    n = n or (epo._FORWARD_BARS + 80)
    head = [(100.3, 99.7, 100.0)] * 13          # bars 0..12 橫盤（low 99.7 > 95 限價）
    tail = [(80.5, 79.5, 80.0)] * (n - 13)      # bar 13+ 崩到止損 80
    return _mk_candles(head + tail)


def _plans(n: int):
    return [EntryPlan(f"t{i}", "BTC", "bull", 0, 95, 80, 110, reality_filled=False)
            for i in range(n)]


def _verdict(series, n, policy, tmp_path):
    plans = _plans(n)
    cbp = {f"t{i}": series for i in range(n)}
    led = TrialLedger(tmp_path / "ledger.jsonl")
    return compare_entry_policy(plans, cbp, policy, bucket_key="BTC|price_up_oi_up",
                               ledger=led, hypothesis="cov-contract")


# ════════════════════════════════════════════════════════════════════════
#  1. 評估層：非劣檢定純函式
# ════════════════════════════════════════════════════════════════════════
def test_noninferior_ev_identical_is_noninferior():
    ok, lo = _noninferior_ev([0.0] * 30, [0.0] * 30)
    assert ok is True and lo is not None and abs(lo) < 1e-9


def test_noninferior_ev_small_concession_passes():
    ok, _ = _noninferior_ev([0.0] * 30, [-0.02] * 30)   # 讓步 0.02R < 0.05R 邊界
    assert ok is True


def test_noninferior_ev_large_concession_fails():
    ok, lo = _noninferior_ev([0.0] * 30, [-0.5] * 30)
    assert ok is False and lo is not None and lo < -NONINF_MARGIN_R


def test_noninferior_ev_too_few_samples():
    assert _noninferior_ev([0.0], [0.0]) == (False, None)


# ════════════════════════════════════════════════════════════════════════
#  1. 評估層：compare_entry_policy 的涵蓋率路徑
# ════════════════════════════════════════════════════════════════════════
def test_D_qualifies_coverage_promote(tmp_path):
    v = _verdict(_flat_series(), 30, CHALLENGER_CONVERT, tmp_path)
    assert v.champ_fill_rate == 0.0, "champion 限價永不成交"
    assert v.coverage_delta_pp is not None and v.coverage_delta_pp >= COV_PROMOTE_MIN_PP
    assert v.ev_noninferior is True, "D 橫盤 EV 對 champion 應非劣"
    assert v.promote is False, "D 本就 EV-neutral → 不走 EV 超越性"
    assert v.coverage_promote is True, "→ 應走涵蓋率非劣性晉升（路徑②）"


def test_D_crash_blocks_coverage_promote(tmp_path):
    v = _verdict(_crash_after_conv(), 30, CHALLENGER_CONVERT, tmp_path)
    assert v.coverage_delta_pp is not None and v.coverage_delta_pp >= COV_PROMOTE_MIN_PP, \
        "涵蓋率確實回補（D 有成交）"
    assert v.ev_noninferior is False, "D 轉市價後打止損 → EV 大幅劣 → 非劣性擋下"
    assert v.coverage_promote is False, "EV 劣 → 不得因涵蓋率而晉升（紅線③不捏造）"


def test_below_min_n_blocks_coverage_promote(tmp_path):
    v = _verdict(_flat_series(), COV_PROMOTE_MIN_N - 1, CHALLENGER_CONVERT, tmp_path)
    assert v.n_aligned == COV_PROMOTE_MIN_N - 1
    assert v.coverage_promote is False, "樣本 <30 → minTRL 精神 fail-closed"


def test_market_excluded_from_coverage_path(tmp_path):
    v = _verdict(_flat_series(), 30, CHALLENGER_MARKET, tmp_path)
    assert v.coverage_promote is False, "market 在 record 層 no-op＝幽靈晉升 → 排除於路徑②"


# ════════════════════════════════════════════════════════════════════════
#  2. 落地層：apply_verdict 接受 coverage_promote
# ════════════════════════════════════════════════════════════════════════
from types import SimpleNamespace


def _ns(promote, coverage_promote, **kw):
    base = dict(promote=promote, coverage_promote=coverage_promote,
                bucket_key="BTC|price_up_oi_up", champion="champion(現行深限價可到期)",
                challenger="D_深限價到期轉市價", champ_mean_r=-0.01, chal_mean_r=-0.03,
                champ_fill_rate=0.0, chal_fill_rate=94.0, coverage_delta_pp=94.0,
                ev_noninferior=True, ev_noninf_lo=-0.01, n_aligned=40,
                self_check_ok=True, l2_passed=False, l2_summary="(contract)", reasons=[])
    base.update(kw)
    return SimpleNamespace(**base)


def test_store_coverage_promote_activates(tmp_path):
    ap, au = tmp_path / eps.ACTIVE_NAME, tmp_path / eps.AUDIT_NAME
    r = eps.apply_verdict(_ns(False, True), symbol="BTC", quadrant="price_up_oi_up",
                          challenger_kind="limit_convert", champion_kind="limit_expire",
                          at_ms=1, active_path=ap, audit_path=au)
    assert r["action"] == "promote"
    assert eps.resolve_entry_policy("BTC", "price_up_oi_up", active_path=ap) == "limit_convert"
    import json
    bkt = json.loads(ap.read_text(encoding="utf-8"))["buckets"]["BTC|price_up_oi_up"]
    assert bkt["promote_basis"] == "coverage_noninferiority"
    rec = json.loads(au.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["coverage_promote"] is True and rec["promote"] is False
    assert rec["promote_basis"] == "coverage_noninferiority"


def test_store_ev_superiority_takes_precedence_in_basis(tmp_path):
    ap, au = tmp_path / eps.ACTIVE_NAME, tmp_path / eps.AUDIT_NAME
    # 兩路徑同時成立 → promote_basis 記 EV 超越性（① 優先）
    r = eps.apply_verdict(_ns(True, True), symbol="BTC", quadrant="price_up_oi_up",
                          challenger_kind="limit_convert", champion_kind="limit_expire",
                          at_ms=1, active_path=ap, audit_path=au)
    assert r["action"] == "promote"
    import json
    bkt = json.loads(ap.read_text(encoding="utf-8"))["buckets"]["BTC|price_up_oi_up"]
    assert bkt["promote_basis"] == "ev_superiority"


def test_store_neither_path_holds(tmp_path):
    ap, au = tmp_path / eps.ACTIVE_NAME, tmp_path / eps.AUDIT_NAME
    r = eps.apply_verdict(_ns(False, False), symbol="BTC", quadrant="price_up_oi_up",
                          challenger_kind="limit_convert", champion_kind="limit_expire",
                          at_ms=1, active_path=ap, audit_path=au)
    assert r["action"] == "hold"
    assert eps.resolve_entry_policy("BTC", "price_up_oi_up", active_path=ap) is None
    assert not ap.exists()


# ════════════════════════════════════════════════════════════════════════
#  3. 編排層：optimize_entry 端到端
# ════════════════════════════════════════════════════════════════════════
def _optimize(series, n, tmp_path):
    plans = _plans(n)
    quad = {f"t{i}": "price_up_oi_up" for i in range(n)}
    cbp = {f"t{i}": series for i in range(n)}
    ap, au = tmp_path / eps.ACTIVE_NAME, tmp_path / eps.AUDIT_NAME
    led = TrialLedger(tmp_path / "ledger.jsonl")
    res = epo.optimize_entry(plans, quad, cbp, at_ms=1, ledger=led,
                             active_path=ap, audit_path=au)
    return res, ap


def test_optimizer_coverage_promotes_D_end_to_end(tmp_path):
    res, ap = _optimize(_flat_series(), 30, tmp_path)
    assert res["n_promoted"] >= 1, "全域池 D 應走涵蓋率路徑晉升"
    # 任一 symbol/regime 經 ladder 解析到 D（至少由全域池覆蓋）
    assert eps.resolve_entry_policy("BTC", "price_up_oi_up", active_path=ap) == "limit_convert"
    assert eps.resolve_entry_policy("ETH", "price_down_oi_down", active_path=ap) == "limit_convert"


def test_optimizer_crash_no_activation(tmp_path):
    res, ap = _optimize(_crash_after_conv(), 30, tmp_path)
    assert res["n_promoted"] == 0, "崩盤樣本 D EV 劣 → 不得晉升"
    assert eps.resolve_entry_policy("BTC", "price_up_oi_up", active_path=ap) is None


def test_optimizer_small_sample_no_activation(tmp_path):
    res, ap = _optimize(_flat_series(), COV_PROMOTE_MIN_N - 1, tmp_path)
    assert res["n_promoted"] == 0, "樣本 <30 → fail-closed"
    assert eps.resolve_entry_policy("BTC", "price_up_oi_up", active_path=ap) is None
