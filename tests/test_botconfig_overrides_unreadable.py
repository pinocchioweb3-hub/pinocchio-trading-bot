"""botconfig 覆寫檔「壞掉/讀不到」不得折成「從來沒設過」— v196（監督員 r90）。

同物種第 16 次（未知被折成「確認沒有」）。這一處挖空的是**風險參數本身**：

    bot_settings.json 目前唯一內容 = RISK_PER_TRADE_PCT / TOTAL_RISK_CAP_PCT / SIGNAL_MODE
    而 .env **沒有** RISK_PER_TRADE_PCT、也沒有 TOTAL_RISK_CAP_PCT（實測）
    ⇒ 這個檔是那兩個鍵的**唯一**來源。

舊碼 _load_overrides() 把「檔不存在」與「檔在但讀不出來」折成同一個 {}，於是：
  * RISK_PER_TRADE_PCT 消失 → _f 回 0.0 → 落 `elif _is_set("RISK_PER_TRADE_USD")`
    → 1R **靜默**從「帳戶 %」模式切成 .env 的固定 USD 金額。畫面上與「使用者從來
    沒設過 %」一模一樣，零警告。
  * TOTAL_RISK_CAP_PCT 消失 → 落 tier 預設，總風險上限靜默改變。
  * 而後**任何一次** set_override 會把只剩一把鍵的 _OVERRIDES 整個覆寫回檔案
    ⇒ 其餘覆寫**永久滅失**，稽核軌跡還記成 before=null（等於謊稱「本來就沒設過」）。

根因與 v157/v162-v166/v195 同一支：set_override/revert_key 用 write_text 直接覆蓋
＝非原子，斷電或被殺在寫到一半留下的就是讀不出來的半截檔 ⇒ 本模組有能力親手做出
那個壞檔再自誤讀。

本檔每一條在舊碼上都必須是紅的（非虛設檢定）。
執行：pytest tests/test_botconfig_overrides_unreadable.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import botconfig as bc

_TMP = Path(tempfile.mkdtemp(prefix="botcfg_unreadable_"))
bc._SETTINGS_FILE = _TMP / "bot_settings.json"
bc._AUDIT_FILE = _TMP / "config_audit.jsonl"

# 線上實況的縮影：這三把鍵只存在於覆寫檔，.env 裡沒有
_REAL_SHAPE = {
    "RISK_PER_TRADE_PCT": 2.5,
    "TOTAL_RISK_CAP_PCT": 6.0,
    "SIGNAL_MODE": "balanced",
}


def _write_raw(text: str) -> None:
    bc._SETTINGS_FILE.write_text(text, encoding="utf-8")


def _clean() -> None:
    for f in (bc._SETTINGS_FILE, bc._AUDIT_FILE):
        if f and f.exists():
            f.unlink()
    bc._OVERRIDES = {}


def _corrupt(*, keep_audit: bool = False) -> str:
    """半截 JSON——正是非原子寫入被中斷會留下的東西。

    keep_audit=True：只弄壞覆寫檔，稽核軌跡（另一個檔）保持完好——revert_key 的
    情境需要它，否則會在「找不到歷史」就先回 False，根本走不到本物種的判斷點。
    """
    if keep_audit:
        bc._OVERRIDES = {}
        if bc._SETTINGS_FILE.exists():
            bc._SETTINGS_FILE.unlink()
    else:
        _clean()
    half = json.dumps(_REAL_SHAPE, ensure_ascii=False, indent=2)[: 40]
    _write_raw(half)
    return half


# --- 1. 檔不存在＝合法的第一次（保持安靜，不可誤報成故障）---
def test_missing_file_is_not_a_fault():
    _clean()
    bc._load_overrides()
    assert bc.overrides_load_error() == "missing"
    assert bc.overrides_unreadable() is None      # missing 不是故障
    assert bc._OVERRIDES == {}


# --- 2. 檔在但讀不出來＝故障，必須說得出來（舊碼：靜默回 {}）---
def test_corrupt_file_is_reported_as_fault():
    half = _corrupt()
    bc._load_overrides()
    err = bc.overrides_unreadable()
    assert err is not None, "半截 JSON 被折成『沒設過』——這正是本物種"
    assert err != "missing"
    # ⛔ 不可自作主張把壞檔清掉或重寫；原始內容必須原封不動留著供查證
    assert bc._SETTINGS_FILE.read_text(encoding="utf-8") == half


# --- 3. 合法 JSON 但不是物件（例如被寫成 list）也是故障 ---
def test_not_a_dict_is_a_fault():
    _clean()
    _write_raw("[1, 2, 3]")
    bc._load_overrides()
    assert bc.overrides_unreadable() == "NotADict"


# --- 4. 讀不出來時 set_override 必須 fail-closed（舊碼：照寫＝永久滅失其餘鍵）---
def test_set_override_refuses_when_overrides_unreadable():
    half = _corrupt()
    with pytest.raises(RuntimeError):
        bc.set_override("RISK_PER_TRADE_PCT", 1.0, source="human")
    # 關鍵：壞檔沒有被「只剩一把鍵」的新檔蓋掉——其餘覆寫仍有救回的可能
    assert bc._SETTINGS_FILE.read_text(encoding="utf-8") == half


# --- 5. 拒寫要留痕（不可靜靜地什麼都沒發生）---
def test_refusal_leaves_audit_trail():
    _corrupt()
    with pytest.raises(RuntimeError):
        bc.set_override("SIGNAL_MODE", "aggressive", source="human")
    lines = [l for l in bc._AUDIT_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    recs = [json.loads(l) for l in lines]
    assert any(str(r.get("source", "")).startswith("blocked") for r in recs), \
        "拒寫沒有進稽核軌跡＝事件不可見"
    # ⛔ 稽核軌跡不可謊稱 before=null（那等於說『本來就沒設過』）
    blocked = [r for r in recs if str(r.get("source", "")).startswith("blocked")]
    assert all(r.get("before") != None for r in blocked)  # noqa: E711


# --- 6. revert_key 同樣 fail-closed ---
def test_revert_key_refuses_when_overrides_unreadable():
    _clean()
    bc.set_override("REVK", "A", source="human")
    bc.set_override("REVK", "B", source="human")
    half = _corrupt(keep_audit=True)   # 覆寫檔壞掉，但稽核軌跡還在
    with pytest.raises(RuntimeError):
        bc.revert_key("REVK", source="human")
    assert bc._SETTINGS_FILE.read_text(encoding="utf-8") == half


# --- 7. 寫入必須原子（tmp + os.replace），且不留殘骸 ---
def test_write_is_atomic_and_leaves_no_tmp(monkeypatch):
    _clean()
    calls: list[tuple] = []
    real_replace = bc.os.replace

    def _spy(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(bc.os, "replace", _spy)
    bc.set_override("RISK_PER_TRADE_PCT", 2.5, source="human")
    # 舊碼 write_text 直接覆蓋＝非原子，這裡會是 0 次 ⇒ 紅
    assert calls, "覆寫檔不是用 os.replace 落地＝非原子，本模組有能力親手做出半截檔"
    assert calls[-1][1] == str(bc._SETTINGS_FILE)
    leftovers = [p.name for p in _TMP.iterdir() if ".tmp" in p.name]
    assert leftovers == [], f"殘留半成品檔：{leftovers}"
    assert json.loads(bc._SETTINGS_FILE.read_text(encoding="utf-8"))["RISK_PER_TRADE_PCT"] == 2.5


# --- 8. 寫入失敗不可靜默吞掉（舊碼 except: pass ⇒ 呼叫端以為成功、重啟後值不見）---
def test_persist_failure_is_not_swallowed(monkeypatch):
    _clean()

    def _boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(bc.os, "replace", _boom)
    with pytest.raises(RuntimeError):
        bc.set_override("RISK_PER_TRADE_PCT", 3.0, source="human")


# --- 9. 乾淨路徑零行為改變（回歸護欄）---
def test_clean_path_unchanged():
    _clean()
    bc._SETTINGS_FILE.write_text(json.dumps(_REAL_SHAPE, ensure_ascii=False), encoding="utf-8")
    bc._load_overrides()
    assert bc.overrides_unreadable() is None
    assert bc.overrides_load_error() is None
    assert bc._raw("RISK_PER_TRADE_PCT") == "2.5"
    assert bc._raw("SIGNAL_MODE") == "balanced"


# --- 10. 空物件是合法的「設過但全清空」，不是故障 ---
def test_empty_object_is_clean():
    _clean()
    bc._SETTINGS_FILE.write_text("{}", encoding="utf-8")
    bc._load_overrides()
    assert bc.overrides_unreadable() is None
    assert bc._OVERRIDES == {}
