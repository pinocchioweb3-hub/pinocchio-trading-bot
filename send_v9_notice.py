"""Send v9 upgrade notice via Python (avoid PS encoding issues)."""
import asyncio
import datetime as dt
import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

import httpx


async def main():
    tk = os.getenv("TELEGRAM_BOT_TOKEN")
    cid = os.getenv("TELEGRAM_CHAT_ID")
    now = dt.datetime.now().strftime("%H:%M:%S")
    text = (
        f"🚀 <b>系統升級 v9 [{now}]</b>\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        "<b>Deep Dive 重大品質提升（8 → 9.5/10）：</b>\n\n"
        "✅ 接入 <b>joshyattridge/smart-money-concepts</b> 套件\n"
        "   — FVG / OB / BoS / CHoCH / Liquidity / Swing 全量化偵測\n"
        "   — 4h 戰術 + 1d 戰略 雙時框\n\n"
        "✅ 加入<b>帳戶約束</b>到 Claude prompt\n"
        "   — 傳「margin $500-800, risk $100/筆」\n"
        "   — Claude 必須算 notional/margin/leverage 並驗算\n"
        "   — 不再給「5x 槓桿但實際塞不進帳戶」的錯誤建議\n\n"
        "✅ <b>TP 後保護動作</b>完整指令\n"
        "   — TP1→SL 移保本\n"
        "   — TP2→SL 移 TP1 價（鎖小利）\n"
        "   — TP3→trailing 1×ATR\n\n"
        "✅ <b>時段風險</b>具體時段警示\n"
        "   — UTC 20:00-01:00 亞洲深夜流動性薄 → 倉位縮 30%\n\n"
        "<b>下一份 Deep Dive 預計 5-10 分鐘內推送</b>，含 trading tier top 3 幣完整 SMC 量化交易計畫。"
    )
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"https://api.telegram.org/bot{tk}/sendMessage",
            json={"chat_id": cid, "text": text, "parse_mode": "HTML"},
        )
        body = r.json()
    if body.get("ok"):
        print(f"OK msg_id={body['result']['message_id']}")
    else:
        print(f"FAIL: {body}")


if __name__ == "__main__":
    asyncio.run(main())
