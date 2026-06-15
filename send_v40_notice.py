"""v40 升級通知（CEO 監督 Session — 每日彙整簡報 + 決策佇列 + 待審 + Phase0 閘門）"""
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
        "🧭 <b>升級上線：v40 — CEO 監督 Session</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "你說過最大的痛點是「埋在細節裡，忘了整個架構長怎樣、現在在哪、還差什麼」。\n"
        "這版就是為了解決它 —— 幫你請了一位 CEO（我），每天把全局塞回<b>一個視窗</b>。\n\n"
        "<b>① 每日 CEO 簡報（每天 09:00 台北推系統主題）</b>\n"
        "分兩段，你只需要看第二段：\n"
        "• ✅ <b>今日重點</b>：系統健康／績效／風控／驗證進度／功能完成度（掃一眼即可）\n"
        "• ⚠️ <b>需發起人決策</b>：只有這段要你動腦\n"
        "想隨時看：輸入 <code>/ceo</code> 立刻拉一份。\n\n"
        "<b>② 決策佇列</b>　目前已幫你列出兩筆等拍板：\n"
        "• 收益分配四階段瀑布是否核准\n"
        "• 預設槓桿是否由 15x 下調 3x（小資保護）\n"
        "看清單 <code>/decisions</code>；決定後 <code>/decided 編號 說明</code>。\n\n"
        "<b>③ 對外內容待審閘門（紅線2 護欄）</b>\n"
        "未來任何對 Threads／社群／信件的內容，AI 一律先進待審，\n"
        "你 <code>/approve 編號</code> 才送、<code>/reject 編號</code> 退回 —— 永不自動發布。\n\n"
        "<b>④ Phase 0 達標偵測（紅線3）</b>\n"
        "系統會自己算「模擬 ≥100 筆 + 真實 ≥30 筆且期望值正」進度（目前模擬 23/100），\n"
        "達標也<b>只會回報、絕不自我宣告解鎖</b> —— 是否對外宣告永遠由你拍板。\n\n"
        "全程純讀：不下任何單、不發任何對外內容，既有 worker 零行為改變。\n"
        "<i>本通知由 Claude Code 自行撰寫。</i>"
    )
    r = await router.client("system").send_message(msg, parse_mode="HTML")
    print(f"[v40] notice: ok={r.get('ok')}")
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
