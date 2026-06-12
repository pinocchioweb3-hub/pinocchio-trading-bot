"""v16 升級通知"""
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
        "🚀 <b>機器人升級 v16 上線（消息面×技術面整合版）</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        "<b>1. 📅 全新「經濟數據」頻道</b>\n"
        "  • 每天 08:10 台北：今日美國數據預告 + FOMC 利率機率（Kalshi 預測市場）\n"
        "  • CPI/PPI/非農/FOMC 發布前 30 分鐘：預警 + <b>訊號靜默期</b>\n"
        "  • 發布後秒級抓實際值 → AI 判讀利好/利空加密與美股\n"
        "  • 實測：今天的初領失業金 229K（預期 219K）數據源直接抓到\n\n"

        "<b>2. 🔇 消息面×技術面整合（你指出的盲點）</b>\n"
        "  高影響數據發布前 30 分／後 15 分鐘 → <b>自動暫停新交易訊號</b>\n"
        "  （技術分析在消息行情中失靈 — 系統現在懂這件事了）\n\n"

        "<b>3. 📜 紙上驗證自動追蹤（你要的流程）</b>\n"
        "  每筆 FIRE 訊號推送即自動開「紙上倉」15 分鐘追蹤 TP/SL，不用等按鈕\n"
        "  • 紙上帳 = 驗證引擎期望值（自動交易 Stage 1 需 100 筆）\n"
        "  • 實倉帳 = 你按 ✅ 的真實績效\n"
        "  • 兩本帳並行，/status 都看得到\n\n"

        "<b>4. 🇺🇸 美股頻道正式啟用</b>\n"
        "  每天兩推：開盤前瞻（21:25 台北）+ 收盤總結（04:05 台北）\n"
        "  25 檔 OKX 美股永續：波動榜 / 成交量榜 / 加密概念股\n\n"

        "<b>5. 🎯 交易計畫歸類修正</b>\n"
        "  Deep Dive 交易計畫從「市場情報」移到「交易訊號」頻道\n\n"

        "<b>6. 💰 Apify 方案建議（X 推文）</b>\n"
        "  分析結論：建議升級 <b>Starter $29/月</b>（詳見聊天室報告）\n"
        "  已做省費優化：增量抓取 + 深夜降頻，實際用量壓到 ~$4/月"
    )
    r = await router.general().send_message(msg, parse_mode="HTML")
    print(f"[v16] notice: ok={r.get('ok')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
