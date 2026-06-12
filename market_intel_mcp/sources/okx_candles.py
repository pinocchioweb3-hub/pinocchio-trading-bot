"""OKX 公開 K 線 API client（補 CoinGlass 缺的 5m/15m/2w/3w/1M）。

無需 API key（公開端點），rate limit 寬鬆（20 req/2s = 600 req/min）。
端點：/api/v5/market/candles
支援 interval：1m, 3m, 5m, 15m, 30m, 1H, 2H, 4H, 6H, 8H, 12H, 1D, 2D, 3D, 1W, 1M
"""
from __future__ import annotations

import asyncio

import httpx

from ..errors import make_error
from ..symbol_mapping import normalize


# OKX 時框對照（我們的 lowercase → OKX 格式）
OKX_INTERVAL = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "2h": "2H", "4h": "4H", "6h": "6H", "8h": "8H", "12h": "12H",
    "1d": "1D", "2d": "2D", "3d": "3D",
    "1w": "1W", "1M": "1M",
}


class OkxCandlesSource:
    name = "okx-candles"
    BASE_URL = "https://www.okx.com"

    def __init__(self):
        self.timeout = 15
        self._client: httpx.AsyncClient | None = None
        self._semaphore = asyncio.Semaphore(8)  # 限併發避免突發爆量

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.BASE_URL, timeout=self.timeout)
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _to_inst(symbol: str) -> str:
        """canonical 'BTC' → 'BTC-USDT-SWAP'（永續）"""
        canonical = normalize(symbol)
        return f"{canonical}-USDT-SWAP"

    async def get_candles(self, symbol: str, interval: str = "1h",
                         limit: int = 100) -> dict:
        """單一時框 candles。"""
        bar = OKX_INTERVAL.get(interval.lower(), interval)
        # 1M 是月，要保留大小寫
        if interval == "1M":
            bar = "1M"
        inst = self._to_inst(symbol)

        async with self._semaphore:
            try:
                r = await self.client.get(
                    "/api/v5/market/candles",
                    params={"instId": inst, "bar": bar,
                            "limit": str(min(max(limit, 1), 300))},
                )
            except httpx.HTTPError as e:
                return make_error(tool="mi_get_kline", symbol=symbol,
                                  source="okx-candles", code="NETWORK_ERROR",
                                  message=str(e))

        if r.status_code != 200:
            return make_error(tool="mi_get_kline", symbol=symbol,
                              source="okx-candles", code="HTTP_ERROR",
                              message=f"HTTP {r.status_code}",
                              upstream_body=r.text[:200])

        try:
            body = r.json()
        except Exception as e:
            return make_error(tool="mi_get_kline", symbol=symbol,
                              source="okx-candles", code="PARSE_ERROR",
                              message=str(e))

        if body.get("code") != "0":
            return make_error(tool="mi_get_kline", symbol=symbol,
                              source="okx-candles", code="API_ERROR",
                              message=str(body.get("msg") or ""))

        # OKX 回傳格式：data = [[ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm], ...]
        # 時間降序（最新在前），我們轉成升序（最舊在前）+ float
        raw = body.get("data") or []
        candles = []
        for row in reversed(raw):
            try:
                candles.append({
                    "ts": int(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                    "volume_usd": float(row[7]) if len(row) > 7 else 0.0,
                    "confirm": int(row[8]) == 1 if len(row) > 8 else True,
                })
            except (TypeError, ValueError, IndexError):
                continue

        if not candles:
            return make_error(tool="mi_get_kline", symbol=symbol,
                              source="okx-candles", code="EMPTY_DATA",
                              message="no candles")

        latest = candles[-1]
        return {
            "symbol": symbol, "source": "okx-candles",
            "interval": interval, "bar": bar, "inst": inst,
            "count": len(candles),
            "latest": latest,
            "candles": candles,
        }

    async def get_multi_tf(self, symbol: str,
                          timeframes: list[str] | None = None,
                          limit_per_tf: int = 100) -> dict:
        """並行拉多個時框。"""
        if timeframes is None:
            # 預設：日內到月線完整時框組
            timeframes = ["5m", "15m", "1h", "4h", "12h", "1d", "1w"]

        results = await asyncio.gather(
            *[self.get_candles(symbol, tf, limit_per_tf) for tf in timeframes],
            return_exceptions=True,
        )

        out: dict[str, dict] = {}
        for tf, r in zip(timeframes, results):
            if isinstance(r, Exception):
                out[tf] = {"error": True, "message": str(r)}
            else:
                out[tf] = r

        return {
            "symbol": symbol,
            "source": "okx-candles",
            "timeframes": timeframes,
            "by_timeframe": out,
        }

    async def health(self) -> dict:
        r = await self.get_candles("BTC", "1h", 1)
        return {"ok": not r.get("error"), "source": "okx-candles",
                "details": r.get("message") if r.get("error") else "operational"}


_INSTANCE: OkxCandlesSource | None = None


def get_okx_candles() -> OkxCandlesSource:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = OkxCandlesSource()
    return _INSTANCE
