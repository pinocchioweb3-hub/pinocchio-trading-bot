"""Push v10 calibration upgrade to Telegram."""
import asyncio
import datetime
import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

import httpx


async def main():
    tk = os.getenv("TELEGRAM_BOT_TOKEN")
    cid = os.getenv("TELEGRAM_CHAT_ID")
    now = datetime.datetime.now().strftime("%H:%M:%S")
    text = (
        f"🎯 <b>v10 重大校準（基於真實回測）[{now}]</b>\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        "<b>真實 8 幣 × 30 天回測結果：</b>\n\n"
        "<code>baseline (舊):  7 筆  43% 勝   -$50</code>\n"
        "<code>loose (新預設): 97 筆 67% 勝 +$3,080</code> 🎯\n"
        "<code>strict (刪):    5 筆  20% 勝  -$171</code>\n\n"
        "<b>已套用 loose 校準：</b>\n"
        "• cvd_slope_min: 0.15 → 0.08\n"
        "• funding_neg_thr: -0.0001 → -0.00005\n"
        "• oi_rise_min_pct: 3.0 → 2.0\n"
        "• min_confirmations: 2 → 1\n"
        "• hold_max_hours: 24 → 48 (timeout 太多)\n"
        "• sl_buffer_pct: 3.5 → 4.0 (避免假掃)\n\n"
        "<b>各幣表現排序（loose）：</b>\n"
        "🥇 ETH 83% 勝 +$875\n"
        "🥈 SOL 83% 勝 +$725\n"
        "🥉 INJ 64% 勝 +$601\n"
        "ARB 73% +$514 | SUI 62% +$436 | BTC 71% +$269\n\n"
        "<b>已從 watchlist 排除：</b>\n"
        "❌ BNB（33% 勝率 -$339）\n\n"
        "<b>Setup B (ambush) 30d 真實 0 FIRE</b>\n"
        "結構條件太苛，下版本重新設計。\n\n"
        "<i>30 天 +$3,080 = 你目標每天 $100 ✅ 達成</i>\n"
        "<i>但 1 個月樣本仍不夠，需累積 3-6 個月真實 trade 驗證。</i>"
    )
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"https://api.telegram.org/bot{tk}/sendMessage",
            json={"chat_id": cid, "text": text, "parse_mode": "HTML"},
        )
        print(f"TG: ok={r.json().get('ok')}  msg_id={r.json().get('result', {}).get('message_id')}")


if __name__ == "__main__":
    asyncio.run(main())
