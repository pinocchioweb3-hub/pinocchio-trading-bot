"""v18-A 上線通知"""
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
        "🚀 <b>v18 大改革啟動 — 第一彈：全市場異常掃描器上線</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "競品分析後的補強計畫（A-F 六項）開始連續作業，"
        "完成一項推一次通知，不需要你做任何事。\n\n"

        "<b>⚡ A 項已上線：全市場異常掃描器</b>\n"
        "  • 監控範圍：<b>8 檔 → 356 檔</b>（OKX 全部 USDT 永續）\n"
        "  • 每 5 分鐘：1h 急漲跌 ±6% / OI 劇變 / 資費極值 多條件聯合偵測\n"
        "  • $10M 流動性門檻 + 6h 冷卻 + 單輪最多 3 則（不洗版）\n"
        "  • SAHARA 級崩盤（6/9 那種 -60%）從此在雷達上\n"
        "  • /status 新增「市場廣度」行（全市場多空檔數）\n"
        "  ⚠️ 異常警報是「人工評估候選」非交易訊號（白名單外無結構分析）\n\n"

        "<b>📋 接下來自動排程：</b>\n"
        "  C 解鎖日曆黑名單（事前數週預警）→ B 訊號附歷史類比勝率 →\n"
        "  D 等待觸發進場 → E 廣度 regime 閘門 → F SMC 圖表標記推圖\n"
        "  （額度用盡會自動暫停，恢復後自動繼續）"
    )
    r = await router.general().send_message(msg, parse_mode="HTML")
    print(f"[v18a] notice: ok={r.get('ok')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
