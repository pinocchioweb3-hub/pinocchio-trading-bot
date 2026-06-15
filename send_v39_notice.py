"""v38+v39 升級通知（SMC 走查回測誠實結論 + 風控小資保護閘門 + 治理章程）"""
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
        "🛠 <b>升級上線：v38 + v39 + 治理章程</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "<b>① v38 — SMC 走查式回測管線（誠實結論）</b>\n"
        "用無前視偏誤的滾動回放，驗證「結構訊號＋數據確認」是否真有期望值。\n"
        "3 年實測（BTC/ETH/SOL 池化 262 筆，已扣手續費滑點）：\n"
        "• 純結構 期望 +0.0394R（PSR 0.74，<b>未達顯著</b>＝弱且未證實）\n"
        "• 加數據確認後 Δ = <b>-0.0180R</b>（價格型 confluence <b>未加分</b>）\n"
        "• 訂單流(OI/CVD) 歷史回測拿不到 → 只能靠實盤前向 EV 記錄驗證\n"
        "結論：結構本身無穩定 edge，不過度宣稱。誠實先於好看。\n\n"
        "<b>② v39 — 風控兩道小資保護閘門</b>\n"
        "• 總曝險上限：所有未平倉風險＋本筆 ≤ 帳戶 6%\n"
        "• 每日最多開倉 3 次（防情緒連續開倉，每日 UTC 0 點重置）\n"
        "• 風控金額改帳戶 % 制單一來源；3000U 小資設 % 即自動生效\n"
        "（現行 $5000 帳戶下行為不變，只在小資/風險浮動時成為保護）\n\n"
        "<b>③ 治理章程 PROJECT_CHARTER 建立</b>\n"
        "把「我們要做什麼／現在在哪／還差什麼」寫成一份活地圖，\n"
        "含三條永久紅線、自動化分級、收益四階段瀑布、Phase 0 解鎖閘門。\n"
        "新增 4 個 P0 工作項（風控/CEO監督Session/雙受眾呈現/信任網頁）。\n\n"
        "📋 詳細 CEO 簡報與分潤比例建議，已在開發對話中提供。\n"
        "<i>本通知由 Claude Code 自行撰寫。</i>"
    )
    r = await router.client("system").send_message(msg, parse_mode="HTML")
    print(f"[v39] notice: ok={r.get('ok')}")
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
