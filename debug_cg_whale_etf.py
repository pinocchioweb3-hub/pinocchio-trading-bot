"""Probe CoinGlass for whale alerts, ETF flows, news, sentiment endpoints."""
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
    # ETF endpoints
    ("etf-bitcoin-list", "/api/etf/bitcoin/list", {}),
    ("etf-bitcoin-flow", "/api/etf/bitcoin/flow-history", {}),
    ("etf-bitcoin-net", "/api/etf/bitcoin/net-assets-history", {}),
    ("etf-ethereum-list", "/api/etf/ethereum/list", {}),
    ("etf-ethereum-flow", "/api/etf/ethereum/flow-history", {}),
    # Whale / large orders
    ("whale-alerts", "/api/futures/whale-alert", {"symbol": "BTC"}),
    ("whale-positions", "/api/futures/whale-position", {"symbol": "BTC"}),
    ("large-orders", "/api/futures/orderbook/large-limit-order-history",
        {"exchange": "Binance", "symbol": "BTCUSDT"}),
    # Liquidation maps
    ("liq-heatmap", "/api/futures/liquidation/heatmap", {"symbol": "BTC", "range": "24h"}),
    ("liq-coin-list", "/api/futures/liquidation/coin-list", {}),
    # On-chain
    ("onchain-active-addresses", "/api/onchain/active-addresses", {"symbol": "BTC"}),
    ("onchain-whale-flow", "/api/onchain/exchange/exchange-flow", {"symbol": "BTC"}),
    # News (probably not)
    ("news", "/api/news", {}),
    # Index / fear-greed
    ("fear-greed", "/api/index/fear-greed-history", {"limit": 3}),
    ("ahr999", "/api/index/ahr999", {"limit": 3}),
    # Bull market peak indicator
    ("bull-market-peak-indicator", "/api/index/bull-market-peak-indicator", {}),
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
                        keys = list(data[0].keys())[:5]
                        detail += f" keys={keys}"
                elif isinstance(data, dict):
                    detail = f"dict keys={list(data.keys())[:8]}"
                else:
                    detail = type(data).__name__ if data is not None else "None"
                print(f"  [{name:32}] {status}  code={code}  msg={msg!r}  data={detail}")
            except Exception:
                print(f"  [{name:32}] {status}  body[:200]={r.text[:200]!r}")


if __name__ == "__main__":
    asyncio.run(main())
