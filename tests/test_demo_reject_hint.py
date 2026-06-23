# -*- coding: utf-8 -*-
"""task#12 治本回歸（監督員 r52）：demo 拒單摘要不得被『最後一筆』綁架，須報最常見拒因；
且 51004 文案須為『槓桿層級持倉上限』非『保證金』。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from l3_dispatcher import demo_journal as dj


def _seed(monkeypatch, tmp_path, rows):
    """rows = list of (intent_id, symbol, exit_reason)。寫進 tmp demo_trades(status=rejected)。
    用 monkeypatch 設 DB_PATH → 測試後自動還原，不汙染其他用 demo_journal 的測試。"""
    monkeypatch.setattr(dj, "DB_PATH", str(tmp_path / "tj.db"))
    dj.init_db()
    conn = dj._conn()
    try:
        for i, (iid, sym, er) in enumerate(rows):
            conn.execute(
                "INSERT INTO demo_trades(intent_id,symbol,direction,entry_price,stop_price,"
                "risk_usd,status,exit_reason,entry_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (iid, sym, "bull", 100.0, 90.0, 100.0, "rejected", er, 1000 + i, 1000 + i))  # entry_at 遞增＝最後一筆在最後
    finally:
        conn.close()


def test_51004_hint_is_position_tier_not_margin():
    h = dj._short_reject_hint('reject:entry failed: okx {"sCode":"51004","sMsg":"max position"}')
    assert "持倉上限" in h                       # 正名為槓桿層級持倉上限
    assert h != "OKX 51004：下單超過可用保證金"   # 不再是舊的誤導文案


def test_count_rejected_reports_dominant_not_last(monkeypatch, tmp_path):
    # 16 筆 not_on_okx（最常見）+ 最後一筆 51004（最新）。舊 bug 會回 51004；治本應回 not_on_okx。
    rows = [(f"i{i}", "FOO", "reject:not_on_okx") for i in range(16)]
    rows.append(("ena", "ENA", 'reject:entry failed: okx {"sCode":"51004","sMsg":"max position"}'))
    _seed(monkeypatch, tmp_path, rows)
    cnt, hint = dj.count_rejected()
    assert cnt == 17
    assert "not_on_okx" in hint, f"應報最常見(not_on_okx)，實得 {hint!r}"
    assert "51004" not in hint, f"不得被最後一筆 51004 綁架，實得 {hint!r}"
    assert "16/17" in hint                        # 帶佔比


def test_count_rejected_single_reason_no_ratio(monkeypatch, tmp_path):
    rows = [(f"j{i}", "BAR", "reject:not_on_okx") for i in range(3)]
    _seed(monkeypatch, tmp_path, rows)
    cnt, hint = dj.count_rejected()
    assert cnt == 3 and "not_on_okx" in hint and "/" not in hint   # 全同一因→不附佔比


def test_count_rejected_zero(monkeypatch, tmp_path):
    monkeypatch.setattr(dj, "DB_PATH", str(tmp_path / "empty.db"))
    dj.init_db()
    assert dj.count_rejected() == (0, None)
