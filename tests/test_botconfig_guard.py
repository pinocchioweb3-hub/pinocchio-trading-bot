"""botconfig 設定寫入路徑測試 — v56（使用者 2026-06-20 移除 step0 寫入鎖後更新）。

政策變更：原 step0 寫入鎖（auto 只准寫 SHADOW_、活鍵 fail-closed）被使用者否決為
「有的沒的安全鎖」並要求移除——理由是現在跑模擬盤、只寫影子＝改得好看卻永不生效。
現行政策：自動優化器可**直接寫活鍵**讓優化即時生效；把關靠統計嚴謹度而非人工逐次點頭。
紅線①不受影響（在執行層把關，config 活鍵到不了真錢；三票對抗驗證 refuted=0）。
保留的不變量（本檔驗證）：
  • source='human' 與 source='auto' 都能寫任何鍵（含活鍵）——無 fail-closed 拒寫。
  • SHADOW_* 仍隔離於熱路徑（get_str/_raw 讀不到，只有 get_shadow 讀得到）——選用暫存區。
  • 每次寫入留稽核軌跡（before/after/source/git_sha）＝透明非閘；revert_key 可人工回滾。

執行：pytest tests/test_botconfig_guard.py  或  python tests/test_botconfig_guard.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import botconfig as bc

# 設定檔與稽核檔指到臨時區，與正式設定完全隔離（import 後改 module 全域）
_TMP = Path(tempfile.mkdtemp(prefix="botcfg_guard_"))
bc._SETTINGS_FILE = _TMP / "bot_settings.json"
bc._AUDIT_FILE = _TMP / "config_audit.jsonl"


def _fresh():
    for f in (bc._SETTINGS_FILE, bc._AUDIT_FILE):
        if f and f.exists():
            f.unlink()
    bc._OVERRIDES = {}


# --- 1. human 可寫任何活鍵（不拋） ---
def test_human_can_write_live_key():
    _fresh()
    bc.set_override("TEST_LIVE_KEY", "123", source="human")
    assert bc._raw("TEST_LIVE_KEY") == "123"


# --- 2. auto 可直接寫活鍵（鎖已移除）+ 留稽核軌跡（透明非閘）---
def test_auto_can_write_live_key():
    _fresh()
    # auto 寫活鍵不再拋例外，且真的寫進去（讓優化即時生效）
    bc.set_override("DEFAULT_LEVERAGE", 50, source="auto")
    assert bc._raw("DEFAULT_LEVERAGE") == "50"
    # 預設 source=auto 亦可寫
    bc.set_override("RISK_PER_TRADE_PCT", "2.5")
    assert bc._raw("RISK_PER_TRADE_PCT") == "2.5"
    # 每筆 auto 寫入都留稽核軌跡（透明：每日報告可浮現、可事後 revert）
    lines = [l for l in bc._AUDIT_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    keys_written = {json.loads(l)["key"] for l in lines}
    assert {"DEFAULT_LEVERAGE", "RISK_PER_TRADE_PCT"} <= keys_written


# --- 3. auto 寫 SHADOW_ 鍵 → 允許 ---
def test_auto_write_shadow_allowed():
    _fresh()
    bc.set_override("SHADOW_SL_PCT", "3.5", source="auto")   # 不應拋
    assert bc.get_shadow("SHADOW_SL_PCT") == "3.5"


# --- 4. SHADOW_ 鍵被熱路徑隔離：get_str / _raw 讀不到，只有 get_shadow 讀得到 ---
def test_shadow_isolated_from_hot_path():
    _fresh()
    bc.set_override("SHADOW_RISK", "999", source="auto")
    assert bc._raw("SHADOW_RISK") is None
    assert bc.get_str("SHADOW_RISK", "DEFAULT") == "DEFAULT"
    assert bc.get_shadow("SHADOW_RISK") == "999"


# --- 5. 每次寫入留稽核軌跡（含 source / git_sha / ts_ms）---
def test_audit_appended():
    _fresh()
    bc.set_override("SHADOW_X", "1", source="auto")
    bc.set_override("TEST_LIVE_KEY", "2", source="human")
    assert bc._AUDIT_FILE.exists()
    lines = [l for l in bc._AUDIT_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) >= 2, f"應有 ≥2 行稽核，實得 {len(lines)}"
    rec = json.loads(lines[-1])
    assert rec["key"] == "TEST_LIVE_KEY" and rec["source"] == "human"
    assert "git_sha" in rec and "ts_ms" in rec and "before" in rec and "after" in rec


# --- 6. revert_key 還原成上一次寫入前的值 ---
def test_revert_restores_previous():
    _fresh()
    bc.set_override("REVERT_K", "A", source="human")
    bc.set_override("REVERT_K", "B", source="human")
    assert bc._raw("REVERT_K") == "B"
    ok = bc.revert_key("REVERT_K", source="human")
    assert ok is True
    assert bc._raw("REVERT_K") == "A", f"revert 應還原成 A，實得 {bc._raw('REVERT_K')}"


# --- 7. revert_key 非人工 → 拒 ---
def test_revert_human_only():
    _fresh()
    raised = False
    try:
        bc.revert_key("X", source="auto")
    except PermissionError:
        raised = True
    assert raised, "revert_key 非人工應拋 PermissionError"


# --- 8. get_shadow 拒讀非 SHADOW_ 鍵（防誤把影子當活鍵用）---
def test_get_shadow_rejects_non_shadow():
    _fresh()
    raised = False
    try:
        bc.get_shadow("DEFAULT_LEVERAGE")
    except ValueError:
        raised = True
    assert raised, "get_shadow 對非 SHADOW_ 鍵應拋 ValueError"


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
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
