"""v23: 建立 📡 即時動態 主題並更新 telegram_topics.json"""
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
    if "pulse" in cfg["topics"]:
        print(f"pulse topic already exists: {cfg['topics']['pulse']}")
        return 0
    tg = TelegramClient()
    r = await tg._post("createForumTopic",
                       {"chat_id": cfg["group_chat_id"], "name": "📡 即時動態"})
    tid = (r.get("result") or {}).get("message_thread_id")
    if not tid:
        print(f"ERROR: create failed: {str(r)[:200]}")
        return 1
    cfg["topics"]["pulse"] = tid
    save_topics_config(cfg["group_chat_id"], cfg["topics"])
    print(f"pulse topic created: thread_id={tid}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
