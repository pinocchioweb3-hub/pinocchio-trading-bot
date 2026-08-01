# -*- coding: utf-8 -*-
"""v199（監督員 r93）：存活戳記／缺口帳本「壞掉·讀不到」不再折成「第一次啟動·沒有斷層」。

破口（l3_dispatcher/liveness.py）三處，全是同一個折疊：
    read_last(): 檔不存在 / 讀不到 / 壞檔 / 形狀不對  →  同一個 None
    _read_gaps(): 同上                                →  同一個 []

① check_gap() 拿到 None 就回 {"gap": False, "reason": "no_prior_stamp"}
   ＝「這是第一次啟動，沒有斷線」。run_bot.py:200 於是靜默不告警。
   ⚠️ 這一處的致命之處在於**故障與症狀完全相關**：本模組存在的唯一理由就是抓
   「當機／斷電／休眠」造成的斷線，而 stamp() 用非原子 write_text 且沒有 fsync，
   斷電寫到一半留下的就是半截 JSON。於是最嚴重的那一次停機——真的斷電的那次——
   正好是唯一會被判成「沒有斷層」而全程靜默的一次。偵測器在它最該出聲時啞掉。

② record_gap() 是 read-modify-write：壞檔那輪 _read_gaps() 回 []，append 一筆之後
   把**整份帳本**寫回去 ⇒ 最多 50 筆歷史缺口被原子且乾淨地抹掉（原位元組不留）。
   與 v196／v197／v198 同型：一次讀失敗被兌現成不可逆的寫抹除。

③ recent_gaps() 壞檔回 [] ⇒ ceo_session.py:335 在 CEO 日報印「過去24h 無離線缺口」，
   把「讀不出來」講成正面保證——紅線③（不捏造）在報告面的實例。

本檔鎖住的語意（含反向護欄，避免把偵測改成一律告警的噪音機）：
  1. 壞檔要留鑑識副本（stamp() 每輪都會蓋掉原檔，不留證就永遠查不出當時發生什麼）。
  2. 壞檔要出聲，⛔ 不得靜默。
  3. 壞檔時 record_gap 必須停手不寫、原檔逐位元不動，並回報失敗。
  4. check_gap 必須把「壞檔」與「真·第一次啟動」分成兩種答案。
  5. stamp() 必須原子寫（temp + os.replace），⛔ 不得就地覆寫。
  6. 反向護欄：真·沒有檔仍是 no_prior_stamp 且 gap=False（⛔ 不得改成告警）。
  7. 反向護欄：正常戳記的門檻內／外判定完全不變。
  8. 反向護欄：缺口帳本仍夾在最近 50 筆。
全離線：monkeypatch data_dir 到暫存目錄；零網路、零交易所、零真錢、零 Telegram。
"""
import json
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from l3_dispatcher import liveness  # noqa: E402


@pytest.fixture()
def livedir(tmp_path, monkeypatch):
    monkeypatch.setattr(liveness, "data_dir", lambda: tmp_path)
    return tmp_path


def _corrupt_stamp(d: Path) -> bytes:
    """製造一份「斷電寫到一半」的戳記檔，回傳原始位元組供比對。"""
    raw = b'{"ts": 178558224'
    (d / "liveness.json").write_bytes(raw)
    return raw


# --------------------------------------------------------------------------
# 1) 戳記壞檔：不得折成「第一次啟動」
# --------------------------------------------------------------------------
def test_corrupt_stamp_is_not_first_boot(livedir):
    _corrupt_stamp(livedir)
    res = liveness.check_gap()
    assert res["reason"] != "no_prior_stamp", (
        "壞掉的戳記被當成『從來沒有戳記＝第一次啟動』——真正的斷線就這樣消失了")
    assert res.get("unreadable") is True


def test_corrupt_stamp_keeps_forensic_copy(livedir):
    raw = _corrupt_stamp(livedir)
    liveness.check_gap()
    bad = livedir / "liveness.bad"
    assert bad.exists(), "壞檔沒留鑑識副本；下一輪 stamp() 就會把第一現場蓋掉"
    assert bad.read_bytes() == raw


def test_corrupt_stamp_is_not_silent(livedir, capsys):
    _corrupt_stamp(livedir)
    liveness.check_gap()
    assert "🚨" in capsys.readouterr().out, "戳記壞掉是靜默的——沒有任何人會發現"


def test_stamp_present_but_wrong_shape_is_unknown_not_first_boot(livedir):
    (livedir / "liveness.json").write_text('["not", "an", "object"]', encoding="utf-8")
    res = liveness.check_gap()
    assert res.get("unreadable") is True
    assert res["reason"] != "no_prior_stamp"


# --------------------------------------------------------------------------
# 2) 缺口帳本壞檔：不得被 read-modify-write 抹掉
# --------------------------------------------------------------------------
def test_record_gap_on_corrupt_ledger_does_not_overwrite(livedir):
    raw = b'[{"detected_at": 1.0, "last_ts": 0.0, "gap_sec": 7200.0}, {"detec'
    p = livedir / "liveness_gaps.json"
    p.write_bytes(raw)
    liveness.record_gap(100.0, 7200.0, now=200.0)
    assert p.read_bytes() == raw, (
        "壞檔那輪把整份帳本覆寫成只剩新增這一筆——最多 50 筆歷史缺口不可逆消失")


def test_record_gap_on_corrupt_ledger_reports_failure(livedir, capsys):
    (livedir / "liveness_gaps.json").write_bytes(b'[{"detected_at": 1.0')
    ok = liveness.record_gap(100.0, 7200.0, now=200.0)
    assert ok is False, "寫入被擋下卻回報成功＝呼叫端無從得知這筆缺口沒被記下"
    assert "🚨" in capsys.readouterr().out


def test_recent_gaps_corrupt_is_not_reported_as_none(livedir, capsys):
    (livedir / "liveness_gaps.json").write_bytes(b'[{"detected_at": 1.0')
    events, status = liveness.recent_gaps_status(86400, now=200.0)
    assert status == "unreadable", (
        "讀不出來被折成空清單 → CEO 日報會印「過去24h 無離線缺口」＝把未知講成保證")
    assert events == []
    assert "🚨" in capsys.readouterr().out


# --------------------------------------------------------------------------
# 3) stamp() 必須原子寫（壞檔的自產來源）
# --------------------------------------------------------------------------
def test_stamp_is_atomic(livedir, monkeypatch):
    calls = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace",
                        lambda a, b: (calls.append((a, b)), real_replace(a, b))[1])
    liveness.stamp({"scanned": 3})
    assert calls, ("stamp() 就地覆寫目的檔——斷電就在目的地留下半截 JSON，"
                   "而那正是上面所有誤判的自產來源")
    assert json.loads((livedir / "liveness.json").read_text(encoding="utf-8"))["scanned"] == 3


# --------------------------------------------------------------------------
# 4) 反向護欄：以下在**改動前的碼**上就必須是綠的
# --------------------------------------------------------------------------
def test_truly_missing_stamp_is_still_first_boot(livedir):
    res = liveness.check_gap()
    assert res["gap"] is False
    assert res["reason"] == "no_prior_stamp"
    assert res.get("unreadable") is not True


def test_fresh_stamp_within_threshold_unchanged(livedir):
    (livedir / "liveness.json").write_text(json.dumps({"ts": 1000.0}), encoding="utf-8")
    res = liveness.check_gap(threshold_sec=3600, now=1600.0)
    assert res["gap"] is False and res["reason"] == "within_threshold"
    assert res["gap_sec"] == 600.0


def test_old_stamp_beyond_threshold_still_alerts(livedir):
    (livedir / "liveness.json").write_text(json.dumps({"ts": 1000.0}), encoding="utf-8")
    res = liveness.check_gap(threshold_sec=3600, now=1000.0 + 7200)
    assert res["gap"] is True and res["reason"] == "threshold_exceeded"
    assert res["last_ts"] == 1000.0


def test_record_gap_on_missing_ledger_still_creates_it(livedir):
    liveness.record_gap(100.0, 7200.0, now=200.0)
    events = json.loads((livedir / "liveness_gaps.json").read_text(encoding="utf-8"))
    assert len(events) == 1 and events[0]["gap_sec"] == 7200.0


def test_gaps_ledger_still_capped(livedir):
    seed = [{"detected_at": float(i), "last_ts": 0.0, "gap_sec": 1.0} for i in range(60)]
    (livedir / "liveness_gaps.json").write_text(json.dumps(seed), encoding="utf-8")
    liveness.record_gap(100.0, 7200.0, now=200.0)
    events = json.loads((livedir / "liveness_gaps.json").read_text(encoding="utf-8"))
    assert len(events) == liveness._GAPS_KEEP
