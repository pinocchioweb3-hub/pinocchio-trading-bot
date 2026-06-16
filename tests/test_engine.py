"""L2 引擎煙霧測試。

執行方式（任一）：
    pytest tests/                    # 用 pytest
    python tests/test_engine.py      # 直接跑（無需 pytest）

每個 case 斷言 action / direction / reason 大致符合預期。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 讓 `python tests/test_engine.py` 也找得到 l2_trigger 套件
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from l2_trigger.configs.ambush import AMBUSH_DEFAULT
from l2_trigger.configs.intraday import INTRADAY_SUI
from l2_trigger.cooldown import CooldownStore
from l2_trigger.engine import evaluate
from l2_trigger.leverage import (
    LEVERAGE_OVERRIDES,
    choose_leverage,
    compute_position,
    compute_tp_prices,
)
from l2_trigger.types import SignalState, TriggerAction
from tests import fixtures as F


# =============================================================================
# Setup A (intraday) tests
# =============================================================================
def test_setup_a_fire_bull():
    d = evaluate(F.sui_intraday_fire_bull(), INTRADAY_SUI)
    assert d.action == TriggerAction.FIRE, f"expected FIRE, got {d.action}: {d.reason}"
    assert d.direction == SignalState.BULL, f"expected BULL, got {d.direction}"
    assert d.setup_name == "intraday"
    # composite_score 應為正且 >= 1.0（三個 BULL 訊號加總）
    assert d.composite_score >= 1.0, f"score too low: {d.composite_score}"
    # reason 含三個訊號名
    for name in ("cvd_divergence", "funding", "large_holder"):
        assert name in d.reason, f"reason missing {name}: {d.reason}"


def test_setup_a_fire_bear():
    d = evaluate(F.sui_intraday_fire_bear(), INTRADAY_SUI)
    assert d.action == TriggerAction.FIRE, f"expected FIRE, got {d.action}: {d.reason}"
    assert d.direction == SignalState.BEAR
    assert d.composite_score <= -1.0


def test_setup_a_hold_btc_gate_closed():
    d = evaluate(F.hold_btc_gate_closed(), INTRADAY_SUI)
    assert d.action == TriggerAction.HOLD
    assert "btc_gate_closed" in d.reason


def test_setup_a_hold_not_hot():
    d = evaluate(F.hold_not_hot(), INTRADAY_SUI)
    assert d.action == TriggerAction.HOLD
    assert "filter_failed:in_hot" in d.reason


def test_setup_a_hold_no_oi_fuel():
    d = evaluate(F.hold_no_oi_fuel(), INTRADAY_SUI)
    assert d.action == TriggerAction.HOLD
    assert "oi_fuel_insufficient" in d.reason


def test_setup_a_hold_mixed_votes():
    d = evaluate(F.hold_mixed_votes(), INTRADAY_SUI)
    assert d.action == TriggerAction.HOLD
    assert "votes_insufficient" in d.reason


def test_setup_a_fires_despite_stale_funding():
    """funding 缺料 → eval_funding 回 STALE 不計票；
    cvd + large_holder 兩個 BULL 仍 >=2 → FIRE BULL。"""
    d = evaluate(F.fire_bull_stale_funding(), INTRADAY_SUI)
    assert d.action == TriggerAction.FIRE, f"expected FIRE despite stale funding, got: {d.reason}"
    assert d.direction == SignalState.BULL


def test_setup_a_hold_stale_btc_gate():
    """BTC 閘 STALE → 整包 HOLD（這比 funding 嚴格，因為閘是門檻）。"""
    d = evaluate(F.hold_stale_btc_gate(), INTRADAY_SUI)
    assert d.action == TriggerAction.HOLD
    assert "btc_gate_stale" in d.reason


# =============================================================================
# Setup B (ambush) tests
# =============================================================================
def test_setup_b_fire_bull():
    d = evaluate(F.arb_ambush_fire_bull(), AMBUSH_DEFAULT)
    assert d.action == TriggerAction.FIRE, f"expected FIRE, got {d.action}: {d.reason}"
    assert d.direction == SignalState.BULL
    assert d.setup_name == "ambush"
    for name in ("cvd_silent_accumulation", "large_holder_creeping"):
        assert name in d.reason, f"reason missing {name}: {d.reason}"


def test_setup_b_hold_no_pattern():
    d = evaluate(F.ambush_hold_no_pattern(), AMBUSH_DEFAULT)
    assert d.action == TriggerAction.HOLD
    assert "filter_failed:higher_lows" in d.reason


def test_setup_b_hold_high_volatility():
    d = evaluate(F.ambush_hold_high_volatility(), AMBUSH_DEFAULT)
    assert d.action == TriggerAction.HOLD
    assert "filter_failed:atr_coiling" in d.reason


# =============================================================================
# Leverage / position calc tests
# =============================================================================
def test_leverage_wlfi_always_5x():
    assert choose_leverage("WLFI", atr_pct_7d=2.0) == 5
    assert choose_leverage("WLFI", atr_pct_7d=None) == 5


def test_leverage_atr_tiers():
    # 低波動 → 回傳「傳入的 default」。顯式帶 default=15 讓本測試不依賴 .env 的
    # DEFAULT_LEVERAGE（v42 後未設定時的保守 tier 預設是 5x；正式部署 .env 設 15
    # 仍得 15x，行為未變）。這裡驗的是「低波動走 default 分支」這條 tier 邏輯。
    assert choose_leverage("ETH", atr_pct_7d=2.5, default=15) == 15   # 低波動 → default
    assert choose_leverage("SOL", atr_pct_7d=6.0) == 10   # 中波動
    assert choose_leverage("SUI", atr_pct_7d=9.5) == 5    # 高波動
    assert choose_leverage("UNKNOWN", atr_pct_7d=None) == 5  # 缺料保守


def test_position_calc_sui_bull():
    """SUI entry 3.45 stop 3.31 risk 100 USD lev 15x"""
    p = compute_position(entry=3.45, stop=3.31, risk_usd=100.0, leverage=15)
    # sl_distance = 0.14, notional = 100/0.14 * 3.45 ≈ 2464
    assert 2400 < p["notional_usd"] < 2500, p
    # margin = notional/15 ≈ 164
    assert 160 < p["margin_usd"] < 170, p
    # sl_distance% ≈ 4.06
    assert 4.0 < p["sl_distance_pct"] < 4.1, p


def test_position_calc_wlfi_5x():
    """WLFI 5x: 同樣 100 U 風險 → 保證金應為 15x 的 3 倍"""
    p15 = compute_position(entry=1.0, stop=0.95, risk_usd=100.0, leverage=15)
    p5 = compute_position(entry=1.0, stop=0.95, risk_usd=100.0, leverage=5)
    assert abs(p5["margin_usd"] - p15["margin_usd"] * 3) < 0.1, (p15, p5)


def test_tp_prices_bull():
    tps = compute_tp_prices(entry=3.45, stop=3.31, direction="bull",
                            r_multiples=(1.0, 1.5, 2.0))
    # sl_distance = 0.14
    assert abs(tps["tp1"] - 3.59) < 0.001, tps
    assert abs(tps["tp2"] - 3.66) < 0.001, tps
    assert abs(tps["tp3"] - 3.73) < 0.001, tps


def test_tp_prices_bear():
    tps = compute_tp_prices(entry=3.95, stop=4.05, direction="bear",
                            r_multiples=(1.0, 1.5, 2.0))
    # sl_distance = 0.10
    assert abs(tps["tp1"] - 3.85) < 0.001, tps
    assert abs(tps["tp2"] - 3.80) < 0.001, tps
    assert abs(tps["tp3"] - 3.75) < 0.001, tps


# =============================================================================
# Cooldown tests
# =============================================================================
def test_cooldown_blocks_repeat_fire():
    store = CooldownStore(cooldown_seconds=3600)
    d = evaluate(F.sui_intraday_fire_bull(), INTRADAY_SUI)
    assert store.should_emit(d, now=1000.0)
    store.mark_fired(d, now=1000.0)
    # 5 分鐘後再次 FIRE → 應被擋
    assert not store.should_emit(d, now=1000.0 + 300)
    # 1 小時後 → 解禁
    assert store.should_emit(d, now=1000.0 + 3600)


def test_cooldown_allows_different_direction():
    """同 symbol 但方向不同（或 setup 不同）→ 不互相擋"""
    store = CooldownStore(cooldown_seconds=3600)
    bull = evaluate(F.sui_intraday_fire_bull(), INTRADAY_SUI)
    bear = evaluate(F.sui_intraday_fire_bear(), INTRADAY_SUI)
    store.mark_fired(bull, now=1000.0)
    assert store.should_emit(bear, now=1000.0 + 60), "BEAR should not be cooled by BULL fire"


# =============================================================================
# 手動執行（無 pytest 也能跑）
# =============================================================================
def _run_all() -> int:
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    passed, failed = 0, 0
    failures: list[tuple[str, str]] = []
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failures.append((t.__name__, str(e)))
            failed += 1
        except Exception as e:
            print(f"  ERR   {t.__name__}: {type(e).__name__}: {e}")
            failures.append((t.__name__, f"{type(e).__name__}: {e}"))
            failed += 1
    print(f"\n=== {passed} passed, {failed} failed ===")
    if failures:
        print("\nFailures:")
        for name, err in failures:
            print(f"  - {name}: {err}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_all())
