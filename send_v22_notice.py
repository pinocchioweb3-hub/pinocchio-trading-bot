"""v22-2 入群閘門上線通知"""
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
    me = await TelegramClient().get_me()
    bot_username = (me.get("result") or {}).get("username", "bot")
    msg = (
        "🚪 <b>v22-2 上線：邀請碼自動入群閘門</b>（48 小時承諾，提前完成）\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "<b>完整漏斗已就緒：</b>\n"
        f"Threads 貼文 → <code>t.me/{bot_username}?start=join</code> →\n"
        "私訊選單 → 貼 OKX UID → 聯盟 API 自動驗證 →\n"
        "專屬入群連結（10 分鐘時效、綁定本人、轉傳無效）→ 自動審批入群\n\n"
        "<b>防濫用：</b>CAS 反垃圾黑名單、每小時 5 次嘗試上限、連結用後即毀\n"
        "<b>舊用戶：</b>自動收集情況轉交你 + <code>/gate_approve</code> 一鍵放行\n\n"
        "<b>🧪 你現在就能測（手機就可以）：</b>\n"
        f"1. 私訊 @{bot_username} 按 START\n"
        "2. 點「🆕 我要註冊 OKX」\n"
        "3. 貼測試 UID：<code>8888888</code>（目前是模擬模式）\n"
        "4. 點收到的連結 → 應該會自動獲准入群\n\n"
        "<b>⏳ 等你的兩件事：</b>\n"
        "1. OKX Affiliate 申請（okx.com/affiliates）— 批准後開一把\n"
        "    <b>唯讀</b> API key，模擬模式自動切換成真實驗證\n"
        "2. 確認 bot 在群組有「邀請用戶」管理員權限（測試時若入群失敗就是這個）"
    )
    r = await router.client("system").send_message(msg, parse_mode="HTML")
    print(f"[v22-2] notice: ok={r.get('ok')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
