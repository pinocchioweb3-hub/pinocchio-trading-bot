"""v42 升級通知（依預算自適應風控分級 — 把「3000U 陪跑」改成「依各自預算」）"""
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
        "🎚️ <b>升級上線：v42 — 風控改吃「依各自預算自適應」</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "原本「3000U 陪跑」這個框架太死板 —— 開源給別人自架時，每個人的本金都不一樣。"
        "這版把<b>本金變成一個可設定的參數</b>，所有風控護欄依本金<b>自動分級</b>。\n"
        "全程<b>純讀、零臆測、不下單、不對外發布</b>。\n\n"
        "<b>① 本金分級（未設定時的保守預設）</b>\n"
        "• micro 本金&lt;1,000U → 槓桿 3x／風險 1.0%／日開倉 2／總曝險 5%／無獨立現貨\n"
        "• small 1,000–4,999U → 5x／1.0%／日 3／6%\n"
        "• standard 5,000–9,999U → 同上 ＋ 解鎖獨立現貨策略\n"
        "• large ≥10,000U → 同 standard\n"
        "<b>本金越小，保護越嚴；且永不比大本金更激進。</b>\n\n"
        "<b>② 鐵律：你明確設定的值，永遠優先（你最大）</b>\n"
        "分級<b>只會去填你沒設定的鍵</b>。你在 .env 明確寫死的值一律壓過 tier 預設。\n\n"
        "<b>③ 你這台目前完全沒被改動（零行為改變）</b>\n"
        "你的部署是 $5,000＝Standard，且 .env 明確設了 <code>DEFAULT_LEVERAGE=15</code>、"
        "<code>RISK_PER_TRADE_USD=100</code> —— 依「明確值優先」原則，<b>1R=$100／15x／"
        "總曝險 6%／日開倉 3 全部照舊</b>。tiering 只在你「清掉明確值」或「別人用不同本金"
        "自架」時才生效。\n\n"
        "<b>④ 教練再補兩招（純提醒，非操作指令）</b>\n"
        "• 🪙 <b>手續費侵蝕</b>：止損抓太窄時，來回 taker 手續費會吃掉一大塊 1R → 提醒改用"
        "較寬的結構止損或較高時框\n"
        "• 📉 <b>回撤降檔</b>：今日逼近熔斷線時 → 提醒把後續每筆風險砍半、放慢節奏\n\n"
        "<b>⑤ 順手拔掉「利潤承諾」</b>\n"
        "把舊提示詞裡「目標每天 +$100、月報酬 X%」這類<b>不該承諾的數字</b>清掉了，"
        "改成只講「追求穩健正期望值與嚴格風控」。這是紅線：不臆造勝率／報酬。\n\n"
        "<b>需要你拍板</b>（<code>/decisions</code>）：\n"
        "#「你自己這台要不要改吃 tier 預設」—— 維持現狀（15x／$100）最自由；"
        "或拿掉那兩個 .env 鍵讓本金自動決定（$5000＝5x／1.0%＝$50）。<b>這純粹是你這台的選擇，"
        "不是非改不可。</b>\n\n"
        "<i>本通知由 Claude Code 自行撰寫。</i>"
    )
    r = await router.client("system").send_message(msg, parse_mode="HTML")
    print(f"[v42] notice: ok={r.get('ok')}")
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
