"""v203（監督員 r98）：macro.py 兩個狀態檔的「讀不出來」不再折成「本來就沒有」。

同物種第 23 次。這一次的下場分兩處：
  • daily_macro_state.json 讀失敗 → 折成「從來沒發過」→ 每次重啟都再發一份完整
    Daily Macro（watchdog 三層自癒本來就會重啟，壞檔期間無上限重發）。
  • pulse_state.json 讀失敗 → 折成「沒有上一則」→ 對 LLM 宣稱「今日第一則」
    （假的事實陳述），且差分報告整份重講。

⛔ 這些測試刻意走**可觀測行為**（有沒有送出／餵給 LLM 的字串長什麼樣），
   而不是只測新函式的回傳值——否則把新函式刪掉就一起消失，等於虛設檢定。
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

import botpaths
from l3_dispatcher import macro


class _Stop(Exception):
    """用來在啟動段跑完後中止無窮主迴圈。"""


def _patch_common(monkeypatch, tmp_path):
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)

    async def fake_compute(source, watchlist):
        return {"regime_advice": {"label": "x", "color": "x"}}

    async def fake_synth(*a, **kw):
        return "NARRATIVE", {"output_chars": 9}

    import l3_dispatcher.synthesizer as synth
    import telegram_bot.message_format as mf
    monkeypatch.setattr(macro, "compute_macro_state", fake_compute)
    monkeypatch.setattr(synth, "synthesize_via_claude_code", fake_synth, raising=False)
    monkeypatch.setattr(mf, "render_macro_report", lambda *a, **kw: "TEMPLATE", raising=False)


def _run_startup(monkeypatch, tmp_path) -> int:
    """跑 run_daily_macro_loop 的啟動段，回傳「啟動版」推送了幾則。"""
    sends: list[str] = []

    async def fake_send(tg, text, prefix="", **kw):
        sends.append(prefix)

    monkeypatch.setattr(macro, "_send_to_telegram", fake_send)

    real_sleep = asyncio.sleep
    calls = {"n": 0}

    async def fake_sleep(sec, *a, **kw):
        calls["n"] += 1
        if calls["n"] > 1:            # 只放行啟動段那一次 sleep，不進每日主迴圈
            raise _Stop()
        return await real_sleep(0)

    monkeypatch.setattr(macro.asyncio, "sleep", fake_sleep)

    async def main():
        try:
            await macro.run_daily_macro_loop(None, None, [], run_on_startup=True)
        except _Stop:
            pass

    asyncio.run(main())
    return sum(1 for p in sends if "啟動版" in p)


# ── Site B：daily_macro_state.json ────────────────────────────────────────

def test_startup_sends_when_state_file_truly_absent(monkeypatch, tmp_path):
    """對照組：真的沒有檔＝真·第一次啟動 → 該發（這一態的行為不可被改壞）。"""
    _patch_common(monkeypatch, tmp_path)
    assert _run_startup(monkeypatch, tmp_path) == 1


def test_startup_skips_when_recently_sent(monkeypatch, tmp_path):
    """對照組：10 分鐘前發過 → 跳過（v23-2 既有行為）。"""
    _patch_common(monkeypatch, tmp_path)
    (tmp_path / "daily_macro_state.json").write_text(
        json.dumps({"last_sent_ts": time.time() - 600}), encoding="utf-8")
    assert _run_startup(monkeypatch, tmp_path) == 0


@pytest.mark.parametrize("payload,label", [
    (b'{"last_sent_ts": 178559', "半截 JSON（斷電殘檔）"),
    (b'[]', "合法 JSON 但非 dict"),
    (b'{}', "dict 但缺 last_sent_ts"),
    (b'{"last_sent_ts": "not-a-number"}', "last_sent_ts 型別不對"),
    (b'', "零長度檔"),
])
def test_startup_does_not_resend_when_stamp_unreadable(monkeypatch, tmp_path,
                                                       payload, label):
    """核心：戳記讀不出來時，⛔ 不可折成「從來沒發過」而重發。

    保守跳過的理由：少發最多是延後到主迴圈 00:00 UTC 那班（不會永久遺失），
    重發卻是每次重啟都來一份、且無上限。
    """
    _patch_common(monkeypatch, tmp_path)
    (tmp_path / "daily_macro_state.json").write_bytes(payload)
    assert _run_startup(monkeypatch, tmp_path) == 0, f"{label} 被折成『從來沒發過』"


def test_unreadable_stamp_is_audible_and_preserved(monkeypatch, tmp_path, capsys):
    """讀不出來要出聲，而且壞檔要留鑑識副本（原檔下一輪就會被蓋掉）。"""
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)
    p = tmp_path / "daily_macro_state.json"
    p.write_bytes(b'{"last_sent_ts": 178559')
    skip, status = macro._daily_macro_startup_skip()
    assert (skip, status) == (True, macro.STATE_UNREADABLE)
    out = capsys.readouterr().out
    assert "讀不出來" in out or "未知" in out
    assert (tmp_path / "daily_macro_state.bad").exists()


def test_mark_sent_reports_write_failure(monkeypatch, tmp_path, capsys):
    """寫戳記失敗要回 False 並出聲——舊版 `except: pass` 讓它與成功完全同形。"""
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(macro, "_atomic_write_state", boom)
    assert macro._mark_daily_macro_sent() is False
    assert "寫入失敗" in capsys.readouterr().out


def test_mark_sent_roundtrip(monkeypatch, tmp_path):
    """寫得進去時：回 True，且寫出來的東西自己讀得回來（原子寫不可寫出壞檔）。"""
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)
    assert macro._mark_daily_macro_sent() is True
    skip, status = macro._daily_macro_startup_skip()
    assert (skip, status) == (True, macro.STATE_OK)


# ── Site A：pulse_state.json ──────────────────────────────────────────────

def test_pulse_baseline_missing_vs_unreadable(monkeypatch, tmp_path):
    """真的沒有檔 → missing；檔在但壞掉 → unreadable。⛔ 兩者不可同形。"""
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)
    assert macro._load_pulse_baseline() == (None, None, macro.STATE_MISSING)

    (tmp_path / "pulse_state.json").write_bytes('{"text": "半截'.encode("utf-8"))
    text, ts, status = macro._load_pulse_baseline()
    assert (text, ts, status) == (None, None, macro.STATE_UNREADABLE)


def test_pulse_baseline_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)
    (tmp_path / "pulse_state.json").write_text(
        json.dumps({"text": "上一則", "ts": "08-01 12:00 UTC"}), encoding="utf-8")
    assert macro._load_pulse_baseline() == ("上一則", "08-01 12:00 UTC", macro.STATE_OK)


def _capture_pulse_prompt(monkeypatch, **kwargs) -> str:
    """跑真正的 synthesize_hourly_pulse，攔下餵給 LLM 的 user_data。"""
    import l3_dispatcher.synthesizer as synth
    seen = {}

    async def fake_prompt(system_prompt, user_data, timeout_sec=180, **kw):
        seen["user_data"] = user_data
        return "TEXT", {}

    monkeypatch.setattr(synth, "_synthesize_with_prompt", fake_prompt)
    monkeypatch.setattr(synth, "_format_pulse_data", lambda st: "DATA")
    asyncio.run(synth.synthesize_hourly_pulse({}, **kwargs))
    return seen["user_data"]


def test_pulse_prompt_does_not_claim_first_when_baseline_unreadable(monkeypatch):
    """核心：基準讀不出來時，⛔ 不可對 LLM 宣稱「今日第一則」（那是假的事實陳述）。"""
    ud = _capture_pulse_prompt(monkeypatch, last_pulse_text=None,
                               baseline_status="unreadable")
    # 比對「肯定句」那一支的措辭本身；提示詞裡的『禁止宣稱這是今日第一則』是反向禁令，
    # 用裸關鍵字會誤判，故對準 missing 分支的完整句型。
    assert "上一次報告：無" not in ud
    assert "讀取失敗" in ud or "基準未知" in ud


def test_pulse_prompt_still_says_first_when_truly_absent(monkeypatch):
    """對照組：真的沒有上一則時，維持既有措辭（這一態不可被改壞）。"""
    ud = _capture_pulse_prompt(monkeypatch, last_pulse_text=None,
                               baseline_status="missing")
    assert "上一次報告：無" in ud


def test_pulse_prompt_uses_baseline_when_present(monkeypatch):
    """對照組：有基準就走差分，狀態旗標不得蓋掉真的有的基準。"""
    ud = _capture_pulse_prompt(monkeypatch, last_pulse_text="上一則全文",
                               last_pulse_ts="08-01 12:00 UTC",
                               baseline_status="unreadable")
    assert "上一則全文" in ud
    assert "上一次報告：無" not in ud
