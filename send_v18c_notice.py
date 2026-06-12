"""v18-C 上線通知"""
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
from telegram_bot.topics import TopicRouter


async def main():
    router = TopicRouter(TelegramClient())
    msg = (
        "🚀 <b>v18 第二彈：代幣解鎖日曆上線（事前預警層）</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "<b>🔓 C 項完成：解鎖日曆黑名單</b>\n"
        "  • 每日掃描 DefiLlama 全部代幣解鎖排程（免費源）\n"
        "  • 「未來 35 天解鎖 ≥5% 流通量」自動入庫\n"
        "  • <b>每日推「7 天內大解鎖預告」到經濟數據頻道</b>\n"
        "  • 做多 FIRE 訊號自動附解鎖警告\n"
        "  • 全市場異常警報自動附解鎖風險註記\n\n"
        "<b>實測首輪即發現（OKX 可交易）：</b>\n"
        "  • <b>SPK</b>（Spark）6/17 解鎖 <b>19.9%</b> 流通量（5 天後！）\n"
        "  • <b>VANA</b> 6/17 解鎖 <b>10.1%</b> 流通量\n"
        "  💡 SAHARA 6/9 崩盤 -60% 的主因之一就是 6/26 解鎖 30% 的搶跑 —\n"
        "  這類風險現在提前數週可見。\n\n"
        "<b>📋 佇列：</b>B 歷史類比 → D 等待觸發 → E 廣度閘門 → F SMC 圖表（自動接續）"
    )
    r = await router.general().send_message(msg, parse_mode="HTML")
    print(f"[v18c] notice: ok={r.get('ok')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
