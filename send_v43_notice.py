"""v43 升級通知（指標白話對照表 — 看不懂術語就查這個；雙受眾呈現層第一塊）"""
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
        "📖 <b>升級上線：v43 — 指標白話對照表</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "機器人滿口 CVD／OI／資金費率／R 倍數 —— <b>看不懂就沒法信任、也沒法照著做</b>。"
        "這版加了一張白話對照表，把機器人實際會吐出的<b>每一個術語</b>用三句話講清楚。\n"
        "全程<b>純讀、零臆測、不下單、不對外發布</b>。\n\n"
        "<b>① 怎麼用</b>\n"
        "• <code>/指標</code>（或 <code>/glossary</code>）→ 全部術語一覽（依五大類）\n"
        "• <code>/指標 CVD</code> → 查單一術語的完整說明\n"
        "• <code>/指標 資金費率</code> → 中文名也查得到\n\n"
        "<b>② 每個術語都講三件事</b>\n"
        "• <b>白話</b>：這到底是什麼（給完全沒概念的人）\n"
        "• <b>怎麼看</b>：數值高/低各代表什麼\n"
        "• <b>誠實提醒</b>：它的<b>侷限</b> —— 例如「資金費率不是越高越好，它衡量擁擠度、"
        "過熱反而是反指標」「強勢分是回看排名、不是勝率保證」「CVD 背離只是線索不是進場訊號」。"
        "<b>這是紅線：對照表本身不預測、不承諾報酬。</b>\n\n"
        "<b>③ 收錄 30 個術語、分五大類</b>\n"
        "🎯 風控與部位／🌊 訂單流與情緒／🏗 結構與趨勢／🌐 宏觀與估值／🐋 鯨魚與機構。\n"
        "只收「機器人實際會用到」的詞，不灌水。\n\n"
        "<b>④ 同一份資料、兩種受眾</b>\n"
        "這張表用<b>單一真實來源</b>同時產出「人看的卡片」與「機器讀的 JSON」"
        "（<code>/指標 json</code>）—— 為之後的 AI Agent 與信任網頁鋪路。\n\n"
        "<i>本通知由 Claude Code 自行撰寫。</i>"
    )
    r = await router.client("system").send_message(msg, parse_mode="HTML")
    print(f"[v43] notice: ok={r.get('ok')}")
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
