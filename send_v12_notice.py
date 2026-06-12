"""v12 升級通知"""
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


async def main():
    tg = TelegramClient()
    if not tg.configured():
        return 1

    msg = (
        "🚀 <b>機器人升級 v12 上線</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>本次解決你反饋的 4 大痛點：</b>\n\n"

        "<b>1. ✅ Trade Monitor（每 15min 自動追蹤）</b>\n"
        "  • 每筆持倉自動 poll OKX 5m 即時價\n"
        "  • 觸 TP1 (50%) / TP2 (30%) / TP3 (20%) 自動 record + 推通知\n"
        "  • 觸 SL 自動 record 全平 + 推通知\n"
        "  • 進場超過 48h 自動 timeout 平倉\n"
        "  • 每個事件會 reply 到原 FIRE 訊息（追蹤關聯）\n\n"

        "<b>2. ✅ Hourly Position Tracker（持倉快照）</b>\n"
        "  • 每整點推「目前持倉狀態」（無持倉則不推）\n"
        "  • 每筆顯示：進場價 / 現價 / 當前 R / 距 TP1 / 距 SL\n"
        "  • 狀態 icon：🎯 過 TP1 / 🟢 半路 / 🟡 微利 / 🟠 微虧 / 🔴 接近 SL\n\n"

        "<b>3. ✅ Deep Dive 排除已開單品種</b>\n"
        "  • 每 6h deep dive 不再推同樣標的\n"
        "  • 若 ETH/SOL 已開單，會自動推下一個強勢非開單品種\n\n"

        "<b>4. ✅ Cooldown 1hr → 4hr</b>\n"
        "  • 大幅降低短時間重複 FIRE\n"
        "  • 配合 risk_manager 的「同 symbol+direction 開單時擋」雙保險\n\n"

        "<b>新 watchlist：</b>\n"
        "  <code>['BCH','FIL','ARB','BTC','ETH','UNI','DOGE','AVAX']</code>\n\n"

        "<b>下一步（已排隊）：</b>\n"
        "  • Phase 2：Auto-Tuner（推薦+按鈕確認模式）\n"
        "  • Phase 3a：X (Twitter) 高訊號帳號清單分析\n"
        "  • Phase 3b：Truth Social RSS + Apify Twitter Scraper 串接"
    )

    r = await tg.send_message(msg, parse_mode="HTML")
    print(f"[v12] notice: ok={r.get('ok')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
