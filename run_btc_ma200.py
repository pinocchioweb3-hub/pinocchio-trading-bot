#!/usr/bin/env python3
"""BTC 4h 200MA 自動交易機器人。

策略：
    - 每 4 小時檢查一次 BTC 4h K 線
    - 計算 200 期 SMA
    - 金叉（價格從下方穿越 MA）→ 開多
    - 死叉（價格從上方穿越 MA）→ 平多開空（或僅平多）
    - 自動設止損（預設 2%）
    - 止盈分批出場

用法：
    # 模擬盤（安全測試）
    python run_btc_ma200.py --demo

    # 實盤（需要 OKX_TRADE_API_KEY）
    python run_btc_ma200.py

    # 單次檢查（不迴圈）
    python run_btc_ma200.py --once

    # 僅做多（不做空）
    python run_btc_ma200.py --long-only

    # 自訂參數
    python run_btc_ma200.py --demo --leverage 5 --risk 50 --sl-pct 3.0
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

# 載入 .env
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

# 設定 MARKET_INTEL_BACKEND=coinglass（覆蓋預設 mock）
os.environ.setdefault("MARKET_INTEL_BACKEND", "coinglass")

from l2_trigger.engine import evaluate
from l2_trigger.configs.ma_crossover import get_ma_crossover_config
from l2_trigger.types import MarketSnapshot, TriggerAction, TriggerConfig
from l2_trigger.leverage import compute_position, compute_tp_prices
from l4_execution.okx_executor import OKXExecutor
from market_intel_mcp.sources.coinglass import CoinGlassSource
from telegram_bot.client import TelegramClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("btc_ma200")


# =========================================================================
# 數據取得
# =========================================================================
async def fetch_btc_4h_snapshot(source: CoinGlassSource) -> MarketSnapshot | None:
    """從 CoinGlass 抓 BTC 4h K 線 250 根，計算 200MA + 穿越偵測。"""
    result = await source.get_price_series("BTC", "4h", 250)
    if isinstance(result, dict) and result.get("error"):
        logger.error("抓取 BTC 4h 價格失敗: %s", result.get("message"))
        return None

    series = result.get("series", [])
    if len(series) < 201:
        logger.error("數據不足: 需要 201 根 4h K 線，僅得到 %d", len(series))
        return None

    # 計算 200 期 SMA
    closes = [p["value"] for p in series]
    ma200 = mean(closes[-200:])

    current_close = closes[-1]
    prev_close = closes[-2]

    ts = series[-1].get("ts", int(time.time() * 1000))

    logger.info(
        "BTC 4h | close=%.2f | prev_close=%.2f | MA200=%.2f | %s MA",
        current_close, prev_close, ma200,
        "above" if current_close > ma200 else "below",
    )

    return MarketSnapshot(
        symbol="BTC",
        ts=ts,
        price=current_close,
        tf="4h",
        ma200_4h=ma200,
        prev_close_4h=prev_close,
        above_4h_200ma=current_close > ma200,
        # 其他欄位不需要（Setup C 不用 gate/hot/OI 等）
    )


# =========================================================================
# 主交易邏輯
# =========================================================================
async def execute_signal(
    executor: OKXExecutor,
    tg: TelegramClient,
    decision,
    config: TriggerConfig,
    long_only: bool,
) -> None:
    """根據 FIRE 決策執行交易。"""
    direction = decision.direction.value  # "bull" / "bear"
    snap = decision.snapshot

    # 先查當前持倉
    pos = await executor.get_position("BTC")

    # ---- BULL FIRE → 開多 ----
    if direction == "bull":
        # 如果有空倉，先平
        if pos and pos["side"] == "short":
            logger.info("金叉！先平空倉...")
            close_result = await executor.close_position("BTC")
            await executor.cancel_all_orders("BTC")
            if close_result.success:
                await _notify(tg, f"🔄 <b>平空倉</b>\n{_pos_summary(close_result)}")

        # 如果已有多倉，不重複開
        if pos and pos["side"] == "long":
            logger.info("已有多倉，跳過開倉")
            return

        # 開多
        sl_pct = config.sl_buffer_pct
        entry_est = snap.price
        sl_price = entry_est * (1 - sl_pct / 100)
        position = compute_position(entry_est, sl_price, config.risk_per_trade_usd,
                                    config.default_leverage)
        tp_info = compute_tp_prices(entry_est, sl_price, "bull", config.tp_r_multiples)
        tp_list = [tp_info[f"tp{i}"] for i in range(1, len(config.tp_r_multiples) + 1)]

        result = await executor.open_position(
            symbol="BTC",
            side="long",
            leverage=config.default_leverage,
            usdt_margin=position["margin_usd"],
            sl_pct=sl_pct,
            tp_prices=tp_list,
        )

        if result.success:
            msg = (
                f"📈 <b>BTC 金叉做多</b>\n"
                f"價格穿越 4h 200MA\n"
                f"進場: ${result.price:,.2f}\n"
                f"止損: ${result.sl_price:,.2f} (-{sl_pct}%)\n"
                f"止盈: {', '.join(f'${tp:,.2f}' for tp in tp_list)}\n"
                f"槓桿: {config.default_leverage}x\n"
                f"保證金: ${position['margin_usd']:.2f}\n"
                f"名目: ${position['notional_usd']:.2f}\n"
                f"MA200: ${snap.ma200_4h:,.2f}"
            )
            await _notify(tg, msg)
        else:
            await _notify(tg, f"❌ <b>開多失敗</b>\n{result.error}")

    # ---- BEAR FIRE → 平多（或開空）----
    elif direction == "bear":
        # 如果有多倉，先平
        if pos and pos["side"] == "long":
            logger.info("死叉！平多倉...")
            close_result = await executor.close_position("BTC")
            await executor.cancel_all_orders("BTC")
            if close_result.success:
                await _notify(tg, f"📉 <b>死叉平多</b>\n{_pos_summary(close_result)}")

        if long_only:
            logger.info("僅做多模式，不開空倉")
            return

        # 如果已有空倉，不重複開
        if pos and pos["side"] == "short":
            logger.info("已有空倉，跳過開倉")
            return

        # 開空
        sl_pct = config.sl_buffer_pct
        entry_est = snap.price
        sl_price = entry_est * (1 + sl_pct / 100)
        position = compute_position(entry_est, sl_price, config.risk_per_trade_usd,
                                    config.default_leverage)
        tp_info = compute_tp_prices(entry_est, sl_price, "bear", config.tp_r_multiples)
        tp_list = [tp_info[f"tp{i}"] for i in range(1, len(config.tp_r_multiples) + 1)]

        result = await executor.open_position(
            symbol="BTC",
            side="short",
            leverage=config.default_leverage,
            usdt_margin=position["margin_usd"],
            sl_pct=sl_pct,
            tp_prices=tp_list,
        )

        if result.success:
            msg = (
                f"📉 <b>BTC 死叉做空</b>\n"
                f"價格跌破 4h 200MA\n"
                f"進場: ${result.price:,.2f}\n"
                f"止損: ${result.sl_price:,.2f} (+{sl_pct}%)\n"
                f"止盈: {', '.join(f'${tp:,.2f}' for tp in tp_list)}\n"
                f"槓桿: {config.default_leverage}x\n"
                f"保證金: ${position['margin_usd']:.2f}\n"
                f"名目: ${position['notional_usd']:.2f}\n"
                f"MA200: ${snap.ma200_4h:,.2f}"
            )
            await _notify(tg, msg)
        else:
            await _notify(tg, f"❌ <b>開空失敗</b>\n{result.error}")


def _pos_summary(result) -> str:
    return f"成交價: ${result.price:,.2f} | 數量: {result.amount}"


async def _notify(tg: TelegramClient, text: str) -> None:
    """Telegram 通知（失敗不中斷交易）。"""
    if tg.configured():
        try:
            await tg.send_message(text)
        except Exception as e:
            logger.warning("Telegram 通知失敗: %s", e)
    logger.info("[TG] %s", text.replace("<b>", "").replace("</b>", ""))


# =========================================================================
# 主迴圈
# =========================================================================
async def scan_once(
    source: CoinGlassSource,
    executor: OKXExecutor,
    tg: TelegramClient,
    config: TriggerConfig,
    long_only: bool,
) -> str:
    """單次掃描。回傳狀態字串。"""
    snap = await fetch_btc_4h_snapshot(source)
    if snap is None:
        return "data_error"

    decision = evaluate(snap, config)
    logger.info(
        "L2 決策: action=%s direction=%s reason=%s",
        decision.action.value, decision.direction.value, decision.reason,
    )

    if decision.action == TriggerAction.FIRE:
        logger.info("🔥 FIRE! %s", decision.reason)
        await execute_signal(executor, tg, decision, config, long_only)
        return f"fire_{decision.direction.value}"

    # HOLD：檢查持倉狀態，回報
    pos = await executor.get_position("BTC")
    if pos:
        logger.info(
            "HOLD（%s）| 持倉: %s %s | 未實現 PnL: $%.2f",
            decision.reason, pos["side"], pos["contracts"],
            pos["unrealized_pnl"],
        )
    else:
        logger.info("HOLD（%s）| 無持倉", decision.reason)

    return "hold"


async def run_loop(args: argparse.Namespace) -> None:
    """主迴圈。"""
    source = CoinGlassSource()
    executor = OKXExecutor(demo=args.demo)
    tg = TelegramClient()

    # 覆蓋 config 中的風控參數
    base_config = get_ma_crossover_config()
    config = TriggerConfig(
        setup_name=base_config.setup_name,
        cvd_slope_min=base_config.cvd_slope_min,
        cvd_slope_ref=base_config.cvd_slope_ref,
        funding_neg_thr=base_config.funding_neg_thr,
        funding_hot_thr=base_config.funding_hot_thr,
        top_trader_long_thr=base_config.top_trader_long_thr,
        top_trader_short_thr=base_config.top_trader_short_thr,
        retail_short_thr=base_config.retail_short_thr,
        retail_long_thr=base_config.retail_long_thr,
        oi_rise_min_pct=base_config.oi_rise_min_pct,
        require_oi_fuel=base_config.require_oi_fuel,
        require_gate_open=base_config.require_gate_open,
        require_hot=base_config.require_hot,
        require_trend_4h=base_config.require_trend_4h,
        min_confirmations=base_config.min_confirmations,
        risk_per_trade_usd=args.risk,
        default_leverage=args.leverage,
        tp_r_multiples=base_config.tp_r_multiples,
        sl_buffer_pct=args.sl_pct,
        hold_max_hours=base_config.hold_max_hours,
    )

    mode_label = "模擬盤" if args.demo else "⚠️ 實盤"
    long_only_label = "（僅做多）" if args.long_only else "（多空雙向）"
    startup_msg = (
        f"🤖 <b>BTC 4h 200MA 機器人啟動</b>\n"
        f"模式: {mode_label} {long_only_label}\n"
        f"槓桿: {config.default_leverage}x\n"
        f"風險: ${config.risk_per_trade_usd}/筆\n"
        f"止損: {config.sl_buffer_pct}%\n"
        f"止盈: {', '.join(f'{r}R' for r in config.tp_r_multiples)}\n"
        f"掃描間隔: {args.interval}s ({args.interval // 60}分鐘)"
    )
    await _notify(tg, startup_msg)

    # 驗證連線
    try:
        balance = await executor.get_balance()
        logger.info("OKX 連線成功 | USDT: %.2f (free: %.2f)",
                     balance["total"], balance["free"])
    except Exception as e:
        logger.error("OKX 連線失敗: %s", e)
        await _notify(tg, f"❌ <b>OKX 連線失敗</b>\n{e}")
        if not args.demo:
            return

    if args.once:
        await scan_once(source, executor, tg, config, args.long_only)
    else:
        while True:
            try:
                status = await scan_once(source, executor, tg, config, args.long_only)
                logger.info("掃描完成: %s | 下次: %ds 後", status, args.interval)
            except Exception as e:
                logger.exception("掃描異常: %s", e)
                await _notify(tg, f"⚠️ <b>掃描異常</b>\n{e}")

            await asyncio.sleep(args.interval)

    await source.close()
    await executor.close()


# =========================================================================
# CLI
# =========================================================================
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="BTC 4h 200MA 自動交易機器人",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--demo", action="store_true", default=True,
                   help="使用 OKX 模擬盤（預設）")
    p.add_argument("--live", action="store_true",
                   help="使用實盤（需要 OKX_TRADE_API_KEY）")
    p.add_argument("--once", action="store_true",
                   help="單次掃描後退出")
    p.add_argument("--long-only", action="store_true",
                   help="僅做多，不做空")
    p.add_argument("--leverage", type=int, default=10,
                   help="槓桿倍數（預設 10）")
    p.add_argument("--risk", type=float, default=100.0,
                   help="每筆風險 USDT（預設 100）")
    p.add_argument("--sl-pct", type=float, default=2.0,
                   help="止損距離 %%（預設 2.0）")
    p.add_argument("--interval", type=int, default=14400,
                   help="掃描間隔秒（預設 14400 = 4 小時）")
    args = p.parse_args()

    if args.live:
        args.demo = False

    return args


def main():
    args = parse_args()

    if not args.demo:
        trade_key = os.getenv("OKX_TRADE_API_KEY", "")
        if not trade_key:
            print("❌ 實盤模式需要 OKX_TRADE_API_KEY。請在 .env 中設定。")
            sys.exit(1)
        print("⚠️  即將以實盤模式啟動！5 秒後開始...")
        time.sleep(5)

    asyncio.run(run_loop(args))


if __name__ == "__main__":
    main()
