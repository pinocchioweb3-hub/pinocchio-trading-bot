"""v19 上線通知"""
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
        "🚀 <b>v19 上線：主題重整 + 💡意見箱積分系統</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "<b>📂 主題分流更乾淨（9 個頻道）</b>\n"
        "  ⚡ <b>異常警報</b>（新）— 全市場掃描器警報獨立出來\n"
        "  🎯 交易訊號 — 從此只放 FIRE/等待觸發/熔斷/交易計畫\n"
        "  💡 <b>意見箱</b>（新）— 社群建議與貢獻積分\n\n"
        "<b>💡 意見箱積分系統（開源計畫的地基）</b>\n"
        "  • 發建議自動記錄計分（每人每小時 1 分、≥10 字）\n"
        "  • 被採納 +5 分（管理者 /adopt 標記）\n"
        "  • <b>累積制永不清零</b> — 未來 50% 分潤依積分占比\n"
        "  • /contrib 排行榜｜/myscore 查個人積分\n\n"
        "<b>📜 VISION.md 已寫入專案</b> — 你的開源計畫願景正式文件化，\n"
        "未來 GitHub 公開的第一份文檔（含修正後的分潤模型與合規邊界）。"
    )
    r = await router.general().send_message(msg, parse_mode="HTML")
    print(f"[v19] notice: ok={r.get('ok')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
