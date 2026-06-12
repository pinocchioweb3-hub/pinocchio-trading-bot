"""v14 升級通知"""
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
        "🚀 <b>機器人升級 v14 上線（智慧過濾 + 全面中文化 + 體質強化）</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        "<b>1. 🧠 Trump / X 推文智慧過濾</b>\n"
        "  • 每篇貼文先經 Claude 判定：總經 / 美股 / 加密 / 影響市場的地緣政治 → 保留\n"
        "  • 純選舉造勢、罵戰、生活瑣事 → 自動丟棄（不再洗版）\n"
        "  • 純連結無內文的貼文直接跳過\n\n"

        "<b>2. 🇹🇼 全面繁體中文化</b>\n"
        "  • 保留的貼文自動翻譯成繁中 + 一句話摘要 + 重要度評分 1-10\n"
        "  • 🔴 9-10 重大　🟠 6-8 明確訊號　🟡 3-5 背景資訊\n"
        "  • 英文原文收在可展開的引用塊裡（點一下展開）\n\n"

        "<b>3. 👥 Telegram 社群分流（待你建群組）</b>\n"
        "  • 一個社群 4 個主題頻道：🎯交易訊號 / 📊市場情報 / 📰新聞快訊 / 🇺🇸美股\n"
        "  • 建好群組後跑 <code>python setup_telegram_group.py</code> 即自動分流\n"
        "  • 未設定前維持現狀（單一對話）\n\n"

        "<b>4. 🛡 體質強化（上次審查的 P0 修復）</b>\n"
        "  • 資料庫遷出 OneDrive（防鎖檔損毀）\n"
        "  • Worker 崩潰自動重啟（不再一個錯誤全滅）+ 崩潰會推警報\n"
        "  • 電腦重開機自動啟動 bot（Startup 註冊）\n"
        "  • 熔斷基準修正：$5,000 部署資金（日 -3% = -$150 暫停）\n"
        "  • 修復 TP/SL 事件通知的隱藏 bug（之前會靜默失敗）"
    )
    r = await tg.send_message(msg, parse_mode="HTML")
    print(f"[v14] notice: ok={r.get('ok')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
