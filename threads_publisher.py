"""Threads 自動發布管線（v20）— 建造日誌自動連載的地基。

設計：
    1. Token 管理：.env THREADS_ACCESS_TOKEN 為 bootstrap，續期後的新 token
       存 threads.db（token 永不出現在聊天室/日誌 — 只顯示遮罩尾碼）
    2. 60 天長效 token 自動續期：距到期 <14 天且 token 齡 >24h 時
       GET /refresh_access_token（Threads 續期不需 app secret）
    3. 發文佇列：posts_queue 表（pending→posted/failed），預設每日上限 1 篇
       — 新帳號穩健起步，避免觸發 Meta 防濫用
    4. 兩段式發布：POST /{uid}/threads（建立容器）→ POST /{uid}/threads_publish
    5. worker：run_threads_publisher_loop() 每 30 分鐘檢查佇列 + token 健康
       未設定 token 時優雅 no-op（每小時靜默重查一次 env）

CLI（手動操作）：
    python threads_publisher.py status            # token 狀態 + 佇列摘要
    python threads_publisher.py whoami            # 驗證 token（顯示帳號名）
    python threads_publisher.py queue "文字"      # 排入佇列（依每日上限自動排程）
    python threads_publisher.py post-now "文字"   # 立即發布（仍受 500 字限制）
    python threads_publisher.py refresh           # 手動強制續期
"""
from __future__ import annotations

import asyncio
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

_NOOP_LOGGED = False


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
    if row:
        tok = {"access_token": row[0], "obtained_ts": row[1], "expires_at": row[2],
               "user_id": row[3], "username": row[4]}
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

async def publish_text(text: str) -> dict:
    """兩段式發布。回 {ok, post_id?|error}。"""
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
    # 1) 建立 media container
    create = await _api_post(f"{API_BASE}/{uid}/threads", {
        "media_type": "TEXT", "text": text,
        "access_token": tok["access_token"],
    })
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


async def process_queue_once() -> dict | None:
    """取一則到期的 pending 發布（尊重每日上限）。回發布結果或 None=本輪無事。"""
    init_db()
    if _posted_in_last_24h() >= POSTS_PER_DAY:
        return None
    now = int(time.time())
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT id, text FROM posts_queue WHERE status='pending' "
            "AND scheduled_for <= ? ORDER BY id LIMIT 1", (now,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    qid, text = row
    result = await publish_text(text)
    conn = _conn()
    try:
        if result["ok"]:
            conn.execute(
                "UPDATE posts_queue SET status='posted', posted_ts=?, threads_post_id=? "
                "WHERE id=?", (now, result["post_id"], qid))
        else:
            conn.execute(
                "UPDATE posts_queue SET status='failed', error=? WHERE id=?",
                (result["error"][:300], qid))
    finally:
        conn.close()
    result["queue_id"] = qid
    result["text_head"] = text[:60]
    return result


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
    """每 30 分鐘：token 健康檢查 + 佇列發布。無 token 時優雅 no-op。"""
    global _NOOP_LOGGED
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

            result = await process_queue_once()
            if result is not None and tg_sys is not None:
                try:
                    if result["ok"]:
                        await tg_sys.send_message(
                            f"🧵 <b>Threads 已發布</b>（佇列 #{result['queue_id']}）\n"
                            f"<i>{result['text_head']}…</i>",
                            parse_mode="HTML")
                    else:
                        await tg_sys.send_message(
                            f"⚠️ <b>Threads 發布失敗</b>（佇列 #{result['queue_id']}）\n"
                            f"<code>{result['error'][:200]}</code>",
                            parse_mode="HTML")
                except Exception:
                    pass
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
    if cmd == "post-now" and len(args) > 1:
        r = asyncio.run(publish_text(args[1]))
        print(f"post: {r}")
        return 0 if r["ok"] else 1
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(_cli())
