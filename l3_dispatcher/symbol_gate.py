"""跨來源 per-symbol 收斂閘（v47）— 修「同幣別短時間重複出單」。

問題（使用者回報）：🎯交易訊號 主題常出現「同一幣別、短時間內重複的單子」。

根因：多條 FIRE 級來源各有各的去重，彼此看不到對方——
    1. scheduler → fire_queue：冷卻 key=(symbol, setup, direction)，只擋「同 setup 同向」。
    2. deepdive（macro.run_per_symbol_loop）：只靠 get_open_trades() 擋已開倉，**沒有時間冷卻**。
    3. us_signals：CooldownStore 是函式區域變數，**進程重啟即失憶**。
→ scheduler 推「BTC 多（intraday）」與 deepdive 推「BTC 交易計畫深度分析（多）」
  可在數分鐘內各送一次，使用者看到的就是同幣重複單。

解法：再加一道「跨來源」收斂閘，key 刻意只含 (symbol, direction)（**不含 setup**），
所有 FIRE 級 worker 在推 🎯主題前統一查它。SQLite WAL 持久化（跨進程 + 重啟存活）。
這是「**額外一層**」，不替換 fire_queue 既有 (symbol,setup,direction) 冷卻。

設定（皆可在 .env 覆寫）：
    SYMBOL_GATE_WINDOW_S        預設 3600（1h）。同 (幣,向) 在窗內已推過 → 後到者跳過。
    SYMBOL_GATE_BLOCK_REVERSAL  預設 0（關）。開啟時連「反向」也節流（bull↔bear 互擋）。
        預設關閉是刻意的：反轉訊號對「持倉出場」有參考價值，且 test_engine 鎖定了
        方向化冷卻的既有設計——不在這裡破壞它。

跨進程安全：SQLite WAL，多 worker（dispatcher / deepdive / us_signals）共寫同一張表。
"""
from __future__ import annotations

import sqlite3
import time

from botpaths import db_path as _db_path

DB_PATH = _db_path("symbol_gate.db")

_DEFAULT_WINDOW_S = 3600


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _init(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS symbol_sends (
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            last_sent INTEGER NOT NULL,
            PRIMARY KEY (symbol, direction)
        )
    """)


def _window_default() -> int:
    """讀 SYMBOL_GATE_WINDOW_S（秒）。設定錯誤 → 退回 3600。"""
    try:
        from botconfig import get_str
        v = int(float(get_str("SYMBOL_GATE_WINDOW_S", str(_DEFAULT_WINDOW_S))))
        return v if v > 0 else _DEFAULT_WINDOW_S
    except Exception:
        return _DEFAULT_WINDOW_S


def _block_reversal() -> bool:
    try:
        from botconfig import get_str
        return get_str("SYMBOL_GATE_BLOCK_REVERSAL", "0").strip().lower() \
            in ("1", "true", "yes", "on")
    except Exception:
        return False


def _last_sent(conn: sqlite3.Connection, symbol: str, direction: str) -> int | None:
    row = conn.execute(
        "SELECT last_sent FROM symbol_sends WHERE symbol=? AND direction=?",
        (symbol, direction),
    ).fetchone()
    return row[0] if row else None


def should_send(symbol: str, direction: str, *,
                window_s: int | None = None, now: int | None = None) -> bool:
    """唯讀檢查：True = 可送（窗內沒有同 (幣,向) 的近期推送）；False = 應跳過。

    刻意「只讀不寫」——標記留給真正送出後的 mark_sent()，避免風控阻擋等
    「沒真的推到 🎯」的情況誤佔冷卻槽。任何 DB 錯誤 → 回 True（放行，絕不因閘故障漏單）。

    window_s 可由呼叫端指定（如 US 用 14400 對齊其 4h 冷卻）；None 則用 SYMBOL_GATE_WINDOW_S。
    """
    win = window_s if window_s is not None else _window_default()
    t = int(now if now is not None else time.time())
    try:
        conn = _conn()
        try:
            _init(conn)
            last = _last_sent(conn, symbol, direction)
            if last is not None and (t - last) < win:
                return False
            if _block_reversal():
                opp = "bear" if direction == "bull" else "bull" if direction == "bear" else None
                if opp is not None:
                    last_opp = _last_sent(conn, symbol, opp)
                    if last_opp is not None and (t - last_opp) < win:
                        return False
            return True
        finally:
            conn.close()
    except Exception:
        return True


def mark_sent(symbol: str, direction: str, *, now: int | None = None) -> None:
    """記一筆「已推到 🎯」。在真正送出成功後呼叫。DB 錯誤靜默吞（不阻塞推播）。"""
    t = int(now if now is not None else time.time())
    try:
        conn = _conn()
        try:
            _init(conn)
            conn.execute(
                "INSERT OR REPLACE INTO symbol_sends(symbol, direction, last_sent)"
                " VALUES (?, ?, ?)",
                (symbol, direction, t),
            )
        finally:
            conn.close()
    except Exception:
        pass


def last_sent_age(symbol: str, direction: str, *, now: int | None = None) -> int | None:
    """距上次推送的秒數（診斷用）；從未推過回 None。"""
    t = int(now if now is not None else time.time())
    try:
        conn = _conn()
        try:
            _init(conn)
            last = _last_sent(conn, symbol, direction)
            return (t - last) if last is not None else None
        finally:
            conn.close()
    except Exception:
        return None


def reset_db() -> None:
    """測試用。生產環境別呼叫。"""
    from pathlib import Path
    if DB_PATH.exists():
        DB_PATH.unlink()
    for ext in ("-wal", "-shm"):
        p = Path(str(DB_PATH) + ext)
        if p.exists():
            p.unlink()
