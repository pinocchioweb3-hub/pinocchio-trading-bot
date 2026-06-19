"""L2 統計嚴謹度閘門測試 — v56 / 復盤引擎 step5（task#50）。

驗證離線唯讀統計閘門的四關 + ledger 完整性 + 防作弊性質：
  1. BHY-FDR：手算小例對照 + 單調性（族群越大越難存活）。
  2. PBO/CSCV：純雜訊 PBO 偏高、真 edge PBO 偏低；無法計算時 fail-closed。
  3. minTRL：MIN_BUCKET_N=30 fail-closed。
  4. DSR：n_trials 越大、跨試驗 SR 變異越大 → 門檻越高（越難過）。
  5. ledger：append-only 鏈式雜湊、verify_chain、竄改偵測。
  6. n_trials 從 ledger 累計、禁手傳（重複評估 → 分母自動 +1）。
  7. file-drawer：失敗試驗也記、也算進 n_trials 與 FDR 族群。
  8. out-of-time holdout：只取凍結後成交單（防 HARKing）。
  9. 門檻只能調嚴（assert_thresholds_only_stricter）。
 10. evaluate_candidate 端到端：強 edge 過、弱 edge 擋。
 11. CI 護欄：本模組不 import 任何 daemon／網路／交易模組（純離線）。

執行：pytest tests/test_l2_stat_gates.py  或  python tests/test_l2_stat_gates.py
"""
from __future__ import annotations

import ast
import json
import os
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 資料目錄指到臨時區，須在 import 前設好（default_ledger_path 會用到）
_TMP = Path(tempfile.mkdtemp(prefix="l2gates_test_"))
os.environ["BOT_DATA_DIR"] = str(_TMP)

import backtest.l2_stat_gates as g  # noqa: E402


def _ledger(tmp: Path) -> g.TrialLedger:
    return g.TrialLedger(tmp / "trial_ledger.jsonl")


# --- 1. BHY-FDR ---
def test_bhy_fdr_extremes():
    rej = g.bhy_fdr_rejected([0.001, 0.2, 0.5, 0.7, 0.9], 0.05)
    assert 0 in rej          # 極小 p 存活
    assert 4 not in rej      # 極大 p 被刷


def test_bhy_fdr_empty():
    assert g.bhy_fdr_rejected([], 0.05) == set()


def test_bhy_fdr_all_tiny_survive():
    # 全部極顯著 → 全存活
    rej = g.bhy_fdr_rejected([1e-6, 2e-6, 3e-6, 4e-6], 0.05)
    assert rej == {0, 1, 2, 3}


def test_bhy_survives_harder_with_bigger_family():
    # 同一候選 p，族群越大（多重檢定懲罰越重）越難存活。
    # 注意 BHY-Yekutieli 的 c(m) 修正偏保守：m=3 時最嚴門檻已 ≈0.0091。
    cand = 0.005
    small = g.bhy_survives(cand, [0.5, 0.6], 0.05)        # m=3：0.005≤0.0091 → 存活
    big = g.bhy_survives(cand, [0.5] * 200, 0.05)         # m=201：門檻 ≈4e-5 → 被刷
    assert small and not big


def test_bhy_known_threshold():
    # m=2、q=0.05、c(2)=1.5 → 最嚴門檻 p(1)≤1/(2·1.5)·0.05=0.01666…
    assert g.bhy_fdr_rejected([0.01, 0.9], 0.05) == {0}      # 0.01≤0.0167 存活
    assert g.bhy_fdr_rejected([0.02, 0.9], 0.05) == set()    # 0.02>0.0167 被刷


# --- 2. PBO / CSCV ---
def test_pbo_pure_noise_high():
    rng = random.Random(7)
    m = [[rng.gauss(0, 1) for _ in range(8)] for _ in range(240)]
    pbo, n_combos, n_cfg, S = g.cscv_pbo(m)
    assert pbo is not None
    assert n_cfg == 8 and S == 16 and n_combos == 12870
    assert pbo >= 0.30          # 純雜訊：選出的 IS 冠軍在 OOS 多半泛化失敗


def test_pbo_genuine_edge_low():
    rng = random.Random(7)
    m = [[rng.gauss(0, 1) for _ in range(7)] + [rng.gauss(0.5, 1)] for _ in range(240)]
    pbo, *_ = g.cscv_pbo(m)
    noise = [[rng.gauss(0, 1) for _ in range(8)] for _ in range(240)]
    pbo_n, *_ = g.cscv_pbo(noise)
    assert pbo is not None and pbo <= pbo_n


def test_pbo_not_computable():
    assert g.cscv_pbo([[0.1]] * 100)[0] is None          # 單配置
    assert g.cscv_pbo([])[0] is None                     # 空
    assert g.cscv_pbo([[0.1, 0.2]])[0] is None           # T 太短


def test_gate_pbo_fail_closed():
    assert not g.gate_pbo(None).passed
    assert not g.gate_pbo([[0.1]] * 50).passed           # 配置<2 → fail-closed


# --- 3. minTRL fail-closed ---
def test_min_trl_fail_closed_small_n():
    res = g.gate_min_trl([0.2] * 10)
    assert not res.passed
    assert "fail-closed" in res.detail


def test_min_trl_pass_with_strong_edge():
    rng = random.Random(1)
    strong = [rng.gauss(0.5, 1.0) for _ in range(200)]
    assert g.gate_min_trl(strong).passed


def test_min_trl_fail_no_edge():
    rng = random.Random(1)
    flat = [rng.gauss(0.0, 1.0) for _ in range(200)]
    assert not g.gate_min_trl(flat).passed       # SR≈0 → minTRL=∞


# --- 4. DSR 通膨 ---
def test_dsr_deflates_with_more_trials():
    rng = random.Random(2)
    r = [rng.gauss(0.25, 1.0) for _ in range(200)]
    d1 = g.gate_dsr(r, 1, None).stat
    d50 = g.gate_dsr(r, 50, 0.02).stat
    assert d50 <= d1                              # 試驗越多 → DSR 越被壓低


# --- 5. ledger 鏈 ---
def test_ledger_append_and_verify(tmp_path):
    led = _ledger(tmp_path)
    ok, _ = led.verify_chain()
    assert ok                                    # 空 ledger 視為完整
    led.append_register(bucket_key="BTC|bull", hypothesis="h", registered_at_ms=1, ts_ms=1)
    led.append_evaluation(bucket_key="BTC|bull", trial_id="t1", hypothesis="h",
                          returns=[0.1, 0.2], sharpe_val=0.3, p_value=0.04,
                          passed=True, ts_ms=2)
    ok, detail = led.verify_chain()
    assert ok, detail
    entries = led.load()
    assert entries[0]["seq"] == 0 and entries[1]["seq"] == 1
    assert entries[1]["prev_hash"] == entries[0]["hash"]


def test_ledger_tamper_detected(tmp_path):
    led = _ledger(tmp_path)
    led.append_evaluation(bucket_key="B", trial_id="t1", hypothesis="h",
                          returns=[0.1], sharpe_val=0.3, p_value=0.04,
                          passed=True, ts_ms=1)
    led.append_evaluation(bucket_key="B", trial_id="t2", hypothesis="h2",
                          returns=[0.2], sharpe_val=0.4, p_value=0.03,
                          passed=True, ts_ms=2)
    p = tmp_path / "trial_ledger.jsonl"
    lines = p.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0]); rec["sharpe"] = 9.99      # 竄改第一行
    lines[0] = json.dumps(rec, ensure_ascii=False)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, _ = led.verify_chain()
    assert not ok


def test_ledger_digest_changes_with_returns():
    a = g._returns_digest([0.1, 0.2, 0.3])
    b = g._returns_digest([0.1, 0.2, 0.4])
    assert a != b and len(a) == 64


# --- 6/7. n_trials 從 ledger 累計、file-drawer ---
def test_n_trials_accumulated_not_handpassed(tmp_path):
    led = _ledger(tmp_path)
    rng = random.Random(3)
    cand = [rng.gauss(0.3, 1.0) for _ in range(120)]
    mat = [[rng.gauss(0, 1) for _ in range(7)] + [rng.gauss(0.5, 1)] for _ in range(200)]
    v1 = led_eval(led, "BTC|bull", cand, mat, "h1", 1)
    v2 = led_eval(led, "BTC|bull", cand, mat, "h2", 2)
    v3 = led_eval(led, "BTC|bull", cand, mat, "h3", 3)
    assert (v1.n_trials, v2.n_trials, v3.n_trials) == (1, 2, 3)


def test_file_drawer_failed_trials_count(tmp_path):
    led = _ledger(tmp_path)
    rng = random.Random(4)
    bad = [rng.gauss(-0.2, 1.0) for _ in range(40)]
    noise = [[rng.gauss(0, 1) for _ in range(8)] for _ in range(200)]
    v1 = led_eval(led, "ETH|bear", bad, noise, "bad1", 1)
    assert not v1.passed                          # 失敗
    v2 = led_eval(led, "ETH|bear", bad, noise, "bad2", 2)
    assert v2.n_trials == 2                        # 失敗的也算進分母


def test_distinct_trials_dedupe_same_id(tmp_path):
    led = _ledger(tmp_path)
    led.append_evaluation(bucket_key="B", trial_id="same", hypothesis="h",
                          returns=[0.1], sharpe_val=0.3, p_value=0.04,
                          passed=True, ts_ms=1)
    led.append_evaluation(bucket_key="B", trial_id="same", hypothesis="h",
                          returns=[0.2], sharpe_val=0.5, p_value=0.02,
                          passed=True, ts_ms=2)
    assert led.count_trials("B") == 1             # 同 trial_id 重估不重複計入族群
    assert led.distinct_trials("B")["same"]["sharpe"] == 0.5   # 取最新


def test_bucket_isolation(tmp_path):
    led = _ledger(tmp_path)
    led.append_evaluation(bucket_key="BTC", trial_id="a", hypothesis="h",
                          returns=[0.1], sharpe_val=0.3, p_value=0.04, passed=True, ts_ms=1)
    led.append_evaluation(bucket_key="ETH", trial_id="b", hypothesis="h",
                          returns=[0.1], sharpe_val=0.3, p_value=0.04, passed=True, ts_ms=2)
    assert led.count_trials("BTC") == 1
    assert led.count_trials("ETH") == 1
    assert led.count_trials() == 2                # 不指定 bucket = 全體


# --- 8. out-of-time holdout（防 HARKing）---
def test_out_of_time_holdout_filters_pre_freeze():
    trades = [{"exit_ts_ms": 100, "realized_r": 1.0},
              {"exit_ts_ms": 200, "realized_r": 2.0},
              {"exit_ts_ms": 50, "realized_r": -9.0}]
    assert g.out_of_time_holdout(trades, 150) == [2.0]


def test_out_of_time_holdout_missing_fields_excluded():
    trades = [{"exit_ts_ms": 200},                       # 缺 R
              {"realized_r": 2.0},                        # 缺 ts
              {"exit_ts_ms": 200, "realized_r": 3.0}]
    assert g.out_of_time_holdout(trades, 150) == [3.0]


def test_register_hypothesis_in_chain(tmp_path):
    led = _ledger(tmp_path)
    tid = g.register_hypothesis(led, bucket_key="BTC|bull",
                                hypothesis="槓桿 5→8x", registered_at_ms=1000, ts_ms=1)
    assert len(tid) == 16
    e = led.load()[0]
    assert e["kind"] == "register" and e["registered_at_ms"] == 1000
    ok, _ = led.verify_chain()
    assert ok                                            # 凍結時刻進鏈、不可竄改


# --- 9. 門檻只能調嚴 ---
def test_thresholds_only_stricter_passes_as_shipped():
    g.assert_thresholds_only_stricter()                  # 出廠值即基準 → 必過


def test_thresholds_loosened_raises(monkeypatch):
    monkeypatch.setattr(g, "DSR_MIN", 0.90)              # 放寬 → 應 raise
    try:
        g.assert_thresholds_only_stricter()
        assert False, "放寬門檻應 raise"
    except AssertionError as e:
        assert "只能調嚴" in str(e)


# --- 10. evaluate_candidate 端到端 ---
def test_evaluate_strong_edge_passes(tmp_path):
    led = _ledger(tmp_path)
    rng = random.Random(11)
    cand = [rng.gauss(0.4, 1.0) for _ in range(150)]
    mat = [[rng.gauss(0, 1) for _ in range(7)] + [rng.gauss(0.5, 1)] for _ in range(200)]
    v = g.evaluate_candidate(led, bucket_key="BTC|bull", candidate_returns=cand,
                             matrix=mat, hypothesis="strong", ts_ms=1)
    assert all(g_.passed for g_ in v.gates), v.summary
    assert v.passed


def test_evaluate_weak_edge_blocked(tmp_path):
    led = _ledger(tmp_path)
    rng = random.Random(12)
    cand = [rng.gauss(0.02, 1.0) for _ in range(40)]     # 幾乎無 edge
    noise = [[rng.gauss(0, 1) for _ in range(8)] for _ in range(200)]
    v = g.evaluate_candidate(led, bucket_key="BTC|bull", candidate_returns=cand,
                             matrix=noise, hypothesis="weak", ts_ms=1)
    assert not v.passed


def test_evaluate_no_matrix_fail_closed(tmp_path):
    led = _ledger(tmp_path)
    rng = random.Random(13)
    cand = [rng.gauss(0.5, 1.0) for _ in range(150)]
    v = g.evaluate_candidate(led, bucket_key="X", candidate_returns=cand,
                             matrix=None, hypothesis="nomat", ts_ms=1)
    assert not v.passed                                  # 強 edge 但無矩陣 → PBO fail-closed
    pbo_gate = [x for x in v.gates if x.name == "PBO/CSCV"][0]
    assert not pbo_gate.passed


def test_render_verdict_runs(tmp_path):
    led = _ledger(tmp_path)
    rng = random.Random(14)
    cand = [rng.gauss(0.4, 1.0) for _ in range(150)]
    mat = [[rng.gauss(0, 1) for _ in range(7)] + [rng.gauss(0.5, 1)] for _ in range(200)]
    v = g.evaluate_candidate(led, bucket_key="BTC|bull", candidate_returns=cand,
                             matrix=mat, hypothesis="r", ts_ms=1)
    txt = g.render_verdict(v)
    assert "L2 統計嚴謹度閘門" in txt and "總判定" in txt


# --- 11. CI 護欄：純離線、不碰 daemon/網路/交易 ---
def test_no_daemon_or_network_imports():
    src = (ROOT / "backtest" / "l2_stat_gates.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    forbidden = ("aiohttp", "requests", "httpx", "websocket", "telegram",
                 "ccxt", "okx", "asyncio")
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in forbidden:
                    bad.append(a.name)
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in forbidden or root == "l3_dispatcher":
                bad.append(node.module)
    assert not bad, f"L2 閘門應純離線，不該 import：{bad}"


# helper（n_trials 累計測試用）
def led_eval(led, bucket, cand, mat, hyp, ts):
    return g.evaluate_candidate(led, bucket_key=bucket, candidate_returns=cand,
                                matrix=mat, hypothesis=hyp, ts_ms=ts)


if __name__ == "__main__":
    # 無 pytest 時的精簡跑法（tmp_path fixture 測試用臨時資料夾替代）
    import inspect
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    passed = skipped = 0
    for name, fn in fns:
        params = inspect.signature(fn).parameters
        kwargs = {}
        if "tmp_path" in params:
            kwargs["tmp_path"] = Path(tempfile.mkdtemp(prefix="l2t_"))
        if "monkeypatch" in params:
            skipped += 1
            print(f"  SKIP {name}（需 pytest monkeypatch）")
            continue
        fn(**kwargs)
        passed += 1
        print(f"  ok  {name}")
    print(f"\n{passed} passed, {skipped} skipped（monkeypatch 類需 pytest）")
