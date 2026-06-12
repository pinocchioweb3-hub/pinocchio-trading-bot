"""即時測試 OKX 官方公告整合 → 直接推到你 Telegram。"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx

from market_intel_mcp.sources.okx_news import get_okx_news


def _esc(s): return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")


def _type_zh(t):
    return {
        "announcements-new-listings": "🆕 新上幣",
        "announcements-delistings": "❌ 下架警報",
        "announcements-trading-updates": "📝 規則變動",
        "announcements-deposit-withdrawal-suspension-resumption": "🚫 入出金中斷",
        "latest-events": "📌 重大事件",
        "announcements-others": "📎 其他",
    }.get(t, t)


def _age(p_ms, now_ms):
    h = (now_ms - p_ms) / 1000 / 3600
    if h < 1: return f"{int(h*60)} 分鐘前"
    if h < 48: return f"{h:.1f} 小時前"
    return f"{int(h/24)} 天前"


async def main():
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not (tg_token and chat_id):
        print("Missing Telegram env"); return 1

    src = get_okx_news()

    # === 拉近 72h 全部公告 + watchlist 相關 ===
    watchlist = ["BTC", "ETH", "SOL", "SUI", "WLFI", "ARB", "DOGE", "XRP",
                 "ADA", "LINK", "BNB", "AVAX"]
    print("[1/2] 拉 OKX 公告（並行 6 個類型，近 72h）...")
    result = await src.get_relevant_for_symbols(watchlist, hours_back=72)
    if result.get("error"):
        print(f"  ❌ {result}"); return 1

    total = result.get("total_recent", 0)
    relevant = result.get("watchlist_relevant", [])
    all_recent = result.get("all_recent", [])
    print(f"  ✅ 拉到近 72h 共 {total} 則公告")
    print(f"  ✅ 其中 {len(relevant)} 則涉及你 watchlist 的標的")
    by_type = {}
    for it in all_recent:
        t = it["annType"]
        by_type[t] = by_type.get(t, 0) + 1
    print("  分類統計：")
    for t, n in sorted(by_type.items(), key=lambda x: -x[1]):
        print(f"     {_type_zh(t):20} : {n}")

    # === 組 Telegram 訊息 ===
    now_ms = int(time.time() * 1000)
    lines = [
        "📢 <b>OKX 官方公告即時測試</b>（72h 內）",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"近 72h 總公告：<code>{total}</code> 則",
    ]
    if relevant:
        lines.append("")
        lines.append("<b>⚠️ 涉及你 watchlist：</b>")
        for it in relevant[:5]:
            matched = it.get("matched_symbol", "")
            t = _type_zh(it.get("annType", ""))
            title = _esc((it.get("title") or "")[:100])
            url = it.get("url", "")
            lines.append(f"• [<code>{matched}</code>] {t}  <i>{_age(it.get('pTime',0), now_ms)}</i>")
            lines.append(f"  {title}")
            if url:
                lines.append(f"  <a href='{_esc(url)}'>查看詳情</a>")

    if all_recent:
        lines.append("")
        if relevant:
            lines.append("<b>📰 其他最新公告：</b>")
        else:
            lines.append("<b>📰 近期公告（無 watchlist 相關）：</b>")
        # Show 5 most recent (not in relevant)
        rel_urls = {it.get("url") for it in relevant}
        non_rel = [it for it in all_recent if it.get("url") not in rel_urls][:5]
        for it in non_rel:
            t = _type_zh(it.get("annType", ""))
            title = _esc((it.get("title") or "")[:100])
            url = it.get("url", "")
            lines.append(f"• {t}  <i>{_age(it.get('pTime',0), now_ms)}</i>")
            lines.append(f"  {title}")

    text = "\n".join(lines)
    print()
    print("[2/2] 推 Telegram...")
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"https://api.telegram.org/bot{tg_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
        )
        body = r.json()
    if body.get("ok"):
        print("  ✅ 推送成功 — 看你 Telegram")
    else:
        print(f"  ❌ {body}")

    await src.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
