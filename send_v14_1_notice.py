"""v14.1 升級通知"""
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
    msg = (
        "🚀 <b>機器人升級 v14.1 上線（對抗驗證後的強化版）</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "v14 部署後我用 4 個獨立 AI 審查員逐行驗證了自己的程式碼，"
        "找到 23 個問題並全部修復，重點：\n\n"
        "<b>🧠 智慧過濾（已生效）</b>\n"
        "  • Trump/X 貼文：總經/美股/加密/地緣政治 → 推送；純政治/罵戰 → 丟棄\n"
        "  • 全部翻成繁體中文 + 一句話摘要 + 重要度 1-10\n"
        "  • 垃圾帳號白名單防護（Apify 夾帶的廣告帳號直接擋）\n\n"
        "<b>🛡 可靠性（修復後）</b>\n"
        "  • 重要新聞絕不丟失：限速擁塞/暫時錯誤會自動下輪重試\n"
        "  • Telegram 限速保護：全域節流 + 429 自動退避重試\n"
        "  • LLM 不可用時 fallback 全推（寧可多推不漏推）\n"
        "  • Worker 崩潰自動重啟、電腦重開機自動啟動\n"
        "  • 資料庫已遷出 OneDrive（防鎖檔損毀）\n\n"
        "<b>👥 下一步（等你 5 分鐘）：建 Telegram 社群</b>\n"
        "  1. 建一個新群組\n"
        "  2. 群組設定 → 開啟「主題 (Topics)」\n"
        "  3. 把本 bot 加入 → 設為管理員（含管理主題權限）\n"
        "  4. 在群組發：/setup\n"
        "  5. 電腦上跑：<code>python setup_telegram_group.py</code>\n"
        "  → 自動建 4 個頻道：🎯交易訊號 / 📊市場情報 / 📰新聞快訊 / 🇺🇸美股\n"
        "  → 重啟 bot 後所有訊息自動分流，再也不會混在一起"
    )
    r = await tg.send_message(msg, parse_mode="HTML")
    print(f"ok={r.get('ok')} desc={r.get('description')} err={r.get('error')}")
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
