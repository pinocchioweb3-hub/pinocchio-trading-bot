"""信任頁資料源「壞掉/讀不到」不得折成「不存在／尚未驗證」— v201（監督員 r95）。

同物種第 21 次（未知被折成「確認沒有」）。這一處的下場**不在流程**、在對外呈現：

  * verification()：舊碼 `_read_json(...) or {}`。gates／honest_backtest_conclusion_zh／
    note_on_metrics **只有** verification-status.json 這一個來源，而 paper_progress /
    live_progress 走的是另外兩個來源（paper_journal、trade_journal.db）。
    ⇒ 這個檔一壞，回傳變成「gates=[] + 全部誠實聲明 None + **沒有 error 旗標**
    + 筆數照送」。前端 platform/web/views/trust.js 只在 `v.error` 時才提早返回，
    否則會照常畫出「紙上交易筆數 369」「實倉交易筆數」兩條進度條——
    對外只剩**數字**，少掉「這是紙上交易／尚未證實」那段限定語。紅線③的形狀。
  * project_facts()：舊碼把讀失敗回成 NOT_FOUND「project-facts.json 不存在」
    ＝對讀失敗說謊。壞檔要修、缺檔要補，處置不同，訊息不能一樣。

根因與 v157／v162-v166／v195-v200 同一支：讀取端把所有例外收斂成一個「空值」，
呼叫端再把空值讀成「本來就沒有」。

本檔每一條在舊碼上都必須是紅的（非虛設檢定）。
執行：pytest tests/test_trust_source_unreadable.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_API = ROOT / "platform" / "api"
for _p in (str(ROOT), str(_API)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

if not (_API / "data_access.py").exists():
    # platform/ 這棵子樹目前**整棵未納版控**（git status: `?? platform/`），
    # 新 clone 不會有它 → 這裡跳過而不是讓整份測試 collection error。
    pytest.skip("platform/api/data_access.py 不在此工作區（該子系統未納版控）",
                allow_module_level=True)

import data_access as da  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="trust_unreadable_"))
da._TRUST_DATA = _TMP

_VERIF = _TMP / "verification-status.json"
_FACTS = _TMP / "project-facts.json"

_GOOD_VERIF = {
    "phase": "Phase 0",
    "is_promoted_for_public": False,
    "note_on_metrics": "以下皆為紙上交易樣本，非真實績效。",
    "honest_backtest_conclusion_zh": "扣費後未證實有 edge。",
    "gates": [{"id": "live30", "label_zh": "真實 30 筆",
               "target": 30, "status": "pending", "status_zh": "未達標",
               "current": 0}],
    "verify_how": "自行重跑回測腳本。",
}


@pytest.fixture(autouse=True)
def _stub_progress(monkeypatch):
    """筆數進度另有來源，本檔不測它——固定成非零，好讓「數字照送」看得出來。"""
    monkeypatch.setattr(da, "_paper_progress", lambda: {
        "n_closed": 369, "n_open": 3, "target": 100, "enough": True})
    monkeypatch.setattr(da, "_live_progress", lambda: {
        "n_closed": 0, "target": 30, "note_zh": "尚未開始實倉"})
    yield


def _clear():
    for p in (_VERIF, _FACTS):
        if p.exists():
            p.unlink()


# ── _read_json_status：三態必須分得開 ────────────────────────────────────────
def test_status_missing_vs_unreadable_vs_ok():
    _clear()
    assert da._read_json_status(_VERIF) == (None, "missing")

    _VERIF.write_text('{"phase": "Phase 0"', encoding="utf-8")  # 半截檔
    data, st = da._read_json_status(_VERIF)
    assert st == "unreadable" and data is None

    _VERIF.write_text("[1, 2, 3]", encoding="utf-8")  # 合法 JSON 但不是 dict
    assert da._read_json_status(_VERIF)[1] == "unreadable"

    _VERIF.write_text(json.dumps(_GOOD_VERIF), encoding="utf-8")
    data, st = da._read_json_status(_VERIF)
    assert st == "ok" and data["phase"] == "Phase 0"


# ── verification()：壞檔 → 整塊 fail-closed，⛔ 不得只掉誠實聲明卻照送數字 ──
def test_verification_unreadable_is_error_not_silent_empty():
    _clear()
    _VERIF.write_text('{"gates": [{"id": "live30"', encoding="utf-8")
    out = da.verification()

    assert out.get("error") is True, "壞檔必須帶 error 旗標（舊碼沒有）"
    assert out.get("code") == "SOURCE_UNREADABLE"
    assert "不等於" in (out.get("message") or ""), "訊息要講明這不是『尚未驗證』"

    # 關鍵：誠實聲明讀不到時，數字一顆都不准漏出去。
    assert "paper_progress" not in out
    assert "live_progress" not in out
    assert "gates" not in out


def test_verification_missing_is_error_too():
    """缺檔同樣不可只送數字——是 NOT_FOUND 不是 SOURCE_UNREADABLE。"""
    _clear()
    out = da.verification()
    assert out.get("error") is True
    assert out.get("code") == "NOT_FOUND"
    assert "paper_progress" not in out and "live_progress" not in out


def test_verification_ok_still_works_and_never_leaks_current():
    _clear()
    _VERIF.write_text(json.dumps(_GOOD_VERIF), encoding="utf-8")
    out = da.verification()

    assert not out.get("error")
    assert out["phase"] == "Phase 0"
    assert out["note_on_metrics"]
    assert out["honest_backtest_conclusion_zh"]
    assert out["paper_progress"]["n_closed"] == 369
    assert out["live_progress"]["n_closed"] == 0
    # 既有鐵則：gates 永不輸出 current 數值。
    assert out["gates"] and all("current" not in g for g in out["gates"])


# ── project_facts()：壞檔不得謊稱「不存在」 ────────────────────────────────
def test_project_facts_unreadable_is_not_reported_as_missing():
    _clear()
    _FACTS.write_text('{"identity": {', encoding="utf-8")
    out = da.project_facts()

    assert out.get("error") is True
    assert out.get("code") == "SOURCE_UNREADABLE", "舊碼在這裡回 NOT_FOUND"
    assert "不存在" not in (out.get("message") or "").replace(
        "這不等於『不存在』", ""), "訊息不得宣稱檔案不存在"


def test_project_facts_missing_stays_not_found():
    _clear()
    out = da.project_facts()
    assert out.get("error") is True and out.get("code") == "NOT_FOUND"


def test_project_facts_empty_object_is_not_missing():
    """`{}` 是合法內容（欄位空），不是『檔案不存在』——舊碼 `if not data` 會折錯。"""
    _clear()
    _FACTS.write_text("{}", encoding="utf-8")
    out = da.project_facts()
    assert out.get("error") is not True, "空 JSON 物件被誤報成 NOT_FOUND"
    assert out["identity"] is None


def test_project_facts_ok():
    _clear()
    _FACTS.write_text(json.dumps({
        "identity": {"name": "皮諾丘"},
        "red_lines": ["不代客下單"],
        "secret_field": "不該被帶出去",
    }), encoding="utf-8")
    out = da.project_facts()
    assert out["identity"] == {"name": "皮諾丘"}
    assert out["red_lines"] == ["不代客下單"]
    assert "secret_field" not in out
