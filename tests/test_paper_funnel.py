"""訂單漏斗誠實層測試（task#57）── get_paper_funnel / render_paper_funnel。

定位：限價單「掛了沒成交」(entry_expired) 是 deepdive 路徑最大的結構性拖累（實測 ~45%）。
本檔把以下誠實欄位鎖住，避免日後改動把「未成交拖累」重新埋掉：
  • entry_expired           — 限價掛單逾時未成交筆數（never_filled 的子集，單獨揭露）。
  • fill_rate_pct           — 有效成交率＝真正進場 / 提出；無提案時誠實回 None（不假裝 0%）。
  • est_proposals_per_bucket— 依現行成交率推估湊滿 1 個 L2 學習桶(30 筆成交)所需提案；
                              成交率為 0 時回 None（不捏造有限數字）。

全離線：monkeypatch DB_PATH 到暫存 sqlite，走真 init_db 建表 + 合成列；零網路、零真錢、零訊號數學。
"""
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from l3_dispatcher import paper_journal as pj


def _seed(db: Path, rows: list[dict]) -> None:
    """用真 init_db 建表後插入合成列（只填漏斗會用到的欄＋NOT NULL 必填欄）。"""
    pj.init_db()
    conn = pj._conn()
    try:
        now = int(time.time() * 1000)
        for r in rows:
            conn.execute(
                "INSERT INTO paper_trades (symbol, setup, direction, entry_price, "
                "stop_price, entry_at, created_at, status, entry_filled_pct, "
                "exit_reason, realized_r) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("BTC", r.get("setup", "deepdive"), "bull", 100.0, 90.0, now, now,
                 r["status"], r["filled"], r.get("exit_reason"), r.get("realized_r")))
    finally:
        conn.close()


def _funnel_rows():
    return [
        # 成交→平倉→止盈
        {"status": "closed", "filled": 1.0, "exit_reason": "tp2", "realized_r": 1.8},
        # 成交→平倉→止損
        {"status": "closed", "filled": 1.0, "exit_reason": "stop", "realized_r": -1.0},
        # 限價掛單逾時未成交（never_filled 的子集；不算進已平倉）
        {"status": "closed", "filled": 0.0, "exit_reason": "entry_expired", "realized_r": 0.0},
        # 成交→進行中
        {"status": "open", "filled": 1.0, "exit_reason": None, "realized_r": None},
        # 成交→平倉→逾時
        {"status": "closed", "filled": 1.0, "exit_reason": "timeout", "realized_r": -0.2},
    ]


def test_funnel_counts_entry_expired_and_fill_rate(tmp_path, monkeypatch):
    monkeypatch.setattr(pj, "DB_PATH", str(tmp_path / "trade_journal.db"))
    _seed(tmp_path / "trade_journal.db", _funnel_rows())

    f = pj.get_paper_funnel(days=30)
    assert f["proposed"] == 5
    assert f["entered"] == 4                       # filled_pct>0：A,B,D,E
    assert f["never_filled"] == 1                  # C
    assert f["entry_expired"] == 1                 # C（單獨揭露）
    assert f["in_progress"] == 1                   # D
    assert f["closed"] == 3                        # A,B,E（C 因 entry_expired 排除）
    assert f["tp_wins"] == 1                       # A
    assert f["sl_losses"] == 2                     # B,E（realized_r<0）
    assert f["timeouts"] == 1                      # E
    assert f["fill_rate_pct"] == 80.0             # 4/5
    # 依 80% 成交率推估湊滿 1 桶(30 成交)：ceil(30*5/4)=38
    assert f["est_proposals_per_bucket"] == 38
    assert f["min_bucket_n"] == pj.MIN_BUCKET_N_MIRROR == 30


def test_funnel_zero_fill_rate_is_honest_none(tmp_path, monkeypatch):
    # 全數限價未成交 → 成交率 0 → est 不捏造有限值，誠實回 None
    monkeypatch.setattr(pj, "DB_PATH", str(tmp_path / "trade_journal.db"))
    _seed(tmp_path / "trade_journal.db", [
        {"status": "closed", "filled": 0.0, "exit_reason": "entry_expired", "realized_r": 0.0},
        {"status": "closed", "filled": 0.0, "exit_reason": "entry_expired", "realized_r": 0.0},
    ])
    f = pj.get_paper_funnel(days=30)
    assert f["proposed"] == 2 and f["entered"] == 0
    assert f["entry_expired"] == 2
    assert f["fill_rate_pct"] == 0.0
    assert f["est_proposals_per_bucket"] is None   # 不除以零、不捏造


def test_funnel_empty_is_none_fill_rate(tmp_path, monkeypatch):
    monkeypatch.setattr(pj, "DB_PATH", str(tmp_path / "trade_journal.db"))
    _seed(tmp_path / "trade_journal.db", [])
    f = pj.get_paper_funnel(days=30)
    assert f["proposed"] == 0
    assert f["fill_rate_pct"] is None              # 無提案：誠實 None 而非 0%
    assert f["est_proposals_per_bucket"] is None


def test_render_funnel_shows_honest_line(tmp_path, monkeypatch):
    monkeypatch.setattr(pj, "DB_PATH", str(tmp_path / "trade_journal.db"))
    _seed(tmp_path / "trade_journal.db", _funnel_rows())
    s = pj.render_paper_funnel(days=30)
    assert "限價未成交" in s
    assert "有效成交率" in s and "80.0%" in s
    assert "L2 學習桶" in s and "38" in s


def test_render_funnel_empty_signal(tmp_path, monkeypatch):
    monkeypatch.setattr(pj, "DB_PATH", str(tmp_path / "trade_journal.db"))
    _seed(tmp_path / "trade_journal.db", [])
    s = pj.render_paper_funnel(days=30)
    assert "尚無訊號" in s
