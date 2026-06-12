"""社群貢獻積分系統（v19.1）— 採納制積分：被實裝才有分。

設計（使用者規格 2026-06-12 修訂）：
    1. 人人可提建議 → 全部入庫留痕 + AI 評估（價值/可行性）
    2. 高潛力建議（AI 評分 ≥7）自動通知管理者審核
    3. 積分只在「採納並完成更新」後給（/adopt 指令），更新公告註明出自誰
    4. 積分累積制永不清零 — 50% 分潤的客觀依據
    5. 發言頻率限制只防洪水（每人每小時回覆評估 1 次），與積分無關
"""
from __future__ import annotations

import sqlite3
import time

from botpaths import db_path as _db_path

DB_PATH = _db_path("community.db")

MIN_LEN_FOR_EVAL = 10         # AI 評估門檻：至少 10 字（太短只留痕）
EVAL_COOLDOWN_S = 3600        # 每人每小時 AI 評估 1 則（防灌水洗 LLM）
ADOPT_DEFAULT_POINTS = 5      # 採納基礎分（可用 /adopt <id> <分數> 覆寫 1-10）


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    conn = _conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                text TEXT NOT NULL,
                ts INTEGER NOT NULL,
                counted INTEGER NOT NULL DEFAULT 0,   -- (v19.1 棄用，保留相容)
                adopted INTEGER NOT NULL DEFAULT 0,   -- 1=已採納實裝
                msg_id INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scores (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                points INTEGER NOT NULL DEFAULT 0,
                adopted_count INTEGER NOT NULL DEFAULT 0,
                last_counted_ts INTEGER NOT NULL DEFAULT 0,
                first_seen INTEGER NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sugg_user ON suggestions(user_id, ts)")
        # v19.1 idempotent migration：AI 評估與採納註記欄位
        existing = {r[1] for r in conn.execute("PRAGMA table_info(suggestions)").fetchall()}
        for col, ddl in (("ai_score", "ALTER TABLE suggestions ADD COLUMN ai_score INTEGER"),
                         ("ai_comment", "ALTER TABLE suggestions ADD COLUMN ai_comment TEXT"),
                         ("adopted_note", "ALTER TABLE suggestions ADD COLUMN adopted_note TEXT")):
            if col not in existing:
                conn.execute(ddl)
    finally:
        conn.close()


def record_suggestion(user_id: int, username: str, text: str,
                      msg_id: int | None = None) -> dict:
    """記錄一則建議（不給分 — 積分只在採納實裝後給）。
    回 {id, eligible_for_eval, reason}。"""
    init_db()
    now = int(time.time())
    text = (text or "").strip()
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT last_counted_ts FROM scores WHERE user_id=?", (user_id,),
        ).fetchone()
        last_ts = row[0] if row else 0

        eligible = True
        reason = ""
        if len(text) < MIN_LEN_FOR_EVAL:
            eligible, reason = False, "too_short"
        elif now - last_ts < EVAL_COOLDOWN_S:
            eligible, reason = False, "cooldown"

        cur = conn.execute(
            "INSERT INTO suggestions (user_id, username, text, ts, counted, msg_id) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (user_id, username, text[:2000], now, msg_id),
        )
        sid = cur.lastrowid
        if row:
            conn.execute(
                "UPDATE scores SET username=?, last_counted_ts=? WHERE user_id=?",
                (username, now if eligible else last_ts, user_id),
            )
        else:
            conn.execute(
                "INSERT INTO scores (user_id, username, points, last_counted_ts, first_seen) "
                "VALUES (?, ?, 0, ?, ?)",
                (user_id, username, now if eligible else 0, now),
            )
        return {"id": sid, "eligible_for_eval": eligible, "reason": reason}
    finally:
        conn.close()


async def ai_evaluate_suggestion(sid: int) -> dict | None:
    """AI 審核層：評估建議的價值與可行性，入庫。回評估結果或 None。"""
    init_db()
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT text, username FROM suggestions WHERE id=?", (sid,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    text, username = row

    from news_feed.llm_filter import _call_claude, _extract_json
    prompt = (
        "你是開源交易機器人的技術評審。評估這則社群建議對系統的真實價值。\n"
        "系統現況：加密+美股永續訊號引擎、全市場掃描、經濟數據、消息面 AI、"
        "紙上驗證帳、Telegram 9 頻道。\n"
        "評分準則：value（對交易期望值/使用體驗的真實提升，1-10）、"
        "feasibility（以現有架構實作可行性，1-10）。"
        "空泛口號（『讓 AI 更聰明』）給低分；具體可做的（『加入 XX 數據判斷 YY』）給高分。\n"
        "只輸出 JSON：{\"value\": N, \"feasibility\": N, \"summary\": \"一句話繁中摘要\", "
        "\"verdict\": \"採納候選|需要更多細節|不建議\"}\n\n"
        f"建議內容：{text[:1500]}"
    )
    raw = await _call_claude(prompt, timeout_sec=60)
    obj = _extract_json(raw) if raw else None
    if not obj:
        return None
    try:
        score = max(1, min(10, int(obj.get("value") or 1)))
    except (TypeError, ValueError):
        score = 1
    comment = (f"{obj.get('verdict', '?')}｜{str(obj.get('summary', ''))[:200]}"
               f"｜可行性 {obj.get('feasibility', '?')}/10")
    conn = _conn()
    try:
        conn.execute("UPDATE suggestions SET ai_score=?, ai_comment=? WHERE id=?",
                     (score, comment, sid))
    finally:
        conn.close()
    return {"id": sid, "username": username, "value": score, "comment": comment,
            "verdict": obj.get("verdict", "")}


def mark_adopted(suggestion_id: int, points: int = ADOPT_DEFAULT_POINTS,
                 note: str = "") -> dict:
    """採納實裝 → 給分（唯一的積分來源）。note = 實裝版本/功能說明。"""
    init_db()
    points = max(1, min(10, points))
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT user_id, username FROM suggestions WHERE id=? AND adopted=0",
            (suggestion_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "msg": "建議不存在或已採納"}
        conn.execute(
            "UPDATE suggestions SET adopted=1, adopted_note=? WHERE id=?",
            (note[:200], suggestion_id))
        conn.execute(
            "UPDATE scores SET points=points+?, adopted_count=adopted_count+1 "
            "WHERE user_id=?", (points, row[0]),
        )
        new_pts = conn.execute(
            "SELECT points FROM scores WHERE user_id=?", (row[0],)).fetchone()[0]
        return {"ok": True, "user_id": row[0], "username": row[1],
                "points_awarded": points, "total_points": new_pts}
    finally:
        conn.close()


def get_pending_review(min_score: int = 7) -> list[dict]:
    """待審核的高潛力建議（給管理者）"""
    init_db()
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, username, text, ai_score, ai_comment FROM suggestions "
            "WHERE adopted=0 AND ai_score >= ? ORDER BY ai_score DESC, ts DESC LIMIT 20",
            (min_score,),
        ).fetchall()
        return [{"id": r[0], "username": r[1], "text": r[2][:150],
                 "ai_score": r[3], "ai_comment": r[4]} for r in rows]
    finally:
        conn.close()


def get_leaderboard(n: int = 10) -> list[dict]:
    init_db()
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT username, points, adopted_count FROM scores "
            "ORDER BY points DESC, adopted_count DESC LIMIT ?", (n,),
        ).fetchall()
        return [{"username": r[0], "points": r[1], "adopted": r[2]} for r in rows]
    finally:
        conn.close()


def get_user_score(user_id: int) -> dict | None:
    init_db()
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT username, points, adopted_count FROM scores WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        total = conn.execute("SELECT COALESCE(SUM(points),0) FROM scores").fetchone()[0]
        share = row[1] / total * 100 if total else 0
        return {"username": row[0], "points": row[1], "adopted": row[2],
                "share_pct": round(share, 1)}
    finally:
        conn.close()


def render_leaderboard() -> str:
    import html as _html
    lb = get_leaderboard(10)
    if not lb:
        return "💡 <b>貢獻排行榜</b>\n（還沒有人提供建議 — 在意見箱發言就會開始累積積分）"
    conn = _conn()
    total = conn.execute("SELECT COALESCE(SUM(points),0) FROM scores").fetchone()[0]
    conn.close()
    lines = ["💡 <b>貢獻排行榜</b>（累積制，永不清零）", "━━━━━━━━━━━━━━━━"]
    medals = ["🥇", "🥈", "🥉"] + ["▫️"] * 7
    for i, u in enumerate(lb):
        share = u["points"] / total * 100 if total else 0
        adopted = f"｜採納 {u['adopted']}" if u["adopted"] else ""
        lines.append(f"{medals[i]} {_html.escape(u['username'] or '匿名')}　"
                     f"<code>{u['points']}</code> 分（{share:.1f}%）{adopted}")
    lines.append("\n<i>積分規則（採納制）：建議經 AI 評估 + 管理者審核，"
                 "<b>採納並完成更新後</b>才給分（基礎 5 分，依貢獻度 1-10），"
                 "更新公告會註明出自誰。累積制永不清零，50% 分潤依積分占比。</i>")
    return "\n".join(lines)


if __name__ == "__main__":
    init_db()
    # v19.1 採納制測試
    r1 = record_suggestion(1001, "test_user", "建議加入 ETH 的 gas 費監控當作鏈上活躍度指標")
    assert r1["eligible_for_eval"] and r1["id"], r1
    r2 = record_suggestion(1001, "test_user", "一小時內第二則：不再送 AI 評估但留痕")
    assert not r2["eligible_for_eval"] and r2["reason"] == "cooldown", r2
    r3 = record_suggestion(1002, "user2", "短")
    assert not r3["eligible_for_eval"] and r3["reason"] == "too_short", r3
    # 發言不給分
    s = get_user_score(1001)
    assert s["points"] == 0, s
    # 採納才給分
    a = mark_adopted(r1["id"], points=7, note="v20 鏈上活躍度指標")
    assert a["ok"] and a["points_awarded"] == 7 and a["total_points"] == 7, a
    print(f"adopted: {a['username']} +{a['points_awarded']} -> {a['total_points']}")
    print(render_leaderboard().replace("<code>", "").replace("</code>", "")[:200])
    conn = _conn()
    conn.execute("DELETE FROM suggestions WHERE user_id IN (1001,1002)")
    conn.execute("DELETE FROM scores WHERE user_id IN (1001,1002)")
    conn.close()
    print("ALL PASS")
