"""v26: 建立 🇺🇸 美股訊號 + 🇺🇸 美股持倉與績效 兩主題"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from telegram_bot.client import TelegramClient
from telegram_bot.topics import load_topics_config, save_topics_config


async def main():
    cfg = load_topics_config()
    if not cfg:
        print("ERROR: no topics config")
        return 1
    tg = TelegramClient()
    want = {"us_signals": "🇺🇸 美股訊號", "us_positions": "🇺🇸 美股持倉與績效"}
    for key, name in want.items():
        if key in cfg["topics"]:
            print(f"{key} 已存在: {cfg['topics'][key]}")
            continue
        r = await tg._post("createForumTopic",
                           {"chat_id": cfg["group_chat_id"], "name": name})
        tid = (r.get("result") or {}).get("message_thread_id")
        if not tid:
            print(f"ERROR {key}: {str(r)[:150]}")
            continue
        cfg["topics"][key] = tid
        print(f"{key} 建立: thread_id={tid}")
    if not save_topics_config(cfg["group_chat_id"], cfg["topics"]):
        # v197：主題已建在群組裡，只是沒記下來 ⇒ ⛔ 不可回報成功（重跑會再建一輪）
        print("⛔ 主題已建但設定檔寫不進去——請人工補進 topics 後再重跑：", cfg["topics"])
        return 1
    print("topics:", cfg["topics"])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
