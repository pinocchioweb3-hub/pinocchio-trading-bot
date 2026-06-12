"""v18 全部完工總結通知"""
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
        "🏆 <b>v18 大改革全部完工！（A-F 六項一夜完成）</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "競品分析驅動的全面補強，六項全數上線：\n\n"

        "⚡ <b>A・全市場異常掃描器</b>\n"
        "　356 檔全覆蓋（原 8 檔）｜上線 1 小時即捕捉 LAB -10.7% 急跌\n\n"
        "🔓 <b>C・代幣解鎖日曆</b>\n"
        "　事前數週預警｜首輪即發現 SPK 6/17 解鎖 19.9%、VANA 10.1%\n\n"
        "📜 <b>B・相同條件歷史類比</b>\n"
        "　每筆訊號附「歷史類似條件 N 次/勝率/平均 R」誠實實證\n\n"
        "⏳ <b>D・等待觸發進場</b>\n"
        "　三態決策：進場區內直接 FIRE／偏離就等回踩／6h 不回自動放棄 — 不追價\n\n"
        "🌐 <b>E・市場廣度 regime</b>\n"
        "　Pulse 頂部固定廣度行｜極端逆風訊號自動加警示\n\n"
        "📐 <b>F・SMC 圖表標記</b>\n"
        "　FIRE 與深度分析自動附圖：K 線上畫 FVG/OB/BoS/swing + 進場止損止盈線\n"
        "　（剛剛交易訊號頻道的 BTC 圖就是示範）\n\n"

        "━━━━━━━━━━━━━━━━\n"
        "<b>現在的系統 vs 競品 SM 浪潮：</b>\n"
        "  他們有的（全市場掃描/歷史類比/三態進場/廣度/黑天鵝）→ ✅ 我們都有了\n"
        "  我們獨有的（總經數據/美股引擎/消息面 AI/誠實雙帳本/圖表標記）→ 持續領先\n\n"
        "<b>🤖 接下來：</b>系統進入累積期 — 紙上帳收集樣本驗證期望值，"
        "我每天自動健檢一次（worker 狀態/警報品質/數據累積），有問題自動修。\n"
        "20 個 worker 24/7 為你運轉中。晚安 🌙"
    )
    r = await router.general().send_message(msg, parse_mode="HTML")
    print(f"[v18-final] notice: ok={r.get('ok')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
