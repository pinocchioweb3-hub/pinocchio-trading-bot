"""完整 paced probe — 序列化、每個請求間隔 1 秒、避免 429。
測試所有 CoinGlass v4 公開端點 + 同步驗證 CryptoPanic + 推 3 則新聞給你 Telegram。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

import httpx

CG_KEY = os.getenv("COINGLASS_API_KEY")
CG_BASE = "https://open-api-v4.coinglass.com"
CG_HEADERS = {"CG-API-KEY": CG_KEY or "", "Accept": "application/json"}

CP_TOKEN = os.getenv("CRYPTOPANIC_TOKEN", "")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

# ==============================================================
# CoinGlass v4 完整 endpoint inventory（依分類）
# ==============================================================
INVENTORY = {
    "Futures 基本": [
        ("supported-exchange-pairs", "/api/futures/supported-exchange-pairs", {}),
        ("price-history", "/api/futures/price/history", {"exchange": "Binance", "symbol": "BTCUSDT", "interval": "4h", "limit": 3}),
    ],
    "Open Interest": [
        ("oi-aggregated-history", "/api/futures/open-interest/aggregated-history", {"symbol": "BTC", "interval": "1h", "limit": 3, "exchange_list": "Binance,OKX,Bybit"}),
        ("oi-history", "/api/futures/open-interest/history", {"exchange": "Binance", "symbol": "BTCUSDT", "interval": "1h", "limit": 3}),
    ],
    "Funding Rate（多維度）": [
        ("funding-history", "/api/futures/funding-rate/history", {"exchange": "Binance", "symbol": "BTCUSDT", "interval": "8h", "limit": 3}),
        ("funding-exchange-list", "/api/futures/funding-rate/exchange-list", {}),
        ("funding-oi-weight", "/api/futures/funding-rate/oi-weight-history", {"symbol": "BTC", "interval": "1h", "limit": 3}),
        ("funding-vol-weight", "/api/futures/funding-rate/vol-weight-history", {"symbol": "BTC", "interval": "1h", "limit": 3}),
        ("funding-arbitrage", "/api/futures/funding-rate/arbitrage", {}),
    ],
    "Long/Short 多空比": [
        ("ls-top-position", "/api/futures/top-long-short-position-ratio/history", {"exchange": "Binance", "symbol": "BTCUSDT", "interval": "4h", "limit": 3}),
        ("ls-top-account", "/api/futures/top-long-short-account-ratio/history", {"exchange": "Binance", "symbol": "BTCUSDT", "interval": "4h", "limit": 3}),
        ("ls-global-account", "/api/futures/global-long-short-account-ratio/history", {"exchange": "Binance", "symbol": "BTCUSDT", "interval": "4h", "limit": 3}),
    ],
    "Volume/Taker（CVD 源）": [
        ("taker-vol-agg", "/api/futures/aggregated-taker-buy-sell-volume/history", {"symbol": "BTC", "interval": "1h", "exchange_list": "Binance,OKX,Bybit", "limit": 3}),
        ("taker-vol-single", "/api/futures/taker-buy-sell-volume/history", {"exchange": "Binance", "symbol": "BTCUSDT", "interval": "1h", "limit": 3}),
    ],
    "Liquidation": [
        ("liq-aggregated-history", "/api/futures/liquidation/aggregated-history", {"symbol": "BTC", "interval": "1h", "exchange_list": "Binance,OKX,Bybit", "limit": 3}),
        ("liq-coin-list", "/api/futures/liquidation/coin-list", {}),
        ("liq-heatmap-model1", "/api/futures/liquidation/aggregated-heatmap/model1", {"symbol": "BTC", "range": "24h"}),
        ("liq-heatmap-model2", "/api/futures/liquidation/aggregated-heatmap/model2", {"symbol": "BTC", "range": "24h"}),
        ("liq-heatmap-model3", "/api/futures/liquidation/aggregated-heatmap/model3", {"symbol": "BTC", "range": "24h"}),
    ],
    "Pairs-Markets（強勢源）": [
        ("pairs-markets", "/api/futures/pairs-markets", {"symbol": "BTC"}),
    ],
    "Spot": [
        ("spot-supported-coins", "/api/spot/supported-coins", {}),
        ("spot-supported-pairs", "/api/spot/supported-exchange-pairs", {}),
        ("spot-price-history", "/api/spot/price/history", {"exchange": "Binance", "symbol": "BTCUSDT", "interval": "1d", "limit": 3}),
    ],
    "Options": [
        ("options-info", "/api/option/info", {"symbol": "BTC"}),
        ("options-max-pain", "/api/option/max-pain", {"symbol": "BTC"}),
        ("options-exchange-vol", "/api/option/exchange-vol-history", {"symbol": "BTC", "interval": "1d", "limit": 3}),
    ],
    "Index/Cycle 指標": [
        ("idx-fear-greed", "/api/index/fear-greed-history", {"limit": 3}),
        ("idx-ahr999", "/api/index/ahr999", {"limit": 3}),
        ("idx-pi-cycle", "/api/index/pi-cycle-indicator", {"limit": 3}),
        ("idx-puell", "/api/index/puell-multiple", {"limit": 3}),
        ("idx-stock-flow", "/api/index/stock-flow", {"limit": 3}),
        ("idx-golden-ratio", "/api/index/golden-ratio-multiplier", {"limit": 3}),
        ("idx-2yr-ma", "/api/index/2-year-ma-multiplier", {"limit": 3}),
    ],
    "ETF": [
        ("etf-btc-list", "/api/etf/bitcoin/list", {}),
        ("etf-btc-flow", "/api/etf/bitcoin/flow-history", {}),
        ("etf-eth-list", "/api/etf/ethereum/list", {}),
        ("etf-eth-flow", "/api/etf/ethereum/flow-history", {}),
    ],
    "Hyperliquid（鯨魚）": [
        ("hl-whale-alert", "/api/hyperliquid/whale-alert", {}),
        ("hl-whale-position", "/api/hyperliquid/whale-position", {}),
    ],
}


async def probe_with_pacing():
    """每個請求間隔 0.9 秒，總計 ~80s < rate limit 80/min 安全範圍"""
    results = {}
    async with httpx.AsyncClient(base_url=CG_BASE, headers=CG_HEADERS, timeout=20) as client:
        for cat, probes in INVENTORY.items():
            print(f"\n=== {cat} ===")
            results[cat] = []
            for name, path, params in probes:
                await asyncio.sleep(0.9)
                try:
                    r = await client.get(path, params=params)
                except Exception as e:
                    out = (name, 0, "EXC", str(e), None)
                    print(f"  ❌ [{name:38}] EXC: {e}")
                    results[cat].append(out)
                    continue
                try:
                    body = r.json()
                    code = body.get("code")
                    msg = (body.get("msg") or "")[:60]
                    data = body.get("data")
                    detail = ""
                    if isinstance(data, list):
                        detail = f"list[{len(data)}]"
                        if data and isinstance(data[0], dict):
                            keys = list(data[0].keys())[:5]
                            detail += f" keys={keys}"
                    elif isinstance(data, dict):
                        detail = f"dict keys={list(data.keys())[:6]}"
                    elif data is None:
                        detail = "None"
                    is_ok = (code in ("0", 0)) and (data is not None) and (
                        not isinstance(data, list) or len(data) > 0)
                    is_paid = "Upgrade" in str(msg) or code == "401"
                    tag = "✅" if is_ok else ("💰" if is_paid else ("❌" if r.status_code == 404 or code == 404 else "⚠️"))
                    print(f"  {tag} [{name:38}] {r.status_code} code={code} {detail}")
                    results[cat].append((name, r.status_code, str(code), msg, detail, tag))
                except Exception as e:
                    print(f"  ⚠️ [{name:38}] parse err: {e}")
                    results[cat].append((name, r.status_code, "parse_err", str(e), None, "⚠️"))
    return results


async def verify_cryptopanic_and_demo():
    """驗證 CP token + 拉 3 則 important 新聞推送到 Telegram"""
    print("\n=== CryptoPanic + Telegram demo ===")
    if not CP_TOKEN:
        print("  ❌ CRYPTOPANIC_TOKEN missing")
        return
    print(f"  CP token: {len(CP_TOKEN)} chars")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                "https://cryptopanic.com/api/v1/posts/",
                params={"auth_token": CP_TOKEN, "filter": "important", "public": "true"},
            )
        if r.status_code != 200:
            print(f"  ❌ CryptoPanic HTTP {r.status_code}: {r.text[:200]}")
            return
        body = r.json()
        results = body.get("results", [])
        print(f"  ✅ CryptoPanic OK, got {len(results)} posts")
        # 取前 3 推 Telegram
        if TG_TOKEN and TG_CHAT and results:
            top3 = results[:3]
            lines = ["📰 <b>CryptoPanic 即時測試（前 3 則重要新聞）</b>", ""]
            for p in top3:
                title = (p.get("title") or "")[:100].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                src = (p.get("source") or {}).get("title", "") or p.get("domain", "")
                pub = p.get("published_at", "")[:16]
                votes = p.get("votes", {})
                imp = votes.get("important", 0)
                pos = votes.get("positive", 0)
                neg = votes.get("negative", 0)
                lines.append(f"• <b>{title}</b>")
                lines.append(f"  <i>{src.replace('&','&amp;')[:30]} | {pub}</i>")
                lines.append(f"  🔥{imp} 👍{pos} 👎{neg}")
                lines.append("")
            text = "\n".join(lines)
            async with httpx.AsyncClient(timeout=15) as tg:
                tr = await tg.post(
                    f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                    json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                          "disable_web_page_preview": True},
                )
                tj = tr.json()
                if tj.get("ok"):
                    print("  ✅ Sent 3 news to your Telegram")
                else:
                    print(f"  ⚠️ Telegram failed: {tj.get('description')}")
    except Exception as e:
        print(f"  ❌ exception: {type(e).__name__}: {e}")


async def summarize(results):
    print("\n" + "=" * 70)
    print("  📋 整體 inventory 總結")
    print("=" * 70)
    working: list[str] = []
    paid: list[str] = []
    missing: list[str] = []
    other: list[str] = []
    for cat, entries in results.items():
        for entry in entries:
            name, status, code, msg, detail, tag = entry
            label = f"{cat} :: {name}"
            if tag == "✅":
                working.append(label)
            elif tag == "💰":
                paid.append(label)
            elif tag == "❌":
                missing.append(label)
            else:
                other.append(f"{label} ({code} {msg[:30]})")
    print(f"\n  ✅ 可用 ({len(working)}):")
    for w in working: print(f"    {w}")
    if paid:
        print(f"\n  💰 需付費升級 ({len(paid)}):")
        for p in paid: print(f"    {p}")
    if missing:
        print(f"\n  ❌ 端點不存在 / wrong path ({len(missing)}):")
        for m in missing: print(f"    {m}")
    if other:
        print(f"\n  ⚠️ 其他 / 需檢查 ({len(other)}):")
        for o in other: print(f"    {o}")


async def main():
    await verify_cryptopanic_and_demo()
    results = await probe_with_pacing()
    await summarize(results)


if __name__ == "__main__":
    asyncio.run(main())
