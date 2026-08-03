"""SQLite-backed FIRE queue + persistent cooldown。

兩張表：
    fires       訊號事件（含 decision 完整 JSON）
    cooldown    每 (symbol, setup, direction) 的最近 FIRE 時間

跨進程安全（SQLite WAL）：scheduler 寫、dispatcher 讀、heartbeat 統計，都不打架。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from l2_trigger.types import TriggerAction, TriggerDecision

from botpaths import db_path as _db_path

DB_PATH = _db_path("fire_queue.db")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _init(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fires (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            setup TEXT NOT NULL,
            direction TEXT NOT NULL,
            decision_json TEXT NOT NULL,
            enqueued_at INTEGER NOT NULL,
            sent_at INTEGER,
            status TEXT NOT NULL DEFAULT 'queued',
            tg_message_id INTEGER,
            fail_reason TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cooldown (
            symbol TEXT NOT NULL,
            setup TEXT NOT NULL,
            direction TEXT NOT NULL,
            last_fired INTEGER NOT NULL,
            PRIMARY KEY (symbol, setup, direction)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fires_status ON fires(status, enqueued_at)")
    # v48: 既有 DB 缺 fail_reason 欄 → 補上（失敗原因可事後稽核分類）。
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fires)").fetchall()}
    if "fail_reason" not in cols:
        conn.execute("ALTER TABLE fires ADD COLUMN fail_reason TEXT")


def _serialize_decision(d: TriggerDecision) -> str:
    snap = d.snapshot
    snap_d = {
        "symbol": snap.symbol, "ts": snap.ts, "price": snap.price, "tf": snap.tf,
        "oi": snap.oi, "oi_delta_pct": snap.oi_delta_pct,
        "funding": snap.funding, "funding_predicted": snap.funding_predicted,
        "cvd": snap.cvd, "cvd_slope": snap.cvd_slope,
        "cvd_price_divergence": snap.cvd_price_divergence,
        "ls_ratio": snap.ls_ratio, "top_trader_ratio": snap.top_trader_ratio,
        "liq_long": snap.liq_long, "liq_short": snap.liq_short,
        "btc_gate_open": snap.btc_gate_open, "btc_regime": snap.btc_regime,
        "above_4h_200ma": snap.above_4h_200ma,
        "is_hot": snap.is_hot, "strength_score": snap.strength_score,
        "atr_pct_7d": snap.atr_pct_7d, "vol_24h_vs_30d": snap.vol_24h_vs_30d,
        "cvd_slope_7d": snap.cvd_slope_7d,
        "top_trader_slope_7d": snap.top_trader_slope_7d,
        "oi_delta_7d_pct": snap.oi_delta_7d_pct,
        "higher_lows_7d": snap.higher_lows_7d,
        "stale_fields": list(snap.stale_fields),
    }
    confirmed = [
        {"name": s.name, "state": s.state.value,
         "score": s.score, "evidence": s.evidence}
        for s in d.confirmed
    ]
    return json.dumps({
        "action": d.action.value,
        "direction": d.direction.value,
        "setup_name": d.setup_name,
        "composite_score": d.composite_score,
        "reason": d.reason,
        "snapshot": snap_d,
        "confirmed": confirmed,
    }, default=str)


def enqueue(decision: TriggerDecision, cooldown_seconds: int = 3600,
            cross_check_payload: dict | None = None) -> bool:
    """若在冷卻內 → 不入隊回 False；否則入隊回 True。
    cross_check_payload 會被合進 decision_json 給 dispatcher 渲染用。
    """
    if decision.action != TriggerAction.FIRE:
        return False
    conn = _conn()
    try:
        _init(conn)
        now = int(time.time())
        key = (decision.snapshot.symbol, decision.setup_name, decision.direction.value)
        row = conn.execute(
            "SELECT last_fired FROM cooldown WHERE symbol=? AND setup=? AND direction=?",
            key,
        ).fetchone()
        if row and (now - row[0]) < cooldown_seconds:
            return False
        # 序列化 decision + 附加 cross_check
        payload = json.loads(_serialize_decision(decision))
        if cross_check_payload:
            payload["cross_check"] = cross_check_payload
        conn.execute(
            "INSERT INTO fires(symbol, setup, direction, decision_json, enqueued_at, status)"
            " VALUES (?, ?, ?, ?, ?, 'queued')",
            (*key, json.dumps(payload), now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO cooldown(symbol, setup, direction, last_fired)"
            " VALUES (?, ?, ?, ?)",
            (*key, now),
        )
        return True
    finally:
        conn.close()


def dequeue_one() -> tuple[int, dict] | None:
    conn = _conn()
    try:
        _init(conn)
        row = conn.execute(
            "SELECT id, decision_json FROM fires"
            " WHERE status='queued' ORDER BY enqueued_at LIMIT 1"
        ).fetchone()
        if not row:
            return None
        # 立刻 mark dispatching 避免兩個 worker 搶。
        # v48: 真正落實樂觀鎖 — 只有把此筆 queued→dispatching 成功的 worker 才取走它。
        # 檢查 rowcount==1；若為 0 代表已被其他 consumer 搶走 → 本輪空手而回（防重複派發）。
        changed = conn.execute(
            "UPDATE fires SET status='dispatching' WHERE id=? AND status='queued'",
            (row[0],),
        ).rowcount
        if changed != 1:
            return None
        return (row[0], json.loads(row[1]))
    finally:
        conn.close()


def mark_sent(fire_id: int, tg_message_id: int | None = None) -> None:
    conn = _conn()
    try:
        conn.execute(
            "UPDATE fires SET status='sent', sent_at=?, tg_message_id=? WHERE id=?",
            (int(time.time()), tg_message_id, fire_id),
        )
    finally:
        conn.close()


def mark_failed(fire_id: int, reason: str = "") -> None:
    # v48: 把 reason 真正寫進 DB（過去收到卻丟棄，事後無法分辨訊號為何沒送出）。
    conn = _conn()
    try:
        _init(conn)
        conn.execute(
            "UPDATE fires SET status='failed', fail_reason=? WHERE id=?",
            (reason or None, fire_id),
        )
    finally:
        conn.close()


def reclaim_orphans() -> int:
    """啟動回收：把卡在 'dispatching' 中間態的 FIRE 重設回 'queued'，回傳回收筆數。

    為何安全：本機只有單一 dispatcher worker。daemon 剛啟動（或 dispatcher 剛被
    supervisor 重啟）的那一刻，必然沒有任何派發正在進行中，所以此時任何
    status='dispatching' 都是上次崩潰/斷電/Ctrl+C 在「dequeue 之後、mark_sent 之前」
    留下的孤兒 → 安全回收重派，避免進場訊號被靜默吞掉（使用者完全無感的漏單）。
    """
    conn = _conn()
    try:
        _init(conn)
        return conn.execute(
            "UPDATE fires SET status='queued' WHERE status='dispatching'"
        ).rowcount
    finally:
        conn.close()


def stats() -> dict:
    conn = _conn()
    try:
        _init(conn)
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM fires GROUP BY status"
        ).fetchall()
        return dict(rows)
    finally:
        conn.close()


def last_fire_ts() -> int:
    """v239：最後一筆 FIRE 進佇列的 unix 秒。**沒有任何一筆**回 0。

    ⛔ 讀不到（DB 壞掉／被鎖住）時**不回 0、也不回 None**——直接讓例外往上拋。
       「表是空的、從來沒 FIRE 過」和「我讀不到這張表」在這裡會算出同一個
       數字，但那是兩個完全不同的事實；把後者折成前者，就會出現「已乾旱
       N 天」這種憑空捏造的結論，或反過來把真乾旱吞掉。呼叫端自己 try。

    ⚠️ active 表會被 archive_and_clean(7 天) 搬走，所以必須同時看 fires_history，
       否則歸檔的隔天就會誤判成「從來沒 FIRE 過」。
    """
    conn = _conn()
    try:
        _init(conn)
        best = conn.execute(
            "SELECT COALESCE(MAX(enqueued_at), 0) FROM fires"
        ).fetchone()[0] or 0
        has_hist = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fires_history'"
        ).fetchone()
        if has_hist:
            h = conn.execute(
                "SELECT COALESCE(MAX(enqueued_at), 0) FROM fires_history"
            ).fetchone()[0] or 0
            best = max(best, h)
        return int(best)
    finally:
        conn.close()


def reset_db() -> None:
    """測試 / demo 用。生產環境別呼叫。"""
    if DB_PATH.exists():
        DB_PATH.unlink()
    # 同時清 WAL
    for ext in ("-wal", "-shm"):
        p = Path(str(DB_PATH) + ext)
        if p.exists():
            p.unlink()


def archive_and_clean(days_old: int = 7) -> dict:
    """歸檔舊 fires 到 fires_history（避免每次重啟流失歷史）。

    保留 N 天內的 active fires + cooldown，舊的搬到歷史表。
    """
    import time as _time
    conn = _conn()
    try:
        _init(conn)
        # 建 history 表（如不存在）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fires_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_id INTEGER,
                symbol TEXT, setup TEXT, direction TEXT,
                decision_json TEXT,
                enqueued_at INTEGER, sent_at INTEGER,
                status TEXT, tg_message_id INTEGER,
                archived_at INTEGER
            )
        """)
        cutoff = int(_time.time()) - days_old * 86400
        # 複製到 history
        moved = conn.execute(
            "INSERT INTO fires_history "
            "(original_id, symbol, setup, direction, decision_json, "
            " enqueued_at, sent_at, status, tg_message_id, archived_at) "
            "SELECT id, symbol, setup, direction, decision_json, "
            " enqueued_at, sent_at, status, tg_message_id, ? "
            "FROM fires WHERE enqueued_at < ?",
            (int(_time.time()), cutoff),
        ).rowcount
        # 從 active 表刪除
        deleted = conn.execute(
            "DELETE FROM fires WHERE enqueued_at < ?", (cutoff,),
        ).rowcount
        return {"archived": moved, "deleted_from_active": deleted}
    finally:
        conn.close()


def get_history(days: int = 30) -> list[dict]:
    """讀歷史 fires + active fires for analysis"""
    import time as _time
    cutoff = int(_time.time()) - days * 86400
    conn = _conn()
    try:
        _init(conn)
        # active
        active = conn.execute(
            "SELECT 'active' as src, id, symbol, setup, direction, "
            " decision_json, enqueued_at, sent_at, status, tg_message_id "
            "FROM fires WHERE enqueued_at >= ?", (cutoff,),
        ).fetchall()
        # historical
        try:
            hist = conn.execute(
                "SELECT 'history' as src, original_id as id, symbol, setup, direction, "
                " decision_json, enqueued_at, sent_at, status, tg_message_id "
                "FROM fires_history WHERE enqueued_at >= ?", (cutoff,),
            ).fetchall()
        except Exception:
            hist = []
        all_rows = list(active) + list(hist)
        return [
            {"src": r[0], "id": r[1], "symbol": r[2], "setup": r[3],
             "direction": r[4], "decision_json": r[5],
             "enqueued_at": r[6], "sent_at": r[7],
             "status": r[8], "tg_message_id": r[9]}
            for r in all_rows
        ]
    finally:
        conn.close()
