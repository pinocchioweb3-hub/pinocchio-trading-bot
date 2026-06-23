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


def test_not_on_okx_is_whitelabelled_as_system_side():
    # 治本（監督員 r53）：not_on_okx 是系統端預檢拒因，須白話化為「非帳戶設定」，
    # 不可讓本人看到生 token、更不可被誤導去調 OKX 帳戶。
    h = dj._short_reject_hint("reject:not_on_okx")
    assert "not_on_okx" in h            # 保留可辨識 token（聚合用）
    assert "非帳戶設定" in h            # 明指這非帳戶設定問題


def test_next_step_reject_advice_is_reason_aware():
    # 治本（監督員 r53）：next_step 不得對所有高拒單率都硬寫「改帳戶模式 51010」。
    from l3_dispatcher.ceo_oversight import next_step
    # not_on_okx 主導 → 應指向系統端、明說「無須調整 OKX」，不得硬塞 51010 帳戶模式建議
    s = next_step(paper_n=45, paper_min=100, live_n=0, live_min=30, demo_n=0,
                  demo_active=True, demo_rejected=5,
                  demo_reject_hint="not_on_okx：標的不在 OKX 永續可交易清單（最常見：17/37 筆）")
    assert "被 OKX 拒絕" in s and "0/30" in s
    assert "無須調整 OKX" in s
    assert "51010" not in s             # not_on_okx 主導時不得誤導去改帳戶模式
    # 51010 確實主導時 → 才給帳戶模式建議
    s2 = next_step(paper_n=45, paper_min=100, live_n=0, live_min=30, demo_n=0,
                   demo_active=True, demo_rejected=5, demo_reject_hint="51010")
    assert "51010" in s2 and "帳戶模式" in s2
