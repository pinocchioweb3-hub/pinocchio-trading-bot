"""fire_queue 安全路徑測試（v48 新增）。

過去 L3 只測最安全的純函式（L2 引擎），最會出事的「派發/冷卻/崩潰回收」零覆蓋。
本檔補上四項有狀態、最易在改版時悄悄回歸的邏輯：

    1. enqueue 冷卻：同 (幣,setup,向) 窗內第二次入隊被擋；窗過（backdate）後可再入隊。
    2. dequeue_one 樂觀鎖：取出即轉 dispatching；同筆再 dequeue 回 None（不重複派發）。
    3. reclaim_orphans：卡在 dispatching 的孤兒回收回 queued、可被再次 dequeue
       （這正是「斷電/重啟後靜默漏單」的修復；本測試直接驗證它真能補回）。
    4. mark_failed 寫入 fail_reason（過去 reason 收到卻丟棄，事後無法稽核為何沒送）。

執行（任一）：
    pytest tests/test_fire_queue.py
    python tests/test_fire_queue.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import l3_dispatcher.fire_queue as fq
from l2_trigger.configs.intraday import INTRADAY_SUI
from l2_trigger.engine import evaluate
from tests import fixtures as F

# 把 DB 指到臨時檔，與正式資料隔離（import 後改 module 全域，_conn 每次讀全域）
fq.DB_PATH = Path(tempfile.mkdtemp(prefix="firequeue_test_")) / "fire_queue_test.db"


def _fresh():
    fq.reset_db()


def _fire_decision():
    """真實 FIRE BULL intraday decision（SUI 軋空情境，與 L2 引擎測試同一 fixture）。"""
    return evaluate(F.sui_intraday_fire_bull(), INTRADAY_SUI)


def test_enqueue_cooldown_hit_then_release_after_window():
    _fresh()
    d = _fire_decision()
    assert fq.enqueue(d, cooldown_seconds=3600) is True
    # 窗內同 key 再入隊 → 擋
    assert fq.enqueue(d, cooldown_seconds=3600) is False
    # backdate cooldown：把 last_fired 推到 4000 秒前 → 窗已過 → 可再入隊
    conn = fq._conn()
    try:
        conn.execute("UPDATE cooldown SET last_fired = last_fired - 4000")
    finally:
        conn.close()
    assert fq.enqueue(d, cooldown_seconds=3600) is True


def test_dequeue_optimistic_lock():
    _fresh()
    d = _fire_decision()
    assert fq.enqueue(d, cooldown_seconds=3600) is True
    item = fq.dequeue_one()
    assert item is not None
    fire_id, decision = item
    assert decision["snapshot"]["symbol"] == "SUI"
    # 已轉 dispatching → 再 dequeue 同筆不應再取出（無其他 queued）
    assert fq.dequeue_one() is None


def test_reclaim_orphans_recovers_stuck_dispatching():
    _fresh()
    d = _fire_decision()
    fq.enqueue(d, cooldown_seconds=3600)
    item = fq.dequeue_one()           # → dispatching
    assert item is not None
    # 模擬崩潰：卡在 dispatching，從未 mark_sent；此時無 queued 可取
    assert fq.dequeue_one() is None
    n = fq.reclaim_orphans()
    assert n == 1
    # 回收後可再次取出（進場訊號不再被靜默吞掉）
    item2 = fq.dequeue_one()
    assert item2 is not None
    assert item2[0] == item[0]        # 同一筆 fire_id


def test_reclaim_orphans_noop_when_none_stuck():
    _fresh()
    d = _fire_decision()
    fq.enqueue(d, cooldown_seconds=3600)
    # 只入隊未 dequeue → 無 dispatching → 回收 0（不會誤動 queued）
    assert fq.reclaim_orphans() == 0


def test_mark_failed_writes_reason():
    _fresh()
    d = _fire_decision()
    fq.enqueue(d, cooldown_seconds=3600)
    item = fq.dequeue_one()
    fire_id = item[0]
    fq.mark_failed(fire_id, "blocked_by_risk: daily_dd_breach")
    conn = fq._conn()
    try:
        row = conn.execute(
            "SELECT status, fail_reason FROM fires WHERE id=?", (fire_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "failed"
    assert row[1] == "blocked_by_risk: daily_dd_breach"


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
