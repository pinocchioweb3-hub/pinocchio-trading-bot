"""v18-B 上線通知"""
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
        "🚀 <b>v18 第三彈：相同條件歷史類比上線</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "<b>📜 B 項完成：每筆訊號附歷史實證</b>\n"
        "從下一筆 FIRE 開始（加密與美股都有），訊號訊息會多一行：\n\n"
        "<i>📜 相同條件歷史：近 300 根 1h 出現 22 次｜先觸 +1R 機率 23%｜"
        "平均 +0.15R 🟢｜中位 12h</i>\n\n"
        "<b>運作方式：</b>\n"
        "  • FIRE 當下回看 300 根 1h K 線（約 12.5 天）\n"
        "  • 找出「動能方向 + K 線方向 + 量能等級」相似的歷史時點\n"
        "  • 每個時點模擬向前 12 小時的結果（與實盤同樣的 4% 止損）\n\n"
        "<b>誠實口徑（與行銷型機器人的差異）：</b>\n"
        "  • 同根 K 高低同時觸及 → 保守算輸\n"
        "  • 樣本 &lt;8 次 → 直接標「歷史樣本不足」不硬擠數字\n"
        "  • 用實盤一致的止損距離，不用寬鬆口徑膨脹勝率\n"
        "  💡 看「平均 R」比看「勝率」更有意義（正 R = 有期望值）\n\n"
        "<b>另：⚡ 掃描器已實戰開張</b> — 上線 1 小時即捕捉 LAB 急跌 -10.7%"
        "（price+OI 雙條件觸發），SAHARA 級事件雷達正式運作。\n\n"
        "<b>📋 佇列：</b>D 等待觸發 → E 廣度閘門 → F SMC 圖表（自動接續）"
    )
    r = await router.general().send_message(msg, parse_mode="HTML")
    print(f"[v18b] notice: ok={r.get('ok')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
