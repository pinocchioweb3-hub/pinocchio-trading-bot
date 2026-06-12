"""逐一 probe CoinGlass v4 端點看回應結構，校正欄位名與路徑。

只印「結構摘要」（top-level keys、data 第一筆 keys、值型別），不印金鑰、不印 raw。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from dotenv import load_dotenv
load_dotenv()

import httpx

KEY = os.getenv("COINGLASS_API_KEY")
BASE = "https://open-api-v4.coinglass.com"
HEADERS = {"CG-API-KEY": KEY or "", "Accept": "application/json"}

# 要 probe 的端點 + params
PROBES = [
    ("positioning-top-pos",
     "/api/futures/top-long-short-position-ratio/history",
     {"exchange": "Binance", "symbol": "BTCUSDT", "interval": "4h", "limit": 3}),
    ("positioning-top-acc",
     "/api/futures/top-long-short-account-ratio/history",
     {"exchange": "Binance", "symbol": "BTCUSDT", "interval": "4h", "limit": 3}),
    ("positioning-global",
     "/api/futures/global-long-short-account-ratio/history",
     {"exchange": "Binance", "symbol": "BTCUSDT", "interval": "4h", "limit": 3}),
    ("oi-agg",
     "/api/futures/open-interest/aggregated-history",
     {"symbol": "BTC", "interval": "1h", "limit": 3,
      "exchange_list": "Binance,OKX,Bybit"}),
    ("oi-agg-symbol-usdt",
     "/api/futures/open-interest/aggregated-history",
     {"symbol": "BTCUSDT", "interval": "1h", "limit": 3,
      "exchange_list": "Binance,OKX,Bybit"}),
    ("oi-single",
     "/api/futures/open-interest/history",
     {"exchange": "Binance", "symbol": "BTCUSDT", "interval": "1h", "limit": 3}),
    ("liq-agg",
     "/api/futures/liquidation/aggregated-history",
     {"symbol": "BTC", "interval": "1h", "limit": 3,
      "exchange_list": "Binance,OKX,Bybit"}),
    ("price-ohlc",
     "/api/futures/price-ohlc-history",
     {"exchange": "Binance", "symbol": "BTCUSDT", "interval": "4h", "limit": 3}),
    ("price-history",
     "/api/futures/price/history",
     {"exchange": "Binance", "symbol": "BTCUSDT", "interval": "4h", "limit": 3}),
    ("coins-markets",
     "/api/futures/coins-markets", {}),
    ("pairs-markets",
     "/api/futures/pairs-markets", {}),
    ("supported-exchange",
     "/api/futures/supported-exchange-pairs", {}),
]


def describe(obj, depth=0, max_depth=2):
    """簡述物件結構（不印值）"""
    if depth > max_depth:
        return f"<{type(obj).__name__}>"
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        items = list(obj.items())[:8]
        return "{" + ", ".join(f"{k}={describe(v, depth+1)}" for k, v in items) + "}"
    if isinstance(obj, list):
        if not obj:
            return "[]"
        return f"[{len(obj)} × {describe(obj[0], depth+1)}]"
    if isinstance(obj, (str, int, float, bool)):
        return type(obj).__name__
    return f"<{type(obj).__name__}>"


async def probe_one(client: httpx.AsyncClient, name: str, path: str, params: dict):
    try:
        r = await client.get(path, params=params, timeout=15)
    except Exception as e:
        print(f"  [{name}] EXCEPTION: {e}")
        return
    print(f"\n  [{name}] HTTP {r.status_code}")
    if r.status_code != 200:
        print(f"    body (first 300): {r.text[:300]}")
        return
    try:
        body = r.json()
    except Exception:
        print(f"    non-JSON: {r.text[:200]}")
        return
    print(f"    envelope: code={body.get('code')!r} msg={body.get('msg')!r}")
    data = body.get("data")
    if data is None:
        print(f"    data: None")
        return
    if isinstance(data, list):
        print(f"    data: list of {len(data)}")
        if data:
            keys = list(data[0].keys()) if isinstance(data[0], dict) else "non-dict"
            print(f"    data[0] keys: {keys}")
            if isinstance(data[0], dict):
                # show value types
                for k, v in list(data[0].items())[:10]:
                    print(f"      {k} = {type(v).__name__} ({repr(v)[:60]})")
    elif isinstance(data, dict):
        print(f"    data keys: {list(data.keys())}")
        for k, v in list(data.items())[:8]:
            print(f"      {k} = {type(v).__name__} ({describe(v)})")
    else:
        print(f"    data: {type(data).__name__}")


async def main():
    if not KEY:
        print("Missing COINGLASS_API_KEY")
        return 1
    async with httpx.AsyncClient(base_url=BASE, headers=HEADERS) as client:
        for name, path, params in PROBES:
            await probe_one(client, name, path, params)
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
