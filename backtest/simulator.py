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
    future_prices: list[tuple[int, float]],   # [(ts, price), ...] 或 [(ts,h,l,c)]
    hold_max_hours: int,
    taker_fee: float = 0.0005,   # v33: 單邊手續費(分數,0.05%)，預設計入=誠實淨值
    slippage: float = 0.0005,    # v33: 單邊滑點(分數,0.05%)
) -> TradeOutcome:
    """逐根 bar 模擬。v33：realized_r 已扣來回手續費+滑點(淨值)。"""
    assert direction in ("bull", "bear")
    assert len(tps) == 3

    sl_distance = abs(entry_price - stop)
    if sl_distance == 0:
        return TradeOutcome(symbol, setup_name, direction, entry_ts,
                            entry_price, stop, tps,
                            exit_reason="invalid_stop", exit_ts=entry_ts)
    # v33: 來回成本(進+出 全名目)換算成 R：cost_r = 2×(fee+slip)×entry/sl_distance
    cost_r = 2 * (taker_fee + slippage) * entry_price / sl_distance

    # 每根貢獻 1/3 倉位
    leg_size = 1.0 / 3
    legs_open = [True, True, True]
    legs_hit: list[str] = []
    realized_r = 0.0
    effective_stop = stop
    exit_ts = entry_ts
    bars = 0

    def _bar(b):
        # v33: 支援 (ts, close) 舊格式 與 (ts, high, low, close) 新格式
        if len(b) >= 4:
            return b[0], b[1], b[2], b[3]
        return b[0], b[1], b[1], b[1]

    for i, b in enumerate(future_prices, start=1):
        ts, hi, lo, cl = _bar(b)
        bars = i
        exit_ts = ts
        if i > hold_max_hours:
            break

        # 檢查 stop（v33：用盤中 low/high 而非 close，看得到插針；保守：同根先判 stop）
        is_stop_long = direction == "bull" and lo <= effective_stop
        is_stop_short = direction == "bear" and hi >= effective_stop
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
                realized_r=round(realized_r - cost_r, 4),
                legs_hit=tuple(legs_hit),
                exit_reason="stop" if not legs_hit else "stop_after_partial",
                exit_ts=exit_ts, bars_held=bars,
            )

        # 檢查 TP（v33：用盤中 high/low；按順序）
        for li, tp in enumerate(tps):
            if not legs_open[li]:
                continue
            hit_tp = (hi >= tp) if direction == "bull" else (lo <= tp)
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
                realized_r=round(realized_r - cost_r, 4),
                legs_hit=tuple(legs_hit),
                exit_reason="tp_all",
                exit_ts=exit_ts, bars_held=bars,
            )

    # Timeout：剩餘部位以最後 close 平
    if future_prices:
        _, _, _, last_close = _bar(future_prices[min(bars, len(future_prices)) - 1])
        for li, open_ in enumerate(legs_open):
            if open_:
                if direction == "bull":
                    r = (last_close - entry_price) / sl_distance
                else:
                    r = (entry_price - last_close) / sl_distance
                realized_r += r * leg_size

    return TradeOutcome(
        symbol, setup_name, direction, entry_ts, entry_price,
        stop, tps,
        # v33 修正(對抗複查 CRITICAL)：timeout 路徑先前漏扣 cost_r，與 stop/tp_all
        # 兩條路徑及 docstring「已扣來回成本」不一致 → 補上，三條出場路徑一致淨值
        realized_r=round(realized_r - cost_r, 4),
        legs_hit=tuple(legs_hit),
        exit_reason="timeout" if legs_hit else "timeout_no_partial",
        exit_ts=exit_ts, bars_held=bars,
    )
