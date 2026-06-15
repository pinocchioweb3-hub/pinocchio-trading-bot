"""v44 升級通知（R 去美元化 + 槓桿開放/中性化 — ER 就是 ER，倍數自己定）"""
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
        "🎚️ <b>升級上線：v44 — R 就是 R、槓桿你自己定</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "之前訊息把「1R＝100U」綁死、又預設幫你挑「最低槓桿」——這其實是替交易員做決定。"
        "這版<b>徹底改掉</b>：金額你自己設、槓桿你自己選，機器人只給「對的資訊、結構、數據」，"
        "<b>不替你決定該冒多少、該開幾倍</b>。全程純讀、零臆測、不下單、不對外發布。\n\n"
        "<b>① R 去美元化（ER 就是 ER）</b>\n"
        "• 訊號的倉位區塊<b>不再顯示任何金額／保證金／「1萬U→100U」範例</b>。\n"
        "• R 只表示「一個風險單位」＝進場到止損的價差；<b>1R 等於多少錢、倉位開多大，全由你自己定</b>。\n"
        "• 訊息附上常見做法：把單筆風險固定成<b>總倉的 2% / 2.5% / 3% / 5%</b>，自己挑一個長期守住。\n\n"
        "<b>② 槓桿開放到 1–50x，且中性化</b>\n"
        "• <code>/settings</code> 新增槓桿一排按鈕：<b>5x／10x／15x／20x／30x／50x</b>，點一下即時套用。\n"
        "• 拿掉「建議用最低槓桿」這類話術——<b>尊重你的決定</b>，只誠實提醒「槓桿越高離爆倉越近」。\n"
        "• 安全護欄仍在（純風險提示，不替你改）：單筆保證金偏重時會提醒；高波動小幣系統自動降槓桿防插針爆倉。\n\n"
        "<b>③ 對照表同步更新</b>\n"
        "• <code>/指標 R</code>、<code>/指標 槓桿</code> 已改寫成新口徑（R 不是固定 100U、槓桿 1–50x 自己定）。\n\n"
        "<b>④ 你這台零行為改變</b>\n"
        "你的部署是 $5,000、明確設了 15x／1R=$100——依「明確值優先」原則，<b>實際下單數學完全沒動</b>，"
        "改的只是「顯示與口吻」和「多了可調選項」。\n\n"
        "<b>⑤ 同步開了兩條並行研究分支</b>（背景 Session，產出設計報告供你過目，不自動改交易邏輯）：\n"
        "• 🎯 <b>止損掃單規避</b>：用即時清算/OI 數據定位「止損磁鐵」，配合結構調整止損位置（研究中）。\n"
        "• 🔌 <b>通用下單指令 schema</b>：讓任何交易所 AI agent 都讀得懂我們的訊號（<b>已完成</b>，"
        "結論：做「交易意圖層」可行、「下單參數層」交給 CCXT，永不自動下實盤）。\n\n"
        "<i>本通知由 Claude Code 自行撰寫。</i>"
    )
    r = await router.client("system").send_message(msg, parse_mode="HTML")
    print(f"[v44] notice: ok={r.get('ok')}")
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
