"""v18-D 上線通知"""
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
        "🚀 <b>v18 第四彈：等待觸發進場上線（不再追價）</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "<b>⏳ D 項完成：三態進場決策</b>\n"
        "從現在起，每筆 FIRE 訊號發出前會先比對「即時價 vs 進場區」：\n\n"
        "  🔥 <b>價在進場區內</b> → 直接推正式訊號（現行流程）\n"
        "  ⏳ <b>價已偏離進場區</b> → 推「等待觸發」通知，"
        "系統每 15 分鐘盯價格，回到區內才推正式訊號（含按鈕）\n"
        "  ❌ <b>等 6 小時沒回來</b> → 自動放棄，安靜通知一行\n\n"
        "<b>為什麼重要：</b>\n"
        "  • 解決「訊號發出時價格已經跑掉」的追價虧損 — 紀律比速度重要\n"
        "  • 紙上帳改在「實際觸發價」開倉 — 驗證數據更接近真實可成交價\n"
        "  • 對手 SM 浪潮的「AI 主交易員：進場/等待/放棄」我們現在也有了，"
        "且邏輯完全透明\n\n"
        "<b>📋 佇列：</b>E 廣度閘門 → F SMC 圖表標記（自動接續，今晚完成）"
    )
    r = await router.general().send_message(msg, parse_mode="HTML")
    print(f"[v18d] notice: ok={r.get('ok')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
