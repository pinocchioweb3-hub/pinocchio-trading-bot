"""Probe CoinGlass for news, alerts, whale endpoints at various paths."""
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
    # News/announcement variations
    ("v4-news", "/api/news", {}),
    ("v4-news-list", "/api/news/list", {}),
    ("v4-news-feed", "/api/news/feed", {}),
    ("v4-news-latest", "/api/news/latest", {}),
    ("v4-announcement", "/api/announcement", {}),
    ("v4-announcements", "/api/announcements", {}),
    ("v4-articles", "/api/articles", {}),
    ("v4-futures-news", "/api/futures/news", {}),
    # Hyperliquid whale (HL has top trader positions endpoint)
    ("hyperliquid-whale", "/api/hyperliquid/whale-alert", {}),
    ("hyperliquid-trades", "/api/hyperliquid/whale-trade", {}),
    ("hyperliquid-position", "/api/hyperliquid/whale-position", {}),
    # Hyperliquid general
    ("hyperliquid-top-trader", "/api/hyperliquid/top-trader-position", {}),
    # Spot data
    ("spot-coins-markets", "/api/spot/coins-markets", {}),
    # Liquidation alternates
    ("liq-orders", "/api/futures/liquidation-order", {"symbol": "BTC", "interval": "1h"}),
    # Bull market cycle / on-chain alternates
    ("index-puell", "/api/index/puell-multiple", {}),
    ("index-stock-flow", "/api/index/stock-flow", {}),
    ("index-pi-cycle", "/api/index/pi-cycle-indicator", {}),
    ("index-rainbow", "/api/index/bitcoin-rainbow-chart", {}),
    # Long/short by exchange
    ("global-account-by-exchange", "/api/futures/global-long-short-account-ratio/exchange-list", {"symbol": "BTC"}),
]


async def main():
    async with httpx.AsyncClient(base_url=BASE, headers=HEADERS, timeout=15) as client:
        for name, path, params in PROBES:
            try:
                r = await client.get(path, params=params)
            except Exception as e:
                print(f"  [{name:32}] EXC: {e}")
                continue
            status = r.status_code
            try:
                body = r.json()
                code = body.get("code")
                msg = (body.get("msg") or "")[:50]
                data = body.get("data")
                if isinstance(data, list):
                    detail = f"list[{len(data)}]"
                    if data and isinstance(data[0], dict):
                        keys = list(data[0].keys())[:6]
                        detail += f" keys={keys}"
                elif isinstance(data, dict):
                    detail = f"dict keys={list(data.keys())[:6]}"
                else:
                    detail = type(data).__name__
                print(f"  [{name:32}] {status} code={code} msg={msg!r} data={detail}")
            except Exception:
                print(f"  [{name:32}] {status}  body[:150]={r.text[:150]!r}")


if __name__ == "__main__":
    asyncio.run(main())
