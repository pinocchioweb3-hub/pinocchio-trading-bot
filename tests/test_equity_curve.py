"""紙上驗證帳累積 R 走勢圖測試（朋友回饋 Q1）。

鎖住誠實邊界，避免日後改動把以下重新埋掉：
  • entry_expired（限價未成交）排除於走勢與統計外。
  • 兩引擎分線：us_breakout 獨立，其餘（deepdive…）歸加密。
  • 累積 R 依平倉時間（exit_at）順序累加；realized_r 為 None 視為 0（不捏造）。
  • 勝率＝realized_r>0 筆數 / 已平倉筆數。
  • 無資料時 render 回 None（不畫空圖、不假裝有績效）。

全離線：monkeypatch DB_PATH 到暫存 sqlite，真 init_db 建表 + 合成列；零網路、零真錢、零訊號數學。
"""
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from l3_dispatcher import equity_curve as ec
from l3_dispatcher import paper_journal as pj


def _seed(db: Path, rows: list[dict]) -> None:
    """真 init_db 建表後插入合成列（含 exit_at / setup / realized_r / exit_reason）。"""
    pj.init_db()
    conn = pj._conn()
    try:
        now = int(time.time() * 1000)
        for r in rows:
            conn.execute(
                "INSERT INTO paper_trades (symbol, setup, direction, entry_price, "
                "stop_price, entry_at, created_at, status, exit_at, exit_reason, "
                "realized_r) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("BTC", r.get("setup", "deepdive"), "bull", 100.0, 90.0, now, now,
                 r["status"], r.get("exit_at"), r.get("exit_reason"),
                 r.get("realized_r")))
    finally:
        conn.close()


def _both_paths(tmp_path, monkeypatch):
    p = str(tmp_path / "trade_journal.db")
    monkeypatch.setattr(pj, "DB_PATH", p)
    monkeypatch.setattr(ec, "DB_PATH", p)
    monkeypatch.setattr(ec, "CHART_DIR", tmp_path / "charts")


def _rows():
    # exit_at 故意亂序插入，驗證查詢端排序正確
    return [
        # 加密 deepdive：止盈 +2.0（t=300）
        {"status": "closed", "setup": "deepdive", "exit_at": 300,
         "exit_reason": "tp2", "realized_r": 2.0},
        # 加密 deepdive：止損 -1.0（t=100，最早）
        {"status": "closed", "setup": "deepdive", "exit_at": 100,
         "exit_reason": "stop", "realized_r": -1.0},
        # 加密 deepdive：限價未成交 → 必須排除
        {"status": "closed", "setup": "deepdive", "exit_at": 150,
         "exit_reason": "entry_expired", "realized_r": 0.0},
        # 加密 deepdive：timeout，realized_r=None → 視為 0（t=200）
        {"status": "closed", "setup": "deepdive", "exit_at": 200,
         "exit_reason": "timeout", "realized_r": None},
        # 美股 us_breakout：止盈 +1.5（獨立引擎）
        {"status": "closed", "setup": "us_breakout", "exit_at": 250,
         "exit_reason": "tp1", "realized_r": 1.5},
        # 美股 us_breakout：未平倉 → 排除（status!=closed）
        {"status": "open", "setup": "us_breakout", "exit_at": None,
         "exit_reason": None, "realized_r": None},
    ]


def test_series_excludes_entry_expired_and_splits_engines(tmp_path, monkeypatch):
    _both_paths(tmp_path, monkeypatch)
    _seed(tmp_path / "trade_journal.db", _rows())

    series = ec._fetch_series()
    # 加密：3 筆（排除 entry_expired），依 exit_at 排序 100→200→300
    cum = [r for _, r in series["crypto"]]
    assert len(series["crypto"]) == 3
    assert cum == [-1.0, -1.0, 1.0]          # -1（stop）, +0（None timeout）, +2（tp）
    # 美股：僅 1 筆已平倉（open 排除）
    assert len(series["us"]) == 1
    assert series["us"][-1][1] == 1.5


def test_stats_winrate_and_cum(tmp_path, monkeypatch):
    _both_paths(tmp_path, monkeypatch)
    _seed(tmp_path / "trade_journal.db", _rows())

    raw = ec._raw_rs()
    cs = ec._stats(ec._fetch_series()["crypto"], raw["crypto"])
    ss = ec._stats(ec._fetch_series()["us"], raw["us"])
    assert cs["n"] == 3 and round(cs["cum_r"], 2) == 1.0
    assert round(cs["win_rate"], 1) == 33.3   # 1 勝（+2）/ 3
    assert ss["n"] == 1 and ss["cum_r"] == 1.5 and ss["win_rate"] == 100.0


def test_render_returns_path_when_data(tmp_path, monkeypatch):
    _both_paths(tmp_path, monkeypatch)
    _seed(tmp_path / "trade_journal.db", _rows())
    p = ec.render_equity_curve()
    assert p is not None and Path(p).exists()


def test_render_none_when_empty(tmp_path, monkeypatch):
    _both_paths(tmp_path, monkeypatch)
    _seed(tmp_path / "trade_journal.db", [])
    assert ec.render_equity_curve() is None


def test_render_none_when_only_entry_expired(tmp_path, monkeypatch):
    # 全數限價未成交 → 無有效已平倉 → 不畫圖（不假裝有績效）
    _both_paths(tmp_path, monkeypatch)
    _seed(tmp_path / "trade_journal.db", [
        {"status": "closed", "setup": "deepdive", "exit_at": 100,
         "exit_reason": "entry_expired", "realized_r": 0.0},
    ])
    assert ec.render_equity_curve() is None
