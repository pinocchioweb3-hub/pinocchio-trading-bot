"""Binance USDⓈ-M 永續 公開行情 client（v33，第二來源交叉驗證）。

研究結論（task wzancy058）：GO — 當 OKX 的第二來源交叉驗證，不取代。
- 全部公開端點、免 API key（守住「秘鑰不入聊天室/不餵」紅線）。
- 限速比 OKX 寬鬆（2400 weight/min）；讀 X-MBX-USED-WEIGHT-1m 退讓。
- 提供 K線 / mark+funding / funding 歷史 / OI 現值+歷史 / taker 多空量比 / 大戶帳戶多空比。
- 原生多空比可省 CoinGlass 額度。
- v33 更正：Binance **有** 美股永續（contractType=TRADIFI_PERPETUAL，underlyingType
  EQUITY/PREMARKET/KR_EQUITY，含 NVDA/TSLA/ANTHROPIC 等，免 key，已即時複驗）。
  本來源同時覆蓋加密永續與美股永續，list_equity_symbols() 可取股票類清單。

注意：base 'BTC' → 'BTCUSDT'。period 端點僅支援 5m/15m/30m/1h/2h/4h/6h/12h/1d。
"""
from __future__ import annotations

import asyncio
import time

import httpx

from ..errors import make_error
from ..symbol_mapping import normalize

# 我們的時框 → Binance（Binance 用小寫 h、大寫 M=月）
BN_INTERVAL = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h", "8h": "8h", "12h": "12h",
    "1d": "1d", "3d": "3d", "1w": "1w", "1M": "1M",
}
# OI 歷史 / 多空比 端點支援的 period（無 1m/3m/8h/3d/1w/1M）
BN_STAT_PERIOD = {"5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"}


class BinancePerpSource:
    name = "binance-perp"
    BASE_URL = "https://fapi.binance.com"

    def __init__(self):
        self.timeout = 15
        self._client: httpx.AsyncClient | None = None
        self._semaphore = asyncio.Semaphore(6)
        self._used_weight = 0   # 最近一次回應的 X-MBX-USED-WEIGHT-1m

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
    def _sym(symbol: str) -> str:
        """canonical 'BTC' → 'BTCUSDT'（USDⓈ-M 永續）"""
        return f"{normalize(symbol)}USDT"

    @staticmethod
    def _stat_period(interval: str) -> str:
        iv = interval.lower()
        return iv if iv in BN_STAT_PERIOD else "4h"

    async def _get(self, path: str, params: dict, symbol: str, tool: str) -> dict | list | None:
        """共用 GET：回 parsed json 或 make_error。讀 weight header 做退讓。"""
        # 接近限額時主動退讓（2400/min 的 ~80%）
        if self._used_weight > 1900:
            await asyncio.sleep(2.0)
        async with self._semaphore:
            try:
                r = await self.client.get(path, params=params)
            except httpx.HTTPError as e:
                return make_error(tool=tool, symbol=symbol, source=self.name,
                                  code="NETWORK_ERROR", message=str(e))
        try:
            self._used_weight = int(r.headers.get("X-MBX-USED-WEIGHT-1m", 0))
        except (TypeError, ValueError):
            pass
        if r.status_code == 429 or r.status_code == 418:
            return make_error(tool=tool, symbol=symbol, source=self.name,
                              code="RATE_LIMIT", message=f"HTTP {r.status_code}")
        if r.status_code != 200:
            return make_error(tool=tool, symbol=symbol, source=self.name,
                              code="HTTP_ERROR", message=f"HTTP {r.status_code}",
                              upstream_body=r.text[:200])
        try:
            return r.json()
        except Exception as e:
            return make_error(tool=tool, symbol=symbol, source=self.name,
                              code="PARSE_ERROR", message=str(e))

    async def get_candles(self, symbol: str, interval: str = "1h",
                          limit: int = 100) -> dict:
        """K 線（升序、float），形狀對齊 OkxCandlesSource.get_candles。"""
        iv = BN_INTERVAL.get(interval.lower(), interval)
        sym = self._sym(symbol)
        body = await self._get("/fapi/v1/klines",
                               {"symbol": sym, "interval": iv,
                                "limit": min(max(limit, 1), 1500)},
                               symbol, "mi_get_kline")
        if isinstance(body, dict) and body.get("error"):
            return body
        if not isinstance(body, list) or not body:
            return make_error(tool="mi_get_kline", symbol=symbol, source=self.name,
                              code="EMPTY_DATA", message="no candles")
        now_ms = time.time() * 1000
        candles = []
        for row in body:   # Binance 已是升序
            try:
                candles.append({
                    "ts": int(row[0]),
                    "open": float(row[1]), "high": float(row[2]),
                    "low": float(row[3]), "close": float(row[4]),
                    "volume": float(row[5]),
                    "volume_usd": float(row[7]),   # quoteAssetVolume
                    "confirm": int(row[6]) < now_ms,   # closeTime 已過 = 收盤
                })
            except (TypeError, ValueError, IndexError):
                continue
        if not candles:
            return make_error(tool="mi_get_kline", symbol=symbol, source=self.name,
                              code="EMPTY_DATA", message="parse empty")
        return {"symbol": symbol, "source": self.name, "interval": interval,
                "inst": sym, "count": len(candles),
                "latest": candles[-1], "candles": candles}

    async def get_funding(self, symbol: str) -> dict:
        """現值 mark price + 最新資金費率。"""
        body = await self._get("/fapi/v1/premiumIndex", {"symbol": self._sym(symbol)},
                               symbol, "mi_get_funding")
        if isinstance(body, dict) and body.get("error"):
            return body
        try:
            return {"symbol": symbol, "source": self.name,
                    "funding": float(body["lastFundingRate"]),
                    "mark_price": float(body["markPrice"]),
                    "next_funding_ts": int(body.get("nextFundingTime", 0))}
        except (KeyError, TypeError, ValueError) as e:
            return make_error(tool="mi_get_funding", symbol=symbol, source=self.name,
                              code="PARSE_ERROR", message=str(e))

    async def get_funding_series(self, symbol: str, limit: int = 60) -> dict:
        """資金費率歷史序列 [{ts,value}]（8h 結算）。"""
        body = await self._get("/fapi/v1/fundingRate",
                               {"symbol": self._sym(symbol), "limit": min(limit, 1000)},
                               symbol, "mi_get_funding")
        if isinstance(body, dict) and body.get("error"):
            return body
        series = []
        for d in (body or []):
            try:
                series.append({"ts": int(d["fundingTime"]),
                               "value": float(d["fundingRate"])})
            except (KeyError, TypeError, ValueError):
                continue
        return {"symbol": symbol, "source": self.name, "series": series,
                "latest": series[-1]["value"] if series else None}

    async def get_oi(self, symbol: str, interval: str = "4h", limit: int = 60) -> dict:
        """OI 歷史序列 [{ts,value(USD)}] + 24h 變化%。"""
        period = self._stat_period(interval)
        body = await self._get("/futures/data/openInterestHist",
                               {"symbol": self._sym(symbol), "period": period,
                                "limit": min(limit, 500)},
                               symbol, "mi_get_oi")
        if isinstance(body, dict) and body.get("error"):
            return body
        series = []
        for d in (body or []):
            try:
                series.append({"ts": int(d["timestamp"]),
                               "value": float(d["sumOpenInterestValue"])})
            except (KeyError, TypeError, ValueError):
                continue
        delta = None
        if len(series) >= 2 and series[0]["value"]:
            delta = (series[-1]["value"] / series[0]["value"] - 1) * 100
        return {"symbol": symbol, "source": self.name, "series": series,
                "delta_pct_24h": delta,
                "latest": series[-1]["value"] if series else None}

    async def get_positioning(self, symbol: str, interval: str = "4h",
                              limit: int = 60) -> dict:
        """大戶帳戶多空比序列 [{ts,value}]（topLongShortAccountRatio）。"""
        period = self._stat_period(interval)
        body = await self._get("/futures/data/topLongShortAccountRatio",
                               {"symbol": self._sym(symbol), "period": period,
                                "limit": min(limit, 500)},
                               symbol, "mi_get_positioning")
        if isinstance(body, dict) and body.get("error"):
            return body
        series = []
        for d in (body or []):
            try:
                series.append({"ts": int(d["timestamp"]),
                               "value": float(d["longShortRatio"])})
            except (KeyError, TypeError, ValueError):
                continue
        return {"symbol": symbol, "source": self.name, "series": series,
                "latest": series[-1]["value"] if series else None}

    async def get_global_positioning(self, symbol: str, interval: str = "4h",
                                     limit: int = 60) -> dict:
        """全體（散戶為主）帳戶多空比序列（globalLongShortAccountRatio）。
        與 get_positioning（大戶）併用，背離＝實證逆勢訊號。"""
        period = self._stat_period(interval)
        body = await self._get("/futures/data/globalLongShortAccountRatio",
                               {"symbol": self._sym(symbol), "period": period,
                                "limit": min(limit, 500)},
                               symbol, "mi_get_global_positioning")
        if isinstance(body, dict) and body.get("error"):
            return body
        series = []
        for d in (body or []):
            try:
                series.append({"ts": int(d["timestamp"]),
                               "value": float(d["longShortRatio"])})
            except (KeyError, TypeError, ValueError):
                continue
        return {"symbol": symbol, "source": self.name, "series": series,
                "latest": series[-1]["value"] if series else None}

    async def list_equity_symbols(self) -> dict:
        """v33: 列出 Binance 美股類永續 base symbol（EQUITY/PREMARKET/KR_EQUITY）。"""
        body = await self._get("/fapi/v1/exchangeInfo", {}, "NVDA", "mi_list_equity")
        if isinstance(body, dict) and body.get("error"):
            return body
        bases = []
        for s in (body.get("symbols") or []):
            if (s.get("underlyingType") in ("EQUITY", "PREMARKET", "KR_EQUITY")
                    and s.get("quoteAsset") == "USDT"
                    and s.get("status") == "TRADING"):
                bases.append(s.get("baseAsset"))
        return {"source": self.name, "count": len(bases), "symbols": bases}

    async def get_taker_ratio(self, symbol: str, interval: str = "4h",
                              limit: int = 60) -> dict:
        """taker 主動買賣量比序列 [{ts,value}]（buyVol/sellVol）。"""
        period = self._stat_period(interval)
        body = await self._get("/futures/data/takerlongshortRatio",
                               {"symbol": self._sym(symbol), "period": period,
                                "limit": min(limit, 500)},
                               symbol, "mi_get_taker")
        if isinstance(body, dict) and body.get("error"):
            return body
        series = []
        for d in (body or []):
            try:
                item = {"ts": int(d["timestamp"]),
                        "value": float(d["buySellRatio"])}
                # v178：帶出原始 taker 買賣量（CVD 備援計算用；v61 忠實閘驗證過的同源數據）
                if d.get("buyVol") is not None and d.get("sellVol") is not None:
                    item["buy_vol"] = float(d["buyVol"])
                    item["sell_vol"] = float(d["sellVol"])
                series.append(item)
            except (KeyError, TypeError, ValueError):
                continue
        return {"symbol": symbol, "source": self.name, "series": series,
                "latest": series[-1]["value"] if series else None}

    async def list_perp_symbols(self) -> dict:
        """動態列出所有 USDT 永續 base symbol（contractType=PERPETUAL）。"""
        body = await self._get("/fapi/v1/exchangeInfo", {}, "BTC", "mi_list_symbols")
        if isinstance(body, dict) and body.get("error"):
            return body
        bases = []
        for s in (body.get("symbols") or []):
            if (s.get("contractType") == "PERPETUAL"
                    and s.get("quoteAsset") == "USDT"
                    and s.get("status") == "TRADING"):
                bases.append(s.get("baseAsset"))
        return {"source": self.name, "count": len(bases), "symbols": bases}

    async def health(self) -> dict:
        r = await self.get_candles("BTC", "1h", 1)
        return {"ok": not r.get("error"), "source": self.name,
                "details": r.get("message") if r.get("error") else "operational"}


_INSTANCE: BinancePerpSource | None = None


def get_binance_perp() -> BinancePerpSource:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = BinancePerpSource()
    return _INSTANCE


if __name__ == "__main__":
    async def selftest():
        src = BinancePerpSource()
        try:
            for label, coro in [
                ("klines", src.get_candles("BTC", "4h", 5)),
                ("funding", src.get_funding("BTC")),
                ("funding_series", src.get_funding_series("BTC", 5)),
                ("oi", src.get_oi("BTC", "4h", 5)),
                ("positioning", src.get_positioning("BTC", "4h", 5)),
                ("taker", src.get_taker_ratio("BTC", "4h", 5)),
                ("symbols", src.list_perp_symbols()),
            ]:
                r = await coro
                err = r.get("error")
                if label == "symbols":
                    print(f"{label}: err={err} count={r.get('count')} sample={r.get('symbols', [])[:5]}")
                elif "series" in r:
                    print(f"{label}: err={err} pts={len(r.get('series') or [])} latest={r.get('latest')}")
                else:
                    print(f"{label}: err={err} {({k: r[k] for k in list(r)[:5]})}")
        finally:
            await src.close()
    asyncio.run(selftest())
