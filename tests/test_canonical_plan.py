"""task#10 地基：telegram_bot/plan.py 單一權威層的 golden parity 測試。

核心契約（讓日後 STEP 2 把 message_format/intent_format 改成委派 plan.py 是「零回歸」的）：
    1. build_canonical_plan(dd, risk_usd=1.0) 與「活的」intent_format._compute_plan(dd)
       逐位元相同（dict 全等）。
    2. canonical_to_intent(build_canonical_plan(dd, risk_usd=1.0), dd) 與「活的」
       intent_format.to_trade_intent(dd) 逐位元相同（dict 全等）—— 證明搬家後的組裝器
       與現役函式 bit-identical。
    3. build_canonical_plan（預設 risk_usd）重現 message_format.render_fire_message
       卡片上的止損/進場區/止盈價字串（人看版同源）。
    4. deepdive：canonical_from_deepdive 用 LLM 絕對價、R 反推、缺漏 TP 壓縮重編號；
       串到 canonical_to_intent 能產出通過 validate_intent 的合法 intent。
    5. 紅線：auto_live 被擋；plan.py 永不碰真錢帳本 `trades`。
"""
from __future__ import annotations

import pathlib

import pytest

from botconfig import CONFIG
from telegram_bot import intent_format, message_format
from telegram_bot.plan import (
    ALLOWED_EXECUTION_MODES,
    build_canonical_plan,
    canonical_from_deepdive,
    canonical_to_intent,
)


def _decision(direction="bull", setup="intraday", symbol="BTC", price=64700.0):
    return {
        "direction": direction,
        "setup_name": setup,
        "composite_score": 2.31,
        "confirmed": [
            {"name": "cvd_divergence", "state": "bull",
             "evidence": {"divergence": "bull", "cvd_slope": 0.142}},
            {"name": "oi_trajectory", "state": "bull", "evidence": {}},
            {"name": "funding", "state": "neutral",
             "evidence": {"funding": 0.000042, "regime": "neutral"}},
        ],
        "snapshot": {
            "symbol": symbol, "price": price, "atr_pct_7d": 4.2,
            "strength_score": 78, "ts": 1_750_000_000_000,
        },
    }


_CASES = [
    ("bull", "intraday"),
    ("bear", "intraday"),
    ("bull", "ambush"),
    ("bear", "ambush"),
]


# ── 契約 1：build_canonical_plan == 活的 _compute_plan（逐位元）────────────────────
@pytest.mark.parametrize("direction,setup", _CASES)
def test_canonical_plan_matches_live_compute_plan(direction, setup):
    dd = _decision(direction, setup)
    # intent 口徑：risk_usd=1.0（_compute_plan 內部硬寫 1.0）
    got = build_canonical_plan(dd, risk_usd=1.0)
    want = intent_format._compute_plan(dd)
    assert got == want, f"{direction}/{setup}: canonical 與 _compute_plan 不一致"


@pytest.mark.parametrize("direction,setup", _CASES)
def test_entry_band_and_stop_exact(direction, setup):
    """進場區 ± 帶與止損與舊三處拷貝逐位元相同。"""
    dd = _decision(direction, setup)
    entry = dd["snapshot"]["price"]
    p = build_canonical_plan(dd, risk_usd=1.0)
    sl_pct = CONFIG.sl_pct(setup)
    exp_stop = (round(entry * (1 - sl_pct / 100), 6) if direction == "bull"
                else round(entry * (1 + sl_pct / 100), 6))
    assert p["stop"] == exp_stop
    band = {
        ("intraday", "bull"): (0.997, 1.002),
        ("intraday", "bear"): (0.998, 1.003),
        ("ambush", "bull"): (0.985, 1.000),
        ("ambush", "bear"): (1.000, 1.015),
    }[(setup, direction)]
    assert p["entry_low"] == round(entry * band[0], 6)
    assert p["entry_high"] == round(entry * band[1], 6)


# ── 契約 2：canonical_to_intent == 活的 to_trade_intent（逐位元）──────────────────
@pytest.mark.parametrize("direction,setup", _CASES)
def test_canonical_to_intent_matches_live(direction, setup):
    dd = _decision(direction, setup)
    got = canonical_to_intent(build_canonical_plan(dd, risk_usd=1.0), dd)
    want = intent_format.to_trade_intent(dd)
    assert got == want, f"{direction}/{setup}: 組裝後 intent 與現役 to_trade_intent 不一致"


def test_canonical_to_intent_equity_matches_live():
    dd = _decision("bull", "intraday", symbol="NVDA")
    got = canonical_to_intent(build_canonical_plan(dd, risk_usd=1.0), dd,
                              asset_class="equity_signal")
    want = intent_format.to_trade_intent(dd, asset_class="equity_signal")
    assert got == want
    assert got["margin_mode"] is None
    assert got["symbol_canonical"] == "NVDA"


# ── 契約 3：人看版（message_format 卡片）同源 ─────────────────────────────────────
@pytest.mark.parametrize("direction,setup", _CASES)
def test_message_card_uses_same_numbers(direction, setup):
    dd = _decision(direction, setup)
    text, _buttons = message_format.render_fire_message(dd)
    p = build_canonical_plan(dd)  # 預設 risk_usd = CONFIG.risk_per_trade_usd（卡片口徑）
    assert f"${p['stop']}" in text, "卡片止損價應與 canonical 一致"
    assert f"${p['entry_low']}" in text, "卡片進場區下緣應與 canonical 一致"
    assert f"${p['entry_high']}" in text, "卡片進場區上緣應與 canonical 一致"
    for i in (1, 2, 3):
        assert f"${p['tps'][f'tp{i}']}" in text, f"卡片 TP{i} 應與 canonical 一致"
    # sl_distance_pct 與 risk_usd 無關 → 卡片與 intent 口徑相同（零回歸的關鍵保證）
    assert p["sl_distance_pct"] == build_canonical_plan(dd, risk_usd=1.0)["sl_distance_pct"]


# ── 契約 4：deepdive 路徑 ────────────────────────────────────────────────────────
def test_deepdive_limit_bull_zone_and_R():
    llm = {"actionable": True, "direction": "bull", "entry_type": "limit",
           "entry": None, "entry_lo": 100.0, "entry_hi": 104.0, "stop": 95.0,
           "tp1": 110.0, "tp2": 115.0, "tp3": 120.0}
    c = canonical_from_deepdive("FOO", llm)
    assert c["setup"] == "deepdive"
    assert c["entry"] == 102.0          # (100+104)/2 中點
    assert c["entry_low"] == 100.0 and c["entry_high"] == 104.0
    risk = 102.0 - 95.0                 # 7
    assert c["tps"]["tp1"] == 110.0 and c["tps"]["tp1_r"] == round(8 / risk, 2)
    assert c["tps"]["tp2_r"] == round(13 / risk, 2)
    assert c["tps"]["tp3_r"] == round(18 / risk, 2)
    assert c["tp_r"] == (round(8 / risk, 2), round(13 / risk, 2), round(18 / risk, 2))
    assert c["sl_distance_pct"] == c["pos"]["sl_distance_pct"]


def test_deepdive_market_with_tp_gap_compacts():
    """market 進場 + tp2 缺 → 壓縮重編號（tp3 變 tp2），take_profits 不會 KeyError。"""
    llm = {"actionable": True, "direction": "bull", "entry_type": "market",
           "entry": 100.0, "entry_lo": None, "entry_hi": None, "stop": 96.0,
           "tp1": 108.0, "tp2": None, "tp3": 116.0}
    c = canonical_from_deepdive("FOO", llm)
    assert c["entry_low"] == 100.0 and c["entry_high"] == 100.0   # market → 單點
    assert set(c["tps"].keys()) == {"tp1", "tp1_r", "tp2", "tp2_r"}
    assert c["tps"]["tp1"] == 108.0 and c["tps"]["tp2"] == 116.0  # tp3 壓成 tp2
    assert len(c["tp_r"]) == 2


def test_deepdive_bear_direction_and_R():
    llm = {"actionable": True, "direction": "bear", "entry_type": "market",
           "entry": 200.0, "entry_lo": None, "entry_hi": None, "stop": 210.0,
           "tp1": 190.0, "tp2": 180.0, "tp3": None}
    c = canonical_from_deepdive("BAR", llm)
    assert c["direction"] == "bear"
    risk = abs(200.0 - 210.0)
    assert c["tps"]["tp1_r"] == round(10 / risk, 2)
    assert c["tps"]["tp2_r"] == round(20 / risk, 2)
    assert len(c["tp_r"]) == 2


def test_deepdive_degenerate_raises():
    for bad in (
        {"direction": "sideways", "entry": 1, "stop": 2, "tp1": 3},     # 方向非法
        {"direction": "bull", "entry": None, "entry_type": "market",
         "stop": 2, "tp1": 3},                                          # 缺 entry
        {"direction": "bull", "entry": 100.0, "stop": 100.0, "tp1": 110.0},  # entry==stop
    ):
        with pytest.raises(ValueError):
            canonical_from_deepdive("X", bad)


def test_deepdive_to_intent_is_valid():
    """deepdive canonical → intent 串通：產出通過 validate_intent 的合法 intent。"""
    llm = {"actionable": True, "direction": "bull", "entry_type": "limit",
           "entry": None, "entry_lo": 100.0, "entry_hi": 104.0, "stop": 95.0,
           "tp1": 110.0, "tp2": 115.0, "tp3": 120.0}
    c = canonical_from_deepdive("FOO", llm)
    dd = {
        "direction": "bull", "setup_name": "deepdive",
        "composite_score": None, "confirmed": [],
        "snapshot": {"symbol": "FOO", "price": 102.0, "ts": 1_750_000_000_000,
                     "strength_score": None},
    }
    intent = canonical_to_intent(c, dd)
    assert not intent_format.validate_intent(intent), "deepdive intent 應通過驗證"
    assert intent["side"] == "long"
    assert intent["symbol_canonical"] == "FOO-USDT"
    assert 1 <= intent["risk"]["suggested_leverage"] <= 50
    assert len(intent["take_profits"]) == len(c["tp_r"]) == 3
    assert intent["execution_policy"]["mode"] == "human_gated"
    assert "深度分析" in intent["rationale"]["narrative"]


# ── 契約 5：紅線 ─────────────────────────────────────────────────────────────────
def test_auto_live_blocked():
    dd = _decision("bull", "intraday")
    with pytest.raises(ValueError):
        canonical_to_intent(build_canonical_plan(dd, risk_usd=1.0), dd,
                            execution_policy="auto_live")
    assert "auto_live" not in ALLOWED_EXECUTION_MODES


def test_plan_module_never_writes_real_money_ledger():
    """plan.py 不得寫真錢帳本 trades（紅線①）：源碼不出現 trades 寫入痕跡。"""
    src = pathlib.Path(__file__).resolve().parents[1] / "telegram_bot" / "plan.py"
    text = src.read_text(encoding="utf-8")
    for forbidden in ("INSERT INTO trades", "record_entry", "trade_journal"):
        assert forbidden not in text, f"plan.py 不該出現 {forbidden!r}（碰真錢帳本）"
