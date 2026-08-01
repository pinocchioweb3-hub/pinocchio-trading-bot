"""v202（監督員 r97）── 進場快照「讀不出來」不得折成「本來就沒有」。

同物種第 22 次「把讀失敗講成不存在」。這一次的落點特別要緊，因為折疊發生在
**會改模擬盤參數的那條路**上：

  舊碼四處各自 `json.loads(raw or "") or {}` + except → {}，於是
    (a)「這筆單本來就沒有快照」（#47 之前的舊單，合法、可預期）與
    (b)「快照在，但解不開／型別不是 dict」（＝讀失敗，當時 regime 其實未知）
  一起變成 quadrant='unknown'。而 unknown 不是垃圾桶——它會被拿去分桶、
  湊 L2 的 minTRL≥30 樣本數、進覆寫表晉升判定。把「讀壞的」混進去＝拿不知道
  當已知去背書參數變更（違反「不為湊樣本改策略」）。

  更硬的一條：(b) 之中「合法 JSON 但不是 dict」（例如 '[1,2,3]'）在舊碼會直接
  對 list 呼叫 .get() 炸 AttributeError。lessons_store 早在註解裡擋過這件事，
  兩支優化器沒擋 ⇒ 一筆壞列就讓整輪 run_optimization 掛掉，再被 auto_tuner 的
  每段 try/except 吞成一行 stderr（無 Telegram），當晚優化器整段不跑。

本檔每一條都先在**改動前**的碼上驗證會紅（非虛設檢定）。
"""
import json
import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from l3_dispatcher import auto_optimizer as ao
from l3_dispatcher import entry_policy_optimizer as epo
from l3_dispatcher import lessons_store as ls
from l3_dispatcher.plan_snapshot import (SNAP_MISSING, SNAP_OK, SNAP_UNREADABLE,
                                         read_plan_snapshot)

_GOOD = json.dumps({"regime_at_entry": {"oi_price_quadrant": "price_up_oi_up"},
                    "context_at_entry": {"avg_funding": 0.01},
                    "missing_context_keys": ["whale_net"], "expected_r": 1.0})
_TRUNCATED = '{"regime_at_entry": {"oi_pri'      # 壞檔：解不開
_NON_DICT = "[1,2,3]"                            # 合法 JSON 但型別不對


# ════════════════════════════════════════════════════════════════════
#  canonical reader 三態
# ════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("raw,expect_status", [
    (_GOOD, SNAP_OK),
    ({"regime_at_entry": {}}, SNAP_OK),      # 已是 dict（記帳路徑直接傳物件）
    (None, SNAP_MISSING),                    # NULL 欄位＝合法舊單
    ("", SNAP_MISSING),
    ("   ", SNAP_MISSING),
    (_TRUNCATED, SNAP_UNREADABLE),
    (_NON_DICT, SNAP_UNREADABLE),
    ("null", SNAP_UNREADABLE),               # 合法 JSON 的 None 也不是 dict
    ('"a string"', SNAP_UNREADABLE),
    (12345, SNAP_UNREADABLE),                # 欄位被寫成非字串＝寫壞了，不是「沒有」
])
def test_reader_three_states(raw, expect_status):
    _snap, status = read_plan_snapshot(raw)
    assert status == expect_status


def test_reader_missing_and_unreadable_are_distinguishable():
    """核心不變式：這兩者永遠不可回同一個狀態。"""
    assert read_plan_snapshot(None)[1] != read_plan_snapshot(_TRUNCATED)[1]
    assert read_plan_snapshot("")[1] != read_plan_snapshot(_NON_DICT)[1]


# ════════════════════════════════════════════════════════════════════
#  lessons_store：壞檔不得宣稱「什麼數據都沒缺」
# ════════════════════════════════════════════════════════════════════
def _row(tid, snap):
    return {"id": tid, "symbol": "BTC-USDT-SWAP", "setup": "deepdive",
            "direction": "long", "realized_r": -1.0, "exit_reason": "stop_loss",
            "entry_at": 1785000000000, "exit_at": 1785000900000, "regime": None,
            "plan_snapshot": snap}


@pytest.mark.parametrize("bad", [_TRUNCATED, _NON_DICT])
def test_distill_unreadable_does_not_claim_nothing_was_missing(bad):
    """⛔ missing_context_keys=[] 是在正面宣稱『當初什麼數據都沒缺』；
    讀不出來時必須是 None（誠實的不知道）——第③問正是靠這欄算的。"""
    card = ls.distill(_row(1, bad))
    assert card["missing_context_keys"] is None
    assert card["snapshot_status"] == SNAP_UNREADABLE


@pytest.mark.parametrize("bad", [_TRUNCATED, _NON_DICT])
def test_distill_unreadable_gets_its_own_quadrant_not_unknown(bad):
    """壞檔不可混進 unknown 桶（那是『本來就沒快照』的合法舊單）。"""
    card = ls.distill(_row(1, bad))
    assert card["quadrant"] == ls.QUAD_UNREADABLE
    assert card["quadrant"] != "unknown"


def test_distill_missing_snapshot_still_unknown_and_empty_list():
    """回歸護欄：合法舊單（無快照）行為不變——仍是 unknown + []。"""
    card = ls.distill(_row(2, None))
    assert card["quadrant"] == "unknown"
    assert card["missing_context_keys"] == []
    assert card["snapshot_status"] == SNAP_MISSING


def test_distill_good_snapshot_unchanged():
    card = ls.distill(_row(3, _GOOD))
    assert card["quadrant"] == "price_up_oi_up"
    assert card["missing_context_keys"] == ["whale_net"]
    assert card["snapshot_status"] == SNAP_OK


def test_summary_surfaces_unreadable_count():
    """讀不出來的筆數要能被看見，不是靜靜躺在某個桶裡。"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "lessons.jsonl"
        cards = [ls.distill(_row(1, _GOOD)), ls.distill(_row(2, _TRUNCATED)),
                 ls.distill(_row(3, None))]
        p.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in cards),
                     encoding="utf-8")
        s = ls.summarize_by_quadrant(p)
        assert s["n_unreadable_snapshot"] == 1
        assert ls.QUAD_UNREADABLE in s["by_quadrant"]
        assert s["by_quadrant"]["unknown"]["n"] == 1      # 壞檔沒被算進 unknown
        assert "讀不出來" in ls.render_summary(p)


# ════════════════════════════════════════════════════════════════════
#  兩支優化器：壞列不得墊高樣本、也不得炸掉整輪
# ════════════════════════════════════════════════════════════════════
def _opt_row(tid, snap, *, q_r=0.5):
    return {"id": tid, "symbol": "BTC-USDT-SWAP", "setup": "intraday",
            "direction": "bull", "entry_price": 100.0, "stop_price": 90.0,
            "tp1": 110.0, "tp2": 120.0, "tp3": 140.0, "entry_at": 0, "exit_at": 10,
            "legs_hit": "tp1,tp2,stop", "exit_reason": "stop", "realized_r": q_r,
            "pnl_usd": q_r * 100, "entry_filled_pct": 1.0,
            "plan_snapshot": snap, "tp_alloc": None}


@pytest.mark.parametrize("mod", [ao, epo])
@pytest.mark.parametrize("bad", [_TRUNCATED, _NON_DICT])
def test_quadrant_of_returns_none_for_unreadable(mod, bad):
    """⛔ 不可回 'unknown'：呼叫端要靠 None 才知道該把這筆排除。"""
    assert mod._quadrant_of({"plan_snapshot": bad}) is None


@pytest.mark.parametrize("mod", [ao, epo])
def test_quadrant_of_missing_still_unknown(mod):
    """回歸護欄：本來就沒快照的舊單仍歸 unknown（行為不變）。"""
    assert mod._quadrant_of({"plan_snapshot": None}) == "unknown"


def test_entry_policy_plan_prices_refuses_to_fall_back_on_unreadable():
    """快照讀不出來時退回欄位＝用未凍結的值冒充凍結計畫重放（v114 的忠實度問題再犯）。"""
    row = _opt_row(1, _TRUNCATED)
    assert epo._plan_prices(row) is None
    # 對照：本來就沒快照的舊單仍可合法退回欄位
    assert epo._plan_prices(_opt_row(2, None)) is not None


def test_run_optimization_survives_non_dict_snapshot_and_counts_it():
    """一筆壞列不得炸掉整輪（舊碼：AttributeError: 'list' object has no attribute 'get'），
    且必須被計數排除、不得混進 unknown 桶去墊高 minTRL 樣本。"""
    with tempfile.TemporaryDirectory() as td:
        from backtest.l2_stat_gates import TrialLedger
        rows = [_opt_row(i, _GOOD) for i in range(3)]
        rows += [_opt_row(98, _NON_DICT), _opt_row(99, _TRUNCATED)]
        res = ao.run_optimization(
            rows=rows, at_ms=1785600000000,
            ledger=TrialLedger(Path(td) / "ledger.jsonl"),
            active_path=Path(td) / "active.json",
            audit_path=Path(td) / "audit.jsonl")
        assert res["n_excluded_unreadable_snapshot"] == 2
        assert "unknown" not in {b["quadrant"] for b in res["buckets"]}


def test_report_speaks_up_when_everything_was_unreadable():
    """全部讀不出來 ⇒ 0 桶。⛔ 不可回 None（靜音）——那長得跟『今天沒單』一模一樣。"""
    with tempfile.TemporaryDirectory() as td:
        from backtest.l2_stat_gates import TrialLedger
        res = ao.run_optimization(
            rows=[_opt_row(1, _TRUNCATED), _opt_row(2, _NON_DICT)],
            at_ms=1785600000000,
            ledger=TrialLedger(Path(td) / "ledger.jsonl"),
            active_path=Path(td) / "active.json",
            audit_path=Path(td) / "audit.jsonl")
        assert res["n_buckets"] == 0
        rep = ao.render_report(res, active_path=Path(td) / "active.json")
        assert rep is not None
        assert "讀不出來" in rep


def test_report_stays_quiet_when_genuinely_no_data():
    """反向護欄：真的沒樣本時維持原本的安靜（不製造新噪音）。"""
    with tempfile.TemporaryDirectory() as td:
        from backtest.l2_stat_gates import TrialLedger
        res = ao.run_optimization(
            rows=[], at_ms=1785600000000,
            ledger=TrialLedger(Path(td) / "ledger.jsonl"),
            active_path=Path(td) / "active.json",
            audit_path=Path(td) / "audit.jsonl")
        assert ao.render_report(res, active_path=Path(td) / "active.json") is None
