"""v11 升級通知：trade journal + risk manager + Setup B 停用"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from l3_dispatcher.risk_manager import get_risk_status, render_risk_status
from l3_dispatcher.trade_journal import get_stats, render_stats_summary, init_db
from telegram_bot.client import TelegramClient


async def main():
    init_db()
    tg = TelegramClient()
    if not tg.configured():
        print("ERROR: Telegram not configured")
        return 1

    parts = []
    parts.append(
        "🚀 <b>機器人升級 v11 上線</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>本次升級內容（基於 30 天真實回測結論）：</b>\n\n"
        "<b>1. 部署 Loose Profile 為預設</b>\n"
        "  • cvd_slope_min 0.08（原 0.15）\n"
        "  • funding_neg_thr -0.00005（原 -0.0001）\n"
        "  • oi_rise_min_pct 2.0（原 3.0）\n"
        "  • min_confirmations 1（原 2）\n"
        "  • hold_max_hours 48（原 24，避免 SUI 類提早 timeout）\n"
        "  • sl_buffer 4%（原 2.5%，給 1h 訊號合理空間）\n"
        "  目標：30 天約 90+ 筆訊號，朝 $100/日 邁進\n\n"
        "<b>2. Setup B（Ambush）已停用</b>\n"
        "  24 組真實回測全 0 FIRE，設計失效\n"
        "  scheduler.py 已移除 ambush 掃描\n"
        "  → 集中火力跑 Setup A intraday\n\n"
        "<b>3. BNB 移出 Watchlist</b>\n"
        "  歷史勝率 33%、淨虧 $339，已從 TRADING_CANDIDATES 移除\n\n"
        "<b>4. 全新：SQLite Trade Journal</b>\n"
        "  • <code>trade_journal.db</code> 記每筆 entry/exit/PnL\n"
        "  • 支援 TP1/TP2/TP3 分批出場（trade_legs 表）\n"
        "  • 每日 08:00 台北自動推 7d/30d 績效統計\n"
        "  • 統計：勝率 / Avg R / Max DD / 每 setup 分桶\n\n"
        "<b>5. 全新：Risk Manager 熔斷</b>\n"
        "  ⛔ 單筆風險：$100 (1R)\n"
        "  ⛔ 同時最多 3 筆持倉\n"
        "  ⛔ BTC family（BTC/ETH/SOL）同方向最多 2 筆\n"
        "  ⛔ 同 symbol 同方向只 1 筆\n"
        "  🟡 每日 PnL ≤ -3% → 自動暫停至明日 UTC 00:00\n"
        "  🔴 每週 PnL ≤ -7% → 完全暫停 + 強制人工 review\n"
        "  → FIRE 前自動檢查，被擋會推「阻擋通知」"
    )

    # 推送主升級訊息
    r = await tg.send_message(parts[0], parse_mode="HTML")
    print(f"[v11] upgrade notice: ok={r.get('ok')}")

    # 推當前風險狀態 + 7d 績效
    try:
        status = get_risk_status()
        stats7 = get_stats(7)
        body = (
            render_risk_status(status) + "\n\n" +
            render_stats_summary(stats7, label="📈 過去 7 天績效")
        )
        r2 = await tg.send_message(body, parse_mode="HTML")
        print(f"[v11] status snapshot: ok={r2.get('ok')}")
    except Exception as e:
        print(f"[v11] status snapshot ERROR: {type(e).__name__}: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
