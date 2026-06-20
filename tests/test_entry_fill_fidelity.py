"""task#60：分批限價單成交偵測保真測試。

定位：trade_monitor 的「掛單成交偵測」舊版只看最近一根的收盤價（bars[-1]['close']），
會漏掉「窗內觸價、收盤又彈回」的真成交 → 偽 entry_expired → 惡化樣本飢餓。
治本＝改用「進場後有利極值」探針，對齊：
  • _check_trade 的 TP/SL 高低聚合（max(high)/min(low)）
  • backtest._try_limit_fill 的盤中觸價語意（bull:low≤限價／bear:high≥限價）

本檔把以下語意鎖住，避免日後改動把保真修重新埋掉：
  • bull 掛單：窗內 low 觸及限價即成交（即使收盤又彈回限價上方）。
  • bear 掛單：窗內 high 觸及限價即成交（即使收盤又回落）。
  • 只認 ts ≥ entry_at 的 bar：掛單成立前的下/上影不得灌成交（紅線③不灌假成交）。
  • 進場過近、本輪無進場後 bar → None（維持 pending，不誤判）。
全離線：monkeypatch DB_PATH 到暫存 sqlite；零網路、零真錢、零訊號數學。
"""
import json
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from l3_dispatcher import paper_journal as pj
from l3_dispatcher import trade_monitor as tm


def test_probe_bull_uses_min_low_over_post_entry_bars():
    e = 1_000_000
    bars = [
        {"ts": e - 300_000, "high": 99, "low": 80, "close": 98},   # 進場前下影（不可灌）
        {"ts": e,           "high": 101, "low": 96, "close": 100},
        {"ts": e + 300_000, "high": 100, "low": 94, "close": 99},  # 窗內回踩 94
        {"ts": e + 600_000, "high": 102, "low": 99, "close": 101},  # 又彈回（舊版只看此收盤會漏）
    ]
    # 取進場後 min(low)=94，而非最後收盤 101；且不被進場前 low=80 污染。
    assert tm._entry_fill_probe(bars, "bull", e) == 94


def test_probe_bear_uses_max_high_over_post_entry_bars():
    e = 1_000_000
    bars = [
        {"ts": e - 300_000, "high": 120, "low": 99, "close": 100},  # 進場前上影（不可灌）
        {"ts": e,           "high": 104, "low": 99, "close": 100},
        {"ts": e + 300_000, "high": 106, "low": 100, "close": 101},  # 窗內衝高 106
        {"ts": e + 600_000, "high": 102, "low": 99, "close": 100},   # 又回落
    ]
    # 取進場後 max(high)=106，不被進場前 high=120 污染。
    assert tm._entry_fill_probe(bars, "bear", e) == 106


def test_probe_no_post_entry_bar_returns_none():
    e = 2_000_000
    bars = [{"ts": e - 600_000, "high": 99, "low": 90, "close": 95}]
    # 進場過近，本輪沒有任何 ts≥entry_at 的 bar → None（維持 pending，不誤判成交）。
    assert tm._entry_fill_probe(bars, "bull", e) is None


def test_dip_and_recover_fills_via_probe(tmp_path, monkeypatch):
    """端到端：bull 限價 95，窗內 low 觸 94 後收盤彈回 101 → 應成交（舊法用收盤 101 會漏）。"""
    monkeypatch.setattr(pj, "DB_PATH", str(tmp_path / "trade_journal.db"))
    pj.init_db()
    e = int(time.time() * 1000)
    conn = pj._conn()
    try:
        splits = [{"price": 95.0, "frac": 1.0, "filled": 0, "filled_at": None}]
        conn.execute(
            "INSERT INTO paper_trades (symbol, setup, direction, entry_price, stop_price, "
            "tp1, entry_at, created_at, status, entry_state, entry_splits, "
            "entry_filled_pct, size_remaining) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("BTC", "deepdive", "bull", 95.0, 90.0, 110.0, e, e, "open", "pending",
             json.dumps(splits), 0.0, 1.0))
        pid = conn.execute("SELECT id FROM paper_trades").fetchone()[0]
    finally:
        conn.close()

    bars = [
        {"ts": e,           "high": 101, "low": 96, "close": 100},
        {"ts": e + 300_000, "high": 100, "low": 94, "close": 99},   # 觸 94 ≤ 95
        {"ts": e + 600_000, "high": 102, "low": 99, "close": 101},  # 收盤彈回 101
    ]
    probe = tm._entry_fill_probe(bars, "bull", e)
    fill = pj.apply_entry_fill(pid, probe)
    assert fill is not None and fill["state"] == "full"

    # 保真修的方向證明：新法 probe=94 觸 95 成交；舊法收盤 101 不觸 95（101<=95 為 False）→ 漏。
    assert probe == 94 and (probe <= 95.0) and not (101.0 <= 95.0)


def test_pre_entry_dip_does_not_fill(tmp_path, monkeypatch):
    """紅線③：掛單成立『之前』的下影不得灌成交。窗內全部 ts<entry_at → 不成交、維持 pending。"""
    monkeypatch.setattr(pj, "DB_PATH", str(tmp_path / "trade_journal.db"))
    pj.init_db()
    e = int(time.time() * 1000)
    conn = pj._conn()
    try:
        splits = [{"price": 95.0, "frac": 1.0, "filled": 0, "filled_at": None}]
        conn.execute(
            "INSERT INTO paper_trades (symbol, setup, direction, entry_price, stop_price, "
            "tp1, entry_at, created_at, status, entry_state, entry_splits, "
            "entry_filled_pct, size_remaining) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("BTC", "deepdive", "bull", 95.0, 90.0, 110.0, e, e, "open", "pending",
             json.dumps(splits), 0.0, 1.0))
        pid = conn.execute("SELECT id FROM paper_trades").fetchone()[0]
    finally:
        conn.close()

    bars = [
        {"ts": e - 600_000, "high": 99, "low": 90, "close": 96},   # 進場前觸 90（不可灌）
        {"ts": e - 300_000, "high": 98, "low": 92, "close": 96},   # 進場前觸 92（不可灌）
    ]
    probe = tm._entry_fill_probe(bars, "bull", e)
    assert probe is None
    # probe=None → 呼叫端不會呼 apply_entry_fill；倉位維持 pending。
    row = pj._conn().execute(
        "SELECT entry_state FROM paper_trades WHERE id=?", (pid,)).fetchone()
    assert row[0] == "pending"
