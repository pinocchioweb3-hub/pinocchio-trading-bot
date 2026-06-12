"""安全驗證 APIFY_TOKEN 是否設定正確且能呼叫 Apify API。

只顯示 token 前 6 字（masked），不曝露完整 token。
跑：python verify_apify_token.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def mask(s: str) -> str:
    if not s:
        return "(empty)"
    if len(s) <= 10:
        return s[:2] + "***"
    return s[:6] + "..." + s[-4:]


async def main() -> int:
    token = os.getenv("APIFY_TOKEN", "").strip()
    actor = os.getenv("APIFY_ACTOR", "kaitoeasyapi~twitter-x-data-tweet-scraper-pay-per-result-cheapest").strip()

    print(f"=== Apify Token 驗證 ===")
    print(f"Token 設定：{mask(token)}")
    print(f"Actor:      {actor}")
    print()

    if not token:
        print("❌ APIFY_TOKEN 未設定或為空")
        print("   請編輯 .env，將 token 貼在 APIFY_TOKEN= 後面")
        return 1

    if not token.startswith("apify_api_"):
        print("⚠️  Token 格式異常（應該以 apify_api_ 開頭）")
        print("   若你確定是正確的 token，請繼續")

    # 呼叫 Apify /v2/users/me 端點驗證
    print("正在呼叫 Apify API 驗證 token 有效性...")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"https://api.apify.com/v2/users/me?token={token}"
            )
        if r.status_code == 200:
            data = r.json().get("data", {})
            print(f"✅ Token 有效！")
            print(f"   帳號：{data.get('username', '?')}")
            print(f"   Email：{mask(data.get('email', ''))}")
            print(f"   方案：{data.get('plan', '?')}")
        elif r.status_code == 401:
            print(f"❌ Token 無效（401 Unauthorized）")
            print(f"   請去 Apify console 重新生成 token")
            return 1
        else:
            print(f"⚠️  HTTP {r.status_code}: {r.text[:200]}")
            return 1
    except Exception as e:
        print(f"❌ 連線錯誤：{type(e).__name__}: {e}")
        return 1

    # 試抓 3 帳號各 2 推（最小成本測試 ~ $0.002）
    print()
    print("=== 試抓 3 帳號最新推文（成本 ~$0.002 USD）===")
    try:
        from news_feed.twitter_apify import fetch_tweets_via_apify
        posts = await fetch_tweets_via_apify(
            ["realDonaldTrump", "elonmusk", "binance"],
            max_per_handle=2,
        )
        print(f"✅ 抓到 {len(posts)} 則推文：")
        for p in posts[:6]:
            print(f"  @{p['handle']:18s} | {p['content'][:80]}")
    except Exception as e:
        print(f"❌ 抓取失敗：{type(e).__name__}: {e}")
        return 1

    print()
    print("=== ✅ 全部驗證通過 ===")
    print("接下來：重啟 daemon，twitter_apify worker 就會自動啟動")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
