"""Demo Journal（task #39 / Phase 0 實倉時鐘）：OKX 模擬盤自動操盤手的帳本。

定位（與既有三本帳的關係）：
    - paper_trades（paper_journal）＝「引擎期望值」驗證：每筆 FIRE 推送即視為紙上開倉，
      用模擬撮合追 TP/SL。它是 Phase 0「模擬盤 ≥100 筆」門檻的來源。
    - trades（trade_journal）＝你**真錢**的交易帳（按 ✅ 才寫；目前 0 筆、休眠）。紅線①。
    - demo_trades（本檔）＝**OKX 模擬盤真實下單**的帳：把 paper 訊號鏡像成 OKX 模擬盤上的
      真實限價單（真實撮合、真實滑點、真實 SL/TP 觸發、真實 realizedPnl）。它驗證的是
      「持倉/績效真實性」——引擎在**真實交易所機制**下到底會發生什麼，而 realized_r 完全
      取自 OKX 真相（positions-history 的 realizedPnl ÷ 實際 1R），**絕不本地捏造**（紅線③）。

      ⚠️ demo_trades 是模擬盤、**不是真錢**，也**不是** Phase 0「真實小額 ≥30 筆」門檻
      （那一格只認 trades 表的真錢交易）。本帳是「真實交易所機制下的驗證樣本」，呈現時
      一律標「模擬盤」，永不當真錢績效宣稱。phase0 是否把本帳當「實倉樣本」採計，是需
      使用者拍板的語意決策（ceo_session 只透明列出、不擅自改 ready 判斷）。

冪等：以 intent_id（demo_trader.make_intent_id，含 paper_id/fire_id）為唯一鍵，
      INSERT OR IGNORE — 同一訊號重入不會開雙倉。high-water-mark 存在 demo_operator_state，
      首次啟動只記當前最大 paper id（不回補歷史）。

純資料層：無網路、無下單、無 demo_guard 呼叫（那些在 demo_operator）。可離線自測。

表（與 trade_journal.db 同檔）：demo_trades、demo_operator_state
"""
from __future__ import annotations

import sqlite3
import time

from botpaths import db_path as _db_path

DB_PATH = _db_path("trade_journal.db")


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
            CREATE TABLE IF NOT EXISTS demo_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                intent_id TEXT NOT NULL UNIQUE,   -- demo_trader.make_intent_id（冪等鍵）
                cl_ord_id TEXT,
                paper_id INTEGER,                 -- 來源 paper_trades.id（鏡像追溯）
                fire_id INTEGER,
                symbol TEXT NOT NULL,
                setup TEXT,
                direction TEXT NOT NULL,          -- 'bull' / 'bear'
                entry_price REAL NOT NULL,        -- 計畫進場（限價）
                stop_price REAL NOT NULL,
                tp1 REAL, tp2 REAL, tp3 REAL,
                leverage INTEGER,
                notional_usd REAL,
                margin_usd REAL,
                contracts REAL,
                ct_val REAL,
                risk_usd REAL NOT NULL,           -- 取整後實際 1R（realized_risk_usd）＝ R 的分母
                entry_order_id TEXT,              -- OKX 進場單 id
                status TEXT NOT NULL DEFAULT 'pending',
                    -- 'pending'(限價已掛未成) / 'open'(已成交持倉中) / 'closed' / 'rejected'
                pnl_usd REAL,                     -- OKX 真相 realizedPnl（淨）
                realized_r REAL,                  -- pnl_usd / risk_usd（OKX 真相，非捏造）
                exit_reason TEXT,
                    -- 'tp'/'stop'/'timeout'/'manual'/'entry_expired'/'reconcile_halt'/'reject:*'
                regime TEXT,
                entry_at INTEGER NOT NULL,        -- 本地下單時刻（ms）
                filled_at INTEGER,               -- 偵測到成交時刻
                exit_at INTEGER,
                last_synced_at INTEGER,          -- 監控層最後一次對 OKX 核對的時刻
                note TEXT,
                created_at INTEGER NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_demo_status ON demo_trades(status, entry_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_demo_paper ON demo_trades(paper_id)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS demo_operator_state (
                k TEXT PRIMARY KEY,
                v TEXT,
                updated_at INTEGER
            )
        """)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# operator state（高水位 / halt 旗標等）
# ---------------------------------------------------------------------------
def get_state(key: str, default: str | None = None) -> str | None:
    init_db()
    conn = _conn()
    try:
        row = conn.execute("SELECT v FROM demo_operator_state WHERE k=?", (key,)).fetchone()
        return row[0] if row else default
    finally:
        conn.close()


def set_state(key: str, value: str) -> None:
    init_db()
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO demo_operator_state (k, v, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
            (key, value, int(time.time() * 1000)),
        )
    finally:
        conn.close()


def get_high_water_mark() -> int:
    """已處理過的最大 paper_trades.id。0 = 尚未初始化（首次啟動應設為當前最大值以略過回補）。"""
    v = get_state("paper_high_water_mark", "0")
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def set_high_water_mark(paper_id: int) -> None:
    """單調前進：只升不降（避免並發/重入回退去重水位）。"""
    cur = get_high_water_mark()
    if paper_id > cur:
        set_state("paper_high_water_mark", str(int(paper_id)))


def is_halted() -> tuple[bool, str]:
    """對帳漂移等致命狀況會 set 此旗標 → 操盤手停所有新單（保守）。回 (halted, reason)。"""
    v = get_state("halt", "")
    return (bool(v), v or "")


def set_halt(reason: str) -> None:
    set_state("halt", reason)


def clear_halt() -> None:
    set_state("halt", "")


# ---------------------------------------------------------------------------
# 下單帳本：寫入 / 查詢 / 平倉
# ---------------------------------------------------------------------------
def intent_exists(intent_id: str) -> bool:
    init_db()
    conn = _conn()
    try:
        row = conn.execute("SELECT 1 FROM demo_trades WHERE intent_id=?", (intent_id,)).fetchone()
        return row is not None
    finally:
        conn.close()


def record_demo_entry(*, intent_id: str, cl_ord_id: str, paper_id: int | None,
                      fire_id: int | None, symbol: str, setup: str | None,
                      direction: str, entry_price: float, stop_price: float,
                      tp1: float | None, tp2: float | None, tp3: float | None,
                      leverage: int, notional_usd: float, margin_usd: float,
                      contracts: float, ct_val: float, risk_usd: float,
                      entry_order_id: str | None, regime: str | None = None,
                      status: str = "pending", exit_reason: str | None = None,
                      note: str | None = None) -> int:
    """寫一筆模擬盤下單（或 rejected/skipped 記錄）。以 intent_id 冪等（重入回傳既有 id）。

    status='rejected' 時用來記錄「決定不下這筆」的審計痕跡（exit_reason='reject:*'），
    使高水位能安全前進、同一訊號不會每輪重試。回傳 demo_trades.id；重入回既有 id（≥0）。"""
    init_db()
    conn = _conn()
    try:
        now_ms = int(time.time() * 1000)
        cur = conn.execute(
            """INSERT OR IGNORE INTO demo_trades
               (intent_id, cl_ord_id, paper_id, fire_id, symbol, setup, direction,
                entry_price, stop_price, tp1, tp2, tp3, leverage, notional_usd,
                margin_usd, contracts, ct_val, risk_usd, entry_order_id, status,
                exit_reason, regime, entry_at, last_synced_at, note, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?)""",
            (intent_id, cl_ord_id, paper_id, fire_id, symbol, setup, direction,
             entry_price, stop_price, tp1, tp2, tp3, leverage, notional_usd,
             margin_usd, contracts, ct_val, risk_usd, entry_order_id, status,
             exit_reason, regime, now_ms, now_ms, note, now_ms),
        )
        if cur.lastrowid:
            return cur.lastrowid
        row = conn.execute("SELECT id FROM demo_trades WHERE intent_id=?", (intent_id,)).fetchone()
        return int(row[0]) if row else -1
    finally:
        conn.close()


def mark_filled(intent_id: str, filled_at_ms: int | None = None) -> bool:
    """限價成交（OKX 上出現對應持倉）→ pending → open。回是否確有狀態轉換。"""
    init_db()
    conn = _conn()
    try:
        ts = int(filled_at_ms if filled_at_ms is not None else time.time() * 1000)
        cur = conn.execute(
            "UPDATE demo_trades SET status='open', filled_at=?, last_synced_at=? "
            "WHERE intent_id=? AND status='pending'",
            (ts, int(time.time() * 1000), intent_id),
        )
        return cur.rowcount > 0
    finally:
        conn.close()


def apply_demo_close(intent_id: str, *, pnl_usd: float, exit_reason: str,
                     exit_at_ms: int | None = None, note: str | None = None) -> dict:
    """以 OKX 真相平倉一筆：realized_r = pnl_usd / risk_usd（**真相，非捏造**）。

    entry_expired（限價從未成交而作廢）以 pnl=0、realized_r=0 記，語意與 paper 一致、
    排除於期望值。任何狀態（pending/open）都可被收斂為 closed（冪等：已 closed 不再覆寫）。"""
    init_db()
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT risk_usd, status FROM demo_trades WHERE intent_id=?", (intent_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "intent_not_found"}
        risk_usd, status = float(row[0] or 0), row[1]
        if status == "closed":
            return {"ok": True, "already_closed": True}
        realized_r = round(pnl_usd / risk_usd, 4) if risk_usd > 0 else 0.0
        ts = int(exit_at_ms if exit_at_ms is not None else time.time() * 1000)
        conn.execute(
            "UPDATE demo_trades SET status='closed', pnl_usd=?, realized_r=?, "
            "exit_reason=?, exit_at=?, last_synced_at=?, "
            "note=COALESCE(?, note) WHERE intent_id=?",
            (round(pnl_usd, 4), realized_r, exit_reason, ts,
             int(time.time() * 1000), note, intent_id),
        )
        return {"ok": True, "realized_r": realized_r, "pnl_usd": round(pnl_usd, 4)}
    finally:
        conn.close()


def touch_synced(intent_id: str) -> None:
    """更新 last_synced_at（監控層每輪核對後呼叫，供「監控有在跑」健康探針）。"""
    init_db()
    conn = _conn()
    try:
        conn.execute("UPDATE demo_trades SET last_synced_at=? WHERE intent_id=?",
                     (int(time.time() * 1000), intent_id))
    finally:
        conn.close()


def get_live_demo_trades() -> list[dict]:
    """回所有未平倉（pending+open）的模擬盤單，供監控層 + 桶風險檢查 + 對帳。"""
    init_db()
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT intent_id, cl_ord_id, symbol, setup, direction, entry_price, "
            "stop_price, leverage, contracts, ct_val, risk_usd, entry_order_id, "
            "status, entry_at, filled_at, paper_id "
            "FROM demo_trades WHERE status IN ('pending','open') ORDER BY entry_at"
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "intent_id": r[0], "cl_ord_id": r[1], "symbol": r[2], "setup": r[3],
                "direction": r[4], "entry_price": r[5], "stop_price": r[6],
                "leverage": r[7], "contracts": r[8], "ct_val": r[9], "risk_usd": r[10],
                "entry_order_id": r[11], "status": r[12], "entry_at": r[13],
                "filled_at": r[14], "paper_id": r[15],
                # demo_trader.bucket_risk_check 讀 'symbol' + 'risk_usd' → 形狀相容
                "pos_side": "long" if r[4] == "bull" else "short",
            })
        return out
    finally:
        conn.close()


def get_demo_stats(days: int = 30) -> dict:
    """模擬盤真實下單帳統計（呈現/監控用）。realized_r 全取自 OKX 真相。
    排除 entry_expired（從未成交）與 rejected（未下單）於期望值。"""
    init_db()
    conn = _conn()
    try:
        cutoff = int(time.time() * 1000) - days * 86400 * 1000
        rows = conn.execute(
            "SELECT status, pnl_usd, realized_r, exit_reason FROM demo_trades "
            "WHERE entry_at >= ?", (cutoff,)).fetchall()
        closed = [r for r in rows
                  if r[0] == "closed" and (r[3] or "") not in ("entry_expired",)]
        pending = [r for r in rows if r[0] == "pending"]
        opens = [r for r in rows if r[0] == "open"]
        wins = [r for r in closed if (r[2] or 0) > 0]
        rs = [r[2] or 0 for r in closed]
        total_pnl = sum(r[1] or 0 for r in closed)
        gain = sum(r for r in rs if r > 0)
        loss = abs(sum(r for r in rs if r < 0))
        pf = (gain / loss) if loss > 0 else (float("inf") if gain > 0 else 0.0)
        return {
            "window_days": days,
            "n_closed": len(closed),
            "n_open": len(opens),
            "n_pending": len(pending),
            "n_wins": len(wins),
            "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
            "total_pnl_usd": round(total_pnl, 2),
            "avg_r": round(sum(rs) / len(rs), 3) if rs else 0.0,
            "profit_factor": round(pf, 2) if pf != float("inf") else None,
        }
    finally:
        conn.close()


def count_closed_for_phase0() -> tuple[int, float]:
    """供 ceo_session 透明呈現：全期間（不限天）模擬盤『真實已平倉』筆數 + 平均 R。
    與 _count_closed 一致排除 entry_expired/rejected。**僅透明用，不改 Phase 0 ready 判斷。**"""
    init_db()
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(AVG(realized_r), 0) FROM demo_trades "
            "WHERE status='closed' AND IFNULL(exit_reason,'') NOT IN "
            "('entry_expired', 'reconcile_halt') "
            "AND IFNULL(exit_reason,'') NOT LIKE 'reject:%'"
        ).fetchone()
        return int(row[0] or 0), round(float(row[1] or 0), 3)
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"demo journal at {DB_PATH}")
    print("live demo trades:", get_live_demo_trades())
    print("stats(30d):", get_demo_stats(30))
    print("phase0 closed:", count_closed_for_phase0())
    print("high_water_mark:", get_high_water_mark())
