"""v22-4: 美股第一線快訊 — TradingView 隱藏新聞 API（零成本、零金鑰）。

數據源（2026-06 實測可用）：
    GET news-headlines.tradingview.com/v2/headlines
        ?client=web&lang=en&category=base&market=stock
    必帶 headers：瀏覽器 UA + Referer: https://www.tradingview.com/
    欄位：id/title/provider/published(unix秒)/relatedSymbols[]/is_flash
    來源含 Dow Jones Newswires（is_flash=true 即終端快訊）、Reuters 等
    — 等於免費拿到 @DeItaone 七成內容。

管線：10 分鐘輪詢 → news_db 去重 → is_flash 優先 →
    既有 llm_filter AI 過濾+繁中翻譯 → 推 usstock 主題。
門檻：flash 重要度 ≥5、一般 ≥6；每輪最多推 4 則（防洪水）。
"""
from __future__ import annotations

import asyncio
import time

import httpx

from .llm_filter import classify_and_translate, is_low_content
from .news_db import already_seen, mark_seen

TV_URL = ("https://news-headlines.tradingview.com/v2/headlines"
          "?client=web&lang=en&category=base&market=stock")
TV_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/137.0.0.0 Safari/537.36"),
    "Referer": "https://www.tradingview.com/",
}

SOURCE = "tvnews"
MAX_AGE_S = 2 * 3600          # 超過 2 小時的舊聞不推（冷啟動防灌水）
MAX_PUSH_PER_CYCLE = 4
MIN_IMPORTANCE_FLASH = 5
MIN_IMPORTANCE_NORMAL = 6


async def fetch_headlines() -> list[dict]:
    """抓最新頭條。失敗回空 list（優雅降級）。"""
    try:
        async with httpx.AsyncClient(timeout=20, headers=TV_HEADERS) as c:
            r = await c.get(TV_URL)
        items = (r.json() or {}).get("items", [])
        return items if isinstance(items, list) else []
    except Exception as e:
        print(f"[us_news] fetch error: {type(e).__name__}: {str(e)[:80]}")
        return []


def _symbols_line(item: dict) -> str:
    syms = []
    for s in (item.get("relatedSymbols") or [])[:4]:
        code = (s.get("symbol") or "").split(":")[-1]
        if code:
            syms.append(code)
    return " ".join(f"#{s}" for s in syms)


async def process_once(tg) -> int:
    """單輪：抓 → 去重 → AI 過濾 → 推送。回推送數。"""
    items = await fetch_headlines()
    if not items:
        return 0
    now = time.time()

    fresh = []
    for it in items:
        pid = str(it.get("id") or "")
        title = (it.get("title") or "").strip()
        if not pid or not title:
            continue
        if now - (it.get("published") or 0) > MAX_AGE_S:
            continue
        provider = str(it.get("provider") or "tv")
        if already_seen(SOURCE, provider, pid):
            continue
        fresh.append(it)

    if not fresh:
        return 0
    # flash 優先、新的優先
    fresh.sort(key=lambda x: (not x.get("is_flash"), -(x.get("published") or 0)))

    pushed = 0
    for it in fresh:
        pid = str(it["id"])
        provider = str(it.get("provider") or "tv")
        title = it["title"].strip()
        is_flash = bool(it.get("is_flash"))

        # 全部標記已讀（不論推不推，避免下輪重複進 AI）
        if is_low_content(title):
            mark_seen(SOURCE, provider, pid, pushed=False, reason="low_content")
            continue
        if pushed >= MAX_PUSH_PER_CYCLE:
            mark_seen(SOURCE, provider, pid, pushed=False, reason="cycle_cap")
            continue

        verdict = await classify_and_translate(
            provider, "美股快訊" + ("（終端 flash）" if is_flash else ""), title)
        gate = MIN_IMPORTANCE_FLASH if is_flash else MIN_IMPORTANCE_NORMAL
        ok = verdict.get("relevant") and verdict.get("importance", 0) >= gate
        if not ok:
            mark_seen(SOURCE, provider, pid, pushed=False,
                      reason=f"filtered i={verdict.get('importance')}")
            continue

        icon = "⚡" if is_flash else "📰"
        ts_str = time.strftime("%H:%M", time.localtime(it.get("published") or now))
        text = (
            f"{icon} <b>美股快訊</b>｜{provider}｜{ts_str}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"{verdict.get('translation_zh') or title}\n"
        )
        if verdict.get("summary_zh"):
            text += f"\n💡 {verdict['summary_zh']}"
        sym_line = _symbols_line(it)
        if sym_line:
            text += f"\n{sym_line}"
        try:
            await tg.send_message(text, parse_mode="HTML")
            pushed += 1
            mark_seen(SOURCE, provider, pid, pushed=True,
                      reason=f"i={verdict.get('importance')}",
                      content_preview=title[:150])
            print(f"[us_news] pushed: [{provider}] {title[:60]} "
                  f"(flash={is_flash}, i={verdict.get('importance')})")
        except Exception as e:
            print(f"[us_news] send error: {e}")
            mark_seen(SOURCE, provider, pid, pushed=False, reason="send_error")
    return pushed


async def run_us_news_loop(tg, interval_seconds: int = 600):
    """Worker：美股快訊輪詢（預設 10 分鐘）。"""
    print(f"[us_news] loop online (TradingView hidden API, {interval_seconds}s)")
    await asyncio.sleep(60)   # 錯開開機高峰
    while True:
        try:
            await process_once(tg)
        except Exception as e:
            print(f"[us_news] loop error: {type(e).__name__}: {str(e)[:120]}")
        await asyncio.sleep(interval_seconds)


if __name__ == "__main__":
    async def selftest():
        items = await fetch_headlines()
        print(f"fetched {len(items)} headlines")
        for it in items[:5]:
            print(f"  [{'⚡' if it.get('is_flash') else ' '}] "
                  f"{it.get('provider')}: {(it.get('title') or '')[:70]}")
        return 0 if items else 1
    import sys
    sys.exit(asyncio.run(selftest()))
