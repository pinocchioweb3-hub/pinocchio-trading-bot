"""交易模擬：給定 FIRE 時點 + 後續價格序列 → 算 outcome。

規則（與規格 + Telegram 訊息一致）：
    倉位分 3 份（TP1/TP2/TP3 各 1/3）
    TP1 觸及：平 1/3，剩餘止損移到開倉價（breakeven）
    TP2 觸及：再平 1/3
    TP3 觸及：平最後 1/3（或 trail，這裡簡化為直接出）
    Stop 觸及：剩餘部位全平
    Timeout (hold_max_hours)：剩餘部位以最後價平倉
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TradeOutcome:
    symbol: str
    setup_name: str
    direction: str            # "bull" | "bear"
    entry_ts: int
    entry_price: float
    stop: float
    tps: tuple[float, ...]    # (tp1, tp2, tp3)

    # 結果欄位（simulator 填）
    realized_r: float = 0.0           # 三段加權後的 R 倍數
    legs_hit: tuple[str, ...] = ()    # ("tp1","tp2") 等
    exit_reason: str = ""             # "tp_all" | "stop" | "timeout" | "mixed"
    exit_ts: int = 0
    bars_held: int = 0


def _hit_long(price: float, stop: float, tp: float) -> tuple[bool, bool]:
    """多單：bar 內可能同時掃到 tp 與 stop。
    保守假設：若兩者都觸到，**先 stop**（避免回測過度樂觀）。
    """
    hit_stop = price <= stop
    hit_tp = price >= tp
    return hit_stop, hit_tp


def _hit_short(price: float, stop: float, tp: float) -> tuple[bool, bool]:
    hit_stop = price >= stop
    hit_tp = price <= tp
    return hit_stop, hit_tp


def simulate(
    *,
    symbol: str,
    setup_name: str,
    direction: str,
    entry_ts: int,
    entry_price: float,
    stop: float,
    tps: tuple[float, ...],
    future_prices: list[tuple[int, float]],   # [(ts, price), ...]
    hold_max_hours: int,
) -> TradeOutcome:
    """逐根 bar 模擬。"""
    assert direction in ("bull", "bear")
    assert len(tps) == 3

    sl_distance = abs(entry_price - stop)
    if sl_distance == 0:
        return TradeOutcome(symbol, setup_name, direction, entry_ts,
                            entry_price, stop, tps,
                            exit_reason="invalid_stop", exit_ts=entry_ts)

    # 每根貢獻 1/3 倉位
    leg_size = 1.0 / 3
    legs_open = [True, True, True]
    legs_hit: list[str] = []
    realized_r = 0.0
    effective_stop = stop
    exit_ts = entry_ts
    bars = 0

    hit_fn = _hit_long if direction == "bull" else _hit_short

    for i, (ts, p) in enumerate(future_prices, start=1):
        bars = i
        exit_ts = ts
        if i > hold_max_hours:
            break

        # 檢查 stop（含 breakeven 後）
        is_stop_long = direction == "bull" and p <= effective_stop
        is_stop_short = direction == "bear" and p >= effective_stop
        if is_stop_long or is_stop_short:
            # 仍持有的 leg 用 effective_stop 平
            for li, open_ in enumerate(legs_open):
                if open_:
                    if direction == "bull":
                        r = (effective_stop - entry_price) / sl_distance
                    else:
                        r = (entry_price - effective_stop) / sl_distance
                    realized_r += r * leg_size
                    legs_open[li] = False
            return TradeOutcome(
                symbol, setup_name, direction, entry_ts, entry_price,
                stop, tps,
                realized_r=round(realized_r, 4),
                legs_hit=tuple(legs_hit),
                exit_reason="stop" if not legs_hit else "stop_after_partial",
                exit_ts=exit_ts, bars_held=bars,
            )

        # 檢查 TP（按順序）
        for li, tp in enumerate(tps):
            if not legs_open[li]:
                continue
            _, hit_tp = hit_fn(p, effective_stop, tp)
            if hit_tp:
                # 算這段的 R
                if direction == "bull":
                    r = (tp - entry_price) / sl_distance
                else:
                    r = (entry_price - tp) / sl_distance
                realized_r += r * leg_size
                legs_open[li] = False
                legs_hit.append(f"tp{li+1}")
                # TP1 後止損移到開倉價
                if li == 0:
                    effective_stop = entry_price

        if not any(legs_open):
            return TradeOutcome(
                symbol, setup_name, direction, entry_ts, entry_price,
                stop, tps,
                realized_r=round(realized_r, 4),
                legs_hit=tuple(legs_hit),
                exit_reason="tp_all",
                exit_ts=exit_ts, bars_held=bars,
            )

    # Timeout：剩餘部位以最後價平
    if future_prices:
        last_price = future_prices[min(bars, len(future_prices)) - 1][1]
        for li, open_ in enumerate(legs_open):
            if open_:
                if direction == "bull":
                    r = (last_price - entry_price) / sl_distance
                else:
                    r = (entry_price - last_price) / sl_distance
                realized_r += r * leg_size

    return TradeOutcome(
        symbol, setup_name, direction, entry_ts, entry_price,
        stop, tps,
        realized_r=round(realized_r, 4),
        legs_hit=tuple(legs_hit),
        exit_reason="timeout" if legs_hit else "timeout_no_partial",
        exit_ts=exit_ts, bars_held=bars,
    )
