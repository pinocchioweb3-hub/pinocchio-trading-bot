"""v52 升級通知（接上 CoinGlass 加密新聞即時推播 — AI 過濾 + 繁中翻譯）"""
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
        "📰 <b>升級上線：v52 — 接上 CoinGlass 加密新聞即時推播</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "依你的指示「<b>CoinGlass 的新聞和即時推播，先幫我串接</b>」，已完成並上線。\n\n"
        "<b>① 接了什麼</b>\n"
        "• 新增「加密快訊」Session：每 10 分鐘自動抓 CoinGlass 新聞列表"
        "（來源含 CoinDesk、The Block 等）。\n"
        "• 每則先過 <b>AI 智慧過濾</b>（只留對交易有參考價值的、重要度 ≥7 才推），"
        "再<b>翻成繁體中文</b>後推送。\n"
        "• 推到你既有的 <b>📰新聞快訊</b> 頻道，與美股快訊並排。\n\n"
        "<b>② 怎麼防洪水 / 防重複</b>\n"
        "• 每輪最多推 2 則（降噪）。\n"
        "• 同一條新聞<b>跨來源</b>（CoinGlass 與美股快訊撞同一事件）只推一次。\n"
        "• CoinGlass 新聞沒有原生 id → 用「標題＋發布時間」雜湊當主鍵，<b>重啟也不會重推</b>。\n"
        "• 只推 <b>3 小時內</b>的新聞（冷啟動不洗版舊聞）。\n\n"
        "<b>③ 範圍與紅線（重要）</b>\n"
        "• 只接「<b>數據與推播</b>」，<b>不下單、不碰金鑰、不動任何訊號數學</b>。\n"
        "• 用你現有的 CoinGlass $79 方案就有，<b>無需升級</b>。\n"
        "• 翻譯走 Claude Code（你的 Max 訂閱，$0 邊際成本）。\n\n"
        "<b>④ 品質</b>\n"
        "• 真 key 實測：成功抓到真實新聞（CoinDesk / The Block）。\n"
        "• 離線測試 10 案全綠、全套件 80 案無回歸、語法檢查通過。\n"
        "• 已照「改 daemon 前防踩踏」流程乾淨重啟（新 PID、錯誤日誌空、各 Session 上線）。\n\n"
        "<i>本通知由 Claude Code 自行撰寫。</i>"
    )
    r = await router.client("system").send_message(msg, parse_mode="HTML")
    print(f"[v52] notice: ok={r.get('ok')}")
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
