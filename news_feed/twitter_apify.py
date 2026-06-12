"""Apify Twitter Scraper Worker。

使用 Apify actor 抓多帳號 X 推文，過濾後推 Telegram。

需要環境變數：
    APIFY_TOKEN          — 你在 apify.com 申請的 personal API token
    APIFY_ACTOR (optional) — 預設用 apidojo/tweet-scraper

成本：
    apidojo/tweet-scraper: $0.40 per 1000 tweets returned
    每 5 分鐘抓所有 38 帳號的最新 5 推 → 約 $2-3/月（看 Trump 等高頻者）

設計：
    - 每 N 秒（預設 300=5min）跑一次
    - 批次抓所有帳號（一次 API call）
    - 用 news_db 做 dedupe（不重推已 seen 的 post_id）
    - 通過 filter 才推 TG
"""
from __future__ import annotations

import asyncio
import os
import time

import httpx

from .news_db import already_seen, init_db, mark_seen
from .news_filter import should_push as filter_should_push
from .twitter_accounts import TWITTER_ACCOUNTS, get_all_handles

APIFY_API = "https://api.apify.com/v2"
# v13.1: 改用 kaitoeasyapi（$0.25/1000 tweets，FREE plan 可用，實測成功）
DEFAULT_ACTOR = "kaitoeasyapi~twitter-x-data-tweet-scraper-pay-per-result-cheapest"
DEFAULT_POLL_SECONDS = 300                # 5 分鐘
DEFAULT_MAX_PER_HANDLE = 3                # 每帳號每輪最多抓幾推（dedupe 後通常 0-1 新）

# 首次啟動保護：歷史 N 篇標 seen 但不推
FIRST_RUN_GRACE = 100

# v15.1: 上次成功抓取的時間戳（since_time 增量抓取用）
_last_fetch_ts: int | None = None


def _get_token() -> str | None:
    return os.getenv("APIFY_TOKEN") or os.getenv("APIFY_API_TOKEN")


def _get_actor() -> str:
    return os.getenv("APIFY_ACTOR", DEFAULT_ACTOR)


def _is_first_run() -> bool:
    from .news_db import _conn
    init_db()
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM seen_posts WHERE source='twitter'"
        ).fetchone()
        return row[0] == 0
    finally:
        conn.close()


def _normalize_item(item: dict) -> dict | None:
    """從多種 actor 的回傳結構萃取統一格式（actor 之間欄位不一致）"""
    # author handle：嘗試多種可能位置
    author_obj = item.get("author") or item.get("user") or {}
    if isinstance(author_obj, dict):
        author = (author_obj.get("userName") or author_obj.get("screen_name")
                  or author_obj.get("username") or author_obj.get("handle") or "")
    else:
        author = str(author_obj or "")
    if not author:
        author = (item.get("authorScreenName") or item.get("username")
                  or item.get("handle") or item.get("user_screen_name") or "")

    # tweet id
    tweet_id = str(
        item.get("id") or item.get("tweetId") or item.get("tweet_id")
        or item.get("id_str") or item.get("conversationId") or ""
    )

    # text
    text = (
        item.get("text") or item.get("fullText") or item.get("full_text")
        or item.get("content") or item.get("body") or ""
    )

    # created_at
    created = (
        item.get("createdAt") or item.get("created_at") or item.get("date")
        or item.get("timestamp") or item.get("published_at") or ""
    )

    # link
    link = item.get("url") or item.get("twitterUrl") or item.get("permalink") or ""
    if not link and author and tweet_id:
        link = f"https://x.com/{author}/status/{tweet_id}"

    if not tweet_id or not text:
        return None
    return {
        "handle": author, "id": tweet_id,
        "content": text, "created_at": created,
        "link": link, "raw": item,
    }


async def _call_apify_actor(actor: str, input_obj: dict,
                             token: str, timeout: float = 90.0) -> list[dict]:
    """單一 actor call。回 dataset items (raw)"""
    url = f"{APIFY_API}/acts/{actor}/run-sync-get-dataset-items?token={token}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, json=input_obj)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Apify HTTP {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except Exception as e:
            raise RuntimeError(f"Apify JSON parse error: {e}")


async def _fetch_batch(handles_batch: list[str], max_per_handle: int,
                         token: str, actor: str, timeout: float,
                         since_ts: int | None = None) -> list[dict]:
    """單一批次 fetch（內部用）。

    v15.1: since_ts（unix 秒）→ 只抓該時刻之後的新推文。
    Apify 按「回傳推文數」收費 — 沒有 since 的話每輪重複付 114 則舊文的錢
    （v15 教訓：一天燒掉整月 $5 額度）。
    """
    max_items = max(len(handles_batch) * max_per_handle, 3)
    input_obj = {
        "searchTerms": [f"from:{h}" for h in handles_batch],
        "maxItems": max_items,
        "queryType": "Latest",
    }
    if since_ts:
        input_obj["since_time"] = str(since_ts)
    data = await _call_apify_actor(actor, input_obj, token, timeout)
    return [
        it for it in data
        if isinstance(it, dict)
        and not it.get("noResults")
        and not it.get("error")
        and it.get("type") != "mock_tweet"
        and it.get("id") != -1
    ]


async def fetch_tweets_via_apify(handles: list[str],
                                  max_per_handle: int = DEFAULT_MAX_PER_HANDLE,
                                  timeout: float = 180.0,
                                  batch_size: int = 13,
                                  since_ts: int | None = None) -> list[dict]:
    """呼叫 Apify actor 抓多帳號推文（v13.2: 批次 + 重試；v15.1: since_time 省費）。

    v13.1 一次塞 38 handles 會 timeout，改 batch_size=10。
    """
    token = _get_token()
    if not token:
        raise RuntimeError("APIFY_TOKEN not set in .env")

    actor = _get_actor()
    all_items: list[dict] = []
    errors: list[str] = []

    # 分批 fetch
    for i in range(0, len(handles), batch_size):
        batch = handles[i:i + batch_size]
        try:
            items = await _fetch_batch(batch, max_per_handle, token, actor, timeout,
                                       since_ts=since_ts)
            all_items.extend(items)
        except Exception as e:
            err_str = f"{type(e).__name__}: {str(e)[:120]}"
            errors.append(f"batch {i//batch_size+1} ({batch[0]}...): {err_str}")
            # v15.1: 月額度用罄 → 不用再試其他批次，直接上拋給 loop 做長退避
            if "platform-feature-disabled" in err_str or "hard limit" in err_str.lower():
                raise RuntimeError("APIFY_MONTHLY_LIMIT") from e
            continue
        # 批次間 1s 緩衝
        await asyncio.sleep(1)

    if errors and not all_items:
        raise RuntimeError(f"All batches failed. First error: {errors[0]}")
    if errors:
        print(f"[twitter_apify] partial success: {len(all_items)} items, {len(errors)} batch errors")

    posts = []
    for item in all_items:
        norm = _normalize_item(item)
        if norm:
            posts.append(norm)
    return posts


def _render_tweet_msg(post: dict, filter_meta: dict, llm: dict | None = None) -> str:
    """渲染推文給 Telegram（v14: 繁中翻譯版）"""
    import html as _html

    handle = post["handle"]
    meta = filter_meta
    label = meta.get("label", handle)
    tier = meta.get("tier", "?")

    # tier 對應 icon
    icon_map = {"T0": "🇺🇸", "Tm": "🌏", "T1": "🏛", "T2": "🐳",
                "T3": "👤", "T4": "📈", "T5": "📰"}
    icon = icon_map.get(tier, "🐦")

    tickers = meta.get("tickers", [])
    tickers_str = ", ".join(tickers) if tickers else "—"

    amount = meta.get("dollar_amount_usd", 0)
    amount_str = f"${amount/1e6:.1f}M" if amount > 0 else ""

    llm = llm or {}
    imp = llm.get("importance")
    imp_str = f"　重要度 <code>{imp}/10</code>" if imp else ""
    # v14.1: 先截原始文字再 escape（escape 後截斷會切壞 entity）；handle/label/link 也 escape
    summary = _html.escape((llm.get("summary_zh") or "")[:300])
    translation = _html.escape((llm.get("translation_zh") or post["content"])[:1500])
    original = _html.escape(post["content"][:800])
    safe_handle = _html.escape(str(handle))
    safe_label = _html.escape(str(label))
    link = _html.escape(post.get("link", ""), quote=True)

    from .push_utils import clamp_news_html

    def build(include_original: bool) -> str:
        lines = [
            f"{icon} <b>@{safe_handle}</b>（{safe_label}）{imp_str}",
            f"━━━━━━━━━━━━━━━━",
        ]
        if summary:
            lines.append(f"📌 {summary}\n")
        lines.append(f"<i>{translation}</i>")
        if llm.get("_fallback"):
            lines.append("\n⚠️ <i>翻譯服務暫時不可用，以上為英文原文</i>")
        elif llm and include_original:
            lines.append(f"\n<blockquote expandable>{original}</blockquote>")
        info_bits = [f"ticker：<code>{tickers_str}</code>"]
        if amount_str:
            info_bits.append(f"金額：<code>{amount_str}</code>")
        lines.append("  ".join(info_bits))
        if link:
            lines.append(f"原文：<a href=\"{link}\">x.com</a>")
        return "\n".join(lines)

    return clamp_news_html(build)


async def poll_once(tg=None, max_push_per_run: int = 10,
                    max_llm_per_run: int = 12) -> dict:
    """跑一次 Apify poll → filter → push TG。

    max_llm_per_run: 單輪 LLM 呼叫上限 — 防 backlog 時 114 篇 × 30s 卡死整輪
    """
    init_db()
    stats = {"fetched": 0, "new": 0, "pushed": 0, "filtered_out": 0,
             "skipped_first_run": 0, "errors": []}

    handles = get_all_handles()
    # v15.1: 只抓上次成功 poll 之後的新推文（Apify 按回傳量收費）
    global _last_fetch_ts
    since = _last_fetch_ts - 120 if _last_fetch_ts else int(time.time()) - 3600
    try:
        posts = await fetch_tweets_via_apify(handles, since_ts=since)
        _last_fetch_ts = int(time.time())
    except RuntimeError as e:
        if "APIFY_MONTHLY_LIMIT" in str(e):
            stats["errors"].append("APIFY_MONTHLY_LIMIT")
            stats["monthly_limit_hit"] = True
            return stats
        stats["errors"].append(f"apify fetch: {type(e).__name__}: {e}")
        return stats
    except Exception as e:
        stats["errors"].append(f"apify fetch: {type(e).__name__}: {e}")
        return stats

    stats["fetched"] = len(posts)
    if not posts:
        return stats

    # === 首次啟動保護 ===
    first_run = _is_first_run()
    if first_run:
        print(f"[twitter_apify] FIRST RUN: marking {len(posts)} tweets as seen (no push)")
        for old in posts:
            mark_seen("twitter", old["handle"], old["id"],
                     content_preview=old["content"][:200],
                     pushed=False, push_reason="first_run_skip")
            stats["skipped_first_run"] += 1
        return stats

    # v14.1 推送原則（與 truth_social 一致）：
    #   - cap / LLM 額度滿 → 不 mark_seen，下輪重撿
    #   - 暫時性發送失敗 → 不 mark_seen
    #   - tier 過濾不過 / LLM 判不相關 / 永久性錯誤 → mark 放棄
    from .llm_filter import classify_and_translate, is_low_content
    from .push_utils import safe_send
    from .twitter_accounts import is_watched

    push_count = 0
    llm_calls = 0
    for post in posts:
        handle = post["handle"]
        if already_seen("twitter", handle, post["id"]):
            continue
        stats["new"] += 1

        # 白名單外帳號（Apify 夾帶的廣告/提及）直接丟
        if not is_watched(handle):
            mark_seen("twitter", handle, post["id"],
                     content_preview=post["content"][:200],
                     pushed=False, push_reason="not_in_watchlist")
            stats.setdefault("unwatched_dropped", 0)
            stats["unwatched_dropped"] += 1
            continue

        # tier 規則過濾（關鍵字層，不花 LLM）
        ok, reason, fmeta = filter_should_push(handle, post["content"])
        if not ok:
            stats["filtered_out"] += 1
            mark_seen("twitter", handle, post["id"],
                     content_preview=post["content"][:200],
                     pushed=False, push_reason=reason)
            continue

        if is_low_content(post["content"]):
            mark_seen("twitter", handle, post["id"],
                     content_preview=post["content"][:200],
                     pushed=False, push_reason="low_content_skip")
            continue

        # cap / LLM 額度滿 → 不 mark_seen，留待下輪
        if tg is None or push_count >= max_push_per_run or llm_calls >= max_llm_per_run:
            stats.setdefault("deferred_to_next_run", 0)
            stats["deferred_to_next_run"] += 1
            continue

        # LLM 二次把關（相關性）+ 繁中翻譯
        llm_calls += 1
        llm = await classify_and_translate(
            handle, fmeta.get("label", handle), post["content"])
        if not llm["relevant"]:
            stats.setdefault("llm_filtered", 0)
            stats["llm_filtered"] += 1
            mark_seen("twitter", handle, post["id"],
                     content_preview=post["content"][:200],
                     pushed=False,
                     push_reason=f"llm_filtered:{llm.get('category')}:{llm.get('reason', '')[:60]}")
            continue
        reason = f"{reason}+llm:imp{llm.get('importance')}"

        try:
            text = _render_tweet_msg(post, fmeta, llm)
            status, resp = await safe_send(tg, text)
            if status == "ok":
                stats["pushed"] += 1
                push_count += 1
                mark_seen("twitter", handle, post["id"],
                         content_preview=post["content"][:200],
                         pushed=True, push_reason=reason)
            elif status == "permanent":
                mark_seen("twitter", handle, post["id"],
                         content_preview=post["content"][:200],
                         pushed=False,
                         push_reason=f"tg_permanent:{resp.get('description', '')[:80]}")
            else:  # transient → 不 mark_seen，下輪重試
                stats["errors"].append(f"transient {post['id']}: {resp.get('description', '')[:80]}")
        except Exception as e:
            stats["errors"].append(f"push {post['id']}: {type(e).__name__}")

    return stats


async def run_twitter_apify_loop(tg, interval_seconds: int = DEFAULT_POLL_SECONDS):
    """Worker 主迴圈。若無 APIFY_TOKEN 則直接 print warning 並退出（讓 daemon 繼續跑其他 worker）"""
    if not _get_token():
        print("[twitter_apify] APIFY_TOKEN not set in .env, worker disabled (set APIFY_TOKEN and restart to enable)")
        return

    print(f"[twitter_apify] starting loop, interval={interval_seconds}s "
          f"actor={_get_actor()} handles={len(get_all_handles())}")
    # 啟動延後 45s（avoid 跟 truth_social 撞）
    await asyncio.sleep(45)

    limit_notified = False
    while True:
        try:
            stats = await poll_once(tg=tg)
            if stats.get("monthly_limit_hit"):
                # v15.1: 月額度用罄 → 退避 6 小時（額度月初重置；持續打只是浪費 log）
                if not limit_notified and tg is not None:
                    limit_notified = True
                    try:
                        await tg.send_message(
                            "⚠️ <b>X 推文抓取暫停：Apify 月額度（$5）已用罄</b>\n"
                            "下月 1 號自動恢復，或到 console.apify.com 加值即可提前恢復。\n"
                            "📰 <i>Trump Truth Social（免費 RSS）不受影響，持續推送中。</i>",
                            parse_mode="HTML")
                    except Exception:
                        pass
                print("[twitter_apify] monthly limit hit, backing off 6h")
                await asyncio.sleep(6 * 3600)
                continue
            if stats["fetched"] > 0:
                print(f"[twitter_apify] fetched={stats['fetched']} new={stats['new']} "
                      f"pushed={stats['pushed']} filtered={stats['filtered_out']} "
                      f"errors={len(stats['errors'])}")
            if stats["errors"]:
                for e in stats["errors"][:3]:
                    print(f"[twitter_apify] err: {e}")
        except Exception as e:
            print(f"[twitter_apify] loop error: {type(e).__name__}: {e}")
        # v16: 深夜降頻（台北 0-6 點 = UTC 16-22；空跑也有 mock data 最低費）
        import datetime as _dt
        hour_utc = _dt.datetime.now(_dt.timezone.utc).hour
        sleep_s = interval_seconds * 2 if 16 <= hour_utc < 22 else interval_seconds
        await asyncio.sleep(sleep_s)


# ===========================================================================
# 自測
# ===========================================================================
if __name__ == "__main__":
    async def selftest():
        token = _get_token()
        if not token:
            print("APIFY_TOKEN not set, cannot run self-test")
            print("Add to .env: APIFY_TOKEN=your_token")
            return
        print(f"Testing Apify fetch (actor={_get_actor()})")
        print(f"Handles: {len(get_all_handles())}")
        try:
            posts = await fetch_tweets_via_apify(["realDonaldTrump", "elonmusk", "binance"],
                                                  max_per_handle=2)
            print(f"\nGot {len(posts)} tweets")
            for p in posts[:5]:
                print(f"\n--- @{p['handle']} ({p['created_at']}) ---")
                print(f"id: {p['id']}")
                print(f"content: {p['content'][:200]}")
        except Exception as e:
            print(f"ERROR: {type(e).__name__}: {e}")

    asyncio.run(selftest())
