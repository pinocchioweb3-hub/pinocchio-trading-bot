"""v45 升級通知（通用交易意圖層 — 訊號變成跨所 AI agent 可讀的 trade-intent JSON）"""
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
        "🔌 <b>升級上線：v45 — 訊號變成「跨所通用、AI agent 讀得懂」的可執行 JSON</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "這版把每個 FIRE 訊號，額外編譯成一份<b>通用交易意圖（trade-intent JSON）</b>——"
        "不再只是「給人看的卡片」，而是<b>任何支援的交易所 AI agent 都能直接讀懂</b>的標準格式。\n\n"
        "<b>① 為什麼是「意圖」不是「訂單」</b>\n"
        "• 六家交易所（OKX／Binance／Gate／BingX／Bitget／Bybit）下單概念相同、拼法全不同，"
        "連最基礎的「數量單位」都分成 張數／幣本位／反向張 三種互不相容。\n"
        "• 所以我們只輸出<b>意圖</b>：進場區、失效價、風險%、R 目標——"
        "把張數、tick 進位、單向雙向、保證金模式，全留給交易所 adapter 在執行邊界解"
        "（借鏡 DeFi intent：宣告式，不是命令式）。\n\n"
        "<b>② 怎麼用</b>\n"
        "• 每張 FIRE 訊號卡片下方多了一顆 <b>「📋 複製可執行 JSON」</b>按鈕，點一下即時產生。\n"
        "• 也可用指令 <code>/intent</code>（最近一筆）或 <code>/intent BTC</code>（指定幣別）。\n"
        "• 手機上 JSON 以可一鍵複製的程式碼區塊呈現，貼給任何 AI agent 即可。\n\n"
        "<b>③ 紅線（程式層硬擋，不可違反）</b>\n"
        "• <b>本系統永不自動下實盤</b>。意圖的執行政策只允許「人工把關 / 模擬盤」，"
        "傳入「自動實盤」會被程式直接拒絕。\n"
        "• 意圖只是讓訊號「<b>可被執行</b>」，不是「<b>自動執行</b>」——下不下單、開多大，永遠你決定。\n\n"
        "<b>④ R 去美元化承襲 v44</b>\n"
        "• 整份 JSON <b>不含張數、也不綁固定金額</b>；曝險只用「帳戶風險%」加進場/失效價表達。\n\n"
        "<b>⑤ 品質</b>\n"
        "• 附 JSON Schema（draft 2020-12）可機器校驗；自我測試 16 項 + 接線測試 11 項全通過；"
        "已重啟、運行正常。\n\n"
        "<i>本通知由 Claude Code 自行撰寫。</i>"
    )
    r = await router.client("system").send_message(msg, parse_mode="HTML")
    print(f"[v45] notice: ok={r.get('ok')}")
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
