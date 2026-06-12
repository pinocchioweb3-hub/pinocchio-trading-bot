"""Backtest 統計指標。"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import mean

from .simulator import TradeOutcome


@dataclass
class Metrics:
    n_trades: int
    n_wins: int                    # realized_r > 0
    n_losses: int                  # realized_r < 0
    n_scratch: int                 # realized_r == 0 (timeout breakeven)
    win_rate: float                # n_wins / n_trades
    expectancy_r: float            # mean(realized_r)
    avg_win_r: float
    avg_loss_r: float
    max_win_r: float
    max_loss_r: float
    max_consecutive_losses: int
    max_drawdown_r: float          # 從前高到後低的累計 R 跌幅
    tp_hit_rate: dict[str, float]  # {"tp1": 0.6, "tp2": 0.4, "tp3": 0.2}
    avg_bars_held: float
    profit_factor: float           # sum(wins) / |sum(losses)|


def aggregate(trades: list[TradeOutcome]) -> Metrics:
    if not trades:
        return Metrics(0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0.0, {}, 0.0, 0.0)

    rs = [t.realized_r for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    scratch = [r for r in rs if r == 0]

    # 連續虧損 + 最大回撤（R 為單位）
    max_consec, cur_consec = 0, 0
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in rs:
        if r < 0:
            cur_consec += 1
            max_consec = max(max_consec, cur_consec)
        else:
            cur_consec = 0
        equity += r
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)   # 負值

    # TP 命中率
    tp_counts = {"tp1": 0, "tp2": 0, "tp3": 0}
    for t in trades:
        for leg in t.legs_hit:
            if leg in tp_counts:
                tp_counts[leg] += 1
    tp_hit_rate = {k: v / len(trades) for k, v in tp_counts.items()}

    sum_wins = sum(wins)
    sum_losses = abs(sum(losses))
    profit_factor = sum_wins / sum_losses if sum_losses > 0 else (float("inf") if sum_wins > 0 else 0.0)

    return Metrics(
        n_trades=len(trades),
        n_wins=len(wins),
        n_losses=len(losses),
        n_scratch=len(scratch),
        win_rate=len(wins) / len(trades),
        expectancy_r=mean(rs),
        avg_win_r=mean(wins) if wins else 0.0,
        avg_loss_r=mean(losses) if losses else 0.0,
        max_win_r=max(wins) if wins else 0.0,
        max_loss_r=min(losses) if losses else 0.0,
        max_consecutive_losses=max_consec,
        max_drawdown_r=max_dd,
        tp_hit_rate=tp_hit_rate,
        avg_bars_held=mean(t.bars_held for t in trades),
        profit_factor=profit_factor,
    )
