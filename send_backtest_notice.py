"""Push backtest reality check to Telegram."""
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
        f"🔬 <b>真實歷史回測結果 [{now}]</b>\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        "<b>30 天真實 BTC + SUI 歷史測試：</b>\n\n"
        "<code>BTC intraday: 1 筆 100% +0.98R</code>\n"
        "<code>BTC ambush  : 0 筆（沒 FIRE）</code>\n"
        "<code>SUI intraday: 2 筆 0%   -0.41R</code>\n"
        "<code>SUI ambush  : 0 筆（沒 FIRE）</code>\n\n"
        "<b>⚠️ 重要揭露：</b>\n"
        "之前 mock 跑出的 <b>89% 勝率是假象</b>。\n"
        "真實小樣本顯示 setup 訊號頻率極低、ambush 完全沒 FIRE、SUI 兩筆都 timeout。\n\n"
        "<b>3 筆樣本無統計意義，但揭露了必須修的問題：</b>\n"
        "• 訊號閾值可能太嚴\n"
        "• Setup B 結構條件太苛刻\n"
        "• Timeout 出場太多 → hold_max_hours 24h 可能不夠\n\n"
        "<i>正在跑更大樣本（6 幣 × 多閾值）。</i>"
    )
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"https://api.telegram.org/bot{tk}/sendMessage",
            json={"chat_id": cid, "text": text, "parse_mode": "HTML"},
        )
        print(f"TG: ok={r.json().get('ok')}  msg_id={r.json().get('result', {}).get('message_id')}")


if __name__ == "__main__":
    asyncio.run(main())
