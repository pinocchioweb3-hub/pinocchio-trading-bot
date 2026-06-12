"""嘗試 6 個額外的 Twitter scraper actors，找 FREE plan 能用的。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


async def try_actor(token: str, actor: str, input_obj: dict) -> dict:
    url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?token={token}"
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(url, json=input_obj)
        if r.status_code not in (200, 201):
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:120]}"}
        data = r.json()
        if not isinstance(data, list):
            return {"ok": False, "error": f"non-list: {str(data)[:200]}"}
        valid = [d for d in data
                 if isinstance(d, dict) and not d.get("noResults")
                 and not d.get("error") and not d.get("demo")]
        if not valid:
            sample_keys = sorted(data[0].keys())[:8] if data else []
            return {"ok": False, "error": f"empty/demo; keys={sample_keys}"}
        first = valid[0]
        text = (first.get("text") or first.get("fullText") or first.get("content") or "")
        author_obj = first.get("author") or first.get("user") or {}
        if isinstance(author_obj, dict):
            author = (author_obj.get("userName") or author_obj.get("screen_name")
                     or author_obj.get("username") or "?")
        else:
            author = str(author_obj or first.get("authorScreenName", "?"))
        return {
            "ok": True, "count": len(valid),
            "first_text": text[:120], "first_author": author,
            "all_keys": sorted(first.keys())[:15],
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:140]}"}


async def main() -> int:
    token = os.getenv("APIFY_TOKEN", "").strip()
    if not token:
        return 1

    handles = ["realDonaldTrump", "elonmusk", "binance"]

    # 各種替代 actors
    tests = [
        # kaito 系列（pay-per-result 最便宜）
        ("kaitoeasyapi~twitter-x-data-tweet-scraper-pay-per-result-cheapest",
         {"twitterHandles": handles, "maxItems": 6}),
        ("kaitoeasyapi~premium-x-twitter-scraper-pay-per-result",
         {"twitterHandles": handles, "maxItems": 6}),
        # web.harvester
        ("web.harvester~twitter-scraper",
         {"handles": handles, "maxTweets": 6}),
        # 替代 try various
        ("u6ge5r4dwz~twitter-x-scraper",
         {"handles": handles, "tweetsPerHandle": 6}),
        # 知名免費 actor
        ("danek~twitter-scraper",
         {"profiles": handles, "tweetsCount": 6}),
        # 試另一個熱門：apidojo 的免費 free-twitter-scraper
        ("apidojo~free-twitter-scraper",
         {"twitterHandles": handles, "maxItems": 6}),
        # 最近熱門
        ("scraperPlus~twitter-scraper",
         {"handles": handles, "maxItems": 6}),
        # XPLOIT 系列
        ("xploit~x-twitter-scraper",
         {"twitterHandles": handles, "maxItems": 6}),
        # nodejs 通用 actor
        ("epctex~twitter-scraper",
         {"keywords": [f"from:{h}" for h in handles], "maxItems": 6}),
    ]

    print(f"=== 測試 {len(tests)} 個替代 actor ===\n")
    winner = None
    for actor, inp in tests:
        print(f"\n>>> {actor}")
        res = await try_actor(token, actor, inp)
        if res["ok"]:
            print(f"    ✅ {res['count']} items by @{res['first_author']}")
            print(f"    tweet: {res['first_text']}")
            print(f"    keys: {res['all_keys']}")
            if not winner:
                winner = (actor, inp)
                break  # 找到就停
        else:
            print(f"    ❌ {res['error'][:140]}")

    if winner:
        print(f"\n=== 🏆 WINNER ===")
        print(f"Actor:  {winner[0]}")
        print(f"Input:  {json.dumps(winner[1])}")
        return 0
    print("\n=== 全部失敗 ===")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
