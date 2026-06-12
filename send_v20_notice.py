"""v20 進度通知：Threads 發布管線就緒 + 測試人員邀請卡點說明"""
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
        "🧵 <b>v20 進度：Threads 自動發布管線已就緒</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "<b>✅ 已完成（全自動）</b>\n"
        "  • Meta 應用程式「pinocchio publisher」建立完成\n"
        "  • threads_basic + threads_content_publish 權限已啟用\n"
        "  • <code>threads_publisher.py</code> 上線：60 天 token 自動續期、\n"
        "    發文佇列（初期每日 1 篇）、發布結果回報到本主題\n"
        "  • daemon 已重啟，worker 待命中 — token 一到手立即運作\n\n"
        "<b>⏸ 卡在最後一步（Meta 防濫用）</b>\n"
        "  新增 Threads 測試人員被靜默拒絕 — 因為皮諾丘的帳號\n"
        "  是今天剛建立的全空帳號（無大頭貼/簡介/串文）。\n\n"
        "<b>🙋 需要你做的（幾分鐘）</b>\n"
        "  1. 用 Threads App 補齊 @pinocchioweb3 個人檔案：\n"
        "      大頭貼 + 簡介（例：AI 共建的開源交易機器人，建造日誌連載中）\n"
        "  2. 手動發出第一篇建造日誌（草稿之前已給你）\n\n"
        "我會自動定時重試邀請（不用你按任何東西）；邀請成功後\n"
        "你只需在 Threads App「設定→帳號→網站權限→邀請」按接受，\n"
        "剩下的 token 產生與串接我會接手完成。"
    )
    r = await router.client("system").send_message(msg, parse_mode="HTML")
    print(f"[v20] notice: ok={r.get('ok')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
