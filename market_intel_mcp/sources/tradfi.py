"""傳統金融跨資產數據（Yahoo Finance v8 公開 API，免費）。

涵蓋：
    SPY  美股大盤
    QQQ  Nasdaq 100（科技股代理）
    ^VIX 波動率指數（恐慌指標）
    ^TNX 10 年期美債殖利率（資金成本）
    DX-Y.NYB / UUP  美元指數
    GLD  黃金（避險）
    COIN  Coinbase（加密股）
    MSTR  MicroStrategy（BTC 持倉股）
    NVDA  Nvidia（AI / 科技領先）
    TSLA  Tesla（風險資產代理）
    AAPL  Apple（消費科技）

Yahoo 不需 API key，但需要 User-Agent header 避免 bot 阻擋。
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ..errors import make_error


YAHOO_BASE = "https://query1.finance.yahoo.com"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
    "Accept": "application/json",
}

TICKERS = {
    "SPY":     "S&P 500 ETF",
    "QQQ":     "Nasdaq 100 ETF",
    "^VIX":    "VIX 波動率指數",
    "^TNX":    "10 年期美債殖利率",
    "DX-Y.NYB": "美元指數 DXY",
    "GLD":     "黃金 ETF",
    "COIN":    "Coinbase",
    "MSTR":    "MicroStrategy",
    "NVDA":    "Nvidia",
    "TSLA":    "Tesla",
    "AAPL":    "Apple",
    # v17: 期貨代碼（24h 報價，解決 crypto 全天候但 ETF 盤後失明的盲區）
    "GC=F":    "黃金期貨（24h）",
    "DX=F":    "美元指數期貨（24h）",
    "ZN=F":    "10年期美債期貨（24h，價漲=殖利率跌）",
}


class TradFiSource:
    name = "tradfi-yahoo"

    def __init__(self):
        self.timeout = 15
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=YAHOO_BASE, headers=HEADERS, timeout=self.timeout,
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def get_ticker(self, ticker: str, range_str: str = "3mo",
                         interval: str = "1d") -> dict:
        """單一 ticker 的 OHLC + 衍生統計（7d/30d 報酬、距期內高/低）"""
        try:
            r = await self.client.get(
                f"/v8/finance/chart/{ticker}",
                params={"range": range_str, "interval": interval},
            )
        except httpx.HTTPError as e:
            return make_error(tool="mi_get_tradfi", symbol=ticker, source="tradfi",
                              code="NETWORK_ERROR", message=str(e))
        if r.status_code != 200:
            return make_error(tool="mi_get_tradfi", symbol=ticker, source="tradfi",
                              code="HTTP_ERROR",
                              message=f"HTTP {r.status_code}",
                              upstream_body=r.text[:300])
        try:
            body = r.json()
        except Exception:
            return make_error(tool="mi_get_tradfi", symbol=ticker, source="tradfi",
                              code="PARSE_ERROR", message="non-JSON",
                              upstream_body=r.text[:200])

        chart = body.get("chart", {})
        if chart.get("error"):
            return make_error(tool="mi_get_tradfi", symbol=ticker, source="tradfi",
                              code="API_ERROR",
                              message=str(chart["error"]))
        results = chart.get("result", [])
        if not results:
            return make_error(tool="mi_get_tradfi", symbol=ticker, source="tradfi",
                              code="EMPTY_DATA", message="no chart data")

        result = results[0]
        meta = result.get("meta", {})
        timestamps = result.get("timestamp", []) or []
        quote = result.get("indicators", {}).get("quote", [{}])[0]
        closes = [c for c in (quote.get("close") or []) if c is not None]
        if len(closes) < 2:
            return make_error(tool="mi_get_tradfi", symbol=ticker, source="tradfi",
                              code="INSUFFICIENT_DATA",
                              message=f"only {len(closes)} closes")

        current = closes[-1]
        n = len(closes)
        prev_1d = closes[-2]                      # n >= 2 已由上方守門
        # ⛔ 窗口湊不齊一律回 None（誠實缺料），不可用 closes[0] 靜默頂替——
        # 舊碼會把「5 根收盤價的變化」貼上「30d」的標籤送進使用者看的宏觀卡
        # （health() 固定用 range_str="5d" 呼叫，此路徑每次都走到）。
        # 部分可讀就用可讀的算：湊得齊 7d 仍照算，只有 30d 缺。
        prev_7d = closes[-7] if n >= 7 else None
        prev_30d = closes[-30] if n >= 30 else None
        high = max(closes)
        low = min(closes)

        def _chg(prev):
            """prev 為 None（窗口不足）或 0（無法定義變化率）→ 誠實回 None。"""
            if not prev:
                return None
            return round((current - prev) / prev * 100, 2)

        return {
            "ticker": ticker,
            "name": TICKERS.get(ticker, ticker),
            "source": "tradfi-yahoo",
            "current": round(current, 4),
            "change_1d_pct": _chg(prev_1d),
            "change_7d_pct": _chg(prev_7d),
            "change_30d_pct": _chg(prev_30d),
            "high_3mo": round(high, 4),
            "low_3mo": round(low, 4),
            "drawdown_from_high_pct": round((current - high) / high * 100, 2) if high else 0,
            "closes_n": n,      # 留痕：上面這組數字實際是用幾根收盤價算出來的
            "currency": meta.get("currency", ""),
        }

    async def get_full_snapshot(self) -> dict:
        """並行拉所有 ticker"""
        items = list(TICKERS.keys())
        results = await asyncio.gather(
            *[self.get_ticker(t) for t in items],
            return_exceptions=True,
        )
        snapshot: dict[str, Any] = {}
        for ticker, r in zip(items, results):
            if isinstance(r, dict) and not r.get("error"):
                snapshot[ticker] = r
            else:
                snapshot[ticker] = {"error": True, "ticker": ticker,
                                    "message": (r.get("message") if isinstance(r, dict)
                                                else str(r))}
        return {"source": "tradfi-yahoo", "items": snapshot}

    async def health(self) -> dict:
        r = await self.get_ticker("SPY", range_str="5d", interval="1d")
        return {"ok": not r.get("error"), "source": "tradfi-yahoo",
                "details": r.get("message") if r.get("error") else "operational"}


_INSTANCE: TradFiSource | None = None


def get_tradfi() -> TradFiSource:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = TradFiSource()
    return _INSTANCE
