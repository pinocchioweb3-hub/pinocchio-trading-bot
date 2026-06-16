"""L1→L3 端到端整合測試（v49 新增）。

過去測試各層都「分開」測：L2 引擎（test_engine）、fire_queue 派發/冷卻/回收
（test_fire_queue）、風控六閘門（test_risk_manager）、收斂閘（test_symbol_gate）。
但「一筆訊號從 L1 快照 → L2 evaluate → 入隊 → dispatcher 真的渲染並送出 → 帳本落地」
這條完整管線『串起來』時會不會斷，零覆蓋。本檔補這條縫。

測什麼（真實串接，不 mock 核心）：
    1. happy path：SUI 軋空快照 → FIRE → enqueue → dispatch_once
       → 假 Telegram 真的收到一則含標的/方向/按鈕的 FIRE 訊息
       → fire_queue 那筆轉 'sent'（帶 message_id）
       → trade_journal 寫一筆 status='signal'、entry_kind='direct_fire' 並回連 fire_id
       → paper_journal 自動開一筆紙上倉（驗證引擎期望值用）
       → 佇列清空後再 dispatch 回 False。
    2. 空佇列：dispatch_once 回 False、不送任何訊息。
    3. 持倉中抑制（v22）：同 symbol 同向已有 open 持倉時，新 FIRE 被靜默略過
       （不送 🎯、fire 標 failed），證明 dispatcher↔trade_journal 的抑制縫真的接上。

中性化了什麼、為什麼（只動「對外 I/O」與「有專屬測試的正交閘門」，核心管線全真跑）：
    - dispatcher._fetch_live_price → None：不打 OKX 抓現價；回 None 等同「價在進場區內」
      → 強制走「直接 FIRE」路徑（等待觸發路徑另有其狀態機，不在本縫範圍）。
    - dispatcher.should_block → 放行：風控閘門有專屬 test_risk_manager 全覆蓋；且其中
      econ_calendar.in_blackout() 依「真實世界經濟數據時間窗」會讓結果不確定 → 此處中性化以保確定性。
    - l3_dispatcher.chart_render（SMC 圖）→ 假模組回 None：避免打 OKX + 載 matplotlib。
    - l3_dispatcher.analogue.analogue_stats → None：避免打 OKX 抓 300 根 K（本就 try/except，
      只是不想等網路 timeout）。
    其餘加料（解鎖警告、廣度警示）皆讀本地、唯讀、try/except 包覆，保留真跑。

DB 全部指到臨時目錄，與正式資料完全隔離（與 test_fire_queue 同慣例）。

執行（任一）：
    pytest tests/test_integration_l1_l3.py
    python tests/test_integration_l1_l3.py
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import l3_dispatcher.analogue as analogue
import l3_dispatcher.dispatcher as dispatcher
import l3_dispatcher.fire_queue as fq
import l3_dispatcher.paper_journal as pj
import l3_dispatcher.symbol_gate as sg
import l3_dispatcher.trade_journal as tj
from l2_trigger.configs.intraday import INTRADAY_SUI
from l2_trigger.engine import evaluate
from l2_trigger.types import TriggerAction
from l3_dispatcher.trade_journal import EntryRecord, record_entry
from tests import fixtures as F

# === DB 隔離：所有相關 module 的 DB_PATH 指到同一臨時目錄 ===
# （import 後改 module 全域；每個 module 的 _conn() 都在呼叫當下讀全域，故安全）
_TMP = Path(tempfile.mkdtemp(prefix="integration_l1l3_"))
fq.DB_PATH = _TMP / "fire_queue.db"
tj.DB_PATH = _TMP / "trade_journal.db"
pj.DB_PATH = _TMP / "trade_journal.db"     # 紙上帳與實倉帳同檔（與正式一致）
sg.DB_PATH = _TMP / "symbol_gate.db"


# ---------------------------------------------------------------------------
# 假 Telegram client（duck-typed；只實作 dispatcher 會用到的兩個方法）
# ---------------------------------------------------------------------------
class FakeTelegram:
    def __init__(self):
        self.messages: list[dict] = []
        self.photos: list[dict] = []
        self._next_id = 1000

    async def send_message(self, text, parse_mode=None, inline_buttons=None):
        self._next_id += 1
        self.messages.append(
            {"text": text, "parse_mode": parse_mode, "inline_buttons": inline_buttons}
        )
        return {"ok": True, "result": {"message_id": self._next_id}}

    async def send_photo(self, photo, caption=None):
        self._next_id += 1
        self.photos.append({"photo": photo, "caption": caption})
        return {"ok": True, "result": {"message_id": self._next_id}}


# ---------------------------------------------------------------------------
# 極簡 monkeypatch 替身：讓同一份測試函式在 pytest 與 `python ...` 直跑都能用。
# 介面（setattr(obj,name,val) / setitem(dic,key,val) / undo()）與 pytest 的 monkeypatch 相容。
# ---------------------------------------------------------------------------
class _MP:
    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value):
        old = getattr(target, name)
        self._undo.append(lambda: setattr(target, name, old))
        setattr(target, name, value)

    def setitem(self, dic, key, value):
        had = key in dic
        old = dic.get(key)

        def _restore():
            if had:
                dic[key] = old
            else:
                dic.pop(key, None)

        self._undo.append(_restore)
        dic[key] = value

    def undo(self):
        for fn in reversed(self._undo):
            fn()
        self._undo.clear()


def _fresh():
    """清乾淨三顆 DB，確保每測互不汙染。"""
    fq.reset_db()
    sg.reset_db()
    for ext in ("", "-wal", "-shm"):
        p = Path(str(tj.DB_PATH) + ext)
        if p.exists():
            p.unlink()


async def _async_none(*args, **kwargs):
    return None


def _neutralize(mp) -> None:
    """只中性化『對外 I/O』與『有專屬測試的正交閘門』，核心管線保持真跑。"""
    # 強制「直接 FIRE」路徑（不打 OKX；None == 視為價在進場區內）
    mp.setattr(dispatcher, "_fetch_live_price", _async_none)
    # 風控閘門放行（有專屬 test_risk_manager；且 econ blackout 依真實時間會不確定）
    mp.setattr(
        dispatcher,
        "should_block",
        lambda decision, *a, **k: (
            False, "ok", {"msg": "整合測試：風控已中性化（test_risk_manager 專屬覆蓋）"}),
    )
    # 類比統計 → None（本就 try/except，只為免等 OKX 網路）
    mp.setattr(analogue, "analogue_stats", _async_none)
    # SMC 圖渲染 → 注入假模組回 None（免打 OKX + 免載 matplotlib）
    fake_chart = types.ModuleType("l3_dispatcher.chart_render")
    fake_chart.render_symbol_chart = _async_none
    mp.setitem(sys.modules, "l3_dispatcher.chart_render", fake_chart)


def _count_paper(symbol: str) -> int:
    conn = sqlite3.connect(str(pj.DB_PATH))
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE symbol=?", (symbol,)
        ).fetchone()[0]
    finally:
        conn.close()


def _trade_entry_kind(fire_id: int) -> str | None:
    conn = sqlite3.connect(str(tj.DB_PATH))
    try:
        row = conn.execute(
            "SELECT entry_kind FROM trades WHERE fire_id=? ORDER BY id DESC LIMIT 1",
            (fire_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# ===========================================================================
# 1) happy path：完整一條龍
# ===========================================================================
def test_l1_to_l3_fire_end_to_end(monkeypatch=None):
    mp = monkeypatch or _MP()
    try:
        _fresh()
        _neutralize(mp)

        # --- L1（快照）→ L2（引擎）---
        decision = evaluate(F.sui_intraday_fire_bull(), INTRADAY_SUI)
        assert decision.action == TriggerAction.FIRE
        assert decision.direction.value == "bull"

        # --- L2 → 佇列 ---
        assert fq.enqueue(decision, cooldown_seconds=3600) is True

        # --- L3（派發）---
        tg = FakeTelegram()
        handled = asyncio.run(dispatcher.dispatch_once(tg))
        assert handled is True

        # 1. 假 TG 真的收到一則 FIRE 訊息
        assert len(tg.messages) == 1, f"預期送 1 則，實得 {len(tg.messages)}"
        sent = tg.messages[0]
        assert "SUI" in sent["text"]
        assert sent["parse_mode"] == "HTML"
        # 按鈕含 fill/skip/intent 三種 callback（dispatcher 固定覆蓋）
        cbs = [b["callback_data"] for row in (sent["inline_buttons"] or []) for b in row]
        assert any(c.startswith("fill:") for c in cbs), cbs
        assert any(c.startswith("skip:") for c in cbs), cbs
        assert any(c.startswith("intent:") for c in cbs), cbs
        # 沒有 chart（已中性化回 None）→ 不應送照片
        assert tg.photos == []

        # 2. fire_queue 那筆轉 sent（帶 message_id）
        st = fq.stats()
        assert st.get("sent") == 1, st
        assert "queued" not in st and "dispatching" not in st, st

        # 3. trade_journal 寫一筆 signal（等使用者按「已下單」）、回連 fire_id、direct_fire
        pending = tj.get_pending_signals()
        assert len(pending) == 1, pending
        sig = pending[0]
        assert sig["symbol"] == "SUI"
        assert sig["direction"] == "bull"
        fire_id = sig["fire_id"]
        assert fire_id is not None
        assert _trade_entry_kind(fire_id) == "direct_fire"

        # 4. paper_journal 自動開一筆紙上倉（Stage 0 期望值驗證）
        assert _count_paper("SUI") == 1

        # 5. 佇列清空 → 再 dispatch 回 False、不再多送訊息
        assert asyncio.run(dispatcher.dispatch_once(tg)) is False
        assert len(tg.messages) == 1
    finally:
        mp.undo()


# ===========================================================================
# 2) 空佇列：dispatch_once 回 False、零訊息
# ===========================================================================
def test_dispatch_empty_queue_returns_false(monkeypatch=None):
    mp = monkeypatch or _MP()
    try:
        _fresh()
        _neutralize(mp)
        tg = FakeTelegram()
        assert asyncio.run(dispatcher.dispatch_once(tg)) is False
        assert tg.messages == []
        assert tg.photos == []
    finally:
        mp.undo()


# ===========================================================================
# 3) 持倉中抑制（v22）：同 symbol 同向已有 open → 新 FIRE 靜默略過
# ===========================================================================
def test_open_position_suppresses_duplicate_fire(monkeypatch=None):
    mp = monkeypatch or _MP()
    try:
        _fresh()
        _neutralize(mp)

        # 先放一筆「已確認」的 SUI 多單持倉（status='open'）
        record_entry(
            EntryRecord(
                symbol="SUI", setup="intraday", direction="bull",
                entry_price=3.40, stop_price=3.27,
                tp1=3.53, tp2=3.60, tp3=3.66,
                risk_usd=100.0, leverage=15,
            ),
            initial_status="open",
        )

        # 同向 SUI 多單 FIRE 入隊
        decision = evaluate(F.sui_intraday_fire_bull(), INTRADAY_SUI)
        assert fq.enqueue(decision, cooldown_seconds=3600) is True

        tg = FakeTelegram()
        handled = asyncio.run(dispatcher.dispatch_once(tg))
        assert handled is True              # 有處理（不論成敗）
        # 被抑制：不送 🎯、不送照片
        assert tg.messages == []
        assert tg.photos == []
        # fire 標 failed（suppressed: position open ...）
        st = fq.stats()
        assert st.get("failed") == 1, st
        assert "sent" not in st, st
    finally:
        mp.undo()


# --- 直接執行（無 pytest 也能跑）---
if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            import traceback
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
