"""SQLite 記錄已抓過的新聞 ID（dedupe）+ 推送 log。"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from botpaths import db_path as _db_path

DB_PATH = _db_path("news_feed.db")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    conn = _conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,         -- 'twitter' / 'truth_social'
                handle TEXT NOT NULL,         -- 'realDonaldTrump' / 'binance'
                post_id TEXT NOT NULL,        -- 平台原 post id
                content_hash TEXT,            -- content sha256（防 id 不穩定）
                seen_at INTEGER NOT NULL,
                pushed INTEGER NOT NULL DEFAULT 0,  -- 0=過濾掉、1=已推 TG
                push_reason TEXT,
                content_preview TEXT,
                UNIQUE(source, handle, post_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_seen_at ON seen_posts(seen_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_handle ON seen_posts(handle)")
    finally:
        conn.close()


def already_seen(source: str, handle: str, post_id: str) -> bool:
    init_db()
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM seen_posts WHERE source=? AND handle=? AND post_id=? LIMIT 1",
            (source, handle, post_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def mark_seen(source: str, handle: str, post_id: str,
              content_preview: str = "",
              pushed: bool = False, push_reason: str = "") -> int:
    """記錄一筆 post。若已存在則 UPDATE pushed/push_reason。回 row id。"""
    init_db()
    conn = _conn()
    try:
        now = int(time.time())
        try:
            cur = conn.execute(
                """INSERT INTO seen_posts
                   (source, handle, post_id, seen_at, pushed, push_reason, content_preview)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (source, handle, post_id, now,
                 1 if pushed else 0, push_reason, content_preview[:500]),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            # 已存在 → update
            conn.execute(
                """UPDATE seen_posts SET pushed=?, push_reason=?
                   WHERE source=? AND handle=? AND post_id=?""",
                (1 if pushed else 0, push_reason, source, handle, post_id),
            )
            row = conn.execute(
                "SELECT id FROM seen_posts WHERE source=? AND handle=? AND post_id=?",
                (source, handle, post_id),
            ).fetchone()
            return row[0]
    finally:
        conn.close()


def get_recent_pushed(hours: int = 24) -> list[dict]:
    """近期推過的訊息（給統計/debug 用）"""
    init_db()
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT source, handle, post_id, seen_at, push_reason, content_preview
               FROM seen_posts
               WHERE pushed=1 AND seen_at >= ?
               ORDER BY seen_at DESC""",
            (int(time.time()) - hours * 3600,),
        ).fetchall()
        return [
            {"source": r[0], "handle": r[1], "post_id": r[2],
             "seen_at": r[3], "push_reason": r[4], "content_preview": r[5]}
            for r in rows
        ]
    finally:
        conn.close()


def get_stats(hours: int = 24) -> dict:
    """過去 N 小時推送 vs 過濾統計"""
    init_db()
    conn = _conn()
    try:
        since = int(time.time()) - hours * 3600
        total = conn.execute(
            "SELECT COUNT(*) FROM seen_posts WHERE seen_at >= ?", (since,)
        ).fetchone()[0]
        pushed = conn.execute(
            "SELECT COUNT(*) FROM seen_posts WHERE seen_at >= ? AND pushed=1", (since,)
        ).fetchone()[0]
        by_handle = conn.execute(
            """SELECT handle, COUNT(*), SUM(pushed)
               FROM seen_posts WHERE seen_at >= ?
               GROUP BY handle ORDER BY COUNT(*) DESC""",
            (since,),
        ).fetchall()
        return {
            "window_hours": hours,
            "total_posts": total,
            "pushed_to_tg": pushed,
            "filter_rate_pct": round((1 - pushed / total) * 100, 1) if total else 0,
            "by_handle": [
                {"handle": h, "seen": s, "pushed": p or 0}
                for h, s, p in by_handle
            ],
        }
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"news_feed.db at {DB_PATH}")
    s = get_stats(24)
    print(f"24h: {s['total_posts']} seen, {s['pushed_to_tg']} pushed "
          f"({s['filter_rate_pct']}% filtered)")
