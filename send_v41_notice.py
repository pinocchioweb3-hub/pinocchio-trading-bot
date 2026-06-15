"""v41 升級通知（教練式持倉提醒 + 紀律遵守率 KPI — 3000U 陪跑核心）"""
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
        "🧑‍🏫 <b>升級上線：v41 — 會踩煞車的教練 + 紀律分數</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "小資爆倉，多半不是看錯方向，而是<b>紀律破口</b>：追高、把止損往後挪、"
        "今天輸了還想凹回來。這版專為「3000U 陪跑」補上兩件事 —— 都<b>純讀、零臆測、"
        "不下單、不對外發布</b>。\n\n"
        "<b>① 教練式持倉提醒（只盯你真正下單的實倉）</b>\n"
        "在持倉主題裡，當偵測到下列情況會輕拍你一下：\n"
        "• 🛑 <b>接近止損</b>（已走完 75% 到止損、仍水下）→ 提醒「別把止損往後挪」\n"
        "• 🏃 <b>追高進場</b>（市價追單）→ 回顧提醒，下次用「等待觸發」進更好的價\n"
        "• ✋ <b>今天別再交易了</b>（達日開倉上限／觸日線熔斷）→ 帳戶級每日提醒一次\n"
        "• ⚖️ <b>曝險接近上限</b>（≥80% 風險上限）→ 寧可錯過，不要重壓\n"
        "全是「教練提醒，非操作指令」；紙上自動驗證倉不教練，不洗你版。\n\n"
        "<b>② 紀律遵守率 KPI（系統客觀記錄，不灌水）</b>\n"
        "輸入 <code>/discipline</code>（或 <code>/kpi</code>）看兩個分數：\n"
        "• <b>決斷率</b>＝有意識處理(進場／平倉／主動略過) ÷ (處理＋放生過期)\n"
        "• <b>不追高率</b>＝區內進場(直接／等待觸發) ÷ (區內＋追高市價)\n"
        "兩項都從交易紀錄客觀欄位算出；<b>樣本不足就顯示「資料累積中」，絕不用小樣本充數</b>。\n\n"
        "<i>現況誠實說</i>：你目前實單紀錄還是空的（尚未真正下過實盤訊號），"
        "所以教練與決斷率<b>會等你真的開始交易才啟動</b> —— 這是對的，不是壞掉。\n\n"
        "CEO 每日簡報也加了一行紀律摘要；<code>/ceo</code> 隨時拉。\n"
        "<i>本通知由 Claude Code 自行撰寫。</i>"
    )
    r = await router.client("system").send_message(msg, parse_mode="HTML")
    print(f"[v41] notice: ok={r.get('ok')}")
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
