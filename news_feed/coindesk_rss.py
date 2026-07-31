# -*- coding: utf-8 -*-
"""v175: CoinDesk RSS 新聞源（免金鑰）。使用者 2026-08-01 指定接入。

管線：15 分鐘輪詢官方 RSS → guid/link 去重 → 冷啟動 3h 窗 → 既有 llm_filter
    AI 過濾＋繁中翻譯（英文源,重要度 ≥7 對齊 cg_news/us_news 一般新聞門檻）→
    跨來源同事件去重（與 tvnews/okxnews 撞同事件時讓先到者贏）→ 📰 主題,
    每輪最多 2 則。顯示鐵則：display_only,永不進開單數學。
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
import xml.etree.ElementTree as ET

import httpx

from .coinglass_news import _recent_pushed_titles
from .llm_filter import classify_and_translate, is_low_content
from .news_db import already_seen, mark_seen
from .us_news import _is_dup_story

SOURCE = "coindesk"
FEED_URL = "https://www.coindesk.com/arc/outboundfeeds/rss/"
POLL_S = 900
MAX_AGE_S = 3 * 3600
MAX_PUSH_PER_CYCLE = 2
MIN_IMPORTANCE = 7
_UA = {"User-Agent": "Mozilla/5.0 (TradingBot news reader)"}


def _post_id(guid: str, title: str) -> str:
    raw = guid or title
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _strip(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", s).strip()


def parse_rss(xml_text: str) -> list[dict]:
    """RSS → [{title, link, guid, pub_ts, desc}]。解析失敗回空表不猜。"""
    out: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
        for item in root.iter("item"):
            title = _strip((item.findtext("title") or ""))
            if not title:
                continue
            pub = item.findtext("pubDate") or ""
            try:
                from email.utils import parsedate_to_datetime
                pub_ts = parsedate_to_datetime(pub).timestamp()
            except Exception:  # noqa: BLE001
                pub_ts = 0.0
            out.append({"title": title,
                        "link": (item.findtext("link") or "").strip(),
                        "guid": (item.findtext("guid") or "").strip(),
                        "pub_ts": pub_ts,
                        "desc": _strip(item.findtext("description") or "")})
    except Exception:  # noqa: BLE001
        return []
    return out


async def run_coindesk_loop(tg, poll_seconds: int = POLL_S):
    """worker：CoinDesk RSS → AI 過濾（≥7）→ 📰。任何失敗靜默到下輪。"""
    print("[coindesk] loop online（RSS 15min,AI 過濾 ≥7,繁中翻譯）")
    while True:
        try:
            async with httpx.AsyncClient(timeout=30, headers=_UA,
                                         follow_redirects=True) as client:
                r = await client.get(FEED_URL)
            items = parse_rss(r.text) if r.status_code == 200 else []
            pushed = 0
            now = time.time()
            recent_titles = _recent_pushed_titles(24)   # 跨來源去重基準（同 cg_news）
            for it in items:
                if pushed >= MAX_PUSH_PER_CYCLE:
                    break
                pid = _post_id(it["guid"], it["title"])
                if already_seen(source=SOURCE, post_id=pid):
                    continue
                if it["pub_ts"] and now - it["pub_ts"] > MAX_AGE_S:
                    mark_seen(source=SOURCE, post_id=pid, push_reason="too_old")
                    continue
                text = f"{it['title']}. {it['desc'][:400]}"
                if is_low_content(text):
                    mark_seen(source=SOURCE, post_id=pid, push_reason="low_content")
                    continue
                if _is_dup_story(it["title"], recent_titles):
                    mark_seen(source=SOURCE, post_id=pid, push_reason="dup_story")
                    continue
                llm = await classify_and_translate("coindesk", "加密新聞", text)
                if not llm or not llm.get("relevant") or \
                        (llm.get("importance", 0) or 0) < MIN_IMPORTANCE:
                    mark_seen(source=SOURCE, post_id=pid,
                              push_reason=f"filtered:{(llm or {}).get('importance')}")
                    continue
                zh = _esc(llm.get("summary_zh") or it["title"])
                msg = (f"📰 <b>CoinDesk</b>\n<b>{zh}</b>\n"
                       f'<a href="{_esc(it["link"])}">原文</a>　'
                       "<i>資訊僅供參考，非交易訊號</i>")
                try:
                    await tg.send_message(msg, parse_mode="HTML",
                                          disable_web_page_preview=True)
                    mark_seen(source=SOURCE, post_id=pid, push_reason="pushed")
                    recent_titles.append(it["title"])   # 同輪內也去重
                    pushed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"[coindesk] 推送失敗（下輪重試）：{type(e).__name__}: {e}")
                    break
            if pushed:
                print(f"[coindesk] pushed {pushed}")
        except Exception as e:  # noqa: BLE001
            print(f"[coindesk] loop 例外（不致命）：{type(e).__name__}: {e}")
        await asyncio.sleep(max(120, int(poll_seconds)))
