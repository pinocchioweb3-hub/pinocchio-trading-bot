"""Paper Journal（v16 / Stage 0）：紙上交易帳本。

設計目的（回應使用者流程認知 + 自動交易前置）：
    - 每筆 FIRE 訊號「推送即視為紙上開倉」，不用按按鈕，15 分鐘週期自動追蹤 TP/SL
    - 與實倉帳（trade_journal，按 ✅ 才算）完全分離：
        * 紙上帳 = 驗證「引擎本身」的期望值（自動交易 Stage 0 的 100 筆門檻）
        * 實倉帳 = 你真實的交易績效
    - 不影響 risk_manager 熔斷/額度（那些只看實倉）

表：paper_trades（與 trade_journal.db 同檔）
"""
from __future__ import annotations

import sqlite3
import time

from botpaths import db_path as _db_path

DB_PATH = _db_path("trade_journal.db")

RISK_USD = 100.0  # 紙上固定 1R = $100，與訊號設計一致


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    conn = _conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                setup TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_price REAL NOT NULL,
                stop_price REAL NOT NULL,
                tp1 REAL, tp2 REAL, tp3 REAL,
                entry_at INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',  -- 'open' / 'closed'
                legs_hit TEXT DEFAULT '',              -- csv: 'tp1,tp2'
                size_remaining REAL NOT NULL DEFAULT 1.0,
                pnl_usd REAL NOT NULL DEFAULT 0,
                realized_r REAL,
                exit_reason TEXT,
                exit_at INTEGER,
                fire_id INTEGER,
                regime TEXT,
                created_at INTEGER NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_trades(status, entry_at)")
    finally:
        conn.close()


def record_paper_entry(symbol: str, setup: str, direction: str,
                       entry_price: float, stop_price: float,
                       tp1: float, tp2: float, tp3: float,
                       fire_id: int | None = None,
                       regime: str | None = None) -> int:
    init_db()
    conn = _conn()
    try:
        now_ms = int(time.time() * 1000)
        cur = conn.execute(
            """INSERT INTO paper_trades
               (symbol, setup, direction, entry_price, stop_price, tp1, tp2, tp3,
                entry_at, fire_id, regime, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, setup, direction, entry_price, stop_price, tp1, tp2, tp3,
             now_ms, fire_id, regime, now_ms),
        )
        return cur.lastrowid
    finally:
        conn.close()


def get_open_paper() -> list[dict]:
    """回所有 open 紙上倉位，dict 形狀與 trade_monitor._check_trade 相容。"""
    init_db()
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, symbol, setup, direction, entry_price, stop_price, "
            "tp1, tp2, tp3, entry_at, legs_hit, size_remaining "
            "FROM paper_trades WHERE status='open' ORDER BY entry_at",
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r[0], "symbol": r[1], "setup": r[2], "direction": r[3],
                "entry_price": r[4], "stop_price": r[5],
                "tp1": r[6], "tp2": r[7], "tp3": r[8],
                "entry_at": r[9],
                "legs_hit": [x for x in (r[10] or "").split(",") if x],
                "size_remaining": r[11],
                "risk_usd": RISK_USD,
                "tg_message_id": None,
            })
        return out
    finally:
        conn.close()


def apply_paper_event(paper_id: int, leg_label: str, size_pct: float,
                      exit_price: float) -> dict:
    """套用一次 TP/SL/timeout 事件，回 {leg_r, leg_pnl, closed, total_pnl}。"""
    init_db()
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT direction, entry_price, stop_price, legs_hit, size_remaining, "
            "pnl_usd, status FROM paper_trades WHERE id=?",
            (paper_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"paper trade {paper_id} not found")
        direction, entry, stop, legs_csv, size_rem, pnl, status = row
        if status != "open":
            return {"closed": True, "total_pnl": pnl, "leg_r": 0, "leg_pnl": 0}

        sl_dist = abs(entry - stop)
        leg_r = ((exit_price - entry) if direction == "bull"
                 else (entry - exit_price)) / sl_dist
        leg_pnl = size_pct * RISK_USD * leg_r

        new_legs = ",".join([x for x in (legs_csv or "").split(",") if x] + [leg_label])
        new_size = round(size_rem - size_pct, 3)
        new_pnl = pnl + leg_pnl
        now_ms = int(time.time() * 1000)

        if new_size <= 0.001 or leg_label in ("stop", "timeout"):
            conn.execute(
                """UPDATE paper_trades SET status='closed', legs_hit=?, size_remaining=0,
                   pnl_usd=?, realized_r=?, exit_reason=?, exit_at=? WHERE id=?""",
                (new_legs, new_pnl, new_pnl / RISK_USD, leg_label, now_ms, paper_id),
            )
            return {"closed": True, "total_pnl": round(new_pnl, 2),
                    "leg_r": round(leg_r, 3), "leg_pnl": round(leg_pnl, 2)}
        conn.execute(
            "UPDATE paper_trades SET legs_hit=?, size_remaining=?, pnl_usd=? WHERE id=?",
            (new_legs, new_size, new_pnl, paper_id),
        )
        return {"closed": False, "total_pnl": round(new_pnl, 2),
                "leg_r": round(leg_r, 3), "leg_pnl": round(leg_pnl, 2)}
    finally:
        conn.close()


def get_paper_stats(days: int = 30, setup: str | None = None) -> dict:
    """紙上帳統計（引擎期望值驗證用）。setup 指定時只統計該引擎。"""
    init_db()
    conn = _conn()
    try:
        cutoff = int(time.time() * 1000) - days * 86400 * 1000
        sql = "SELECT status, pnl_usd, realized_r FROM paper_trades WHERE entry_at >= ?"
        args: list = [cutoff]
        if setup:
            sql += " AND setup=?"
            args.append(setup)
        rows = conn.execute(sql, args).fetchall()
        closed = [r for r in rows if r[0] == "closed"]
        opens = [r for r in rows if r[0] == "open"]
        wins = [r for r in closed if (r[2] or 0) > 0]
        total_pnl = sum(r[1] or 0 for r in closed)
        rs = [r[2] or 0 for r in closed]
        return {
            "window_days": days,
            "n_closed": len(closed),
            "n_open": len(opens),
            "n_wins": len(wins),
            "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
            "total_pnl_usd": round(total_pnl, 2),
            "avg_r": round(sum(rs) / len(rs), 3) if rs else 0.0,
            "stage0_progress": f"{len(closed)}/100",  # 自動交易 Stage 1 門檻
        }
    finally:
        conn.close()


def render_paper_summary(stats: dict) -> str:
    """紙上帳一行摘要（嵌進 /status 與每日績效）"""
    if stats["n_closed"] == 0 and stats["n_open"] == 0:
        return "📜 紙上驗證：尚無紀錄（每筆訊號自動追蹤中）"
    return (f"📜 紙上驗證（{stats['window_days']}d）："
            f"已平 <code>{stats['n_closed']}</code> 筆 "
            f"勝率 <code>{stats['win_rate_pct']}%</code> "
            f"期望值 <code>{stats['avg_r']:+.2f}R</code>/筆　"
            f"Stage1 門檻 <code>{stats['stage0_progress']}</code>"
            f"　<i>(100U 風險基準 PnL ${stats['total_pnl_usd']:+.0f})</i>")


if __name__ == "__main__":
    init_db()
    print(f"paper journal at {DB_PATH}")
    print(render_paper_summary(get_paper_stats(30)))
