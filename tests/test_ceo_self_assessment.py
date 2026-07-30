# -*- coding: utf-8 -*-
"""task#7：CEO 深度綜合『系統自評』跨 session 瓶頸歸因（純函式 + 整合）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from l3_dispatcher.ceo_session import _synthesize_bottleneck, build_ceo_brief


def test_too_few_samples_is_honest_not_fabricated():
    out = _synthesize_bottleneck(3, 100, 0, 30, 0, 0)
    assert "尚無足夠基礎" in out and "不臆測" in out


def test_sample_supply_is_bottleneck_not_strategy():
    out = _synthesize_bottleneck(69, 100, 0, 30, 1, 0)
    assert "樣本供給不足" in out and "非策略失效" in out and "L2" in out


def test_high_demo_reject_rate_surfaced():
    out = _synthesize_bottleneck(69, 100, 0, 30, 1, 34)  # 34/35 拒
    assert "拒單率偏高" in out


def test_low_demo_reject_no_warning():
    out = _synthesize_bottleneck(69, 100, 0, 30, 10, 1)
    assert "拒單率偏高" not in out


def test_samples_met_changes_attribution():
    out = _synthesize_bottleneck(120, 100, 35, 30, 35, 0)
    assert "樣本達標" in out


def test_ample_paper_but_unproven_edge_not_mislabeled_as_sample_short():
    """治本 v101：紙上樣本充足(169≥100)但真錢 0/30(人工閘)＋edge 未顯著(t≈1.07)時，
    舊版會謊報『樣本供給不足』。修後須誠實歸因『edge 未達統計顯著、非樣本量』。"""
    out = _synthesize_bottleneck(169, 100, 0, 30, 20, 0, paper_t=1.07)
    assert "樣本供給不足" not in out          # 不再把『樣本充足』謊報成不足
    assert "edge 未達統計顯著" in out and "1.07" in out and "非樣本量" in out


def test_significant_edge_pending_realmoney_gate():
    """紙上足且**毛與淨都**已顯著(t≥2)、真錢未開 → 歸因到真錢人工閘(紅線①)。"""
    out = _synthesize_bottleneck(169, 100, 0, 30, 20, 0,
                                 paper_t=2.5, paper_t_net=2.2, net_n=120)
    assert "真錢" in out and "紅線①" in out and "樣本供給不足" not in out


# --------------------------------------------------- v150 毛/淨雙口徑 fail-closed
def test_gross_significant_but_no_net_evidence_is_not_ready():
    """v150 治本：毛 t≥2 但沒有扣費後證據時，舊碼會歸因『只差真錢人工閘』＝假準備就緒。
    新碼必須擋下來，說明真瓶頸是扣費後的 edge、且點名淨值證據不足。"""
    out = _synthesize_bottleneck(169, 100, 0, 30, 20, 0, paper_t=2.5)   # 無淨口徑
    assert "待真錢人工逐筆驗證" not in out          # 不得翻成『只差人工閘』
    assert "扣費後" in out and "不得視為已證實" in out


def test_gross_significant_but_net_coverage_short_is_not_ready():
    """有淨 t 但配對覆蓋 <30 筆＝證據不足，同樣不得放行（覆蓋太少時 t 再漂亮也不算）。"""
    out = _synthesize_bottleneck(169, 100, 0, 30, 20, 0,
                                 paper_t=2.5, paper_t_net=3.0, net_n=12)
    assert "待真錢人工逐筆驗證" not in out and "12<30" in out


def test_gross_significant_but_net_below_threshold_named_honestly():
    """美股當前真實形狀：毛 t≈2.64 過閘、扣費後淨 t≈1.70 跌破 → 誠實說費用吃掉 edge。"""
    out = _synthesize_bottleneck(169, 100, 0, 30, 20, 0,
                                 paper_t=2.64, paper_t_net=1.70, net_n=56)
    assert "待真錢人工逐筆驗證" not in out
    assert "1.70" in out and "費用" in out and "未證實" in out


def test_gross_not_significant_still_reports_edge_bottleneck():
    """毛就沒過時維持舊敘事（優先序不變）：瓶頸是 edge 大小、非樣本量。"""
    out = _synthesize_bottleneck(169, 100, 0, 30, 20, 0,
                                 paper_t=1.07, paper_t_net=-0.54, net_n=110)
    assert "edge 未達統計顯著" in out and "1.07" in out and "非樣本量" in out


def test_no_t_at_all_falls_back_to_legacy_attribution():
    """兩個口徑都沒給（呼叫端未提供）→ 退回舊式描述，不臆測顯著與否。"""
    out = _synthesize_bottleneck(120, 100, 35, 30, 35, 0)
    assert "樣本達標" in out and "扣費後" not in out


def test_net_tstat_uses_paired_subset(tmp_path, monkeypatch):
    """basis='net' 必須是**配對子集**（毛淨同一批交易），否則毛/淨差異分不清是費用還是換樣本。"""
    import sqlite3
    from l3_dispatcher import ceo_session as cs
    db = tmp_path / "tj.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE paper_trades (setup TEXT, status TEXT, "
                 "exit_reason TEXT, realized_r REAL, net_r REAL)")
    rows = [("deepdive", "closed", None, 1.0, 0.5), ("deepdive", "closed", None, 2.0, 1.5),
            ("deepdive", "closed", None, 3.0, None),          # 只有毛→淨口徑須排除
            ("deepdive", "closed", "entry_expired", 9.0, 9.0)]  # 餓死單→兩口徑都排除
    conn.executemany("INSERT INTO paper_trades VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    monkeypatch.setattr(cs, "_TJ_DB", str(db))
    assert cs._paper_edge_tstat_ex(setup="deepdive", basis="gross")[0] == 3
    assert cs._paper_edge_tstat_ex(setup="deepdive", basis="net")[0] == 2


def test_missing_net_column_is_fail_closed_not_zero():
    """net_r 欄不存在（如真錢 trades 表）→ 回 (0, None)＝無證據，絕不能回 0.0 當『淨值為零』。"""
    from l3_dispatcher.ceo_session import _paper_edge_tstat_ex
    assert _paper_edge_tstat_ex(table="__no_such_table__", basis="net") == (0, None)


def test_build_brief_runs_and_includes_synthesis():
    brief = build_ceo_brief()
    assert isinstance(brief, str) and "系統自評" in brief and "跨 session 綜合" in brief


# ------------------------------------------------- v120 學習迴圈健康探針（稽核rank10）
def _write_audit(tmp_path, fname, rows):
    import json as _json
    (tmp_path / fname).write_text(
        "\n".join(_json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_probe_detects_stuck_promotion(tmp_path):
    """l2_passed=True 且 promote=False（v114 型卡死）→ 必須被抓出。"""
    import time as _t
    from l3_dispatcher.ceo_session import probe_learning_loop
    now = _t.time() * 1000
    _write_audit(tmp_path, "entry_policy_audit.jsonl", [
        {"at_ms": now, "bucket": "*|unknown", "l2_passed": True, "promote": False,
         "reasons": ["self_check_blocked"], "action": "hold"},
        {"at_ms": now, "bucket": "BTC|q1", "l2_passed": False, "promote": False,
         "reasons": ["minTRL"], "action": "hold"},
    ])
    p = probe_learning_loop(data_dir_fn=lambda: tmp_path)
    assert len(p["stuck"]) == 1
    assert p["stuck"][0]["bucket"] == "*|unknown"
    assert "self_check_blocked" in p["stuck"][0]["reasons"]


def test_probe_healthy_and_windowing(tmp_path):
    """統計未過而 hold＝正常 fail-closed 不報；出窗舊列不掃。"""
    import time as _t
    from l3_dispatcher.ceo_session import probe_learning_loop, _section_learning_loop
    now = _t.time() * 1000
    _write_audit(tmp_path, "auto_params_audit.jsonl", [
        {"at_ms": now - 90 * 3600 * 1000, "bucket": "OLD|x",
         "l2_summary": "✅ 過閘", "promote": False},        # 出窗（90h 前）→ 不算
        {"at_ms": now, "bucket": "ZEC|unknown",
         "l2_summary": "❌ 未過閘（n=1）：minTRL✗", "promote": False},
    ])
    p = probe_learning_loop(data_dir_fn=lambda: tmp_path)
    assert p["stuck"] == [] and p["rounds_checked"] >= 1
