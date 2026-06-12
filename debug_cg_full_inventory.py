"""全面 probe CoinGlass v4 所有公開端點（依文件分類）。
目的：找出你 Startup $79 等級能用的所有資料。
"""
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

# 分類組織 — 每個 (name, path, params)
PROBES = {
    "Futures - 基本": [
        ("instruments", "/api/futures/instruments", {}),
        ("price-mark", "/api/futures/mark-price/history", {"exchange": "Binance", "symbol": "BTCUSDT", "interval": "1h", "limit": 3}),
        ("price-index", "/api/futures/index-price/history", {"exchange": "Binance", "symbol": "BTCUSDT", "interval": "1h", "limit": 3}),
    ],
    "Futures - OI": [
        ("oi-by-exchange-history", "/api/futures/open-interest/exchange-history", {"symbol": "BTC", "interval": "1h", "limit": 3}),
        ("oi-coin-list", "/api/futures/open-interest/coin-list", {}),
        ("oi-pair-list", "/api/futures/open-interest/pair-list", {}),
        ("oi-stablecoin", "/api/futures/open-interest/stablecoin-margin-history", {"symbol": "BTC", "interval": "1h", "limit": 3}),
        ("oi-coin-margin", "/api/futures/open-interest/coin-margin-history", {"symbol": "BTC", "interval": "1h", "limit": 3}),
    ],
    "Futures - Funding": [
        ("funding-coin-list", "/api/futures/funding-rate/coin-list", {}),
        ("funding-exchange-list", "/api/futures/funding-rate/exchange-list", {}),
        ("funding-vol-weight", "/api/futures/funding-rate/oi-weight-history", {"symbol": "BTC", "interval": "1h", "limit": 3}),
        ("funding-vol-weight-vol", "/api/futures/funding-rate/vol-weight-history", {"symbol": "BTC", "interval": "1h", "limit": 3}),
        ("funding-accumulated", "/api/futures/funding-rate/accumulated-exchange-list", {"symbol": "BTC"}),
        ("funding-arbitrage", "/api/futures/funding-rate/arbitrage", {}),
    ],
    "Futures - Volume/Taker": [
        ("taker-vol-exchange", "/api/futures/taker-buy-sell-volume/exchange-list", {"symbol": "BTC", "interval": "1h"}),
        ("taker-vol-agg-history", "/api/futures/aggregated-taker-buy-sell-volume/history", {"symbol": "BTC", "interval": "1h", "exchange_list": "Binance,OKX", "limit": 3}),
    ],
    "Futures - 多空比": [
        ("ls-history-by-exchange-acc", "/api/futures/top-long-short-account-ratio/exchange-list", {"symbol": "BTC"}),
    ],
    "Futures - 清算": [
        ("liq-history-by-exchange", "/api/futures/liquidation/exchange-list", {}),
        ("liq-order-history", "/api/futures/liquidation/orders", {"symbol": "BTCUSDT", "exchange": "Binance"}),
        ("liq-pair-history", "/api/futures/liquidation/pair", {"exchange": "Binance", "symbol": "BTCUSDT"}),
        ("liq-aggregated-heatmap-model1", "/api/futures/liquidation/aggregated-heatmap/model1", {"symbol": "BTC", "range": "24h"}),
        ("liq-aggregated-heatmap-model2", "/api/futures/liquidation/aggregated-heatmap/model2", {"symbol": "BTC", "range": "24h"}),
        ("liq-aggregated-heatmap-model3", "/api/futures/liquidation/aggregated-heatmap/model3", {"symbol": "BTC", "range": "24h"}),
        ("liq-map-coin-margin", "/api/futures/liquidation/coin-margin", {"symbol": "BTC", "range": "24h"}),
    ],
    "Spot": [
        ("spot-supported-coins", "/api/spot/supported-coins", {}),
        ("spot-supported-pairs", "/api/spot/supported-exchange-pairs", {}),
        ("spot-price-history", "/api/spot/price/history", {"exchange": "Binance", "symbol": "BTCUSDT", "interval": "1d", "limit": 3}),
        ("spot-orderbook-bid-ask", "/api/spot/orderbook/bid-ask-range", {"symbol": "BTC"}),
    ],
    "Options": [
        ("options-info", "/api/option/info", {"symbol": "BTC"}),
        ("options-max-pain", "/api/option/max-pain", {"symbol": "BTC"}),
        ("options-pcr", "/api/option/put-call-ratio", {"symbol": "BTC", "interval": "1d", "limit": 3}),
        ("options-oi", "/api/option/open-interest/history", {"symbol": "BTC", "interval": "1d", "limit": 3}),
        ("options-vol", "/api/option/volume/history", {"symbol": "BTC", "interval": "1d", "limit": 3}),
        ("options-exchange-vol", "/api/option/exchange-vol-history", {"symbol": "BTC", "interval": "1d", "limit": 3}),
    ],
    "Index - 市場週期": [
        ("fear-greed", "/api/index/fear-greed-history", {"limit": 3}),
        ("ahr999", "/api/index/ahr999", {"limit": 3}),
        ("pi-cycle", "/api/index/pi-cycle-indicator", {"limit": 3}),
        ("puell", "/api/index/puell-multiple", {"limit": 3}),
        ("stock-flow", "/api/index/stock-flow", {"limit": 3}),
        ("golden-ratio", "/api/index/golden-ratio-multiplier", {"limit": 3}),
        ("two-year-ma", "/api/index/2-year-ma-multiplier", {"limit": 3}),
        ("200week-ma-heatmap", "/api/index/200-week-moving-avg-heatmap", {"limit": 3}),
        ("crypto-bubble", "/api/index/crypto-bubble", {}),
        ("altcoin-season", "/api/index/altcoin-season-index", {"limit": 3}),
        ("bitcoin-profitable-days", "/api/index/bitcoin-profitable-days", {}),
        ("bitcoin-rainbow-chart", "/api/index/bitcoin-rainbow-chart", {}),
        ("mvrv-zscore", "/api/index/mvrv-z-score", {"limit": 3}),
    ],
    "ETF": [
        ("etf-btc-list", "/api/etf/bitcoin/list", {}),
        ("etf-btc-flow", "/api/etf/bitcoin/flow-history", {}),
        ("etf-btc-premium", "/api/etf/bitcoin/premium-history", {}),
        ("etf-eth-list", "/api/etf/ethereum/list", {}),
        ("etf-eth-flow", "/api/etf/ethereum/flow-history", {}),
    ],
    "Hyperliquid": [
        ("hl-whale-alert", "/api/hyperliquid/whale-alert", {}),
        ("hl-whale-position", "/api/hyperliquid/whale-position", {}),
    ],
    "On-Chain (可能 paid)": [
        ("oc-exchange-flow", "/api/onchain/exchange/exchange-flow", {"symbol": "BTC"}),
        ("oc-supported-coins", "/api/onchain/supported-coins", {}),
    ],
    "Bull/Bear": [
        ("bull-bear-market-cycle", "/api/index/bull-bear-cycle", {}),
    ],
}


async def probe_one(client, name, path, params):
    try:
        r = await client.get(path, params=params, timeout=15)
    except Exception as e:
        return f"  [{name:38}] EXC: {type(e).__name__}"
    try:
        body = r.json()
        code = body.get("code")
        msg = (body.get("msg") or "")[:40]
        data = body.get("data")
        if isinstance(data, list):
            detail = f"list[{len(data)}]"
            if data and isinstance(data[0], dict):
                keys = list(data[0].keys())[:4]
                detail += f" keys={keys}"
        elif isinstance(data, dict):
            detail = f"dict keys={list(data.keys())[:5]}"
        elif data is None:
            detail = "None"
        else:
            detail = type(data).__name__
        if code in ("0", 0) and (isinstance(data, list) and data or isinstance(data, dict)):
            tag = "✅"
        elif code == "401" or "Upgrade" in str(msg):
            tag = "💰"
        elif r.status_code == 404 or code == 404:
            tag = "❌"
        else:
            tag = "⚠️"
        return f"  {tag} [{name:38}] {r.status_code} code={code} {msg!r}  {detail}"
    except Exception:
        return f"  ⚠️ [{name:38}] {r.status_code} body={r.text[:80]!r}"


async def main():
    async with httpx.AsyncClient(base_url=BASE, headers=HEADERS) as client:
        for cat, probes in PROBES.items():
            print(f"\n=== {cat} ===")
            results = await asyncio.gather(
                *[probe_one(client, name, path, params) for name, path, params in probes]
            )
            for line in results:
                print(line)


if __name__ == "__main__":
    asyncio.run(main())
