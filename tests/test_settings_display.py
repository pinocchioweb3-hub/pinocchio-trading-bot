"""task#10(2e) 設定選單『顯示模式』往返測試（補驗證稽核兩則 medium 缺口）。

稽核發現：test_display_mode.py 釘住了 macro 端的 parity/純呈現，但沒有測到 Telegram
使用者**實際能翻動這顆開關**——即 render_settings() 是否顯示目前模式並把對的按鈕標 ●，
以及 handle_settings_callback('set:display:...') 是否確實寫入 botconfig（壞值落回 novice）。
本檔補這兩段往返（render + callback），不碰任何訊號/下單數學、不碰磁碟（set_override 被攔）。

執行方式：
    pytest tests/test_settings_display.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import botconfig
from telegram_bot.settings_menu import handle_settings_callback, render_settings


def _force_disp(monkeypatch, mode: str):
    """把 DISPLAY_MODE 釘成指定值（不碰 bot_settings.json）。
    render_settings 內部 `from botconfig import get_str`，故 patch botconfig.get_str。"""
    real = botconfig.get_str

    def fake(key, default=None):
        if key == "DISPLAY_MODE":
            return mode
        return real(key, default)

    monkeypatch.setattr(botconfig, "get_str", fake)


def _display_row(buttons):
    """從按鈕矩陣撈出『顯示模式』那一列（callback_data 以 set:display: 開頭）。"""
    for row in buttons:
        if any((b.get("callback_data") or "").startswith("set:display:") for b in row):
            return row
    return []


# ── 1. render_settings：狀態行 + ● 標記隨 DISPLAY_MODE 正確切換 ───────────────
def test_render_shows_novice_marked(monkeypatch):
    _force_disp(monkeypatch, "novice")
    text, buttons = render_settings()
    assert "顯示模式" in text
    assert "新手" in text                      # 狀態行顯示新手
    row = _display_row(buttons)
    assert row, "應有 set:display: 那一列"
    by_val = {b["callback_data"]: b["text"] for b in row}
    # 新手被選中 → 前綴 ●；專家未選 → 無 ●
    assert by_val["set:display:novice"].startswith("● ")
    assert "🔰新手" in by_val["set:display:novice"]
    assert not by_val["set:display:expert"].startswith("● ")


def test_render_shows_expert_marked(monkeypatch):
    _force_disp(monkeypatch, "expert")
    text, buttons = render_settings()
    assert "專家" in text
    row = _display_row(buttons)
    by_val = {b["callback_data"]: b["text"] for b in row}
    assert by_val["set:display:expert"].startswith("● ")
    assert not by_val["set:display:novice"].startswith("● ")


def test_render_bad_value_falls_back_to_novice(monkeypatch):
    """DISPLAY_MODE 髒值（如殘留舊鍵）→ 狀態行與 ● 應落回新手，不崩潰。"""
    _force_disp(monkeypatch, "garbage")
    text, buttons = render_settings()
    assert "新手" in text
    row = _display_row(buttons)
    by_val = {b["callback_data"]: b["text"] for b in row}
    assert by_val["set:display:novice"].startswith("● ")


# ── 2. handle_settings_callback：set:display: 寫入 botconfig（壞值落回 novice）──
class _FakeTG:
    """攔截 answer_callback_query / _post（重繪），不對外發網路。"""

    def __init__(self):
        self.answered = []
        self.posts = []

    async def answer_callback_query(self, cq_id, text):
        self.answered.append((cq_id, text))

    async def _post(self, method, payload):
        self.posts.append((method, payload))
        return {"ok": True}


def _run_display_callback(monkeypatch, value):
    """攔 set_override → 不寫磁碟，回傳被攔下的呼叫清單與是否已處理。"""
    calls = []

    def rec(key, val, source=None):
        calls.append((key, val, source))

    monkeypatch.setattr(botconfig, "set_override", rec)
    tg = _FakeTG()
    cq = {"id": "cq1", "data": f"set:display:{value}",
          "message": {"chat": {"id": 123}, "message_id": 456}}
    handled = asyncio.run(handle_settings_callback(tg, cq))
    return handled, calls, tg


def test_callback_sets_expert(monkeypatch):
    handled, calls, tg = _run_display_callback(monkeypatch, "expert")
    assert handled is True
    assert ("DISPLAY_MODE", "expert", "human") in calls
    assert tg.answered, "應回 answer_callback_query 給使用者提示"


def test_callback_sets_novice(monkeypatch):
    handled, calls, _ = _run_display_callback(monkeypatch, "novice")
    assert handled is True
    assert ("DISPLAY_MODE", "novice", "human") in calls


def test_callback_bad_value_falls_back_to_novice(monkeypatch):
    """非法值（注入/打錯）→ 必須落回 novice 才寫入，絕不把髒值寫進 botconfig。"""
    handled, calls, _ = _run_display_callback(monkeypatch, "bogus")
    assert handled is True
    assert ("DISPLAY_MODE", "novice", "human") in calls
    # 確認沒有把髒值寫進去
    assert all(v != "bogus" for (_k, v, _s) in calls)


def test_non_display_callback_not_hijacked(monkeypatch):
    """回歸保險：set:display 分支不得吃掉其他 set: 動作（這裡用 refresh 驗證仍回 True
    且不誤呼 DISPLAY_MODE 寫入）。"""
    calls = []
    monkeypatch.setattr(botconfig, "set_override",
                        lambda *a, **k: calls.append(a))
    tg = _FakeTG()
    cq = {"id": "cq2", "data": "set:refresh",
          "message": {"chat": {"id": 1}, "message_id": 2}}
    handled = asyncio.run(handle_settings_callback(tg, cq))
    assert handled is True
    assert all((a and a[0] != "DISPLAY_MODE") for a in calls)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
