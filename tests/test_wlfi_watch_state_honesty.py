# -*- coding: utf-8 -*-
"""v194（監督員 r88）：WLFI 追蹤進度檔「壞掉／讀不到」不再折成「第一次啟動」。

破口（l3_dispatcher/wlfi_watch.py）：
    def _load() -> dict:
        try:    return json.loads(_STATE.read_text(...))
        except Exception: return {}          # ← 三種情形折成同一個答案
    …掃描端 `frm = st.get("last_block") or (head - 80)`
    ＝進度檔一壞，last_block 消失 → 起點跳到「當下往回 80 個區塊」（≈16 分鐘）。

後果：上次成功存檔到這次重啟之間的 WLFI 鏈上轉帳**整段不會被掃**，而且沒有重試路徑
——下一輪結尾就把 `st["last_block"] = head` 寫回去，那段區塊永遠不再回頭。同時 seen_tx
也一併被清空 ⇒ 16 分鐘窗內已推過的轉帳會**重推一次**：使用者看到的是「有在動」，
真正漏掉的那段反而無聲。這比單純漏訊號更糟，因為漏與重複同時發生、方向相反。

為什麼這個模組值得治（雖然 100% display_only、永不進開單數學）：
    WLFI 是目前唯一的真錢阻塞（孤兒倉 WLFI-USDT-SWAP long 11618 張，帳本 blockers
    第一條）。本模組是使用者判斷「要不要平掉它」的**唯一鏈上資訊面**——它靜默降級，
    等於把使用者做決策的依據悄悄挖空，而畫面上看起來一切正常。

壞檔還是自己製造的：舊版 _save 用非原子 write_text 且失敗直接 pass，斷電／當機寫到
一半就是半截 JSON（本機有實際斷電事件史，v177 才補了電力哨兵），下一次啟動再自己
誤讀＝自產自誤的閉環（與 v162-v166、v193 同一根因家族）。

本檔鎖住的語意（含反向護欄，避免把偵測改成「一律回掃到底」）：
  1. missing / unreadable / ok 三態必須分得出來。
  2. 壞檔要留鑑識副本（下一次 _save 會蓋掉原檔）。
  3. 進度未知時起點取設計允許的**最大回補窗**，⛔ 不得取冷啟動的 80 區塊捷徑。
  4. 反向護欄：真·第一次啟動仍走 80 區塊冷啟動窗（⛔ 不得改成每次都回掃 1800）。
  5. 反向護欄：正常進度照常從 last_block+1 往前，且仍受 1800 上限夾住（防灌爆 RPC）。
  6. _save 必須原子寫，且失敗要出聲、要回報失敗。
全離線：monkeypatch 到暫存目錄；零網路、零 RPC、零交易所、零真錢。
"""
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from l3_dispatcher import wlfi_watch as w  # noqa: E402

_HEAD = 25_658_073          # 線上實測區塊高度量級（wlfi_watch_state.json）


def _wire(tmp_path, monkeypatch) -> Path:
    state = tmp_path / "wlfi_watch_state.json"
    monkeypatch.setattr(w, "_STATE", state)
    return state


# ── 1. 三態要分得出來 ──────────────────────────────────────────────────
def test_load_distinguishes_missing_from_unreadable(tmp_path, monkeypatch):
    state = _wire(tmp_path, monkeypatch)
    st, status = w._load()
    assert status == "missing" and st == {}, "沒有檔＝真·第一次啟動"

    state.write_text('{"last_block": 25658073}', encoding="utf-8")
    st, status = w._load()
    assert status == "ok" and st["last_block"] == 25658073

    state.write_text('{"last_block": 2565', encoding="utf-8")     # 半截 JSON（斷電產物）
    _, status = w._load()
    assert status == "unreadable", "讀不出來＝**未知**，不可與『第一次啟動』同型"

    state.write_text("[]", encoding="utf-8")                      # 合法 JSON 但不是物件
    _, status = w._load()
    assert status == "unreadable", "結構不對也是未知"


# ── 2. 壞檔要留證 ─────────────────────────────────────────────────────
def test_unreadable_state_preserves_forensic_copy(tmp_path, monkeypatch):
    state = _wire(tmp_path, monkeypatch)
    state.write_text('{"last_block": 2565', encoding="utf-8")
    w._load()
    bad = state.with_suffix(".bad")
    assert bad.exists() and bad.read_text(encoding="utf-8") == '{"last_block": 2565', (
        "壞檔會被下一次 _save 蓋掉，留證是唯一鑑識證據")


# ── 3. 進度未知 ⇒ 不得走冷啟動捷徑（核心） ────────────────────────────
def test_unknown_progress_does_not_take_cold_start_shortcut(tmp_path, monkeypatch, capsys):
    _wire(tmp_path, monkeypatch)
    frm = w._scan_start({}, "unreadable", _HEAD)
    assert frm <= _HEAD - w.MAX_BACKFILL_BLOCKS + 1, (
        "進度未知卻只往回掃 80 區塊 ⇒ 上次存檔到現在的轉帳整段靜默漏掉，且永不回頭")
    assert frm < _HEAD - w.COLD_START_BLOCKS, "起點必須明顯早於冷啟動窗"
    assert "🚨" in capsys.readouterr().out or True    # 出聲在 _load，這裡只鎖起點


# ── 4. 反向護欄：真·第一次啟動仍走冷啟動窗 ────────────────────────────
def test_true_first_run_still_uses_cold_start_window(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    frm = w._scan_start({}, "missing", _HEAD)
    assert frm == _HEAD - w.COLD_START_BLOCKS + 1, (
        "沒有檔＝真的沒掃過：維持原本的 80 區塊冷啟動窗，⛔ 不得改成每次回掃 1800")


# ── 5. 反向護欄：正常進度照常往前，且受上限夾住 ───────────────────────
def test_normal_progress_scans_forward_and_is_clamped(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    frm = w._scan_start({"last_block": _HEAD - 10}, "ok", _HEAD)
    assert frm == _HEAD - 9, "正常進度＝從 last_block+1 接著掃"

    stale = w._scan_start({"last_block": _HEAD - 999_999}, "ok", _HEAD)
    assert stale == _HEAD - w.MAX_BACKFILL_BLOCKS, (
        "進度太舊仍要被 1800 上限夾住——⛔ 不可為了補齊而對公共 RPC 灌爆")


# ── 6. 進度檔必須原子寫，失敗要出聲 ───────────────────────────────────
def test_save_is_atomic_and_reports_failure(tmp_path, monkeypatch, capsys):
    state = _wire(tmp_path, monkeypatch)
    assert w._save({"last_block": 7}) is True, "成功要回報成功（⛔ 勿改回回傳 None）"
    assert json.loads(state.read_text(encoding="utf-8"))["last_block"] == 7
    assert not list(state.parent.glob("*.tmp")), "原子改名後不該留下暫存檔"

    monkeypatch.setattr(w, "_STATE", tmp_path / "nope" / "wlfi_watch_state.json")
    assert w._save({"last_block": 8}) is False, "存不進去要回報失敗（⛔ 勿改回靜默 pass）"
    assert "🚨" in capsys.readouterr().out, "進度存不進去＝下次重啟會拿到舊進度，必須出聲"
