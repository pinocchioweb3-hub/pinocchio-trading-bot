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
MAX_PUSH_PER_CYCLE = 2        # v23-3 降噪：4→2（實測首小時 21 則過量）
MIN_IMPORTANCE_FLASH = 5
MIN_IMPORTANCE_NORMAL = 7     # v23-3 降噪：6→7（一般新聞抬高門檻，flash 不動）


def _word_set(title: str) -> set[str]:
    import re
    return {w.lower() for w in re.findall(r"[A-Za-z]{3,}", title)}


def _is_dup_story(title: str, recent_titles: list[str]) -> bool:
    """跨來源同事件去重：與近期已推標題的字詞重疊 >50% 視為同一條新聞。
    （實測 SpaceX 上市新聞 3 個來源各推一次 — 內容相同只差措辭）"""
    ws = _word_set(title)
    if len(ws) < 3:
        return False
    for old in recent_titles:
        ow = _word_set(old)
        if not ow:
            continue
        overlap = len(ws & ow) / min(len(ws), len(ow))
        if overlap > 0.5:
            return True
    return False


def _recent_pushed_titles(hours: int = 24) -> list[str]:
    import sqlite3
    from botpaths import db_path
    try:
        conn = sqlite3.connect(db_path("news_feed.db"))
        rows = conn.execute(
            "SELECT content_preview FROM seen_posts "
            "WHERE source=? AND pushed=1 AND seen_at > ?",
            (SOURCE, int(time.time()) - hours * 3600)).fetchall()
        conn.close()
        return [r[0] or "" for r in rows]
    except Exception:
        return []


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
    recent_titles = _recent_pushed_titles(24)   # v23-3: 跨來源去重基準
    for it in fresh:
        pid = str(it["id"])
        provider = str(it.get("provider") or "tv")
        title = it["title"].strip()
        is_flash = bool(it.get("is_flash"))

        # 全部標記已讀（不論推不推，避免下輪重複進 AI）
        if is_low_content(title):
            mark_seen(SOURCE, provider, pid, pushed=False, push_reason="low_content")
            continue
        # v23-3: 同一事件已從別的來源推過 → 不再推
        if _is_dup_story(title, recent_titles):
            mark_seen(SOURCE, provider, pid, pushed=False, push_reason="dup_story")
            continue
        if pushed >= MAX_PUSH_PER_CYCLE:
            mark_seen(SOURCE, provider, pid, pushed=False, push_reason="cycle_cap")
            continue

        verdict = await classify_and_translate(
            provider, "美股快訊" + ("（終端 flash）" if is_flash else ""), title)
        gate = MIN_IMPORTANCE_FLASH if is_flash else MIN_IMPORTANCE_NORMAL
        ok = verdict.get("relevant") and verdict.get("importance", 0) >= gate
        if not ok:
            mark_seen(SOURCE, provider, pid, pushed=False,
                      push_reason=f"filtered i={verdict.get('importance')}")
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
                      push_reason=f"i={verdict.get('importance')}",
                      content_preview=title[:150])
            recent_titles.append(title)   # 同輪內也去重
            print(f"[us_news] pushed: [{provider}] {title[:60]} "
                  f"(flash={is_flash}, i={verdict.get('importance')})")
        except Exception as e:
            print(f"[us_news] send error: {e}")
            mark_seen(SOURCE, provider, pid, pushed=False, push_reason="send_error")
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
