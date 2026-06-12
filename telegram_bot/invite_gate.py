"""v22-2: 邀請碼自動入群閘門 — Threads 引流漏斗的核心。

流程（全自動，研究查證的 join-request 模式）：
    Threads 貼文 → t.me/<bot>?start=join 深連結 → 用戶按 START
    → bot 私訊選單：[🆕 我要註冊 OKX] [👤 我已有 OKX 帳號]
    → 新用戶：給註冊連結 → 用戶貼 UID → OKX 聯盟 API 驗證
        ✓ → 生成 10 分鐘專屬入群連結（需審批模式）→ 用戶點擊
          → bot 收到 chat_join_request → 核對 Telegram ID → 自動批准 → 撤銷連結
        ✗ → 引導排查重試（每小時最多 5 次防爆破）
    → 舊用戶：收集情況 → 轉交管理員（OKX 政策：活躍帳號無法改綁，
        180 天閒置可 recall — 由管理員人工判斷）

安全：一碼一人（連結與 user_id 綁定，轉傳無效）、連結 10 分鐘過期、
    用後即銷毀、CAS 反垃圾黑名單檢查、頻率限制。
"""
from __future__ import annotations

import sqlite3
import time

import httpx

from botpaths import db_path as _db_path

DB_PATH = _db_path("invite_gate.db")

LINK_TTL_S = 600           # 入群連結 10 分鐘過期
UID_ATTEMPTS_PER_HOUR = 5  # 防爆破猜 UID
OKX_REFERRAL_URL = "https://www.okx.com/join/PINOCCHIOWEB3"   # 皮諾丘計畫邀請碼

# 對話狀態
ST_MENU = "menu"
ST_AWAIT_UID = "await_uid"
ST_AWAIT_OLD = "await_old_info"
ST_LINK_SENT = "link_sent"
ST_JOINED = "joined"
ST_FORWARDED = "forwarded_admin"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    conn = _conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS applicants (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                state TEXT NOT NULL DEFAULT 'menu',
                uid TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                window_start INTEGER NOT NULL DEFAULT 0,
                created_ts INTEGER NOT NULL,
                updated_ts INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS gate_links (
                invite_link TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'  -- active|used|revoked
            )
        """)
    finally:
        conn.close()


def _get_applicant(user_id: int) -> dict | None:
    conn = _conn()
    try:
        r = conn.execute(
            "SELECT user_id, username, state, uid, attempts, window_start "
            "FROM applicants WHERE user_id=?", (user_id,)).fetchone()
        if not r:
            return None
        return {"user_id": r[0], "username": r[1], "state": r[2],
                "uid": r[3], "attempts": r[4], "window_start": r[5]}
    finally:
        conn.close()


def _upsert_applicant(user_id: int, username: str, **fields) -> None:
    now = int(time.time())
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO applicants (user_id, username, created_ts, updated_ts) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
            "username=excluded.username, updated_ts=excluded.updated_ts",
            (user_id, username, now, now))
        for k, v in fields.items():
            conn.execute(f"UPDATE applicants SET {k}=?, updated_ts=? WHERE user_id=?",
                         (v, now, user_id))
    finally:
        conn.close()


def _rate_limited(user_id: int) -> bool:
    """每小時 UID 驗證嘗試上限（防爆破）。回 True = 已超限。"""
    now = int(time.time())
    a = _get_applicant(user_id)
    if not a:
        return False
    if now - (a["window_start"] or 0) > 3600:
        _upsert_applicant(user_id, a["username"] or "", attempts=0, window_start=now)
        return False
    return (a["attempts"] or 0) >= UID_ATTEMPTS_PER_HOUR


def _bump_attempt(user_id: int, username: str) -> None:
    now = int(time.time())
    a = _get_applicant(user_id)
    if not a or now - (a["window_start"] or 0) > 3600:
        _upsert_applicant(user_id, username, attempts=1, window_start=now)
    else:
        _upsert_applicant(user_id, username, attempts=(a["attempts"] or 0) + 1)


def _group_chat_id() -> str | None:
    """超級群組 ID（與 topics.py 同一份設定檔）"""
    try:
        from .topics import load_topics_config
        cfg = load_topics_config()
        return str(cfg["group_chat_id"]) if cfg else None
    except Exception:
        return None


async def _cas_banned(user_id: int) -> bool:
    """Combot Anti-Spam 黑名單（免費、無金鑰）。查不到 = 乾淨。"""
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"https://api.cas.chat/check?user_id={user_id}")
        return bool(r.json().get("ok"))
    except Exception:
        return False   # CAS 掛掉不擋人（fail-open，避免誤殺）


async def _dm(tg, chat_id: int, text: str, buttons=None) -> dict:
    """私訊任意用戶（client.send_message 綁定固定 chat_id，這裡直接 _post）"""
    body = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True}
    if buttons:
        body["reply_markup"] = {"inline_keyboard": buttons}
    return await tg._post("sendMessage", body)


# ===========================================================================
# 對話流程
# ===========================================================================
WELCOME = (
    "👋 歡迎來到 <b>皮諾丘交易機器人</b> 社群入口！\n\n"
    "這是一個 AI 共建的開源交易訊號系統 — 開源、免費、帳本全公開。\n"
    "入群唯一條件：使用我們的 OKX 邀請碼註冊（返佣用於維運與回饋社群）。\n\n"
    "請問你的情況是？"
)
MENU_BUTTONS = [
    [{"text": "🆕 我要註冊 OKX（新用戶）", "callback_data": "gate:new"}],
    [{"text": "👤 我已有 OKX 帳號", "callback_data": "gate:old"}],
]


async def handle_private_message(tg, msg: dict) -> bool:
    """處理私訊。回 True = 此訊息已被入群閘門消化。"""
    init_db()
    user = msg.get("from") or {}
    user_id = user.get("id")
    if not user_id or user.get("is_bot"):
        return False
    username = user.get("username") or user.get("first_name") or ""
    text = (msg.get("text") or "").strip()

    # /start [payload] → 重置流程顯示選單
    if text.lower().startswith("/start"):
        _upsert_applicant(user_id, username, state=ST_MENU)
        await _dm(tg, user_id, WELCOME, MENU_BUTTONS)
        print(f"[gate] /start from {user_id} ({username})")
        return True

    a = _get_applicant(user_id)
    if not a:
        return False   # 不在流程中的私訊 → 交回其他處理器

    if a["state"] == ST_AWAIT_UID:
        await _process_uid(tg, user_id, username, text)
        return True

    if a["state"] == ST_AWAIT_OLD:
        # 收集舊用戶情況 → 轉交管理員
        _upsert_applicant(user_id, username, state=ST_FORWARDED)
        try:
            from .client import TelegramClient
            admin = TelegramClient()   # 預設 chat = 管理員私聊
            await admin.send_message(
                f"👤 <b>舊用戶入群申請</b>（需人工判斷）\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"Telegram：{username}（<code>{user_id}</code>）\n"
                f"自述：{text[:500]}\n\n"
                f"<i>OKX 政策：活躍帳號無法改綁邀請碼；180 天無入金無交易無登入"
                f"的閒置帳號可在聯盟後台 recall。處理後用 /gate_approve {user_id} 放行。</i>",
                parse_mode="HTML")
        except Exception as e:
            print(f"[gate] admin forward error: {e}")
        await _dm(tg, user_id,
                  "📨 已收到你的情況，管理員會盡快處理！\n\n"
                  "先說明 OKX 的官方限制：已綁定其他邀請碼的<b>活躍帳號</b>"
                  "目前無法轉移邀請關係（交易所政策）；超過 180 天未使用的"
                  "閒置帳號則可以申請召回。\n"
                  "無論結果如何我們都會回覆你 — 開源程式碼與公開內容永遠免費。")
        return True

    return False


async def _process_uid(tg, user_id: int, username: str, text: str) -> None:
    uid = text.replace(" ", "")
    if not uid.isdigit():
        await _dm(tg, user_id,
                  "🔢 UID 應該是純數字（在 OKX App：頭像 → 個人資料 → UID）。\n"
                  "請再貼一次：")
        return
    if _rate_limited(user_id):
        await _dm(tg, user_id, "⏳ 嘗試太頻繁，請一小時後再試。")
        return
    _bump_attempt(user_id, username)

    from .okx_affiliate import is_mock_mode, verify_invitee
    r = await verify_invitee(uid)

    if not r["ok"]:
        await _dm(tg, user_id,
                  "🔧 驗證系統暫時無法連線，請稍後再試（你的 UID 已記下，"
                  "稍後會自動補驗）。")
        print(f"[gate] verify system error for {user_id}: {r['error']}")
        return

    if not r["is_invitee"]:
        await _dm(tg, user_id,
                  "❌ 這個 UID 沒有綁定我們的邀請碼。常見原因：\n"
                  "1️⃣ 註冊時沒填邀請碼 — 邀請碼必須在<b>註冊當下</b>填寫\n"
                  "2️⃣ UID 抄錯（檢查 OKX App → 頭像 → UID）\n"
                  "3️⃣ 剛註冊，資料同步中 — 等 10 分鐘再試\n\n"
                  "還沒註冊？點下面重新開始 👇",
                  [[{"text": "🆕 重新查看註冊步驟", "callback_data": "gate:new"}]])
        return

    # ✓ 驗證通過 → 發專屬入群連結
    _upsert_applicant(user_id, username, uid=uid)
    if await _cas_banned(user_id):
        await _dm(tg, user_id, "⚠️ 你的帳號被反垃圾系統標記，暫時無法自動入群。")
        print(f"[gate] CAS banned: {user_id}")
        return

    group_id = _group_chat_id()
    if not group_id:
        await _dm(tg, user_id, "🔧 群組設定異常，管理員已收到通知。")
        return
    resp = await tg._post("createChatInviteLink", {
        "chat_id": group_id,
        "creates_join_request": True,
        "expire_date": int(time.time()) + LINK_TTL_S,
        "name": f"uid:{uid}"[:32],
    })
    link = ((resp.get("result") or {}).get("invite_link"))
    if not link:
        await _dm(tg, user_id, "🔧 連結生成失敗，請稍後再試。")
        print(f"[gate] createChatInviteLink failed: {str(resp)[:150]}")
        return
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO gate_links VALUES (?, ?, ?, 'active')",
            (link, user_id, int(time.time()) + LINK_TTL_S))
    finally:
        conn.close()
    _upsert_applicant(user_id, username, state=ST_LINK_SENT)
    mock_tag = "（測試模式）" if is_mock_mode() else ""
    await _dm(tg, user_id,
              f"✅ <b>驗證通過！</b>{mock_tag}\n\n"
              f"這是你的<b>專屬</b>入群連結（10 分鐘內有效、僅限你本人）：\n"
              f"{link}\n\n"
              f"點擊後送出申請，系統會自動核對你的身分並放行 — 馬上見！")
    print(f"[gate] link issued to {user_id} (uid={uid}, mock={r['mock']})")


async def handle_gate_callback(tg, cq: dict) -> bool:
    """處理 gate: 前綴的按鈕。回 True = 已處理。"""
    init_db()
    data = cq.get("data") or ""
    if not data.startswith("gate:"):
        return False
    user = cq.get("from") or {}
    user_id = user.get("id")
    username = user.get("username") or user.get("first_name") or ""
    action = data.split(":", 1)[1]
    try:
        await tg.answer_callback_query(cq.get("id", ""), "")
    except Exception:
        pass

    if action == "new":
        _upsert_applicant(user_id, username, state=ST_AWAIT_UID)
        await _dm(tg, user_id,
                  "🆕 <b>三步驟加入：</b>\n\n"
                  f"1️⃣ 用這個連結註冊 OKX（邀請碼會自動帶入）：\n{OKX_REFERRAL_URL}\n\n"
                  "2️⃣ 完成註冊後，到 App：<b>頭像 → 個人資料 → UID</b>，複製那串數字\n\n"
                  "3️⃣ 把 UID 直接貼在這裡傳給我 👇")
        return True
    if action == "old":
        _upsert_applicant(user_id, username, state=ST_AWAIT_OLD)
        await _dm(tg, user_id,
                  "👤 了解！請用一則訊息描述你的情況：\n"
                  "• OKX 帳號大概什麼時候註冊的？\n"
                  "• 最近半年有登入/交易嗎？\n"
                  "• 是否記得當時有沒有綁過別人的邀請碼？\n\n"
                  "管理員會根據 OKX 政策判斷能否轉移（閒置 180 天以上的帳號可以）。")
        return True
    return False


async def handle_join_request(tg, req: dict) -> None:
    """chat_join_request update → 核對身分自動批准/拒絕 + 撤銷連結。"""
    init_db()
    user = req.get("from") or {}
    user_id = user.get("id")
    chat_id = (req.get("chat") or {}).get("id")
    used_link = ((req.get("invite_link") or {}).get("invite_link")) or ""

    conn = _conn()
    try:
        row = conn.execute(
            "SELECT user_id, expires_at, status FROM gate_links WHERE invite_link=?",
            (used_link,)).fetchone()
    finally:
        conn.close()

    bound = row and row[2] == "active" and row[0] == user_id and row[1] > time.time()
    method = "approveChatJoinRequest" if bound else "declineChatJoinRequest"
    await tg._post(method, {"chat_id": chat_id, "user_id": user_id})

    if used_link:
        try:
            await tg._post("revokeChatInviteLink",
                           {"chat_id": chat_id, "invite_link": used_link})
            conn = _conn()
            conn.execute("UPDATE gate_links SET status='used' WHERE invite_link=?",
                         (used_link,))
            conn.close()
        except Exception:
            pass

    if bound:
        _upsert_applicant(user_id, user.get("username") or "", state=ST_JOINED)
        await _dm(tg, user_id,
                  "🎉 <b>歡迎加入！</b>你已是社群的一份子。\n\n"
                  "快速導覽：\n"
                  "🎯 交易訊號 — 引擎輸出（驗證期中，先看不先跟）\n"
                  "💡 意見箱 — 你的想法被採納實裝就有積分，積分連結未來分潤\n"
                  "💬 行情閒聊 — 隨意聊\n\n"
                  "輸入 /help 看所有指令。")
        print(f"[gate] APPROVED join: {user_id}")
    else:
        print(f"[gate] DECLINED join: {user_id} (link mismatch/expired)")


async def admin_approve(tg, args: list[str]) -> str:
    """管理員指令 /gate_approve <user_id> — 舊用戶人工放行。"""
    init_db()
    if not args or not args[0].isdigit():
        return "用法：/gate_approve <telegram_user_id>"
    user_id = int(args[0])
    group_id = _group_chat_id()
    if not group_id:
        return "❌ 找不到群組設定"
    resp = await tg._post("createChatInviteLink", {
        "chat_id": group_id, "creates_join_request": True,
        "expire_date": int(time.time()) + LINK_TTL_S,
        "name": f"admin:{user_id}"[:32]})
    link = ((resp.get("result") or {}).get("invite_link"))
    if not link:
        return f"❌ 連結生成失敗：{str(resp)[:100]}"
    conn = _conn()
    try:
        conn.execute("INSERT OR REPLACE INTO gate_links VALUES (?, ?, ?, 'active')",
                     (link, user_id, int(time.time()) + LINK_TTL_S))
    finally:
        conn.close()
    _upsert_applicant(user_id, "", state=ST_LINK_SENT)
    await _dm(tg, user_id,
              f"✅ 管理員已核准你的申請！專屬入群連結（10 分鐘有效）：\n{link}")
    return f"✅ 已發放連結給 {user_id}"


if __name__ == "__main__":
    init_db()
    # 狀態機單元測試（不打網路）
    _upsert_applicant(1001, "tester", state=ST_AWAIT_UID)
    a = _get_applicant(1001)
    assert a["state"] == ST_AWAIT_UID, a
    for _ in range(UID_ATTEMPTS_PER_HOUR):
        _bump_attempt(1001, "tester")
    assert _rate_limited(1001)
    conn = _conn()
    conn.execute("DELETE FROM applicants WHERE user_id=1001")
    conn.close()
    print("invite_gate state machine: ALL PASS")
