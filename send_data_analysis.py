"""把數據探索分析結論推到 Telegram。"""
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
        f"📊 <b>數據探索分析報告 [{now}]</b>\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        "<b>用 /data:explore-data 框架深挖 8 幣 × 3 閾值 30d 真實回測：</b>\n\n"
        "<b>🎯 三大發現</b>\n\n"
        "<b>1. 勝率分水嶺 = 70%</b>\n"
        "• ETH/SOL 83% +$725-875 🌟\n"
        "• ARB/BTC 71-73% +$269-514\n"
        "• INJ/SUI 62-64% +$436-601\n"
        "• BNB 33% -$339 → 已踢出\n\n"
        "<b>2. Baseline 失敗 5 個根因</b>\n"
        "• min_confirmations=2 → 真實環境 2 訊號很少同時對齊\n"
        "• cvd_slope_min=0.15 → 1h CVD 多在 0.05-0.10\n"
        "• funding_neg_thr=-0.0001 → 真實多在 ±0.00005\n"
        "• hold_max=24h → 平均 hold 28h，被強制 timeout\n"
        "• 5 個條件相乘 → 30d 只 7 筆\n\n"
        "<b>3. Setup B (ambush) 24/24 全失能 🔴</b>\n"
        "8 幣 × 3 profile 全 0 FIRE\n"
        "根因：cvd_slope_7d 粗估，需重新設計\n\n"
        "<b>🛠 已修系統 BUG：</b>\n"
        "• fire_queue.db 之前每次重啟被清空 → 改成歸檔 + 保留歷史\n"
        "• 加 get_history() 讓未來可做 trade journal 後驗\n\n"
        "<b>下一步重點：</b>\n"
        "累積 7-14 天真實 FIRE 紀錄 → 對比 backtest 預期 → 確認是否真能達到每天 $100"
    )
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"https://api.telegram.org/bot{tk}/sendMessage",
            json={"chat_id": cid, "text": text, "parse_mode": "HTML"},
        )
        print(f"TG: ok={r.json().get('ok')}  msg_id={r.json().get('result', {}).get('message_id')}")


if __name__ == "__main__":
    asyncio.run(main())
