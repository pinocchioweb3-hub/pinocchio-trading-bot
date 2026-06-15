"""v36 群組精簡 12→6 遷移腳本。

設計原則（對應使用者紅線「對外發文/動群組結構需使用者確認」）：
  - 預設 DRY-RUN：只印計畫，不動 Telegram、不改 json。
  - 加 --apply 才真的執行（會動到實際社群群組）：
      1. 關閉(closeForumTopic) 6 個退役主題 → 只是「不再收新訊息」，
         **絕不刪除(deleteForumTopic)**，歷史訊息全保留、隨時可在 Telegram 重開。
      2. 把 telegram_topics.json 收斂成 6 個 key（會先備份 .v36bak）。
  - 加 --tombstone：關閉前先在每個退役主題貼一則「已併入 X」導引訊息，
    讓社群成員知道內容搬去哪。

保留的 6 群名稱「不變」（🎯交易訊號/📈持倉與績效/📊市場情報/📰新聞快訊/🛠系統狀態/💡意見箱），
只是內容擴充（加密+美股合併、資產別用 🪙/🇺🇸/🥇 emoji 在訊息內區分）。

Part A 的「🔗原始訊號」回連用的是全群唯一 msg_id，主題怎麼重排都連得到，不受本遷移影響。

用法：
    python migrate_topics_v36.py                 # 看計畫（安全，不動任何東西）
    python migrate_topics_v36.py --apply         # 真的關閉退役主題 + 收斂 json
    python migrate_topics_v36.py --apply --tombstone   # 同上，並先貼導引訊息
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")  # 讓 --apply 取得 TELEGRAM_BOT_TOKEN

from telegram_bot.client import TelegramClient
from telegram_bot.topics import (RETIRED_TOPIC_KEYS_V36, TOPIC_DEFS,
                                  TOPICS_FILE, load_topics_config)

KEPT_LABEL = dict(TOPIC_DEFS)  # key -> 顯示名稱


async def run(apply: bool, tombstone: bool) -> None:
    cfg = load_topics_config()
    if not cfg:
        print(f"❌ 找不到 {TOPICS_FILE}，無法遷移（forum 尚未設定）。")
        return
    chat_id = cfg["group_chat_id"]
    topics = dict(cfg["topics"])

    base = TelegramClient()
    if not base.token:
        print("❌ TELEGRAM_BOT_TOKEN 未設定，--apply 無法執行。")
        if apply:
            return
    client = base.for_topic(chat_id, None)

    print(f"群組 {chat_id}；目前 {len(topics)} 個主題。\n")
    print("保留 6 群（名稱不變、內容擴充）：")
    for k, label in TOPIC_DEFS:
        tid = topics.get(k)
        flag = "" if tid is not None else "  ⚠️ json 缺此 key"
        print(f"  ✅ {label:14s} key={k:10s} thread_id={tid}{flag}")

    print("\n退役 6 群（關閉封存 → 不刪、可重開）：")
    plan_close = []
    for k, dest in RETIRED_TOPIC_KEYS_V36.items():
        tid = topics.get(k)
        dest_label = KEPT_LABEL.get(dest, dest)
        print(f"  🔒 key={k:12s} thread_id={tid}  → 內容導向 {dest_label}")
        if tid is not None:
            plan_close.append((k, tid, dest))

    if not apply:
        print("\n[DRY-RUN] 未做任何變更。確認無誤後加 --apply 才會動到實際 Telegram 群組。")
        return

    # 備份 json
    bak = TOPICS_FILE.parent / (TOPICS_FILE.name + ".v36bak")
    bak.write_text(TOPICS_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"\n已備份 json → {bak}")

    for k, tid, dest in plan_close:
        if tombstone:
            dest_label = KEPT_LABEL.get(dest, dest)
            msg = ("📦 <b>本頻道已整併</b>\n\n"
                   f"為了讓社群更好追蹤，此主題內容已併入「{dest_label}」。\n"
                   "往後請到該主題查看最新訊息；這裡保留作為歷史存檔。")
            r = await client.send_message(msg, parse_mode="HTML",
                                          message_thread_id=tid)
            print(f"  tombstone {k}: ok={r.get('ok')}")
        r = await client._post("closeForumTopic",
                               {"chat_id": chat_id, "message_thread_id": tid})
        ok = r.get("ok")
        extra = "" if ok else f"  desc={r.get('description')}"
        print(f"  close {k} (thread {tid}): ok={ok}{extra}")

    # 收斂 json → 只留 6 key
    new_topics = {k: topics[k] for k, _ in TOPIC_DEFS if k in topics}
    TOPICS_FILE.write_text(
        json.dumps({"group_chat_id": chat_id, "topics": new_topics},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"\ntelegram_topics.json 已收斂為 {len(new_topics)} key：{list(new_topics)}")
    print("✅ 完成。請重啟 daemon 套用新路由。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="真的執行（會動到實際 Telegram 群組）")
    ap.add_argument("--tombstone", action="store_true",
                    help="關閉前先在退役主題貼導引訊息")
    a = ap.parse_args()
    asyncio.run(run(a.apply, a.tombstone))
