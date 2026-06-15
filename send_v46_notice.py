"""v46 升級通知（接上 Hyperliquid 鏈上永續原生數據 — 免金鑰、唯讀、與 CEX 獨立的 confluence 確認源）"""
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
        "🔌 <b>升級上線：v46 — 接上 Hyperliquid 鏈上永續原生數據</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "依你的指示，<b>取消 Pionex、改接 Hyperliquid（HL）</b>這間鏈上永續 DEX 的交易數據，已完成並上線。\n\n"
        "<b>① 接了什麼</b>\n"
        "• HL 公開 info API（免金鑰、<b>純唯讀</b>），新增四個資料工具：\n"
        "　− 單幣盤面（標記/預言/中價、24h 漲跌、未平倉 OI、量）\n"
        "　− 資金費率（HL 是<b>每小時收</b>，已自動換算 8 小時%／年化%）\n"
        "　− K 線\n"
        "　− 全市場一覽（總 OI、總量、OI 排行、資金費最熱/最冷）\n\n"
        "<b>② 為什麼這對訊號有價值</b>\n"
        "• HL 是<b>鏈上 DEX</b>，與既有的中心化交易所（CEX）數據彼此獨立。\n"
        "• 當 CEX 與鏈上同時指向同一方向，這個「跨場所一致」本身就是更強的<b>佐證（confluence）</b>；"
        "兩邊背離時，反而是該提高警覺的訊號。\n"
        "• 與原本就有的 CoinGlass 鯨魚倉位互補：一個看「大戶在哪」，一個看「鏈上整體資金費/未平倉怎麼動」。\n\n"
        "<b>③ 範圍與紅線（重要）</b>\n"
        "• 這次<b>只接「數據」</b>，不接下單；HL 工具全部唯讀，零金鑰、零下單能力。\n"
        "• <b>沒有改任何訊號數學</b>——既有判斷邏輯、回測結論完全不動，純粹是多了一個獨立的查證來源。\n"
        "• 下一步若要把 HL 的資金費/OI <b>顯示</b>進訊號卡片，我會先問過你再做（display-only，仍不動訊號行為）。\n\n"
        "<b>④ 品質</b>\n"
        "• 對 HL 主網實測通過：230 檔永續、全市場總 OI 約 $6.9B；模組獨立、含快取＋退避重試＋永不拋例外的錯誤封裝。\n"
        "• 已重啟、運行正常。\n\n"
        "<i>本通知由 Claude Code 自行撰寫。</i>"
    )
    r = await router.client("system").send_message(msg, parse_mode="HTML")
    print(f"[v46] notice: ok={r.get('ok')}")
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
