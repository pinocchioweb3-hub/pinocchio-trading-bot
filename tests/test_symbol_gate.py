"""跨來源 per-symbol 收斂閘（symbol_gate）測試。

執行方式（任一）：
    pytest tests/test_symbol_gate.py
    python tests/test_symbol_gate.py

驗證重點：
    1. 同 (幣,向) 窗內第二次推送被擋（核心去重）。
    2. 窗過後可再推。
    3. 反向（bull↔bear）預設「不」互擋（保留刻意設計）。
    4. window_s 可由呼叫端覆寫（US 用 14400）。
    5. SYMBOL_GATE_BLOCK_REVERSAL=1 開啟後反向也互擋。
    6. 持久化：mark_sent 後新查詢（新連線）看得到 → 模擬「進程重啟存活 / 跨 worker」。
    7. 跨來源情境：A worker 標記後，B worker 的 should_send 回 False。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import l3_dispatcher.symbol_gate as sg

# 把 DB 指到臨時檔，與正式資料隔離（import 後改 module 全域，_conn 每次讀全域）
sg.DB_PATH = Path(tempfile.mkdtemp(prefix="symgate_test_")) / "symbol_gate_test.db"


def _fresh():
    sg.reset_db()


def test_first_send_allowed_then_duplicate_blocked():
    _fresh()
    t0 = 1_000_000
    assert sg.should_send("BTC", "bull", now=t0) is True
    sg.mark_sent("BTC", "bull", now=t0)
    # 5 分鐘後同幣同向 → 擋
    assert sg.should_send("BTC", "bull", window_s=3600, now=t0 + 300) is False


def test_allowed_again_after_window():
    _fresh()
    t0 = 2_000_000
    sg.mark_sent("ETH", "bear", now=t0)
    assert sg.should_send("ETH", "bear", window_s=3600, now=t0 + 3599) is False
    assert sg.should_send("ETH", "bear", window_s=3600, now=t0 + 3601) is True


def test_reversal_not_blocked_by_default():
    _fresh()
    os.environ.pop("SYMBOL_GATE_BLOCK_REVERSAL", None)
    t0 = 3_000_000
    sg.mark_sent("SOL", "bull", now=t0)
    # 反向（bear）預設不被擋 — 反轉訊號對持倉出場有價值
    assert sg.should_send("SOL", "bear", window_s=3600, now=t0 + 60) is True
    # 同向仍擋
    assert sg.should_send("SOL", "bull", window_s=3600, now=t0 + 60) is False


def test_custom_window_us():
    _fresh()
    t0 = 4_000_000
    sg.mark_sent("AAPL", "bull", now=t0)
    # US 用 14400(4h)：1h 後仍擋
    assert sg.should_send("AAPL", "bull", window_s=14400, now=t0 + 3600) is False
    # 4h+1 後放行
    assert sg.should_send("AAPL", "bull", window_s=14400, now=t0 + 14401) is True


def test_reversal_blocked_when_enabled():
    _fresh()
    os.environ["SYMBOL_GATE_BLOCK_REVERSAL"] = "1"
    try:
        t0 = 5_000_000
        sg.mark_sent("BNB", "bull", now=t0)
        # 開啟反向節流後，bear 也被擋
        assert sg.should_send("BNB", "bear", window_s=3600, now=t0 + 60) is False
    finally:
        os.environ.pop("SYMBOL_GATE_BLOCK_REVERSAL", None)
    # 還原後反向恢復放行
    assert sg.should_send("BNB", "bear", window_s=3600, now=5_000_060) is True


def test_persistence_new_connection_sees_mark():
    """mark_sent 寫進 SQLite；之後新查詢（等同進程重啟 / 另一 worker）讀得到。"""
    _fresh()
    t0 = 6_000_000
    sg.mark_sent("XRP", "bull", now=t0)
    age = sg.last_sent_age("XRP", "bull", now=t0 + 120)
    assert age == 120
    # 不同 symbol 從未推過 → None
    assert sg.last_sent_age("DOGE", "bull", now=t0) is None


def test_cross_source_dedup_scenario():
    """模擬：scheduler(A) 先推 BTC 多，deepdive(B) 數分鐘後想推同幣同向 → 被擋。"""
    _fresh()
    t0 = 7_000_000
    # A 來源（dispatcher）送出並標記
    assert sg.should_send("BTC", "bull", now=t0) is True
    sg.mark_sent("BTC", "bull", now=t0)
    # B 來源（deepdive）3 分鐘後查 → 看到 A 已推，跳過
    assert sg.should_send("BTC", "bull", now=t0 + 180) is False
    # 不同幣不受影響
    assert sg.should_send("ETH", "bull", now=t0 + 180) is True


# --- 直接執行（無 pytest 也能跑）---
if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
