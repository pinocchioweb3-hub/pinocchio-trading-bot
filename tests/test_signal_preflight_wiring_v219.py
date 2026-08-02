"""訊號預檢閘接線測試（v219）── 閘算出來了，但有沒有真的接進出卡路徑？

⚠️ 這兩支是本次的**真檢定**：純函式測試在改動前也會綠（新模組本來就照它自己的規格寫），
   只有這裡會在改動前的 synthesizer 上紅——桶數沒印進 prompt、actionable 沒被程式端擋。
"""
from __future__ import annotations

import asyncio

from l3_dispatcher import synthesizer as sy


_DARK_STATE = {                      # 數據面四桶全暗、型態面有料
    "coinglass": {},
    "snapshot": {"price": 1.23},
    "pattern": {"consensus": "bull"},
    "smc_levels": {"4h": {"current_price": 1.23, "swing_points": []}},
}
_LIT_STATE = {
    "coinglass": {"oi": [1, 2], "funding": 0.0001, "ls_ratio": 1.1, "cvd": [3, 4]},
    "snapshot": {"price": 1.23},
    "pattern": {"consensus": "bull"},
    "smc_levels": {"4h": {"current_price": 1.23, "swing_points": []}},
}


def test_prompt_carries_the_bucket_ledger():
    """模式規則講「≥2 桶」，prompt 裡就必須有那個數字。"""
    txt = sy._format_symbol_data("XRP", _LIT_STATE)
    assert "訊號預檢閘" in txt, "桶數總帳沒進 prompt＝門檻仍靠目測"
    assert "4/4" in txt


def test_prompt_states_forced_block_when_all_dark():
    txt = sy._format_symbol_data("XRP", _DARK_STATE)
    assert "0/4" in txt
    assert "actionable=false" in txt


def _run(coro):
    return asyncio.run(coro)


def test_actionable_is_forced_false_in_code_not_just_prompt(monkeypatch):
    """LLM 不聽話時，程式端必須擋下來——prompt 是請求，不是把關。"""
    canned = ('看多。\n===PLAN_JSON===\n{"actionable": true, "direction": "bull", '
              '"entry": 1.0, "stop": 0.9, "tp1": 1.2}\n===END_PLAN===')

    async def _fake(system_prompt, user_data, timeout_sec=180):
        return canned, {"model": "test"}

    monkeypatch.setattr(sy, "_synthesize_with_prompt", _fake)
    text, meta = _run(sy.synthesize_per_symbol("XRP", _DARK_STATE))

    assert meta["plan"]["actionable"] is False, "閘擋了但 actionable 還是 true"
    assert meta["plan"].get("preflight_downgraded") is True
    assert meta.get("preflight", {}).get("data_n") == 0
    assert "訊號預檢閘" in text, "降級必須看得見，靜靜改掉＝另一種無聲折疊"


def test_passing_gate_leaves_plan_untouched(monkeypatch):
    canned = ('看多。\n===PLAN_JSON===\n{"actionable": true, "direction": "bull", '
              '"entry": 1.0, "stop": 0.9, "tp1": 1.2}\n===END_PLAN===')

    async def _fake(system_prompt, user_data, timeout_sec=180):
        return canned, {"model": "test"}

    monkeypatch.setattr(sy, "_synthesize_with_prompt", _fake)
    text, meta = _run(sy.synthesize_per_symbol("XRP", _LIT_STATE))

    assert meta["plan"]["actionable"] is True
    assert "preflight_downgraded" not in meta["plan"]
    assert meta["preflight"]["data_n"] == 4
