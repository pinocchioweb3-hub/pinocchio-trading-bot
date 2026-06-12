"""Truth Social RSS Worker（透過 trumpstruth.org 聚合）。

trumpstruth.org 是社群維護的 Trump Truth Social 鏡像，提供 RSS feed。
免費、不需要 API key、不需要登入。

延遲：通常 < 5 分鐘（看他們的 cron 頻率）。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import re
import time
import xml.etree.ElementTree as ET

import httpx

from .news_db import already_seen, init_db, mark_seen
from .news_filter import should_push as filter_should_push

# 主 feed（聚合所有 Trump 貼文）
TRUTH_SOCIAL_RSS = "https://trumpstruth.org/feed"
# 備援源（萬一主源掛）
FALLBACKS = [
    "https://trumpstruth.org/rss.xml",
]

# 抓取頻率
DEFAULT_POLL_SECONDS = 300  # 5 分鐘


def _strip_html(text: str) -> str:
    """剝 HTML 標籤 + 解碼 entities（v14.1: 改用標準 html.unescape，
    正確處理順序與 &#8217; 等數字實體）"""
    if not text:
        return ""
    import html as _html
    clean = re.sub(r"<[^>]+>", "", text)
    return _html.unescape(clean).strip()


async def fetch_rss(url: str = TRUTH_SOCIAL_RSS, timeout: float = 15.0) -> list[dict]:
    """抓 RSS 並 parse。回 list of {id, title, content, link, pub_date}"""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        r = await client.get(url, headers={
            "User-Agent": "Mozilla/5.0 (TradingBot/v12; +github.com)"
        })
        r.raise_for_status()
        xml_text = r.text

    posts: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"[truth_social] RSS parse error: {e}")
        return []

    # RSS 2.0：channel/item
    for item in root.iter("item"):
        guid = item.findtext("guid") or item.findtext("link") or ""
        link = item.findtext("link") or ""
        title = _strip_html(item.findtext("title") or "")
        desc = _strip_html(item.findtext("description") or "")
        pub = item.findtext("pubDate") or ""

        # post_id：用 guid 或 link 末段
        post_id = guid.strip() or link.strip()
        if not post_id:
            # fallback：用 title+pub 算 hash
            post_id = hashlib.sha256((title + pub).encode()).hexdigest()[:32]

        # content 用 description（Trump 推文本身）；若空則用 title
        content = desc or title
        if not content:
            continue

        posts.append({
            "id": post_id,
            "title": title,
            "content": content,
            "link": link,
            "pub_date": pub,
        })

    return posts


def _render_truth_social_msg(post: dict, llm: dict) -> str:
    """渲染 Trump Truth Social 訊息給 Telegram（v14.1: 繁中翻譯 + 長度守門）

    v14.1 修正：先截原始文字再 escape（escape 後截斷會切壞 entity）、
    link 也 escape、總長 >4090 自動降級（去掉原文引用塊）。
    """
    import html as _html

    from .push_utils import clamp_news_html

    imp = llm.get("importance", 5)
    icon = "🔴" if imp >= 9 else ("🟠" if imp >= 6 else "🟡")
    cat_label = {
        "macro": "總體經濟", "crypto": "加密貨幣", "stocks": "美股",
        "geopolitics": "地緣政治", "politics": "政治", "other": "其他",
    }.get(llm.get("category", "other"), "其他")

    raw_translation = (llm.get("translation_zh") or post["content"])[:1500]
    raw_original = post["content"][:800]
    raw_summary = (llm.get("summary_zh") or "")[:300]
    translation = _html.escape(raw_translation)
    original = _html.escape(raw_original)
    summary = _html.escape(raw_summary)
    link = _html.escape(post.get("link", ""), quote=True)

    def build(include_original: bool) -> str:
        lines = [
            f"{icon} <b>Trump 在 Truth Social 發文</b>　重要度 <code>{imp}/10</code>　<code>{cat_label}</code>",
            f"━━━━━━━━━━━━━━━━",
        ]
        if summary:
            lines.append(f"📌 {summary}\n")
        lines.append(f"<i>{translation}</i>")
        if llm.get("_fallback"):
            lines.append("\n⚠️ <i>翻譯服務暫時不可用，以上為英文原文</i>")
        elif include_original:
            lines.append(f"\n<blockquote expandable>{original}</blockquote>")
        if link:
            lines.append(f"原文連結：<a href=\"{link}\">trumpstruth.org</a>")
        return "\n".join(lines)

    return clamp_news_html(build)


def _is_first_run() -> bool:
    """檢查 SQLite 中是否完全沒有 truth_social 紀錄（= 首次跑）"""
    from .news_db import _conn
    init_db()
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM seen_posts WHERE source='truth_social'"
        ).fetchone()
        return row[0] == 0
    finally:
        conn.close()


async def poll_once(tg=None, max_push_per_run: int = 5,
                    max_llm_per_run: int = 10) -> dict:
    """跑一次 RSS poll → filter → push TG。回統計 dict。

    Args:
        tg: Telegram client
        max_push_per_run: 單輪最多推幾篇（防雜訊爆炸）
        max_llm_per_run: 單輪最多 LLM 呼叫數（防 backlog 時整輪卡數十分鐘）
    """
    init_db()
    stats = {"fetched": 0, "new": 0, "pushed": 0, "skipped_first_run": 0, "errors": []}

    posts: list[dict] = []
    sources = [TRUTH_SOCIAL_RSS] + FALLBACKS
    for src in sources:
        try:
            posts = await fetch_rss(src)
            if posts:
                stats["source"] = src
                break
        except Exception as e:
            stats["errors"].append(f"{src}: {type(e).__name__}: {e}")
            continue

    stats["fetched"] = len(posts)
    if not posts:
        return stats

    # === 首次啟動保護：把歷史 99 篇標 seen 但不推（只推最新 1 篇打招呼）===
    first_run = _is_first_run()
    if first_run:
        print(f"[truth_social] FIRST RUN detected, marking {len(posts)-1} historical posts as seen (no push)")
        # posts 按時間倒序（最新在前）
        for old_post in posts[1:]:
            mark_seen("truth_social", "realDonaldTrump", old_post["id"],
                     content_preview=old_post["content"][:200],
                     pushed=False, push_reason="first_run_skip")
            stats["skipped_first_run"] += 1
        # 留下 posts[0] 走正常流程作「上線打招呼」
        posts = posts[:1]

    # v14.1 推送原則（來自對抗驗證）：
    #   - cap 滿 / LLM 額度用盡 → 「不 mark_seen」，下一輪自然重撿，絕不永久丟失
    #   - 暫時性發送失敗（429/網路）→ 同上不 mark_seen
    #   - 只有「永久性錯誤」（內容壞掉）與「LLM 判不相關」才 mark 放棄
    from .llm_filter import classify_and_translate, is_low_content
    from .push_utils import safe_send

    push_count = 0
    llm_calls = 0
    for post in posts:
        if already_seen("truth_social", "realDonaldTrump", post["id"]):
            continue
        stats["new"] += 1

        # 純連結/過短貼文直接丟（沒內容可分析）
        if is_low_content(post["content"]):
            mark_seen("truth_social", "realDonaldTrump", post["id"],
                     content_preview=post["content"][:200],
                     pushed=False, push_reason="low_content_skip")
            continue

        # cap 滿 → 不呼叫 LLM、不 mark_seen，留待下輪
        if tg is None or push_count >= max_push_per_run or llm_calls >= max_llm_per_run:
            stats.setdefault("deferred_to_next_run", 0)
            stats["deferred_to_next_run"] += 1
            continue

        # LLM 智慧過濾（總經/美股/加密/地緣政治留，純政治丟）+ 繁中翻譯
        llm_calls += 1
        llm = await classify_and_translate("realDonaldTrump", "Trump", post["content"])
        if not llm["relevant"]:
            mark_seen("truth_social", "realDonaldTrump", post["id"],
                     content_preview=post["content"][:200],
                     pushed=False,
                     push_reason=f"llm_filtered:{llm.get('category')}:{llm.get('reason', '')[:80]}")
            stats.setdefault("llm_filtered", 0)
            stats["llm_filtered"] += 1
            continue
        reason = f"llm_relevant:{llm.get('category')}:imp{llm.get('importance')}"

        try:
            text = _render_truth_social_msg(post, llm)
            status, resp = await safe_send(tg, text)
            if status == "ok":
                stats["pushed"] += 1
                push_count += 1
                mark_seen("truth_social", "realDonaldTrump", post["id"],
                         content_preview=post["content"][:200],
                         pushed=True, push_reason=reason)
            elif status == "permanent":
                mark_seen("truth_social", "realDonaldTrump", post["id"],
                         content_preview=post["content"][:200],
                         pushed=False,
                         push_reason=f"tg_permanent:{resp.get('description', '')[:80]}")
            else:  # transient → 不 mark_seen，下輪重試
                stats["errors"].append(f"transient {post['id']}: {resp.get('description', '')[:80]}")
        except Exception as e:
            # render 等程式錯誤 → 不 mark_seen（修好後下輪自動重推）
            stats["errors"].append(f"push {post['id']}: {type(e).__name__}: {e}")

    return stats


async def run_truth_social_loop(tg, interval_seconds: int = DEFAULT_POLL_SECONDS):
    """Worker 主迴圈：每 N 秒抓一次 Trump Truth Social"""
    print(f"[truth_social] starting loop, interval={interval_seconds}s")
    # 啟動延後 30s
    await asyncio.sleep(30)

    while True:
        try:
            stats = await poll_once(tg=tg)
            if stats["new"] > 0:
                print(f"[truth_social] fetched={stats['fetched']} new={stats['new']} "
                      f"pushed={stats['pushed']} errors={len(stats['errors'])}")
            if stats["errors"]:
                for e in stats["errors"][:3]:
                    print(f"[truth_social] err: {e}")
        except Exception as e:
            print(f"[truth_social] loop error: {type(e).__name__}: {e}")
        await asyncio.sleep(interval_seconds)


# ===========================================================================
# 自測
# ===========================================================================
if __name__ == "__main__":
    async def selftest():
        print(f"Testing fetch from {TRUTH_SOCIAL_RSS}")
        try:
            posts = await fetch_rss()
            print(f"Got {len(posts)} posts")
            for p in posts[:5]:
                print(f"\n--- {p['pub_date']} ---")
                print(f"id: {p['id']}")
                print(f"link: {p['link']}")
                print(f"content: {p['content'][:200]}")
        except Exception as e:
            print(f"ERROR: {type(e).__name__}: {e}")

    asyncio.run(selftest())
