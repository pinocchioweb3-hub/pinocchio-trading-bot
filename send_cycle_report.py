"""自主作業循環報告（16:30 輪）"""
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
        "🩺 <b>自主作業循環報告</b>（16:30 輪）\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>✅ 系統健康</b>\n"
        "  • daemon 存活（15:16 重啟後 20 個 worker 運行中）\n"
        "  • 掃描器：2 分鐘前新快照（69,064 筆）、廣度資料即時\n"
        "  • 新聞流：8 分鐘前有新資料\n"
        "  • 紙上帳：3 筆已平倉（最後進場約 14 小時前）\n"
        "  • 近 24h 無 FIRE 訊號 — 市況安靜 + 嚴格閘門，屬正常\n\n"
        "<b>🧵 Threads 邀請重試結果</b>\n"
        "  本輪未能送出 — 瀏覽器擴充功能對 Meta 開發者後台的\n"
        "  連線通道暫時卡死（Threads 網域正常，僅 Meta 後台異常）。\n"
        "  下一輪自動重試；皮諾丘個人檔案目前仍是空的，\n"
        "  補上大頭貼＋簡介＋第一篇文會大幅提高邀請成功率 🙏\n\n"
        "<i>下一輪自主作業：約 1 小時後</i>"
    )
    r = await router.client("system").send_message(msg, parse_mode="HTML")
    print(f"[cycle] report: ok={r.get('ok')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
