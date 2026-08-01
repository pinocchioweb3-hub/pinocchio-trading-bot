"""復盤引擎 step7（task#52）── 教訓庫 lessons_store 測試。

覆蓋：learning invariant 全分支（僥倖單不進正向集＝紅線③延伸）、distill（含/不含
plan_snapshot）、檔案往返（rebuild→query→learning_set→summarize）、彙總誠實樣本不足橫幅。
全離線、零網路、零真 DB（合成 row + 暫存檔）。
"""
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from l3_dispatcher import lessons_store as ls


def test_module_selftest_passes():
    assert ls._selftest() is True


# ── learning invariant 全分支 ──────────────────────────────────────────
def test_invariant_plan_worked_aligned_is_positive():
    o, lucky, pos, neg = ls.apply_learning_invariant(1.5, "plan_worked", True)
    assert o == "win" and pos is True and lucky is False and neg is False


def test_invariant_plan_worked_htf_unknown_still_positive():
    # htf_aligned None（未知）不懲罰 → 仍可進正向集
    o, lucky, pos, neg = ls.apply_learning_invariant(1.5, "plan_worked", None)
    assert pos is True and lucky is False


def test_invariant_plan_worked_but_counter_trend_is_lucky():
    # 照計畫吃到 TP 但明確逆 HTF 大勢 → 僥倖，排除正向集
    o, lucky, pos, neg = ls.apply_learning_invariant(1.5, "plan_worked", False)
    assert lucky is True and pos is False


def test_invariant_win_not_by_plan_is_lucky():
    # 賺錢但非照計畫機制（逾時飄出去賺到）→ 僥倖
    o, lucky, pos, neg = ls.apply_learning_invariant(0.8, "inconclusive", True)
    assert o == "win" and lucky is True and pos is False


def test_invariant_loss_is_negative_evidence_only():
    o, lucky, pos, neg = ls.apply_learning_invariant(-1.0, "stop_as_planned", True)
    assert o == "loss" and neg is True and pos is False and lucky is False


def test_invariant_scratch_is_neither():
    o, lucky, pos, neg = ls.apply_learning_invariant(0.0, "inconclusive", True)
    assert o == "scratch" and not pos and not lucky and not neg


# ── distill ────────────────────────────────────────────────────────────
def _row(**over):
    snap = {"regime_at_entry": {"oi_price_quadrant": "price_up_oi_up",
                                "funding_state": "hot", "cvd_state": "up",
                                "vol_trend": "expanding"},
            "context_at_entry": {"htf_aligned": True},
            "expected_r": 1.0, "missing_context_keys": ["whale_net"]}
    base = {"id": 1, "symbol": "BTC", "setup": "intraday", "direction": "bull",
            "realized_r": 1.8, "exit_reason": "tp2", "entry_at": 0, "exit_at": 5,
            "regime": None, "plan_snapshot": json.dumps(snap)}
    base.update(over)
    return base


def test_distill_quadrant_and_positive():
    d = ls.distill(_row())
    assert d["quadrant"] == "price_up_oi_up"
    assert d["counts_as_positive_evidence"] is True and d["lucky"] is False
    assert d["has_snapshot"] is True


def test_distill_delta_r_and_missing_keys():
    d = ls.distill(_row())
    assert d["delta_r"] == round(1.8 - 1.0, 4)
    assert d["missing_context_keys"] == ["whale_net"]


def test_distill_old_trade_no_snapshot_is_unknown_quadrant():
    d = ls.distill(_row(id=2, plan_snapshot=None, realized_r=1.2, exit_reason="tp1"))
    assert d["quadrant"] == "unknown"
    assert d["has_snapshot"] is False
    assert d["expected_r"] is None and d["delta_r"] is None


def test_distill_counter_trend_win_marked_lucky():
    snap = {"regime_at_entry": {"oi_price_quadrant": "price_down_oi_up"},
            "context_at_entry": {"htf_aligned": False}, "expected_r": 1.0}
    d = ls.distill(_row(plan_snapshot=json.dumps(snap), realized_r=1.5, exit_reason="tp1"))
    assert d["lucky"] is True and d["counts_as_positive_evidence"] is False


# ── 檔案往返：query / learning_set / summarize ──────────────────────────
def test_file_roundtrip_query_learning_set_and_summary():
    win = ls.distill(_row(id=10))                                   # 正向
    lucky = ls.distill(_row(id=11, realized_r=0.9, exit_reason="timeout"))  # 僥倖
    loss = ls.distill(_row(id=12, realized_r=-1.0, exit_reason="stop"))     # 賠
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "lessons.jsonl"
        with open(p, "w", encoding="utf-8") as f:
            for d in (win, lucky, loss):
                f.write(json.dumps(d, ensure_ascii=False) + "\n")

        assert len(ls.query_lessons(quadrant="price_up_oi_up", path=p)) == 3
        assert len(ls.query_lessons(quadrant="no_such", path=p)) == 0
        assert len(ls.query_lessons(positive_only=True, path=p)) == 1
        assert len(ls.learning_set(p)) == 1  # 僥倖+賠被排除

        summ = ls.summarize_by_quadrant(p)
        b = summ["by_quadrant"]["price_up_oi_up"]
        assert b["n"] == 3 and b["n_positive"] == 1
        assert b["n_lucky"] == 1 and b["n_loss"] == 1
        assert b["sample_sufficient"] is False  # 3 < 30 → 誠實標樣本不足


def test_summary_sample_sufficient_threshold():
    # 湊滿 MIN_BUCKET_HONEST 筆正向 → sample_sufficient True
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "lessons.jsonl"
        with open(p, "w", encoding="utf-8") as f:
            for i in range(ls.MIN_BUCKET_HONEST):
                f.write(json.dumps(ls.distill(_row(id=200 + i)),
                                   ensure_ascii=False) + "\n")
        b = ls.summarize_by_quadrant(p)["by_quadrant"]["price_up_oi_up"]
        assert b["n"] == ls.MIN_BUCKET_HONEST
        assert b["sample_sufficient"] is True


def test_query_empty_file_returns_empty():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "nope.jsonl"   # 不存在
        assert ls.query_lessons(path=p) == []
        assert ls.learning_set(p) == []
        assert ls.summarize_by_quadrant(p)["total"] == 0


# ── task#57：壞快照韌性（合法 JSON 但非 dict / 截斷 JSON 不得炸） ──────────
def test_parse_snap_rejects_non_dict():
    # 合法 JSON 但型別不對 → 一律降級為 None（distill 才不會對 list/int 呼叫 .get()）
    assert ls._parse_snap("[]") is None
    assert ls._parse_snap("42") is None
    assert ls._parse_snap('"x"') is None
    assert ls._parse_snap("null") is None
    # 截斷／非法 JSON → None
    assert ls._parse_snap("{not json") is None
    assert ls._parse_snap("") is None
    # 正常 dict → 原樣回傳
    assert ls._parse_snap('{"a": 1}') == {"a": 1}


def test_distill_non_dict_snapshot_degrades_without_crashing():
    # 限價單若某天寫進結構異常的 plan_snapshot（list/int/str），distill 不得拋例外，
    # 且無快照可用（has_snapshot=False、expected_r/delta_r=None）。
    # v202 校正：⛔ 不再降級成 quadrant='unknown'——「讀不出來」與「本來就沒快照」
    # 不是同一件事，混在一起會讓壞列去墊高 minTRL≥30 的樣本數。改歸自己的桶。
    for bad in ("[]", "42", '"oops"', "{truncated"):
        d = ls.distill(_row(id=99, plan_snapshot=bad, realized_r=1.2, exit_reason="tp1"))
        assert d["quadrant"] == ls.QUAD_UNREADABLE
        assert d["quadrant"] != "unknown"
        assert d["snapshot_status"] == "unreadable"
        assert d["has_snapshot"] is False
        assert d["expected_r"] is None and d["delta_r"] is None


def test_build_lessons_skips_bad_row_keeps_good(monkeypatch, capsys):
    # 一筆會讓 distill 拋例外的毒列（realized_r 非數值）不得拖垮整檔重建——
    # build_lessons 須跳過該列、告警 stderr，並保住其餘好列（韌性 > 完整）。
    good = _row(id=1)
    bad = _row(id=2, realized_r="not-a-number")     # float() 會在 distill 內炸
    monkeypatch.setattr(ls, "_load_rows", lambda days=365: [good, bad])
    out = ls.build_lessons()
    assert len(out) == 1 and out[0]["paper_id"] == 1
    err = capsys.readouterr().err
    assert "跳過壞列" in err and "id=2" in err
