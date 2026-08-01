"""決策快照「解不開」不得折成「這筆本來就沒存快照」— v204（監督員 r99）。

同物種第 24 次（未知被折成「確認沒有」）。這一處的下場落在**人手執行真錢單**那一步：

  `l3_dispatcher.trade_journal.get_signal_for_intent()` 舊碼：

      if snap_json:
          try:
              blob = json.loads(snap_json)
          except Exception:
              blob = None          # ← 壞掉的快照，和「欄位是 NULL」變成同一件事

  兩種情況都掉進下面的「退化重建」路徑，回一個
  `composite_score=None / confirmed=[] / rationale 空` 的最小 decision。
  呼叫端（Telegram「📋 複製可執行 JSON」按鈕、`/intent` 指令、
  platform/api 的 /api/intent/build 與 /api/signal）拿到的東西**完全一樣**，
  所以對使用者顯示的是「一筆脈絡較薄的舊訊號」——而真相是「這筆的快照資料損壞」。
  使用者是**照著這份 JSON 用手下真錢單**的人，兩者該不該複核完全不同。

  ⚠️ 價位/方向本身不是捏造：那些欄位來自 trades 表的真實欄位，不是壞掉的 blob。
  壞的是**限定語消失**——這正是紅線③的形狀（數字照送、限定語不見）。

修法：讀取端把「解不開」與「本來就沒有」分成兩種狀態，
用 `decision["snapshot_unreadable"]` 帶出去；三個呈現面各自加上限定語。
⛔ 沒有替 trade_journal 加新的 try/except、沒有吞掉任何例外、沒有填補任何數字。

本檔每一條在舊碼上都必須是紅的（非虛設檢定）。
執行：pytest tests/test_intent_snapshot_unreadable.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_API = ROOT / "platform" / "api"
for _p in (str(ROOT), str(_API)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from l3_dispatcher import trade_journal as tj  # noqa: E402

_FULL_BLOB = {
    "direction": "bull",
    "setup_name": "sweep_reclaim",
    "composite_score": 7.5,
    "confirmed": ["oi_up", "cvd_up"],
    "snapshot": {"symbol": "BTC", "price": 100.0, "ts": 1700000000},
}


def _seed(monkeypatch, tmp_path, snap_json, symbol="BTC", fire_id=1):
    """把 trade_journal 指到臨時 DB，塞一筆 trades 列（decision_snapshot 由呼叫端指定）。"""
    monkeypatch.setattr(tj, "DB_PATH", str(tmp_path / "tj.db"))
    tj.init_db()
    conn = tj._conn()
    try:
        now_ms = int(time.time() * 1000)
        conn.execute(
            """INSERT INTO trades (symbol, setup, direction, entry_price, stop_price,
                                   entry_at, status, fire_id, decision_snapshot,
                                   created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'signal', ?, ?, ?, ?)""",
            (symbol, "sweep_reclaim", "bull", 100.0, 95.0, now_ms, fire_id,
             snap_json, now_ms, now_ms))
    finally:
        conn.close()


# ── 讀取端：兩種狀態必須分得出來（探針對兩邊都要有鑑別力）────────────────────
def test_corrupt_snapshot_is_marked_unreadable(monkeypatch, tmp_path):
    """壞 JSON ⇒ 仍回可用 decision，但必須帶 snapshot_unreadable 標記。"""
    _seed(monkeypatch, tmp_path, '{"snapshot": {')
    d = tj.get_signal_for_intent(fire_id=1)
    assert d is not None, "壞快照不該讓整筆訊號消失（價位/方向來自 trades 欄位，仍是真的）"
    assert d.get("snapshot_unreadable"), "解不開的快照必須被標出來，不可靜靜退化"
    assert "解" in str(d["snapshot_unreadable"]) or "JSON" in str(d["snapshot_unreadable"])


def test_missing_snapshot_is_not_marked(monkeypatch, tmp_path):
    """對照組：欄位是 NULL＝這筆本來就沒存快照（早於 v45）⇒ 不得標成壞掉。"""
    _seed(monkeypatch, tmp_path, None)
    d = tj.get_signal_for_intent(fire_id=1)
    assert d is not None
    assert not d.get("snapshot_unreadable"), "『本來就沒有』被誤標成『壞掉』＝反向誤報"


def test_full_snapshot_untouched(monkeypatch, tmp_path):
    """對照組：完整快照照舊忠實回傳，不得多出標記。"""
    _seed(monkeypatch, tmp_path, json.dumps(_FULL_BLOB))
    d = tj.get_signal_for_intent(fire_id=1)
    assert d.get("composite_score") == 7.5
    assert not d.get("snapshot_unreadable")


def test_non_dict_snapshot_is_marked(monkeypatch, tmp_path):
    """合法 JSON 但不是 dict（例如被寫進一個字串）⇒ 一樣是讀不出來，不是沒有。"""
    _seed(monkeypatch, tmp_path, '"這不是 decision"')
    d = tj.get_signal_for_intent(fire_id=1)
    assert d is not None
    assert d.get("snapshot_unreadable")


# ── 呈現面①：Telegram（📋 按鈕 / /intent）────────────────────────────────
def test_telegram_intro_carries_qualifier():
    from telegram_bot import callbacks as cb
    base = cb._INTENT_INTRO   # 生產路徑就是這樣傳：_intent_intro_for(decision, intro)
    clean = cb._intent_intro_for({"direction": "bull"}, base)
    dirty = cb._intent_intro_for(
        {"snapshot_unreadable": "decision_snapshot 解不開（JSONDecodeError）"}, base)
    assert clean == base, "沒壞的訊號不該被加警語（反向誤報）"
    assert dirty != base and "讀不出來" in dirty, "壞快照必須在 JSON 前面帶限定語"
    assert base in dirty, "限定語是加上去的，不可蓋掉原本的說明"


# ── 呈現面②：platform/api（/api/intent/build、/api/signal）───────────────
@pytest.mark.skipif(not (_API / "intent_api.py").exists(),
                    reason="platform/ 子樹不在此工作區（未納版控）")
def test_intent_api_surfaces_unreadable(monkeypatch, tmp_path):
    import intent_api
    _seed(monkeypatch, tmp_path, '{"snapshot": {')
    out = intent_api.build_intent(fire_id=1)
    # 不論 intent 組得起來或組不起來，這個標記都必須在（不可只在成功路徑才誠實）
    assert out.get("snapshot_unreadable"), f"intent_api 沒把讀不出來帶出來：{out}"
    if "validation" in out:
        assert any("讀不出來" in p for p in out["validation"]), \
            "成功路徑必須把限定語放進 validation，前端才看得到"


@pytest.mark.skipif(not (_API / "data_access.py").exists(),
                    reason="platform/ 子樹不在此工作區（未納版控）")
def test_signal_detail_surfaces_unreadable(monkeypatch, tmp_path):
    import data_access as da
    _seed(monkeypatch, tmp_path, '{"snapshot": {')
    out = da.signal_detail(fire_id=1)
    assert out.get("snapshot_unreadable"), f"/api/signal 沒把讀不出來帶出來：{out}"
