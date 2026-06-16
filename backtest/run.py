"""Backtest runner（⚠️ 已被取代 / DEPRECATED — 僅供管線冒煙測試）。

    這支用的是 **合成 mock 歷史**（historical.generate + seed），數字**不是真實績效**，
    只能拿來驗「回測管線本身跑不跑得起來」。請**勿**把這裡的勝率/R 當成真實成果（紅線③）。

    要看「真實歷史走查回測」（每週自動跑、餵 CEO 簡報與 auto_tuner）請用：
        backtest/backtest_session.py（worker：run_backtest_loop；真實 K 線、walk-forward）

用法（僅冒煙測試）：
    python -m backtest.run                          # 跑全部預設情境（mock）
    python -m backtest.run --symbol SUI --days 60   # 自訂（mock）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows PowerShell 編碼
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from l2_trigger.configs.ambush import AMBUSH_DEFAULT, get_ambush_config
from l2_trigger.configs.intraday import INTRADAY_SUI, get_intraday_config

from . import historical, metrics, replay, report


def run_scenario(*, symbol: str, setup: str, days: int, seed: int,
                 cooldown_hours: int, future_window_hours: int,
                 show_log: bool = True) -> None:
    """跑單一情境 + 印報告"""
    hist = historical.generate(symbol=symbol, days=days, seed=seed)
    if setup == "intraday":
        config = get_intraday_config(symbol)
    elif setup == "ambush":
        config = get_ambush_config(symbol)
    else:
        raise ValueError(f"unknown setup: {setup}")

    trades, fires = replay.run(
        hist, config,
        cooldown_hours=cooldown_hours,
        future_window_hours=future_window_hours,
    )
    m = metrics.aggregate(trades)
    print(report.render_summary(
        symbol=symbol, setup_name=setup,
        risk_per_trade_usd=config.risk_per_trade_usd,
        metrics=m, fires=fires,
    ))
    if show_log and trades:
        print(report.render_trade_log(trades, limit=15))

    return m, trades, fires


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default=None,
                        help="single symbol；省略=跑全部預設情境")
    parser.add_argument("--setup", choices=("intraday", "ambush"), default=None)
    parser.add_argument("--days", type=int, default=60,
                        help="歷史長度（天）")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--show-log", action="store_true", default=True)
    args = parser.parse_args()

    print(f"\n{'═' * 72}")
    print(f"  ⚠️  DEPRECATED 回測器 — 合成 mock 歷史，{args.days} 天，seed={args.seed}")
    print(f"  ⚠️  數字非真實績效、勿當成果（紅線③）。真實走查回測請用 "
          f"backtest.backtest_session")
    print(f"{'═' * 72}\n")

    if args.symbol:
        scenarios = [(args.symbol, args.setup or "intraday")]
    else:
        scenarios = [
            ("SUI", "intraday"),     # 設計上會 FIRE squeeze 事件
            ("SUI", "ambush"),       # 設計上會 FIRE accumulation 事件
            ("ARB", "intraday"),     # 對照：is_hot=True 但低強度
            ("ARB", "ambush"),       # 設計上 ARB 在 accumulation 時 FIRE
        ]

    for sym, setup in scenarios:
        cooldown_h = 24 if setup == "ambush" else 4
        future_h = 72 if setup == "ambush" else 36
        run_scenario(
            symbol=sym, setup=setup, days=args.days, seed=args.seed,
            cooldown_hours=cooldown_h, future_window_hours=future_h,
            show_log=args.show_log,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
