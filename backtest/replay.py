"""主回放迴圈：吃 HistoryPoint 序列 → 跑 L2 → FIRE 時模擬交易 → 收集 outcome。"""
from __future__ import annotations

from dataclasses import dataclass

from l2_trigger.cooldown import CooldownStore
from l2_trigger.engine import evaluate
from l2_trigger.leverage import choose_leverage, compute_tp_prices
from l2_trigger.types import MarketSnapshot, TriggerAction, TriggerConfig

from .historical import HistoryPoint
from .simulator import TradeOutcome, simulate


def _to_snapshot(p: HistoryPoint) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=p.symbol, ts=p.ts, price=p.price, tf="1h",
        oi=p.oi, oi_delta_pct=p.oi_delta_pct,
        funding=p.funding, funding_predicted=p.funding_predicted,
        cvd=p.cvd, cvd_slope=p.cvd_slope,
        cvd_price_divergence=p.cvd_price_divergence,
        ls_ratio=p.ls_ratio, top_trader_ratio=p.top_trader_ratio,
        liq_long=p.liq_long, liq_short=p.liq_short,
        btc_gate_open=p.btc_gate_open, btc_regime=p.btc_regime,
        above_4h_200ma=p.above_4h_200ma,
        is_hot=p.is_hot, strength_score=p.strength_score,
        atr_pct_7d=p.atr_pct_7d, vol_24h_vs_30d=p.vol_24h_vs_30d,
        cvd_slope_7d=p.cvd_slope_7d,
        top_trader_slope_7d=p.top_trader_slope_7d,
        oi_delta_7d_pct=p.oi_delta_7d_pct,
        higher_lows_7d=p.higher_lows_7d,
    )


@dataclass
class FireEvent:
    """記錄 FIRE 當下的訊號詳情 + tag（事件對應）"""
    ts: int
    setup_name: str
    direction: str
    snapshot_tag: str
    reason: str


def run(
    history: list[HistoryPoint],
    config: TriggerConfig,
    *,
    cooldown_hours: int = 4,
    future_window_hours: int = 48,
) -> tuple[list[TradeOutcome], list[FireEvent]]:
    """跑回放，回 (trades, fires)。

    trades 包含模擬完成的 outcome；fires 包含所有 FIRE 事件（含被冷卻擋掉的）。
    """
    cooldown = CooldownStore(cooldown_seconds=cooldown_hours * 3600)
    trades: list[TradeOutcome] = []
    fires: list[FireEvent] = []

    for idx, point in enumerate(history):
        snap = _to_snapshot(point)
        decision = evaluate(snap, config)
        if decision.action != TriggerAction.FIRE:
            continue

        fires.append(FireEvent(
            ts=point.ts, setup_name=decision.setup_name,
            direction=decision.direction.value,
            snapshot_tag=point.event_tag,
            reason=decision.reason,
        ))

        # 冷卻：上一次 FIRE 還沒過冷卻期就略過實際交易（但 fire 仍記錄）
        if not cooldown.should_emit(decision, now=point.ts / 1000.0):
            continue
        cooldown.mark_fired(decision, now=point.ts / 1000.0)

        # === 算進場/止損/TP ===
        lev = choose_leverage(point.symbol, point.atr_pct_7d)
        direction = decision.direction.value
        entry = point.price
        sl_pct = config.sl_buffer_pct / 100
        stop = entry * (1 - sl_pct) if direction == "bull" else entry * (1 + sl_pct)
        tp_prices = compute_tp_prices(entry, stop, direction, config.tp_r_multiples)
        tps = (tp_prices["tp1"], tp_prices["tp2"], tp_prices["tp3"])

        # === 取未來價格序列 ===
        future = [(history[j].ts, history[j].price)
                  for j in range(idx + 1, min(idx + 1 + future_window_hours, len(history)))]

        outcome = simulate(
            symbol=point.symbol, setup_name=config.setup_name,
            direction=direction, entry_ts=point.ts,
            entry_price=entry, stop=stop, tps=tps,
            future_prices=future,
            hold_max_hours=config.hold_max_hours,
        )
        trades.append(outcome)

    return trades, fires
