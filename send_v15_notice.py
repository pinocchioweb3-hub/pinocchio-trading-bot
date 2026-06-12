"""v15 升級通知（UltraCode 進化版）"""
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
        "🚀 <b>機器人升級 v15 上線（UltraCode 深度進化版）</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "本次由 8 個 AI 分析員深度診斷後全面進化：\n\n"

        "<b>1. 🔘 FIRE 按鈕正式啟用（最重要！）</b>\n"
        "  從下一筆訊號開始，請按訊息下方按鈕回報：\n"
        "  ✅ <b>已下單</b> → 記錄為持倉，開始 TP/SL 自動監控\n"
        "  ⏭ <b>略過</b> → 不記錄\n"
        "  ⏰ 4 小時不按 → 訊號自動過期\n"
        "  💡 <b>只有按過「已下單」的交易才計入勝率與績效</b>——\n"
        "  統計從此 100% 反映你的真實交易，不再有幽靈單。\n\n"

        "<b>2. 💬 新指令（隨時可用）</b>\n"
        "  /status — 系統儀表板（持倉、待確認訊號、PnL、風控）\n"
        "  /stats 30 — 過去 30 天績效\n"
        "  /help — 指令清單\n\n"

        "<b>3. 📂 主題從 4 個擴到 6 個</b>\n"
        "  🎯 交易訊號 — <b>只放 FIRE + 熔斷警報</b>（不再被淹沒）\n"
        "  📈 持倉與績效（新）— TP/SL 事件、持倉快照、績效、風控阻擋\n"
        "  🛠 系統狀態（新）— 開關機、Worker 警報\n"
        "  📊 市場情報、📰 新聞快訊、🇺🇸 美股 — 不變\n\n"

        "<b>4. 🛡 修正的隱藏問題</b>\n"
        "  • 訊息顯示止損 -3.5% 但系統監控 -4.0% 的不一致（已統一 4.0%）\n"
        "  • 熔斷觸發改為「即時警報」推到交易訊號頻道（之前要隔天才知道）\n"
        "  • Worker 崩潰警報加 30 分鐘節流（防轟炸）\n"
        "  • 每筆訊號開始記錄市場波動狀態（自我學習數據累積中）\n\n"

        "<b>5. 🤖 自動交易路線圖（已規劃）</b>\n"
        "  Stage 0 紙上交易驗證 → Stage 1 按鈕一鍵下單 → Stage 2 小倉位全自動\n"
        "  前置條件：紙上 100 筆 + 實倉 30 筆驗證期望值為正\n"
        "  （詳細計畫已存檔，累積夠數據後啟動）"
    )
    r = await router.general().send_message(msg, parse_mode="HTML")
    print(f"[v15] notice: ok={r.get('ok')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
