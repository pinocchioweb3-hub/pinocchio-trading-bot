# -*- coding: utf-8 -*-
"""v193（監督員 r87）：intent outbox 進度檔「壞掉／讀不到」不再折成「第一次啟動」。

破口（l4_execution/intent_outbox.py）：
    def _load_state():
        try:    return json.loads(_STATE.read_text(...))
        except Exception: return {"last_id": 0}      # ← 三種情形折成同一個答案
    …啟動時 `if last_id == 0:` 被當成「真·第一次啟動」→ 直接跳到 MAX(id)。

為什麼比 v192（消費端單一壞檔）更嚴重：
  • 這是**訊號產生端**。進度檔一壞，last_id 直接跳到當下最大 id ⇒ 上一次成功存檔
    到這次重啟之間的**每一筆**訊號都不會被寫成 intent，消費端連檔都看不到。
  • 沒有任何重試路徑：last_id 已經跳過去了，paper_trades 裡的原始訊號永遠不會回頭補。
  • 壞檔正是自己製造的：舊版 _save_state 用非原子 write_text，斷電／當機寫到一半就是
    半截 JSON（本機有實際斷電事件史）；下一次啟動再自己誤讀 ⇒ 自產自誤的閉環。

本檔鎖住的語意（含反向護欄，避免把偵測改成「一律回填整部歷史」）：
  1. missing / unreadable / ok 三態必須分得出來。
  2. 壞檔要留鑑識證據（下一次 _save_state 會蓋掉原檔）。
  3. 進度未知時起點取「仍在有效窗內的最舊訊號」之前，⛔ 不得取 MAX(id)。
  4. 真·第一次啟動仍然不回填歷史（反向護欄）。
  5. 起始 id 查不出來時，⛔ 不得寫下「last_id: 0」這種假進度。
  6. _save_state 必須原子寫，且失敗要出聲、要回報失敗。
全離線：monkeypatch 到暫存目錄與暫存 sqlite；零網路、零交易所、零真錢。
"""
import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from l4_execution import intent_outbox as io


class _Stop(BaseException):
    """跳出無窮迴圈用。用 BaseException 以免被 loop 內的 except Exception 吃掉。"""


class _FakeAsyncio:
    """只攔 sleep（第 N 次就中止迴圈），to_thread 直跑同執行緒。"""

    def __init__(self, stop_after: int = 1):
        self.calls = 0
        self.stop_after = stop_after

    async def to_thread(self, fn, *a, **k):
        return fn(*a, **k)

    async def sleep(self, *a, **k):
        self.calls += 1
        if self.calls >= self.stop_after:
            raise _Stop


_COLS = ("id", "symbol", "setup", "direction", "entry_price", "stop_price",
         "tp1", "tp2", "tp3", "entry_at", "status")


def _mkdb(path: Path, rows) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE paper_trades (id INTEGER PRIMARY KEY, symbol TEXT, setup TEXT, "
        "direction TEXT, entry_price REAL, stop_price REAL, tp1 REAL, tp2 REAL, "
        "tp3 REAL, entry_at INTEGER, status TEXT)")
    conn.executemany(
        f"INSERT INTO paper_trades ({','.join(_COLS)}) VALUES ({','.join('?' * len(_COLS))})",
        rows)
    conn.commit()
    conn.close()


def _row(rid, setup, entry_at, symbol="SOL"):
    return (rid, symbol, setup, "bull", 100.0, 95.0, 110.0, None, None,
            int(entry_at), "open")


def _wire(tmp_path, monkeypatch, rows, *, db_ok=True):
    """把模組的三個外部接點導到暫存區；回 (db_path, outbox_dir, state_path)。"""
    db = tmp_path / "trade_journal.db"
    if db_ok:
        _mkdb(db, rows)
    else:
        db = tmp_path / "nope" / "trade_journal.db"     # 目錄不存在 → 連不上
    outbox = tmp_path / "intent_outbox"
    state = tmp_path / "intent_outbox_state.json"
    monkeypatch.setattr(io, "OUTBOX_DIR", outbox)
    monkeypatch.setattr(io, "_STATE", state)
    monkeypatch.setattr(io, "db_path", lambda name: str(db))
    return db, outbox, state


def _run_loop_once(monkeypatch, stop_after=1):
    fake = _FakeAsyncio(stop_after=stop_after)
    monkeypatch.setattr(io, "asyncio", fake)
    with pytest.raises(_Stop):
        asyncio.run(io.run_intent_outbox_loop(poll_seconds=15))


def _intent_file(outbox: Path, rid, setup, entry_at, symbol="SOL") -> Path:
    intent = io.build_intent(dict(zip(_COLS, _row(rid, setup, entry_at, symbol))))
    return outbox / f"{intent['intent_id']}.json"


# ── 1. 三態要分得出來 ──────────────────────────────────────────────────
def test_load_state_distinguishes_missing_from_unreadable(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, [])
    st, status = io._load_state()
    assert status == "missing" and st.get("last_id") == 0, "沒有檔＝真·第一次啟動"

    io._STATE.write_text('{"last_id": 469}', encoding="utf-8")
    st, status = io._load_state()
    assert status == "ok" and st["last_id"] == 469

    io._STATE.write_text('{"last_id": 46', encoding="utf-8")       # 半截 JSON（斷電產物）
    st, status = io._load_state()
    assert status == "unreadable", "讀不出來＝**未知**，不可與『第一次啟動』同型"

    io._STATE.write_text("[]", encoding="utf-8")                   # 合法 JSON 但不是物件
    _, status = io._load_state()
    assert status == "unreadable", "結構不對也是未知"


# ── 2. 壞檔要留證 ─────────────────────────────────────────────────────
def test_unreadable_state_preserves_forensic_copy(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, [])
    io._STATE.write_text('{"last_id": 46', encoding="utf-8")
    io._load_state()
    bad = io._STATE.with_suffix(".bad")
    assert bad.exists() and bad.read_text(encoding="utf-8") == '{"last_id": 46', (
        "壞檔會被下一次 _save_state 蓋掉，留證是唯一鑑識證據")


# ── 3. 進度未知 ⇒ 不得跳到 MAX(id)（核心） ─────────────────────────────
def test_unreadable_state_does_not_silently_skip_pending_signals(tmp_path, monkeypatch):
    now_ms = time.time() * 1000
    rows = [
        _row(100, "deepdive", now_ms - 20 * 3600_000, "OLD"),   # 早已過期（不該回填）
        _row(101, "deepdive", now_ms - 1 * 3600_000, "SOL"),    # 有效窗內·待送
        _row(102, "us_breakout", now_ms - 0.5 * 3600_000, "NVDA"),
    ]
    _, outbox, state = _wire(tmp_path, monkeypatch, rows)
    state.write_text('{"last_id": 10', encoding="utf-8")         # 斷電留下的半截檔
    _run_loop_once(monkeypatch)

    assert _intent_file(outbox, 101, "deepdive", now_ms - 1 * 3600_000).exists(), (
        "進度未知就跳到 MAX(id) ⇒ 這筆訊號永遠不會被寫成 intent，且無任何重試路徑")
    assert _intent_file(outbox, 102, "us_breakout", now_ms - 0.5 * 3600_000,
                        "NVDA").exists()
    assert not _intent_file(outbox, 100, "deepdive", now_ms - 20 * 3600_000,
                            "OLD").exists(), "過期訊號不該回填（反向護欄）"


# ── 4. 反向護欄：真·第一次啟動仍不回填歷史 ────────────────────────────
def test_first_run_still_does_not_backfill_history(tmp_path, monkeypatch):
    now_ms = time.time() * 1000
    rows = [_row(101, "deepdive", now_ms - 1 * 3600_000, "SOL")]
    _, outbox, state = _wire(tmp_path, monkeypatch, rows)
    assert not state.exists()
    _run_loop_once(monkeypatch)
    assert not _intent_file(outbox, 101, "deepdive", now_ms - 1 * 3600_000).exists(), (
        "沒有進度檔＝真·第一次啟動：舊訊號價位早已失效，維持不回填")
    assert json.loads(state.read_text(encoding="utf-8"))["last_id"] == 101


# ── 5. 反向護欄：正常進度照常往前掃 ───────────────────────────────────
def test_normal_state_scans_forward(tmp_path, monkeypatch):
    now_ms = time.time() * 1000
    rows = [_row(101, "deepdive", now_ms - 1 * 3600_000, "SOL"),
            _row(102, "deepdive", now_ms - 0.5 * 3600_000, "AVAX")]
    _, outbox, state = _wire(tmp_path, monkeypatch, rows)
    state.write_text('{"last_id": 101}', encoding="utf-8")
    _run_loop_once(monkeypatch)
    assert _intent_file(outbox, 102, "deepdive", now_ms - 0.5 * 3600_000, "AVAX").exists()
    assert not _intent_file(outbox, 101, "deepdive", now_ms - 1 * 3600_000).exists()
    assert json.loads(state.read_text(encoding="utf-8"))["last_id"] == 102


# ── 6. 起始 id 查不出來 ⇒ 不得寫下假進度 ──────────────────────────────
def test_unknown_start_id_writes_no_fake_progress(tmp_path, monkeypatch, capsys):
    _, _, state = _wire(tmp_path, monkeypatch, [], db_ok=False)
    _run_loop_once(monkeypatch)
    assert not state.exists(), (
        "查不到 MAX(id) 就存 last_id:0 ⇒ 下一輪會把整部歷史當新訊號回填")
    assert "🚨" in capsys.readouterr().out


# ── 7. 進度檔必須原子寫，失敗要出聲 ───────────────────────────────────
def test_save_state_is_atomic_and_reports_failure(tmp_path, monkeypatch, capsys):
    _, _, state = _wire(tmp_path, monkeypatch, [])
    assert io._save_state({"last_id": 7}) is True
    assert json.loads(state.read_text(encoding="utf-8"))["last_id"] == 7
    assert not list(state.parent.glob("*.tmp")), "原子改名後不該留下暫存檔"

    monkeypatch.setattr(io, "_STATE", tmp_path / "no_such_dir" / "state.json")
    assert io._save_state({"last_id": 8}) is False, "寫不進去要回報失敗，不可靜默 pass"
    assert "🚨" in capsys.readouterr().out
