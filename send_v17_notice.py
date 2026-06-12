"""v17 升級通知"""
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
        "🚀 <b>機器人升級 v17 上線（美股引擎 + 情報重整版）</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        "<b>1. 🧪🇺🇸 美股 24h 突破訊號引擎（全新）</b>\n"
        "  • 8 檔高流動性白名單：MU/SNDK/SOXL/MRVL/NVDA/INTC/ORCL/QQQ\n"
        "  • 訊號邏輯：1h 收盤突破 24h 高低 + 量能/資費/買賣流確認投票\n"
        "  • QQQ 大盤閘 + 經濟數據靜默 + 財報日停發 + 夜間週末停發\n"
        "  • ⚠️ <b>實驗性：僅紙上自動追蹤，請勿實單跟隨</b>\n"
        "  　（累積 30 筆驗證期望值為正後才升級）\n\n"

        "<b>2. 🐦 X 追蹤清單重整（38 → 33 帳號）</b>\n"
        "  新增美股/總經快訊：Walter Bloomberg、First Squawk、\n"
        "  Unusual Whales、Kobeissi Letter、<b>Fed 傳聲筒 Nick Timiraos</b>、Fed 官方\n"
        "  移除 11 個低訊號帳號（含 X 上的 Trump — Truth Social 已全量覆蓋）\n"
        "  快訊帳號智慧限流：盤中寬鬆、盤外嚴格、每帳號每小時上限 15 則\n\n"

        "<b>3. 📐 市場情報全面改版（解決視覺疲勞）</b>\n"
        "  • 每日宏觀：掃讀層 ≤600 字（風險燈號 + 固定儀表板：\n"
        "  　加密/美股/美元債息/黃金 各一行）→ 細節收進可展開引用塊\n"
        "  • 每小時 Pulse：≤300 字，無顯著變化只推一行\n"
        "  • 新增黃金/美元/美債「期貨代碼」24h 報價（盤後不再失明）\n\n"

        "<b>4. 📊 經濟數據源評比結論</b>\n"
        "  研究了 GitHub 全部主流方案（OpenBB/investpy/各日曆庫）—\n"
        "  <b>我們現有的三源組合已是免費方案最優</b>，無需更換。\n"
        "  下一步補強：FRED 官方數據校驗（需你註冊免費 key，之後教你）"
    )
    r = await router.general().send_message(msg, parse_mode="HTML")
    print(f"[v17] notice: ok={r.get('ok')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
