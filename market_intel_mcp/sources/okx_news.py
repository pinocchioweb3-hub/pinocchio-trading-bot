"""OKX 官方公告新聞 client（無需 API key，公開端點）。

涵蓋 12 種公告類型，重點抓 5 種對交易影響大的：
    new-listings     新上市永續 / 現貨（常爆漲第一天）
    delistings       下架（必須出場警報）
    trading-updates  費率、槓桿規則變動
    deposit-withdrawal-suspension-resumption  流動性中斷
    latest-events    其他重大事件
"""
from __future__ import annotations

import asyncio
import time

import httpx

from ..errors import make_error


CRITICAL_TYPES = [
    "announcements-new-listings",
    "announcements-delistings",
    "announcements-trading-updates",
    "announcements-deposit-withdrawal-suspension-resumption",
    "latest-events",
    "announcements-others",
]


class OkxNewsSource:
    name = "okx-news"
    BASE_URL = "https://www.okx.com"

    def __init__(self):
        self.timeout = 15
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.BASE_URL, timeout=self.timeout)
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def get_announcements(self, ann_type: str, page: int = 1) -> dict:
        """單一類型公告（按時間降序）"""
        try:
            r = await self.client.get(
                "/api/v5/support/announcements",
                params={"annType": ann_type, "page": str(page)},
            )
        except httpx.HTTPError as e:
            return make_error(
                tool="mi_get_okx_news", symbol=None, source="okx-news",
                code="NETWORK_ERROR", message=str(e),
            )
        try:
            body = r.json()
        except Exception as e:
            return make_error(
                tool="mi_get_okx_news", symbol=None, source="okx-news",
                code="PARSE_ERROR", message=str(e), upstream_body=r.text[:300],
            )

        if body.get("code") != "0":
            return make_error(
                tool="mi_get_okx_news", symbol=None, source="okx-news",
                code="API_ERROR",
                message=str(body.get("msg") or "unknown"),
                upstream_body=str(body)[:300],
            )

        items = []
        for entry in body.get("data", []):
            for d in entry.get("details", []):
                pt = d.get("pTime") or 0
                try:
                    pt = int(pt)
                except (TypeError, ValueError):
                    pt = 0
                items.append({
                    "title": d.get("title") or "",
                    "url": d.get("url") or "",
                    "annType": d.get("annType") or ann_type,
                    "pTime": pt,
                })

        return {"source": "okx-news", "annType": ann_type,
                "count": len(items), "items": items}

    async def get_recent_critical(self, hours_back: int = 24,
                                  max_items: int = 30) -> dict:
        """聚合 5 個關鍵類型、近 N 小時內、去重、按時間排序。"""
        cutoff_ms = int((time.time() - hours_back * 3600) * 1000)
        results = await asyncio.gather(
            *[self.get_announcements(t) for t in CRITICAL_TYPES],
            return_exceptions=True,
        )

        all_items = []
        seen_urls = set()
        for r in results:
            if not (isinstance(r, dict) and not r.get("error")):
                continue
            for it in r.get("items", []):
                url = it.get("url")
                if not url or url in seen_urls:
                    continue
                if it.get("pTime", 0) < cutoff_ms:
                    continue
                seen_urls.add(url)
                all_items.append(it)

        all_items.sort(key=lambda x: x["pTime"], reverse=True)

        # 分類統計
        by_type: dict[str, int] = {}
        for it in all_items:
            t = it["annType"]
            by_type[t] = by_type.get(t, 0) + 1

        return {
            "source": "okx-news",
            "lookback_hours": hours_back,
            "total_count": len(all_items),
            "by_type": by_type,
            "items": all_items[:max_items],
        }

    async def get_relevant_for_symbols(self, symbols: list[str],
                                       hours_back: int = 72) -> dict:
        """過濾出標題含 watchlist 任一 symbol 的公告。"""
        critical = await self.get_recent_critical(hours_back, max_items=100)
        if critical.get("error"):
            return critical

        syms_upper = [s.upper() for s in symbols]
        relevant = []
        for it in critical.get("items", []):
            title_upper = it.get("title", "").upper()
            matched = next((s for s in syms_upper if s in title_upper.split()), None)
            if matched:
                relevant.append({**it, "matched_symbol": matched})

        return {
            "source": "okx-news",
            "lookback_hours": hours_back,
            "watchlist_count": len(relevant),
            "watchlist_relevant": relevant,
            "total_recent": critical.get("total_count", 0),
            "all_recent": critical.get("items", []),
        }

    async def health(self) -> dict:
        try:
            r = await self.client.get("/api/v5/support/announcement-types")
            return {"ok": r.status_code == 200, "source": "okx-news",
                    "details": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"ok": False, "source": "okx-news", "details": str(e)}


# 模組級單例
_INSTANCE: OkxNewsSource | None = None


def get_okx_news() -> OkxNewsSource:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = OkxNewsSource()
    return _INSTANCE
