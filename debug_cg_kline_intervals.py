"""Probe CoinGlass K 線時間框架支援度（你 Startup $79 等級到底拿得到什麼）。"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

import httpx

KEY = os.getenv("COINGLASS_API_KEY")
BASE = "https://open-api-v4.coinglass.com"
HEADERS = {"CG-API-KEY": KEY or "", "Accept": "application/json"}

# 全部要測的時框
INTERVALS = [
    ("1m",  "1 分鐘"),
    ("3m",  "3 分鐘"),
    ("5m",  "5 分鐘"),
    ("15m", "15 分鐘"),
    ("30m", "30 分鐘"),
    ("1h",  "1 小時"),
    ("2h",  "2 小時"),
    ("4h",  "4 小時"),
    ("6h",  "6 小時"),
    ("8h",  "8 小時"),
    ("12h", "12 小時"),
    ("1d",  "1 日"),
    ("3d",  "3 日"),
    ("1w",  "1 週"),
    ("2w",  "2 週"),
    ("1M",  "1 月"),
]

# 對 BTC 期貨 price-history 測（最常用端點）
ENDPOINT = "/api/futures/price/history"


async def probe_interval(client, interval: str, label: str) -> dict:
    """測試該 interval 是否可用 + 拿到幾根 + 最遠日期"""
    params = {
        "exchange": "Binance",
        "symbol": "BTCUSDT",
        "interval": interval,
        "limit": 500,
    }
    try:
        r = await client.get(ENDPOINT, params=params, timeout=15)
    except Exception as e:
        return {"interval": interval, "label": label, "ok": False, "err": str(e)}

    if r.status_code != 200:
        return {"interval": interval, "label": label, "ok": False,
                "http": r.status_code, "body": r.text[:100]}

    try:
        body = r.json()
    except Exception:
        return {"interval": interval, "label": label, "ok": False, "err": "non-JSON"}

    code = body.get("code")
    if code not in ("0", 0):
        return {"interval": interval, "label": label, "ok": False,
                "code": str(code), "msg": str(body.get("msg") or "")[:80]}

    data = body.get("data") or []
    if not data:
        return {"interval": interval, "label": label, "ok": True,
                "rows": 0, "depth": "no_data"}

    # 算最遠日期 + 拿到幾根
    rows = len(data)
    try:
        first_ts = int(data[0].get("time", 0))
        last_ts = int(data[-1].get("time", 0))
        from datetime import datetime, timezone
        first_dt = datetime.fromtimestamp(first_ts / 1000, tz=timezone.utc)
        last_dt = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc)
        depth_days = (last_dt - first_dt).total_seconds() / 86400
        depth_str = (
            f"{depth_days:.1f} 天" if depth_days < 90 else
            f"{depth_days/30:.1f} 月" if depth_days < 730 else
            f"{depth_days/365:.1f} 年"
        )
        first_str = first_dt.strftime("%Y-%m-%d")
        return {"interval": interval, "label": label, "ok": True,
                "rows": rows, "depth_str": depth_str, "first_date": first_str}
    except Exception as e:
        return {"interval": interval, "label": label, "ok": True,
                "rows": rows, "err": f"date parse: {e}"}


async def main():
    if not KEY:
        print("Missing COINGLASS_API_KEY"); return 1

    print("=" * 80)
    print("  CoinGlass K 線時間框架支援度測試（BTC 期貨 / Binance）")
    print(f"  端點：{ENDPOINT}")
    print(f"  Limit：500（最大）")
    print("=" * 80)
    print(f"\n  {'時框':<8} {'說明':<10} {'狀態':<6} {'回傳根數':<10} {'歷史深度':<15} {'最早日期'}")
    print(f"  {'-'*8} {'-'*10} {'-'*6} {'-'*10} {'-'*15} {'-'*12}")

    async with httpx.AsyncClient(base_url=BASE, headers=HEADERS) as client:
        for interval, label in INTERVALS:
            await asyncio.sleep(0.9)  # paced，避免 429
            r = await probe_interval(client, interval, label)
            ok = "✅" if r.get("ok") and r.get("rows", 0) > 0 else (
                 "⚠️" if r.get("ok") else "❌")
            rows = r.get("rows", 0)
            depth = r.get("depth_str", "—")
            first = r.get("first_date", "—")
            note = r.get("err") or r.get("msg") or ""
            print(f"  {interval:<8} {label:<10} {ok:<6} {rows:<10} {depth:<15} {first}  {note[:30]}")

    print()
    print("=" * 80)
    print("  說明：")
    print("    rows = 500 表示該時框是 CoinGlass 標準支援（理論上 limit 上限）")
    print("    rows < 500 但 > 0 表示有資料但歷史深度有限")
    print("    rows = 0 / 404 表示該時框不在 Startup 等級權限內")
    print("    若 1M / 2w / 3w 不支援，可從 1d / 1w 自製聚合")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
