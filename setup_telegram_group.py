"""一鍵設定 Telegram 社群（Forum 超級群組 + 4 個主題頻道）。

v14.2 即時監看模式：
    1. 你在目標群組隨便發一則訊息（任何內容都行）→ 腳本抓到群組
    2. 腳本每 5 秒查一次群組狀態，等你打開「主題 (Topics)」開關
    3. 開關一打開 → 自動建 4 個主題 → 存設定 → 發測試訊息

跑法：python setup_telegram_group.py
"""
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
from telegram_bot.topics import (TOPIC_DEFS, TOPICS_FILE, load_topics_config_status,
                                 save_topics_config)


async def _find_group(tg: TelegramClient, minutes: float = 10) -> tuple[str, str] | None:
    """短輪詢 getUpdates 抓最近互動過的群組（避開長連線在不穩網路的超時）。

    任何 group/supergroup 的 message / my_chat_member（bot 被加入）都算。
    """
    offset = None
    rounds = int(minutes * 60 / 3) + 1  # 每 3 秒一輪
    for _ in range(rounds):
        try:
            resp = await tg.get_updates(offset=offset, timeout=0)  # 短輪詢
        except Exception:
            await asyncio.sleep(3)
            continue
        if not resp.get("ok"):
            await asyncio.sleep(3)
            continue
        for upd in resp.get("result", []):
            offset = upd["update_id"] + 1
            # message / my_chat_member（bot 被加群）/ chat_member 都可能帶 chat
            container = (upd.get("message") or upd.get("my_chat_member")
                        or upd.get("chat_member") or upd.get("callback_query", {}).get("message")
                        or {})
            chat = container.get("chat") or {}
            if chat.get("type") in ("group", "supergroup"):
                try:
                    await tg.get_updates(offset=offset, timeout=0)  # commit offset
                except Exception:
                    pass
                return str(chat["id"]), chat.get("title", "")
        await asyncio.sleep(3)
    return None


async def _wait_forum_enabled(tg: TelegramClient, chat_id: str, title: str,
                              minutes: float = 15) -> str | None:
    """每 5 秒 getChat 檢查 is_forum；群組升級（id 改變）時自動跟進。回最終 chat_id。"""
    cur_id = chat_id
    notified = False
    for i in range(int(minutes * 60 / 5)):
        r = await tg.get_chat(cur_id)
        if not r.get("ok"):
            desc = str(r.get("description", ""))
            # 基本群組開主題會升級成 supergroup → 舊 id 失效，回應含 migrate 資訊
            mig = (r.get("parameters") or {}).get("migrate_to_chat_id")
            if mig:
                cur_id = str(mig)
                print(f"  群組已升級成超級群組（新 id={cur_id}）")
                continue
            print(f"  getChat 失敗: {desc[:80]}")
            await asyncio.sleep(5)
            continue
        chat = r.get("result", {})
        if chat.get("is_forum"):
            return cur_id
        if not notified:
            print(f"  ⏳ 已鎖定「{title}」(id={cur_id})，等待你開啟「主題 (Topics)」開關…")
            print(f"     （腳本每 5 秒自動檢查，開啟後立刻繼續，最多等 {minutes:.0f} 分鐘）")
            notified = True
        if i % 12 == 11:
            print(f"  ⏳ 仍在等待主題開啟…（已等 {(i+1)*5//60} 分鐘）")
        await asyncio.sleep(5)
    return None


async def main() -> int:
    tg = TelegramClient()
    if not tg.token:
        print("ERROR: TELEGRAM_BOT_TOKEN not set")
        return 1

    existing, status = load_topics_config_status()
    if existing:
        print(f"已有設定：group={existing['group_chat_id']} topics={existing['topics']}")
        print(f"（要重新設定請先刪除 {TOPICS_FILE}）")
        return 0
    if status != "missing":
        # v197：⛔ 這是本腳本唯一會造成**不可逆**破壞的入口。設定檔存在卻讀不出來時，
        # 舊碼把它讀成「還沒設定過」，於是在同一個群組把 TOPIC_DEFS 全部再建一次，
        # 再用新的 thread_id 整包覆寫設定檔——原 thread_id 永久滅失、歷史訊息留在
        # 孤兒主題裡，而 Telegram 沒有「合併主題」這種操作。
        print(f"⛔ 停止：{TOPICS_FILE} 存在但讀不出來（status={status}）。")
        print("   若在這裡繼續，我會把所有主題在同一個群組**再建一次**，並用新的")
        print("   thread_id 整包覆寫設定檔——原本的 thread_id 會永久滅失，")
        print("   歷史訊息會留在沒人看的孤兒主題裡（Telegram 無法合併主題）。")
        print(f"   請先人工檢視 {TOPICS_FILE.with_suffix('.bad')}，把設定檔修回來再重跑。")
        return 1

    me = await tg.get_me()
    bot_name = me.get("result", {}).get("username", "?")
    print(f"Bot: @{bot_name}")
    print()
    print("步驟：")
    print("  1. 在目標群組裡『隨便發一則訊息』（任何內容都行）讓我抓到群組")
    print("  2. 然後去開「主題 (Topics)」開關 — 腳本會即時偵測")
    print()
    print("等待群組訊息中…（最多 45 分鐘，慢慢操作）")

    found = await _find_group(tg, minutes=45)
    if not found:
        print("逾時：沒收到任何群組訊息。請在群組發一則訊息後重跑。")
        return 1
    chat_id, title = found
    print(f"✅ 鎖定群組：「{title}」 (id={chat_id})")

    # 即時等待主題開啟
    final_id = await _wait_forum_enabled(tg, chat_id, title, minutes=30)
    if not final_id:
        print("逾時：主題開關仍未開啟。")
        print("開啟位置：")
        print("  電腦版（新版）：群組 → 點群組名稱 → ⋮ 或 ✏️ → 「主題」開關")
        print("  手機版：群組 → 點群組名稱 → ✏️ 編輯 → 「主題」→ 開啟")
        return 1

    print(f"✅ 主題功能已開啟！開始建頻道…")

    # 建 4 個主題
    group_client = tg.for_topic(final_id, None)
    topics: dict[str, int] = {}
    for key, name in TOPIC_DEFS:
        r = await group_client.create_forum_topic(name)
        if r.get("ok"):
            tid = r["result"]["message_thread_id"]
            topics[key] = tid
            print(f"  ✅ 建主題 {name} (thread_id={tid})")
        else:
            print(f"  ❌ 建主題 {name} 失敗: {r.get('description')}")
            print("     → 請把 bot 設為管理員並開啟「管理主題 (Manage Topics)」權限後重跑")
            return 1
        await asyncio.sleep(0.5)

    if not save_topics_config(final_id, topics):
        # v197：主題已經在群組裡建好了，只是沒記下來。⛔ 不可回報成功——下一次重跑
        # 會判定「還沒設定過」而再建一輪重複主題。把 id 印出來讓人工補寫回去。
        print(f"\n⛔ 主題已建好，但設定檔寫不進去（{TOPICS_FILE}）。")
        print("   ⛔ 修好之前不要重跑本腳本——會再建一組重複的主題。")
        print("   請人工把下面這份內容存成該檔案：")
        print(f"   {{\"group_chat_id\": \"{final_id}\", \"topics\": {topics}}}")
        return 1
    print(f"\n✅ 設定已存至 {TOPICS_FILE}")

    # 每個主題發測試訊息
    tests = {
        "trade":   "🎯 <b>交易訊號頻道已就緒</b>\nFIRE 訊號、TP/SL 事件、持倉快照、績效報告會推到這裡。",
        "intel":   "📊 <b>市場情報頻道已就緒</b>\n每日宏觀、每小時 Pulse、Deep Dive 會推到這裡。",
        "news":    "📰 <b>新聞快訊頻道已就緒</b>\nTrump 與 X 推文（已過濾+繁中翻譯）會推到這裡。",
        "usstock": "🇺🇸 <b>美股頻道已就緒</b>\n未來美股報單功能上線後會推到這裡。",
    }
    for key, text in tests.items():
        c = tg.for_topic(final_id, topics[key])
        r = await c.send_message(text, parse_mode="HTML")
        print(f"  測試 {key}: {'✅' if r.get('ok') else '❌ ' + str(r.get('description'))}")
        await asyncio.sleep(1.0)

    print("\n🎉 完成！重啟 bot 後所有訊息自動分流。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
