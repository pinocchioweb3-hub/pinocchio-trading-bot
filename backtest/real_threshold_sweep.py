"""更全面的真實回測：6 symbol × 2 setup × 3 種閾值組合，看調參能不能提升。

對比：
- baseline: 現有閾值（cvd_slope_min=0.15, oi_rise_min=3.0, funding_neg_thr=-0.0001）
- loose:    放寬（cvd_slope_min=0.10, oi_rise_min=2.0, funding_neg_thr=-0.00005）
- strict:   收緊（cvd_slope_min=0.20, oi_rise_min=4.0, funding_neg_thr=-0.0002）

目標：找出在真實 30 天資料中，哪組閾值期望值最高。
"""
from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
os.environ["MARKET_INTEL_BACKEND"] = "coinglass"

sys.path.insert(0, str(ROOT))

from l2_trigger.configs.ambush import get_ambush_config
from l2_trigger.configs.intraday import get_intraday_config

from market_intel_mcp.sources.coinglass import CoinGlassSource
from .metrics import aggregate
from .real_historical import fetch_real_history, replay_real


SYMBOLS_TO_TEST = ["BTC", "ETH", "SOL", "SUI", "ARB", "INJ", "BNB", "AVAX"]


def tune_config(base_config, profile: str):
    """產生不同閾值的 config 變體"""
    if profile == "baseline":
        return base_config
    new_values = dataclasses.asdict(base_config)
    if profile == "loose":
        new_values["cvd_slope_min"] = 0.08
        new_values["oi_rise_min_pct"] = 2.0
        new_values["funding_neg_thr"] = -0.00005
        new_values["min_confirmations"] = 1
        new_values["sl_buffer_pct"] = 4.0
        new_values["hold_max_hours"] = 48
    elif profile == "strict":
        new_values["cvd_slope_min"] = 0.20
        new_values["oi_rise_min_pct"] = 4.0
        new_values["funding_neg_thr"] = -0.0002
        new_values["min_confirmations"] = 2
        new_values["sl_buffer_pct"] = 3.0
        new_values["hold_max_hours"] = 24
    return type(base_config)(**new_values)


async def main(symbols: list[str] | None = None, days: int = 30):
    print(f"\n=== 真實回測 sweep ({days} 天 × {len(symbols or SYMBOLS_TO_TEST)} 幣 × 3 閾值組) ===\n")
    syms = symbols or SYMBOLS_TO_TEST
    cg = CoinGlassSource()

    # 先一次拉全部歷史
    all_history = {}
    for sym in syms:
        try:
            history = await fetch_real_history(sym, days, cg)
            if len(history) >= 168:
                all_history[sym] = history
            else:
                print(f"  [skip] {sym} insufficient")
        except Exception as e:
            print(f"  [error] {sym}: {e}")

    # 為每 symbol × profile 跑 replay
    results = {}  # key=(sym, setup_name, profile) → metrics
    for sym, history in all_history.items():
        for setup_label, get_cfg in [("intraday", get_intraday_config),
                                      ("ambush", get_ambush_config)]:
            base = get_cfg(sym)
            for profile in ["baseline", "loose", "strict"]:
                cfg = tune_config(base, profile)
                try:
                    trades, fires = await replay_real(history, cfg, future_window=48)
                    m = aggregate(trades)
                    results[(sym, setup_label, profile)] = m
                except Exception as e:
                    print(f"  [error] {sym}/{setup_label}/{profile}: {e}")

    await cg.close()

    # 印對比表
    print("\n" + "=" * 100)
    print(f"  {'symbol':6} {'setup':10} {'profile':10} {'trades':6} {'win%':6} "
          f"{'期望R':10} {'max_DD_R':10} {'avg_hold_h':10} {'PnL_USD':10}")
    print("  " + "-" * 96)
    for (sym, setup, profile), m in sorted(results.items()):
        if m.n_trades == 0:
            print(f"  {sym:6} {setup:10} {profile:10} {'0':>6}  {'—':>6}  {'no trades':<10}")
            continue
        win_rate = m.win_rate * 100
        pnl_usd = m.expectancy_r * m.n_trades * 100
        print(f"  {sym:6} {setup:10} {profile:10} {m.n_trades:>6} "
              f"{win_rate:>6.1f}  {m.expectancy_r:>+10.3f}  "
              f"{m.max_drawdown_r:>10.2f}  {m.avg_bars_held:>10.1f}  "
              f"${pnl_usd:>+9.0f}")
    print("=" * 100)

    # 統計各 profile 總體
    print("\n=== 各 profile 整體對比 ===")
    for profile in ["baseline", "loose", "strict"]:
        rs = [m for (s, st, p), m in results.items() if p == profile]
        total_trades = sum(m.n_trades for m in rs)
        total_pnl = sum(m.expectancy_r * m.n_trades * 100 for m in rs)
        wins = sum(m.n_wins for m in rs)
        losses = sum(m.n_losses for m in rs)
        win_rate_agg = wins / total_trades * 100 if total_trades > 0 else 0
        print(f"  {profile:10} total_trades={total_trades:3d}  agg_win_rate={win_rate_agg:5.1f}%  "
              f"total_PnL=${total_pnl:+,.0f}")


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    asyncio.run(main(days=days))
