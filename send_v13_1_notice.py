"""v13.1 升級通知（Apify Twitter 正式上線）"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from telegram_bot.client import TelegramClient


async def main():
    tg = TelegramClient()
    if not tg.configured():
        return 1

    msg = (
        "🚀 <b>機器人升級 v13.1 — Apify Twitter 正式上線</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        "<b>✅ Apify Token 驗證通過：</b>\n"
        "  • 帳號：<code>exquisite_overkill</code>\n"
        "  • 方案：FREE（每月 $5 credits）\n\n"

        "<b>✅ Twitter Scraper 真實抓取成功：</b>\n"
        "  • Actor：<code>kaitoeasyapi/twitter-x-data-tweet-scraper-pay-per-result-cheapest</code>\n"
        "  • 成本：<b>$0.25/1000 tweets</b>（比原計畫 apidojo 還便宜 38%）\n"
        "  • 首次測試：60 則真實 Trump 推文已抓回\n\n"

        "<b>📊 預估月用量：</b>\n"
        "  • 38 帳號 × 8 新推/日 × 30 = ~9,000 tweets/月\n"
        "  • 月成本：<b>~$2.25</b>，落在 $5 FREE credits 內\n\n"

        "<b>🛡 過濾規則啟用（7 tier）：</b>\n"
        "  • T0 全推：Trump + 政府機構 (5 個)\n"
        "  • Tm Macro：Elon + Cathie (2 個)\n"
        "  • T1 交易所：list/delist 才推 (6 個)\n"
        "  • T2 鯨魚：$30M+ 才推 (7 個)\n"
        "  • T3 創辦人：ticker 或政策才推 (5 個)\n"
        "  • T4 交易員：ticker mention 才推 (7 個)\n"
        "  • T5 新聞：ticker 或關鍵字才推 (6 個)\n\n"

        "<b>⚠️ 首次啟動保護：</b>\n"
        "  歷史推文會被標記 seen 但不推（避免洗版），只推真正的新推文。\n\n"

        "<b>🎬 接下來 5-10 分鐘內，</b>\n"
        "  你會收到第一則「過濾通過的真實 X 推文」推播。"
    )

    r = await tg.send_message(msg, parse_mode="HTML")
    print(f"[v13.1] notice: ok={r.get('ok')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
