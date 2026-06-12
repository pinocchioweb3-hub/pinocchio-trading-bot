"""CryptoPanic 新聞 API client（free tier）。

註冊：https://cryptopanic.com/developers/api/
免費額度：1000 req/天（足夠每 5min 查一次 = 288 次/天）

環境變數：
    CRYPTOPANIC_TOKEN=你的 auth_token

API：
    GET https://cryptopanic.com/api/v1/posts/?auth_token=X&filter=hot&currencies=BTC,ETH
    filters: rising | hot | bullish | bearish | important | saved | lol
"""
from __future__ import annotations

import datetime as dt
import os
from typing import Literal

import httpx

from ..errors import make_error

Filter = Literal["rising", "hot", "bullish", "bearish", "important"]


class CryptoPanicSource:
    name = "cryptopanic"
    BASE_URL = "https://cryptopanic.com/api/v1"

    def __init__(self):
        self.token = os.getenv("CRYPTOPANIC_TOKEN", "")
        self._client: httpx.AsyncClient | None = None

    def configured(self) -> bool:
        return bool(self.token)

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.BASE_URL, timeout=15)
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def get_posts(
        self,
        currencies: list[str] | None = None,
        filter_kind: Filter = "important",
        kind: str = "news",   # news | media
        page: int = 1,
    ) -> dict:
        """拉新聞列表。currencies 例如 ["BTC","ETH"]；filter important = 大新聞。"""
        if not self.token:
            return make_error(
                tool="mi_get_news", symbol=None, source="cryptopanic",
                code="BACKEND_NOT_READY",
                message="CRYPTOPANIC_TOKEN env not set",
                suggestion="到 https://cryptopanic.com/developers/api/keys 註冊免費 token，填入 .env",
            )
        params = {
            "auth_token": self.token,
            "filter": filter_kind,
            "kind": kind,
            "page": page,
            "public": "true",
        }
        if currencies:
            params["currencies"] = ",".join(currencies)

        try:
            r = await self.client.get("/posts/", params=params)
        except httpx.HTTPError as e:
            return make_error(
                tool="mi_get_news", symbol=None, source="cryptopanic",
                code="NETWORK_ERROR", message=str(e),
            )

        if r.status_code == 429:
            return make_error(
                tool="mi_get_news", symbol=None, source="cryptopanic",
                code="RATE_LIMITED", message="CryptoPanic rate limited",
                suggestion="降低查詢頻率",
            )
        if r.status_code != 200:
            return make_error(
                tool="mi_get_news", symbol=None, source="cryptopanic",
                code="HTTP_ERROR",
                message=f"HTTP {r.status_code}",
                upstream_body=r.text[:400],
            )

        try:
            body = r.json()
        except Exception as e:
            return make_error(
                tool="mi_get_news", symbol=None, source="cryptopanic",
                code="PARSE_ERROR", message=str(e),
            )

        results = body.get("results", [])
        posts = []
        now_utc = dt.datetime.now(tz=dt.timezone.utc)
        for r in results:
            published = r.get("published_at", "")
            # 算多久以前發
            age_minutes = None
            try:
                pub_dt = dt.datetime.fromisoformat(published.replace("Z", "+00:00"))
                age_minutes = int((now_utc - pub_dt).total_seconds() / 60)
            except Exception:
                pass

            votes = r.get("votes", {})
            posts.append({
                "id": r.get("id"),
                "title": r.get("title"),
                "url": r.get("url"),
                "source": (r.get("source") or {}).get("title", ""),
                "domain": r.get("domain"),
                "published_at": published,
                "age_minutes": age_minutes,
                "currencies": [c.get("code") for c in r.get("currencies", []) if c.get("code")],
                "positive_votes": votes.get("positive", 0),
                "negative_votes": votes.get("negative", 0),
                "important_votes": votes.get("important", 0),
            })

        return {
            "source": "cryptopanic",
            "filter": filter_kind,
            "currencies": currencies,
            "count": len(posts),
            "posts": posts,
        }

    async def health(self) -> dict:
        if not self.token:
            return {"ok": False, "source": "cryptopanic",
                    "details": "CRYPTOPANIC_TOKEN missing"}
        # Quick check
        r = await self.get_posts(filter_kind="hot", kind="news")
        if r.get("error"):
            return {"ok": False, "source": "cryptopanic",
                    "details": r.get("message")}
        return {"ok": True, "source": "cryptopanic", "details": "operational"}


# 模組級單例（用 lazy init 避免無 token 時報錯）
_INSTANCE: CryptoPanicSource | None = None


def get_cryptopanic() -> CryptoPanicSource:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = CryptoPanicSource()
    return _INSTANCE
