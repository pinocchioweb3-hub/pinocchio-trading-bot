"""Probe 更多 CoinGlass 端點：taker buy/sell（CVD 替代源）+ 強勢排行替代源。"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

import httpx

KEY = os.getenv("COINGLASS_API_KEY")
BASE = "https://open-api-v4.coinglass.com"
HEADERS = {"CG-API-KEY": KEY or "", "Accept": "application/json"}

PROBES = [
    # Taker buy/sell volume —— 這就是「CVD 原料」
    ("taker-volume-agg",
     "/api/futures/aggregated-taker-buy-sell-volume/history",
     {"symbol": "BTC", "interval": "1h", "exchange_list": "Binance,OKX,Bybit", "limit": 3}),
    ("taker-volume-exchange",
     "/api/futures/taker-buy-sell-volume/history",
     {"exchange": "Binance", "symbol": "BTCUSDT", "interval": "1h", "limit": 3}),
    ("taker-vol-ratio",
     "/api/futures/taker-buy-sell-volume-ratio/history",
     {"exchange": "Binance", "symbol": "BTCUSDT", "interval": "1h", "limit": 3}),
    # Vol 端點
    ("volume-history",
     "/api/futures/volume/history",
     {"exchange": "Binance", "symbol": "BTCUSDT", "interval": "1h", "limit": 3}),
    # Strength rank 替代源
    ("supported-coins",
     "/api/futures/supported-coins", {}),
    ("perpetual-market",
     "/api/futures/perpetual-market", {}),
    ("pairs-markets-symbol",
     "/api/futures/pairs-markets", {"symbol": "BTC"}),
    # 替代強勢端點：可能要付費但試試
    ("aggregated-coins-markets",
     "/api/futures/aggregated-coins-markets", {}),
    # 替代 Open Interest 排行
    ("oi-history-chart",
     "/api/futures/open-interest/chart",
     {"symbol": "BTC", "interval": "1h", "limit": 3, "exchange_list": "Binance,OKX,Bybit"}),
]


def keys_of(obj, depth=2):
    if depth <= 0 or not isinstance(obj, (dict, list)):
        return type(obj).__name__
    if isinstance(obj, dict):
        return list(obj.keys())[:12]
    if isinstance(obj, list):
        return f"[{len(obj)}× " + str(keys_of(obj[0], depth-1)) + "]"


async def main():
    async with httpx.AsyncClient(base_url=BASE, headers=HEADERS, timeout=15) as client:
        for name, path, params in PROBES:
            try:
                r = await client.get(path, params=params)
            except Exception as e:
                print(f"\n  [{name}] EXC: {e}")
                continue
            print(f"\n  [{name}] HTTP {r.status_code}")
            if r.status_code != 200:
                print(f"    body: {r.text[:200]}")
                continue
            try:
                body = r.json()
            except Exception:
                print(f"    non-JSON")
                continue
            print(f"    code={body.get('code')!r}  msg={body.get('msg')!r}")
            data = body.get("data")
            if data is None:
                continue
            if isinstance(data, list):
                print(f"    data: [{len(data)} items]")
                if data and isinstance(data[0], dict):
                    print(f"    data[0] keys: {list(data[0].keys())}")
                    for k, v in list(data[0].items())[:8]:
                        print(f"      {k} = {type(v).__name__} ({repr(v)[:80]})")
            elif isinstance(data, dict):
                print(f"    data keys: {list(data.keys())[:12]}")
                for k, v in list(data.items())[:6]:
                    print(f"      {k}: {keys_of(v)}")


if __name__ == "__main__":
    asyncio.run(main())
