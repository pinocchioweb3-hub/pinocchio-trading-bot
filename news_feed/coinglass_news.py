"""v52: CoinGlass 加密新聞/即時快訊 — GET /api/article/list（Startup $79 即有）。

數據源（2026-06 真 key 實測 code=0）：
    GET open-api-v4.coinglass.com/api/article/list   header CG-API-KEY
    回 20 筆，欄位：article_title / article_content(HTML) /
        article_release_time(毫秒) / article_picture / source_name / source_website_logo
    — 無 id / url / language 欄、內容恆英文。

管線：10 分鐘輪詢 → 依 (標題+發布時間) 雜湊去重（無原生 id）→
    跨來源同事件去重（與 us_news 共用 _is_dup_story）→
    既有 llm_filter AI 過濾 + 繁中翻譯 → 推 📰新聞快訊主題。
門檻：重要度 ≥7（與美股一般新聞同級降噪）；每輪最多推 2 則（防洪水）。

設計鐵則（承 v22-4 us_news 教訓）：
    * 用 BARE `tg.send_message` + `mark_seen`（不走 safe_send，避免稽核層
      無限重試卻不 mark_seen 的回歸）。
    * mark_seen 一律具名參數呼叫。
    * 共用 run_bot 傳入的 source 物件（同一限流器/快取），不自建第二個。
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time

from .llm_filter import classify_and_translate, is_low_content
from .news_db import already_seen, mark_seen
from .us_news import _is_dup_story  # 跨來源同事件去重（英文標題字詞重疊 >50%）

SOURCE = "cgnews"
MAX_AGE_S = 3 * 3600          # 超過 3 小時的舊聞不推（冷啟動防灌水）
MAX_PUSH_PER_CYCLE = 2        # 降噪：每輪最多 2 則
MIN_IMPORTANCE = 7            # 加密新聞門檻（對齊 us_news 一般新聞 7）

# 跨來源去重比對的新聞來源集合（CoinGlass 與 TradingView 美股新聞常撞同一事件）
_DEDUP_SOURCES = ("cgnews", "tvnews")


def _post_id(title: str, release_time) -> str:
    """無原生 id → 以 (標題 + 發布時間) sha256 前 16 碼當穩定主鍵。"""
    raw = f"{title}|{release_time}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _strip_html(html: str) -> str:
    """剝 HTML 標籤 + 還原常見 entity（article_content 是 HTML body）。"""
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">")
                .replace("&#39;", "'").replace("&apos;", "'")
                .replace("&quot;", '"'))
    return re.sub(r"\s+", " ", text).strip()


def _esc(s: str) -> str:
    """Telegram parse_mode=HTML 的動態欄位轉義（防標題含 < & > 破版）。"""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _recent_pushed_titles(hours: int = 24, sources=_DEDUP_SOURCES) -> list[str]:
    """近期已推的英文標題（跨新聞來源），給 _is_dup_story 當比對基準。"""
    import sqlite3
    from botpaths import db_path
    try:
        conn = sqlite3.connect(db_path("news_feed.db"))
        ph = ",".join("?" * len(sources))
        rows = conn.execute(
            f"SELECT content_preview FROM seen_posts "
            f"WHERE source IN ({ph}) AND pushed=1 AND seen_at > ?",
            (*sources, int(time.time()) - hours * 3600)).fetchall()
        conn.close()
        return [r[0] or "" for r in rows]
    except Exception:
        return []


async def process_once(tg, source) -> int:
    """單輪：抓 → 去重 → AI 過濾 → 推送。回推送數。"""
    out = await source.get_article_list(limit=50)
    articles = out.get("articles") or []
    if not articles:
        return 0
    now = time.time()

    fresh = []
    for a in articles:
        if not isinstance(a, dict):
            continue
        title = (a.get("article_title") or "").strip()
        if not title:
            continue
        rel_ms = a.get("article_release_time") or 0
        try:
            rel_s = float(rel_ms) / 1000.0
        except (TypeError, ValueError):
            rel_s = 0.0
        if rel_s and now - rel_s > MAX_AGE_S:
            continue
        handle = (a.get("source_name") or "").strip() or "cg"
        pid = _post_id(title, rel_ms)
        if already_seen(SOURCE, handle, pid):
            continue
        a["_pid"] = pid
        a["_handle"] = handle
        a["_rel_s"] = rel_s or now
        fresh.append(a)

    if not fresh:
        return 0
    fresh.sort(key=lambda x: -x["_rel_s"])   # 新的優先

    pushed = 0
    recent_titles = _recent_pushed_titles(24)   # 跨來源去重基準
    for a in fresh:
        title = a["article_title"].strip()
        handle = a["_handle"]
        pid = a["_pid"]
        body = _strip_html(a.get("article_content") or "")

        # 全部標記已讀（不論推不推，避免下輪重複進 AI）
        if is_low_content(title):
            mark_seen(SOURCE, handle, pid, pushed=False, push_reason="low_content")
            continue
        if _is_dup_story(title, recent_titles):
            mark_seen(SOURCE, handle, pid, pushed=False, push_reason="dup_story")
            continue
        if pushed >= MAX_PUSH_PER_CYCLE:
            mark_seen(SOURCE, handle, pid, pushed=False, push_reason="cycle_cap")
            continue

        feed = title if not body else f"{title}\n\n{body[:1200]}"
        verdict = await classify_and_translate(handle, "加密新聞", feed)
        ok = verdict.get("relevant") and verdict.get("importance", 0) >= MIN_IMPORTANCE
        if not ok:
            mark_seen(SOURCE, handle, pid, pushed=False,
                      push_reason=f"filtered i={verdict.get('importance')}")
            continue

        main = (verdict.get("summary_zh") or "").strip()
        if not main:   # fallback（LLM 不可用）：退回繁中全文翻譯，再退回英文原標題
            main = (verdict.get("translation_zh") or title).strip()
        ts_str = time.strftime("%H:%M", time.localtime(a["_rel_s"]))
        text = (
            f"📰 <b>加密快訊</b>｜{_esc(handle)}｜{ts_str}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"{_esc(main[:500])}"
        )
        try:
            await tg.send_message(text, parse_mode="HTML")
            pushed += 1
            mark_seen(SOURCE, handle, pid, pushed=True,
                      push_reason=f"i={verdict.get('importance')}",
                      content_preview=title[:150])
            recent_titles.append(title)   # 同輪內也去重
            print(f"[cg_news] pushed: [{handle}] {title[:60]} "
                  f"(i={verdict.get('importance')})")
        except Exception as e:
            print(f"[cg_news] send error: {e}")
            mark_seen(SOURCE, handle, pid, pushed=False, push_reason="send_error")
    return pushed


async def run_coinglass_news_loop(tg, source=None, interval_seconds: int = 600):
    """Worker：CoinGlass 加密快訊輪詢（預設 10 分鐘）。

    source 由 run_bot 傳入共享的 CoinGlassSource（同一限流器/快取）；
    若未傳（如獨立自測）則自建一個。"""
    print(f"[cg_news] loop online (CoinGlass /api/article/list, {interval_seconds}s)")
    if source is None:
        from market_intel_mcp.sources.coinglass import CoinGlassSource
        source = CoinGlassSource()
    await asyncio.sleep(90)   # 錯開開機高峰（晚於 us_news 的 60s）
    while True:
        try:
            await process_once(tg, source)
        except Exception as e:
            print(f"[cg_news] loop error: {type(e).__name__}: {str(e)[:120]}")
        await asyncio.sleep(interval_seconds)


if __name__ == "__main__":
    # 獨立自測：真打一次 API（需 .env 內 CoinGlass key），不推 TG，只印結構。
    async def selftest():
        import sys
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        sys.path.insert(0, str(root))
        from dotenv import load_dotenv
        load_dotenv(root / ".env")
        from market_intel_mcp.sources.coinglass import CoinGlassSource
        cg = CoinGlassSource()
        out = await cg.get_article_list(limit=5)
        arts = out.get("articles") or []
        print(f"fetched {len(arts)} articles (err={out.get('error')})")
        for a in arts[:5]:
            t = (a.get("article_title") or "")[:70]
            print(f"  [{a.get('source_name')}] {t}")
        await cg.close()
        return 0 if arts else 1
    import sys as _sys
    _sys.exit(asyncio.run(selftest()))
