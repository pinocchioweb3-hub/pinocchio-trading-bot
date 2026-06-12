"""v13 升級通知"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from news_feed.twitter_accounts import TWITTER_ACCOUNTS
from telegram_bot.client import TelegramClient


async def main():
    tg = TelegramClient()
    if not tg.configured():
        return 1

    from collections import Counter
    tier_count = Counter(v["tier"] for v in TWITTER_ACCOUNTS.values())

    msg = (
        "🚀 <b>機器人升級 v13 上線（情報維度擴張）</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>本次新功能：</b>\n\n"

        "<b>1. ✅ Truth Social RSS（Trump 第一手）</b>\n"
        "  • 每 5 分鐘抓 trumpstruth.org（免費、不需 API）\n"
        "  • Trump 每篇貼文都會推給你（包含廢文、政治、外交、crypto）\n"
        "  • 首次啟動已標記 99 篇歷史貼文為 seen（不會洗版）\n"
        "  • 自動 dedupe，不重推\n\n"

        "<b>2. ⏳ Apify X (Twitter) — 待你 token</b>\n"
        f"  • 38 個帳號分 7 個 tier 過濾\n"
        f"  • T0 全推（{tier_count.get('T0', 0)}）：Trump + 政府機構\n"
        f"  • Tm Macro（{tier_count.get('Tm', 0)}）：Elon + Cathie\n"
        f"  • T1 交易所（{tier_count.get('T1', 0)}）：list/delist 才推\n"
        f"  • T2 鯨魚（{tier_count.get('T2', 0)}）：$30M+ 才推\n"
        f"  • T3 創辦人（{tier_count.get('T3', 0)}）：ticker 或政策才推\n"
        f"  • T4 交易員（{tier_count.get('T4', 0)}）：ticker mention 才推\n"
        f"  • T5 新聞（{tier_count.get('T5', 0)}）：ticker 或關鍵字才推\n\n"

        "<b>📋 啟用 X 的 5 步驟：</b>\n"
        "  1. 開 console.apify.com 註冊（Google 登入最快）\n"
        "  2. Settings → Subscription → 免費方案（$5 credits/月）\n"
        "  3. Settings → Integrations → Personal API tokens → Create\n"
        "  4. 複製 token（千萬不要貼給我！）\n"
        "  5. 編輯 .env 加：<code>APIFY_TOKEN=你的token</code> → 重啟 bot\n\n"

        "<b>啟用後預估月成本：~$2-3 USD</b>\n"
        "<b>過濾率預估 80-90%</b>，TG 不會被洗版\n\n"

        "💡 <b>接下來 1-2 小時內會收到第一則 Trump 推送（如果他剛發文）</b>"
    )

    r = await tg.send_message(msg, parse_mode="HTML")
    print(f"[v13] notice: ok={r.get('ok')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
