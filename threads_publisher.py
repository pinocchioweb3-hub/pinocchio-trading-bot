"""Threads 發布管線（v20；v47 紅線 2 程式層硬擋）— 建造日誌的地基。

⛔ 紅線 2：AI／背景 daemon **永不自動發文**，發布永遠由人類當下逐則送出。
   本版把這條線寫死在程式層（不再只靠『沒 token＋沒人寫佇列』的運行態空值）：
     ① 背景 worker（process_queue_once）物理上不送出，只偵測待送草稿並通知人類。
     ② 真正送出（publish_text）必須 human_authorized=True（人類當下逐則授權）。
     ③ 此閘 per-session、不被既往 /approve 覆蓋。詳見 WORKER_AUTOPUBLISH_HARD_BLOCK。

設計：
    1. Token 管理：.env THREADS_ACCESS_TOKEN 為 bootstrap，續期後的新 token
       存 threads.db（token 永不出現在聊天室/日誌 — 只顯示遮罩尾碼）；洩漏 token 黑名單防呆。
    2. 60 天長效 token 自動續期：距到期 <14 天且 token 齡 >24h 時
       GET /refresh_access_token（Threads 續期不需 app secret）
    3. 發文佇列：posts_queue 表（pending→posted/failed），預設每日上限 1 篇
    4. 兩段式發布：POST /{uid}/threads（建立容器）→ POST /{uid}/threads_publish
    5. worker：run_threads_publisher_loop() 每 30 分鐘做 token 健康 + 待送草稿通知
       （永不自動發文）；未設定 token 時優雅 no-op（每小時靜默重查一次 env）

CLI（手動操作）：
    python threads_publisher.py status              # token 狀態 + 佇列摘要
    python threads_publisher.py whoami              # 驗證 token（顯示帳號名）
    python threads_publisher.py queue "文字"        # 排入佇列（不會自動送出，待人手發）
    python threads_publisher.py post-now "文字" --yes  # 人類親自立即發布（紅線 2：須 --yes）
    python threads_publisher.py refresh             # 手動強制續期
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from botpaths import db_path as _db_path

DB_PATH = _db_path("threads.db")
API_BASE = "https://graph.threads.net/v1.0"
REFRESH_URL = "https://graph.threads.net/refresh_access_token"

POST_MAX_LEN = 500            # Threads 單篇上限
POSTS_PER_DAY = 1             # 初期每日 1 篇（穩健起步）
REFRESH_BEFORE_DAYS = 14      # 距到期 <14 天就續期
TOKEN_MIN_AGE_S = 86400       # Meta 規定 token 滿 24h 才能續期
TOKEN_TTL_S = 60 * 86400      # 長效 token 60 天

# ⛔⛔ 紅線 2 程式層硬擋（v47）⛔⛔ ─────────────────────────────────────────
# 永久紅線 2：「AI／背景 daemon 永不自動發文，發布永遠由人類當下逐則送出。」
# 過去這條線只靠『沒填 token＋沒人寫佇列』這種運行態空值維持（稽核 #1 HIGH）：
# 一旦有人填了 token、且任何程式或 CLI 把內容寫進 posts_queue，每 30 分的 worker
# 就會自動發文 —— 與承諾不符。本版把它寫死在程式層，雙層防護：
#   ① 背景 worker（process_queue_once）物理上不送出，只做 token 健康 + 待送偵測/通知。
#   ② 真正送出（publish_text）必須由「人類當下、逐則」明確授權（human_authorized=True）；
#      背景 worker 路徑永不傳此旗標，故即使有 token＋佇列有料，worker 也送不出去。
#   ③ 此閘 per-session、不被既往 /approve 覆蓋（已核准＝內容過關，≠ 自動送出許可）。
# 對應記憶：trading-bot-threads-operating-model。
WORKER_AUTOPUBLISH_HARD_BLOCK = True

# 洩漏 token 黑名單（防呆）：曾外洩作廢的 THREADS_ACCESS_TOKEN 一律拒用。
# 為免把秘鑰寫進原始碼／日誌，這裡只存 SHA-256 指紋（小寫 hex），不存 token 本體。
# 使用者若要把舊洩漏 token 列黑：在 Meta 後台重新產生新 token（舊的即在伺服器端失效），
# 並可選擇性把舊 token 的 sha256 hex 加進下方集合做本地雙保險。
_BLACKLISTED_TOKEN_SHA256: set[str] = set()

_NOOP_LOGGED = False
_DRAFT_READY_NOTIFIED = -1   # 上次已通知的「待送草稿數」（避免每 30 分重複洗版）


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    conn = _conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS token_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                access_token TEXT NOT NULL,
                obtained_ts INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                user_id TEXT,
                username TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS posts_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',  -- pending|posted|failed
                created_ts INTEGER NOT NULL,
                scheduled_for INTEGER NOT NULL DEFAULT 0,
                posted_ts INTEGER,
                threads_post_id TEXT,
                error TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_status "
                     "ON posts_queue(status, scheduled_for)")
    finally:
        conn.close()


def _mask(token: str) -> str:
    return f"...{token[-4:]}" if token and len(token) > 8 else "(無)"


def _token_blacklisted(token: str) -> bool:
    """token 是否在洩漏黑名單（比對 SHA-256 指紋，永不回顯 token 本體）。"""
    if not token or not _BLACKLISTED_TOKEN_SHA256:
        return False
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return digest in _BLACKLISTED_TOKEN_SHA256


# ── Token 管理 ──────────────────────────────────────────────────────────

def load_token() -> dict | None:
    """優先 DB（續期後最新），fallback .env bootstrap。回 None = 未設定。"""
    init_db()
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT access_token, obtained_ts, expires_at, user_id, username "
            "FROM token_state WHERE id=1").fetchone()
    finally:
        conn.close()
    env_tok = (os.environ.get("THREADS_ACCESS_TOKEN") or "").strip()
    # 防呆：.env 若塞了已洩漏作廢的 token，直接拒用（不 seed、不回顯）
    if env_tok and _token_blacklisted(env_tok):
        print("[threads] ⛔ .env 的 THREADS_ACCESS_TOKEN 命中洩漏黑名單，拒用 —— "
              "請到 Meta 後台重新產生新 token")
        env_tok = ""
    if row:
        tok = {"access_token": row[0], "obtained_ts": row[1], "expires_at": row[2],
               "user_id": row[3], "username": row[4]}
        # DB 內若是已洩漏 token（理論上不該發生）→ 拒用
        if _token_blacklisted(tok["access_token"]):
            print("[threads] ⛔ DB 內 token 命中洩漏黑名單，拒用")
            return None
        # .env 換了新 token（手動重新產生）→ 以 .env 為準重新 seed
        if env_tok and env_tok != tok["access_token"]:
            return _seed_token(env_tok)
        return tok
    if env_tok:
        return _seed_token(env_tok)
    return None


def _seed_token(token: str) -> dict:
    now = int(time.time())
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO token_state (id, access_token, obtained_ts, expires_at) "
            "VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET access_token=excluded.access_token, "
            "obtained_ts=excluded.obtained_ts, expires_at=excluded.expires_at, "
            "user_id=NULL, username=NULL",
            (token, now, now + TOKEN_TTL_S))
    finally:
        conn.close()
    print(f"[threads] token seeded from .env ({_mask(token)})")
    return {"access_token": token, "obtained_ts": now,
            "expires_at": now + TOKEN_TTL_S, "user_id": None, "username": None}


def _save_token(token: str, expires_in: int) -> None:
    now = int(time.time())
    conn = _conn()
    try:
        conn.execute(
            "UPDATE token_state SET access_token=?, obtained_ts=?, expires_at=? WHERE id=1",
            (token, now, now + int(expires_in)))
    finally:
        conn.close()


async def _api_get(url: str, params: dict) -> dict:
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.get(url, params=params,
                         timeout=aiohttp.ClientTimeout(total=30)) as r:
            return await r.json(content_type=None)


async def _api_post(url: str, params: dict) -> dict:
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.post(url, params=params,
                          timeout=aiohttp.ClientTimeout(total=30)) as r:
            return await r.json(content_type=None)


async def whoami() -> dict | None:
    """驗證 token + 補 user_id/username 到 DB。"""
    tok = load_token()
    if not tok:
        return None
    resp = await _api_get(f"{API_BASE}/me",
                          {"fields": "id,username", "access_token": tok["access_token"]})
    if "id" in resp:
        conn = _conn()
        try:
            conn.execute("UPDATE token_state SET user_id=?, username=? WHERE id=1",
                         (resp["id"], resp.get("username")))
        finally:
            conn.close()
        return resp
    print(f"[threads] whoami failed: {str(resp)[:200]}")
    return None


async def refresh_token_if_needed(force: bool = False) -> str:
    """回 'ok'|'refreshed'|'skipped_young'|'failed'|'no_token'。"""
    tok = load_token()
    if not tok:
        return "no_token"
    now = int(time.time())
    age = now - tok["obtained_ts"]
    remaining = tok["expires_at"] - now
    if not force and remaining > REFRESH_BEFORE_DAYS * 86400:
        return "ok"
    if age < TOKEN_MIN_AGE_S:
        return "skipped_young"   # Meta：滿 24h 才能續期
    resp = await _api_get(REFRESH_URL, {
        "grant_type": "th_refresh_token",
        "access_token": tok["access_token"],
    })
    if "access_token" in resp:
        _save_token(resp["access_token"], resp.get("expires_in", TOKEN_TTL_S))
        print(f"[threads] token refreshed ({_mask(resp['access_token'])}), "
              f"expires_in={resp.get('expires_in')}")
        return "refreshed"
    print(f"[threads] refresh failed: {str(resp)[:200]}")
    return "failed"


# ── 發布 ────────────────────────────────────────────────────────────────

async def publish_text(text: str, reply_to_id: str | None = None,
                       *, human_authorized: bool = False) -> dict:
    """兩段式發布。reply_to_id 非空時發成「回覆」（用於串文鏈接）。回 {ok, post_id?|error}。

    ⛔ 紅線 2 程式層硬擋：必須 ``human_authorized=True`` 才會真的送出 —— 代表「人類當下、
    逐則」明確授權（CLI post-now --yes，或未來的 /send 由人在 session 中下達）。
    背景 worker（process_queue_once）永不傳此旗標，故物理上無法自動發文。
    """
    if not human_authorized:
        return {"ok": False,
                "error": "blocked_redline2: 需人類當下逐則授權（human_authorized=True）"
                         "才可發文；背景自動發文已於程式層擋死"}
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty"}
    if len(text) > POST_MAX_LEN:
        return {"ok": False, "error": f"too_long ({len(text)}>{POST_MAX_LEN})"}
    tok = load_token()
    if not tok:
        return {"ok": False, "error": "no_token"}
    uid = tok.get("user_id")
    if not uid:
        me = await whoami()
        if not me:
            return {"ok": False, "error": "token_invalid (whoami failed)"}
        uid = me["id"]
    # 1) 建立 media container（reply_to_id → 串文鏈接）
    _params = {"media_type": "TEXT", "text": text,
               "access_token": tok["access_token"]}
    if reply_to_id:
        _params["reply_to_id"] = reply_to_id
    create = await _api_post(f"{API_BASE}/{uid}/threads", _params)
    cid = create.get("id")
    if not cid:
        return {"ok": False, "error": f"create: {str(create)[:200]}"}
    await asyncio.sleep(2)   # 官方建議：container 建立後稍候再 publish
    # 2) 正式發布
    pub = await _api_post(f"{API_BASE}/{uid}/threads_publish", {
        "creation_id": cid, "access_token": tok["access_token"],
    })
    pid = pub.get("id")
    if not pid:
        return {"ok": False, "error": f"publish: {str(pub)[:200]}"}
    return {"ok": True, "post_id": pid}


# ── 佇列 ────────────────────────────────────────────────────────────────

def _posted_in_last_24h() -> int:
    conn = _conn()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM posts_queue WHERE status='posted' AND posted_ts > ?",
            (int(time.time()) - 86400,)).fetchone()[0]
    finally:
        conn.close()


def enqueue_post(text: str, scheduled_for: int = 0) -> dict:
    """排入佇列。超過 500 字直接拒絕（不靜默截斷 — 內容是門面）。"""
    init_db()
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty"}
    if len(text) > POST_MAX_LEN:
        return {"ok": False, "error": f"too_long ({len(text)}>{POST_MAX_LEN})"}
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO posts_queue (text, created_ts, scheduled_for) VALUES (?, ?, ?)",
            (text, int(time.time()), scheduled_for))
        return {"ok": True, "id": cur.lastrowid}
    finally:
        conn.close()


def _count_pending_ready() -> int:
    """到期、待送的 pending 草稿數（供 worker 通知人類，不送出）。"""
    init_db()
    now = int(time.time())
    conn = _conn()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM posts_queue WHERE status='pending' "
            "AND scheduled_for <= ?", (now,)).fetchone()[0]
    finally:
        conn.close()


async def process_queue_once() -> dict | None:
    """⛔ 紅線 2 程式層硬擋：背景 worker 永不自動發文。

    本函式**不送出任何內容**，只偵測「有無到期、待送的草稿」並回報，讓 worker 能
    通知人類去手動送出。真正送出永遠要人類當下逐則授權（見 publish_text /
    CLI post-now --yes / 未來 /send）。
    回 None＝本輪無待送；回 dict（blocked=True, pending_ready=N）＝有 N 則待人手送出。
    """
    if not WORKER_AUTOPUBLISH_HARD_BLOCK:
        # 安全旗標被關掉是嚴重事故 —— 仍拒絕送出並出聲，絕不悄悄自動發文。
        print("[threads] ⚠️ WORKER_AUTOPUBLISH_HARD_BLOCK 被關閉，"
              "仍依紅線 2 拒絕背景自動發文")
    ready = _count_pending_ready()
    if ready <= 0:
        return None
    return {"ok": False, "blocked": True, "pending_ready": ready,
            "error": "redline2_hard_block: 草稿待人手送出，背景永不自動發文"}


def queue_summary() -> dict:
    init_db()
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM posts_queue GROUP BY status").fetchall()
    finally:
        conn.close()
    return {r[0]: r[1] for r in rows}


# ── Worker（接入 run_bot.py supervise）────────────────────────────────────

async def run_threads_publisher_loop(tg_sys=None, interval_s: int = 1800) -> None:
    """每 30 分鐘：token 健康檢查 + 待送草稿偵測通知。

    ⛔ 紅線 2：本 worker **永不自動發文**（process_queue_once 已程式層擋死），
    只在有草稿待送時提醒人類去手動送出。無 token 時優雅 no-op。
    """
    global _NOOP_LOGGED, _DRAFT_READY_NOTIFIED
    while True:
        try:
            tok = load_token()
            if not tok:
                if not _NOOP_LOGGED:
                    print("[threads] THREADS_ACCESS_TOKEN 未設定 — worker 待命中")
                    _NOOP_LOGGED = True
                await asyncio.sleep(3600)
                continue
            _NOOP_LOGGED = False

            status = await refresh_token_if_needed()
            if status == "failed" and tg_sys is not None:
                try:
                    await tg_sys.send_message(
                        "⚠️ <b>Threads token 續期失敗</b>\n"
                        "請到 Meta 開發者後台重新產生長效 token 並更新 .env 的 "
                        "<code>THREADS_ACCESS_TOKEN</code>，daemon 會自動接手。",
                        parse_mode="HTML")
                except Exception:
                    pass

            # ⛔ 紅線 2：偵測待送草稿 → 只通知人類，永不自動發文
            result = await process_queue_once()
            ready = result.get("pending_ready", 0) if result else 0
            if ready != _DRAFT_READY_NOTIFIED:
                if ready > 0 and tg_sys is not None:
                    try:
                        await tg_sys.send_message(
                            f"🧵 <b>有 {ready} 則 Threads 草稿待送</b>\n"
                            "依紅線 2，我<b>不會自動發文</b> —— 請你手動複製貼到 Threads，"
                            "或用 <code>python threads_publisher.py post-now \"文字\" --yes</code> "
                            "親自送出。",
                            parse_mode="HTML")
                    except Exception:
                        pass
                _DRAFT_READY_NOTIFIED = ready
        except Exception as e:
            print(f"[threads] loop error: {type(e).__name__}: {str(e)[:200]}")
        await asyncio.sleep(interval_s)


# ── CLI ─────────────────────────────────────────────────────────────────

def _cli() -> int:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    args = sys.argv[1:]
    cmd = args[0] if args else "status"

    if cmd == "status":
        tok = load_token()
        if not tok:
            print("token: 未設定（.env THREADS_ACCESS_TOKEN）")
        else:
            days = (tok["expires_at"] - time.time()) / 86400
            print(f"token: {_mask(tok['access_token'])}  "
                  f"剩 {days:.1f} 天  user={tok.get('username') or '?'}")
        print(f"queue: {queue_summary() or '(空)'}")
        print(f"24h 已發: {_posted_in_last_24h()}/{POSTS_PER_DAY}")
        return 0
    if cmd == "whoami":
        me = asyncio.run(whoami())
        print(f"whoami: {me or 'FAILED'}")
        return 0 if me else 1
    if cmd == "refresh":
        print(f"refresh: {asyncio.run(refresh_token_if_needed(force=True))}")
        return 0
    if cmd == "queue" and len(args) > 1:
        r = enqueue_post(args[1])
        print(f"enqueue: {r}")
        return 0 if r["ok"] else 1
    if cmd == "post-now":
        text_args = [a for a in args[1:] if a != "--yes"]
        if not text_args:
            print(__doc__)
            return 1
        if "--yes" not in args:
            print("⛔ 紅線 2：post-now 會真的發到 Threads，需你（人類）當下明確確認。")
            print('   請改用： python threads_publisher.py post-now "文字" --yes')
            return 1
        r = asyncio.run(publish_text(text_args[0], human_authorized=True))
        print(f"post: {r}")
        return 0 if r["ok"] else 1
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(_cli())
