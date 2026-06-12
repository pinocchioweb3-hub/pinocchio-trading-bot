"""人類可讀的 backtest 報告。"""
from __future__ import annotations

import datetime as dt

from .metrics import Metrics
from .replay import FireEvent
from .simulator import TradeOutcome


def _fmt_ts(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


def render_summary(*, symbol: str, setup_name: str, risk_per_trade_usd: float,
                   metrics: Metrics, fires: list[FireEvent]) -> str:
    if metrics.n_trades == 0:
        return f"[{symbol}/{setup_name}] no trades — try different config or longer history\n"

    pnl_usd = metrics.expectancy_r * metrics.n_trades * risk_per_trade_usd
    fire_tags = {}
    for f in fires:
        fire_tags[f.snapshot_tag or "noise"] = fire_tags.get(f.snapshot_tag or "noise", 0) + 1

    lines = []
    lines.append("=" * 72)
    lines.append(f"  Backtest report — {symbol} / setup={setup_name}")
    lines.append("=" * 72)
    lines.append(f"  trades:      {metrics.n_trades}  (wins={metrics.n_wins} losses={metrics.n_losses} scratch={metrics.n_scratch})")
    lines.append(f"  win rate:    {metrics.win_rate*100:.1f}%")
    lines.append(f"  expectancy:  {metrics.expectancy_r:+.3f} R per trade")
    lines.append(f"  PnL (R):     {metrics.expectancy_r * metrics.n_trades:+.2f} R")
    lines.append(f"  PnL (USD):   ${pnl_usd:+,.2f}  (risk ${risk_per_trade_usd}/trade)")
    lines.append(f"  profit fac:  {metrics.profit_factor:.2f}")
    lines.append("")
    lines.append(f"  avg win:     {metrics.avg_win_r:+.3f} R   max win:  {metrics.max_win_r:+.3f} R")
    lines.append(f"  avg loss:    {metrics.avg_loss_r:+.3f} R   max loss: {metrics.max_loss_r:+.3f} R")
    lines.append(f"  max consec losses: {metrics.max_consecutive_losses}")
    lines.append(f"  max drawdown (R):   {metrics.max_drawdown_r:.2f} R")
    lines.append(f"  avg bars held:      {metrics.avg_bars_held:.1f} h")
    lines.append("")
    lines.append(f"  TP hit rate: tp1={metrics.tp_hit_rate.get('tp1',0)*100:.1f}%  "
                 f"tp2={metrics.tp_hit_rate.get('tp2',0)*100:.1f}%  "
                 f"tp3={metrics.tp_hit_rate.get('tp3',0)*100:.1f}%")
    lines.append("")
    lines.append(f"  FIRE 來源（依事件標籤）：")
    for tag, n in sorted(fire_tags.items(), key=lambda x: -x[1]):
        label = tag if tag else "noise"
        lines.append(f"    {label:14s} {n} 次")
    lines.append("=" * 72)
    return "\n".join(lines) + "\n"


def render_trade_log(trades: list[TradeOutcome], limit: int = 20) -> str:
    if not trades:
        return ""
    lines = ["\n  Trade log (前 %d 筆)：" % min(limit, len(trades))]
    lines.append(f"  {'#':>3} {'ts':18} {'dir':4} {'R':>7} {'legs':>14} {'exit':18}")
    lines.append("  " + "-" * 70)
    for i, t in enumerate(trades[:limit], start=1):
        legs = ",".join(t.legs_hit) if t.legs_hit else "-"
        lines.append(f"  {i:>3} {_fmt_ts(t.entry_ts)} {t.direction:4} "
                     f"{t.realized_r:+7.3f} {legs:>14} {t.exit_reason}")
    return "\n".join(lines) + "\n"
