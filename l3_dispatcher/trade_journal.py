"""Trade Journal：SQLite 記每筆 entry/exit/PnL，計算累積績效。

設計原則：
- 每筆 FIRE 自動 record_entry，狀態 'open'
- 後續 record_exit 更新（含 partial close）
- 統計查詢支援任意時窗（7d/30d/lifetime）
- daily_pnl 表為 derived view，給 risk_manager 熔斷判斷用

Schema:
    trades:        每筆交易完整紀錄
    trade_legs:    分批出場紀錄（TP1/TP2/TP3 各一筆 leg）
    daily_pnl:     每日 realized PnL（給熔斷用，避免每次掃全表）
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean

from botpaths import db_path as _db_path

DB_PATH = _db_path("trade_journal.db")


# ===========================================================================
# 連線 + schema
# ===========================================================================
def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    """建表（idempotent）"""
    conn = _conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                setup TEXT NOT NULL,
                direction TEXT NOT NULL,            -- 'bull' / 'bear'
                entry_price REAL NOT NULL,
                stop_price REAL NOT NULL,
                tp1 REAL, tp2 REAL, tp3 REAL,
                risk_usd REAL NOT NULL DEFAULT 100.0,
                leverage INTEGER,
                margin_usd REAL,
                notional_usd REAL,
                entry_at INTEGER NOT NULL,           -- epoch ms
                exit_at INTEGER,                     -- epoch ms（平倉後）
                exit_price REAL,                     -- 完全平倉的均價
                exit_reason TEXT,                    -- 'stop' / 'tp1' / 'tp2' / 'tp3' / 'timeout' / 'manual' / 'mixed'
                pnl_usd REAL,                        -- 實現 PnL（USD）
                realized_r REAL,                     -- R 倍數（pnl / risk）
                status TEXT NOT NULL DEFAULT 'open', -- 'open' / 'closed' / 'cancelled'
                fire_id INTEGER,                     -- 對應 fire_queue.fires 的 id
                tg_message_id INTEGER,
                decision_snapshot TEXT,              -- JSON：當下完整 decision/snapshot
                cross_check_confidence INTEGER,
                notes TEXT,
                tags TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_legs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id INTEGER NOT NULL,
                leg_label TEXT NOT NULL,             -- 'tp1' / 'tp2' / 'tp3' / 'stop' / 'manual'
                size_pct REAL NOT NULL,              -- 此 leg 占原始倉位比例（0.0-1.0）
                exit_price REAL NOT NULL,
                exit_at INTEGER NOT NULL,
                pnl_usd REAL NOT NULL,
                realized_r REAL NOT NULL,
                FOREIGN KEY (trade_id) REFERENCES trades(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_pnl (
                date_utc TEXT PRIMARY KEY,           -- 'YYYY-MM-DD'
                total_pnl_usd REAL NOT NULL DEFAULT 0,
                n_trades_closed INTEGER NOT NULL DEFAULT 0,
                n_wins INTEGER NOT NULL DEFAULT 0,
                n_losses INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_status_entry ON trades(status, entry_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol_status ON trades(symbol, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_legs_trade ON trade_legs(trade_id)")

        # v23-3 idempotent migration：訂單生命週期欄位
        existing = {r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()}
        for col, ddl in (
            ("entry_kind",    "ALTER TABLE trades ADD COLUMN entry_kind TEXT"),
            #   'direct_fire'  價在區內直接推送
            #   'wait_trigger' 先等待、價回區後觸發
            #   'market_chase' 確認成交價超出進場區（市價追入）
            ("entry_zone_lo", "ALTER TABLE trades ADD COLUMN entry_zone_lo REAL"),
            ("entry_zone_hi", "ALTER TABLE trades ADD COLUMN entry_zone_hi REAL"),
            ("fill_price",    "ALTER TABLE trades ADD COLUMN fill_price REAL"),
            ("triggered_at",  "ALTER TABLE trades ADD COLUMN triggered_at INTEGER"),
        ):
            if col not in existing:
                conn.execute(ddl)
    finally:
        conn.close()


# ===========================================================================
# 寫入：FIRE 進場
# ===========================================================================
@dataclass
class EntryRecord:
    symbol: str
    setup: str
    direction: str
    entry_price: float
    stop_price: float
    tp1: float
    tp2: float
    tp3: float
    risk_usd: float = 100.0
    leverage: int | None = None
    margin_usd: float | None = None
    notional_usd: float | None = None
    fire_id: int | None = None
    tg_message_id: int | None = None
    decision_snapshot: dict | None = None
    cross_check_confidence: int | None = None
    tags: list[str] = field(default_factory=list)
    notes: str = ""
    # v23-3: 訂單生命週期欄位
    entry_kind: str | None = None        # 'direct_fire' / 'wait_trigger' / 'market_chase'
    entry_zone_lo: float | None = None
    entry_zone_hi: float | None = None


def record_entry(entry: EntryRecord, initial_status: str = "signal") -> int:
    """寫一筆新訊號/進場，回 trade_id。

    v15 狀態機：
        signal  → 訊號已推送，等使用者確認（不算持倉、不進統計、不被 monitor 盯）
        open    → 使用者按「✅已下單」確認（或未來自動交易成交）
        closed  → 完全平倉
        skipped → 使用者按「⏭略過」
        expired → 訊號超時未確認（預設 4h）
        cancelled → 其他取消
    """
    init_db()
    conn = _conn()
    try:
        now_ms = int(time.time() * 1000)
        # v23-3: entry_kind 未指定時由 initial_status 推導
        kind = entry.entry_kind or (
            "wait_trigger" if initial_status == "waiting" else "direct_fire")
        cur = conn.execute(
            """INSERT INTO trades (
                symbol, setup, direction, entry_price, stop_price,
                tp1, tp2, tp3, risk_usd, leverage, margin_usd, notional_usd,
                entry_at, status, fire_id, tg_message_id, decision_snapshot,
                cross_check_confidence, notes, tags,
                created_at, updated_at,
                entry_kind, entry_zone_lo, entry_zone_hi
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry.symbol, entry.setup, entry.direction,
                entry.entry_price, entry.stop_price,
                entry.tp1, entry.tp2, entry.tp3,
                entry.risk_usd, entry.leverage, entry.margin_usd, entry.notional_usd,
                now_ms, initial_status,
                entry.fire_id, entry.tg_message_id,
                json.dumps(entry.decision_snapshot, default=str) if entry.decision_snapshot else None,
                entry.cross_check_confidence, entry.notes,
                ",".join(entry.tags) if entry.tags else None,
                now_ms, now_ms,
                kind, entry.entry_zone_lo, entry.entry_zone_hi,
            ),
        )
        return cur.lastrowid
    finally:
        conn.close()


# ===========================================================================
# v15: 訊號確認狀態機（幽靈帳本修復核心）
# ===========================================================================
def find_trade_by_fire(fire_id: int) -> dict | None:
    """用 fire_queue 的 fire_id 找對應 trade（按鈕 callback 用）"""
    init_db()
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT id, symbol, setup, direction, entry_price, stop_price, "
            "status, entry_at FROM trades WHERE fire_id=? ORDER BY id DESC LIMIT 1",
            (fire_id,),
        ).fetchone()
        if not row:
            return None
        return {"id": row[0], "symbol": row[1], "setup": row[2], "direction": row[3],
                "entry_price": row[4], "stop_price": row[5], "status": row[6],
                "entry_at": row[7]}
    finally:
        conn.close()


def get_signal_for_intent(fire_id: int | None = None,
                          symbol: str | None = None) -> dict | None:
    """給 /intent 指令與「📋 複製可執行 JSON」按鈕：回一個可直接餵
    telegram_bot.intent_format.to_trade_intent 的 decision_dict。

    優先用 record_entry 當下存的「完整 decision 快照」（v45 起 dispatcher 存全量）；
    舊訊號快照不完整（只有 {snapshot, reason}）時，用 trades 欄位重建最小可用版——
    rationale 會較空，但進場區/止損/止盈/槓桿等「可執行欄位」仍完整。

    查找順序：fire_id 優先 → 否則 symbol 取最近一筆 → 都沒給則全表最近一筆。查無回 None。
    """
    init_db()
    conn = _conn()
    try:
        cols = ("decision_snapshot, symbol, setup, direction, entry_price, "
                "stop_price, entry_at")
        if fire_id is not None:
            row = conn.execute(
                f"SELECT {cols} FROM trades WHERE fire_id=? ORDER BY id DESC LIMIT 1",
                (fire_id,)).fetchone()
        elif symbol is not None:
            row = conn.execute(
                f"SELECT {cols} FROM trades WHERE symbol=? ORDER BY id DESC LIMIT 1",
                (symbol.upper(),)).fetchone()
        else:
            row = conn.execute(
                f"SELECT {cols} FROM trades ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None
        snap_json, sym, setup, direction, entry_price, stop_price, entry_at = row

        blob = None
        if snap_json:
            try:
                blob = json.loads(snap_json)
            except Exception:
                blob = None

        # 完整 decision（v45 起）：忠實照用
        if (isinstance(blob, dict) and blob.get("direction") and blob.get("setup_name")
                and isinstance(blob.get("snapshot"), dict)):
            return blob

        # 退化：用 trades 欄位 +（可能存在的）部分快照重建最小可用 decision
        snap = (blob or {}).get("snapshot") if isinstance(blob, dict) else None
        if not isinstance(snap, dict):
            snap = {}
        snap.setdefault("symbol", sym)
        snap.setdefault("price", entry_price)
        snap.setdefault("ts", entry_at)
        return {
            "direction": direction,
            "setup_name": setup,
            "composite_score": None,
            "confirmed": [],
            "snapshot": snap,
        }
    finally:
        conn.close()


def confirm_trade(trade_id: int, fill_price: float | None = None) -> dict:
    """使用者確認已下單：signal → open。

    fill_price 提供時更新 entry_price（實際成交價）；entry_at 更新為確認時間
    （持倉時長/timeout 從真實進場起算）。
    回 {ok, status, msg}。
    """
    init_db()
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT status, entry_price FROM trades WHERE id=?", (trade_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "msg": f"trade {trade_id} 不存在"}
        status, old_price = row
        if status == "open":
            return {"ok": True, "status": "open", "msg": "已是持倉狀態（重複點擊）"}
        if status not in ("signal", "expired"):
            return {"ok": False, "status": status,
                    "msg": f"狀態 {status} 不能確認（已平倉或已略過）"}
        now_ms = int(time.time() * 1000)
        if fill_price is not None and fill_price > 0:
            # v23-3: fill_price 獨立保存（與訊號價分離）；成交價超出進場區
            #        → 標記市價追單（market_chase）
            zone = conn.execute(
                "SELECT entry_zone_lo, entry_zone_hi FROM trades WHERE id=?",
                (trade_id,)).fetchone()
            chase = (zone and zone[0] and zone[1]
                     and not (zone[0] <= fill_price <= zone[1]))
            if chase:
                conn.execute(
                    "UPDATE trades SET status='open', entry_price=?, fill_price=?, "
                    "entry_kind='market_chase', entry_at=?, updated_at=? WHERE id=?",
                    (fill_price, fill_price, now_ms, now_ms, trade_id))
            else:
                conn.execute(
                    "UPDATE trades SET status='open', entry_price=?, fill_price=?, "
                    "entry_at=?, updated_at=? WHERE id=?",
                    (fill_price, fill_price, now_ms, now_ms, trade_id))
        else:
            conn.execute(
                "UPDATE trades SET status='open', fill_price=entry_price, "
                "entry_at=?, updated_at=? WHERE id=?",
                (now_ms, now_ms, trade_id),
            )
        return {"ok": True, "status": "open",
                "msg": f"已確認進場 @ {fill_price or old_price}"}
    finally:
        conn.close()


def skip_trade(trade_id: int) -> dict:
    """使用者略過訊號：signal → skipped。"""
    init_db()
    conn = _conn()
    try:
        row = conn.execute("SELECT status FROM trades WHERE id=?", (trade_id,)).fetchone()
        if not row:
            return {"ok": False, "msg": f"trade {trade_id} 不存在"}
        if row[0] not in ("signal", "expired"):
            return {"ok": False, "status": row[0],
                    "msg": f"狀態 {row[0]} 不能略過"}
        conn.execute(
            "UPDATE trades SET status='skipped', updated_at=? WHERE id=?",
            (int(time.time() * 1000), trade_id),
        )
        return {"ok": True, "status": "skipped", "msg": "已標記略過"}
    finally:
        conn.close()


def expire_stale_signals(max_age_hours: float = 4.0) -> list[dict]:
    """超過 N 小時未確認的 signal → expired（入場區早已失效）。回過期清單。"""
    init_db()
    conn = _conn()
    try:
        cutoff_ms = int(time.time() * 1000) - int(max_age_hours * 3600 * 1000)
        rows = conn.execute(
            "SELECT id, symbol, direction, fire_id FROM trades "
            "WHERE status='signal' AND entry_at < ?",
            (cutoff_ms,),
        ).fetchall()
        if rows:
            conn.execute(
                "UPDATE trades SET status='expired', updated_at=? "
                "WHERE status='signal' AND entry_at < ?",
                (int(time.time() * 1000), cutoff_ms),
            )
        return [{"id": r[0], "symbol": r[1], "direction": r[2], "fire_id": r[3]}
                for r in rows]
    finally:
        conn.close()


# ===========================================================================
# v22: 持倉中抑制重複訊號 — 已確認開單的 symbol 不再推新 FIRE
# ===========================================================================
def get_open_trade(symbol: str) -> dict | None:
    """該 symbol 是否有 status='open' 的持倉。有 → 回 {id, direction, setup,
    entry_price}；無 → None。dispatcher 用此抑制持倉中的重複訊號。"""
    init_db()
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT id, direction, setup, entry_price FROM trades "
            "WHERE symbol=? AND status='open' ORDER BY entry_at DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        if not row:
            return None
        return {"id": row[0], "direction": row[1], "setup": row[2],
                "entry_price": row[3]}
    finally:
        conn.close()


# ===========================================================================
# v23-3: 訂單生命週期查詢（訂單卡與每日總帳的資料源）
# ===========================================================================
def get_trade_full(trade_id: int) -> dict | None:
    """單筆訂單完整資料：trades 整列 + legs 時間線 + 派生欄位。"""
    init_db()
    conn = _conn()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        if not row:
            return None
        t = dict(row)
        legs = [dict(r) for r in conn.execute(
            "SELECT leg_label, size_pct, exit_price, exit_at, pnl_usd, realized_r "
            "FROM trade_legs WHERE trade_id=? ORDER BY exit_at", (trade_id,))]
        t["legs"] = legs
        t["leg_sequence"] = "→".join(l["leg_label"].upper() for l in legs) or None
        if t.get("entry_at") and t.get("exit_at"):
            t["duration_h"] = round((t["exit_at"] - t["entry_at"]) / 3600000, 1)
        # 出場劇本分類
        labels = [l["leg_label"] for l in legs]
        if not labels:
            t["exit_scenario"] = None
        elif all(l.startswith("tp") for l in labels) and len(labels) >= 3:
            t["exit_scenario"] = "tp_full"        # TP 全收
        elif any(l.startswith("tp") for l in labels) and \
                any(l in ("stop", "timeout") for l in labels):
            t["exit_scenario"] = "tp_then_exit"   # 部分止盈後止損/逾時
        elif labels == ["stop"]:
            t["exit_scenario"] = "stop"
        elif labels == ["timeout"]:
            t["exit_scenario"] = "timeout"
        else:
            t["exit_scenario"] = "mixed"
        return t
    finally:
        conn.close()


def get_funnel_stats(since_ms: int, until_ms: int | None = None) -> dict:
    """訊號漏斗統計：推送 → 確認/略過/過期 → 平倉（每日總帳用）。

    注意：風控阻擋/持倉抑制不寫 trades（只在 fire_queue mark_failed），
    這裡先回 trades 端的漏斗；fire_queue 端的分母由呼叫方補。"""
    init_db()
    until_ms = until_ms or int(time.time() * 1000)
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT entry_kind, status, COUNT(*) FROM trades "
            "WHERE created_at BETWEEN ? AND ? GROUP BY entry_kind, status",
            (since_ms, until_ms)).fetchall()
        out = {"total": 0, "direct": {}, "waiting": {}, "chase": {}}
        for kind, status, n in rows:
            out["total"] += n
            bucket = ("waiting" if kind == "wait_trigger" else
                      "chase" if kind == "market_chase" else "direct")
            out[bucket][status] = out[bucket].get(status, 0) + n
        return out
    finally:
        conn.close()


_KIND_ZH = {"direct_fire": "⚡ 直接進場", "wait_trigger": "⏳ 等待觸發",
            "market_chase": "🏃 市價追單"}
_SCENARIO_ZH = {"tp_full": "TP 全收", "tp_then_exit": "部分止盈後出場",
                "stop": "直接止損", "timeout": "逾時出場", "mixed": "混合出場"}


def render_order_card(t: dict) -> str:
    """v23-4: 訂單卡 — 一筆訂單的完整脈絡（get_trade_full 的 dict 進來）。"""
    import html as _html

    def _ts(ms):
        return time.strftime("%m/%d %H:%M", time.localtime(ms / 1000)) if ms else "—"

    sym = _html.escape(t["symbol"])
    dir_zh = "做多" if t["direction"] == "bull" else "做空"
    kind = _KIND_ZH.get(t.get("entry_kind") or "", "⚡ 直接進場")
    if t.get("setup") == "us_breakout":
        kind = "🧪 美股紙上"
    lines = [
        f"🎫 <b>訂單卡 #{t['id']}｜{sym} {dir_zh}</b>｜{kind}",
        "━━━━━━━━━━━━━━━━",
    ]
    if t.get("entry_zone_lo") and t.get("entry_zone_hi"):
        lines.append(f"計畫：進場區 <code>${t['entry_zone_lo']:,.6g}–"
                     f"${t['entry_zone_hi']:,.6g}</code>　"
                     f"SL <code>${t['stop_price']:,.6g}</code>(1R)")
    else:
        lines.append(f"計畫：進場 <code>${t['entry_price']:,.6g}</code>　"
                     f"SL <code>${t['stop_price']:,.6g}</code>(1R)")

    # 時間線
    tl = [f"  {_ts(t['created_at'])} 📍 訊號建立（{kind}）"]
    if t.get("triggered_at"):
        tl.append(f"  {_ts(t['triggered_at'])} 🔔 價格觸發轉正式訊號")
    if t["status"] in ("open", "closed") and t.get("entry_at"):
        fill = t.get("fill_price") or t["entry_price"]
        tl.append(f"  {_ts(t['entry_at'])} ✅ 確認進場 @ <code>${fill:,.6g}</code>")
    for l in t.get("legs", []):
        icon = "🎯" if l["leg_label"].startswith("tp") else (
            "⏰" if l["leg_label"] == "timeout" else "🛑")
        tl.append(f"  {_ts(l['exit_at'])} {icon} {l['leg_label'].upper()} "
                  f"平 {l['size_pct']*100:.0f}% @ <code>${l['exit_price']:,.6g}</code>"
                  f"（{l['realized_r']:+.2f}R）")
    lines.append("📍 <b>時間線</b>\n" + "\n".join(tl))

    # 終態
    st = t["status"]
    if st == "closed":
        win = (t.get("realized_r") or 0) > 0
        scenario = _SCENARIO_ZH.get(t.get("exit_scenario") or "", "")
        lines.append(
            f"{'✅' if win else '❌'} <b>已平倉 {t.get('realized_r', 0):+.2f}R</b>"
            f"（${t.get('pnl_usd', 0):+,.0f}）｜{scenario}"
            + (f"｜持倉 {t['duration_h']}h" if t.get("duration_h") else ""))
    elif st == "open":
        hit = {l["leg_label"] for l in t.get("legs", [])}
        boxes = "".join("✅" if f"tp{i}" in hit else "⬜" for i in (1, 2, 3))
        lines.append(f"📈 <b>持倉中</b>｜腿 TP{boxes}")
    elif st == "skipped":
        lines.append("⏭ 你選擇略過")
    elif st == "expired":
        lines.append("⏰ 過期未確認（自動失效）")
    elif st == "waiting":
        lines.append("⏳ 等待價格回進場區（最多 6h）")
    return "\n".join(lines)


def get_paper_outcome_by_fire(fire_id: int) -> dict | None:
    """v23-4: 錯過卡用 — 該訊號的紙上對照結果（JOIN fire_id）。"""
    if not fire_id:
        return None
    import sqlite3 as _sq
    conn = _sq.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT status, realized_r, pnl_usd, legs_hit FROM paper_trades "
            "WHERE fire_id=? ORDER BY id DESC LIMIT 1", (fire_id,)).fetchone()
        if not row:
            return None
        return {"status": row[0], "realized_r": row[1], "pnl_usd": row[2],
                "legs_hit": row[3] or ""}
    finally:
        conn.close()


# ===========================================================================
# v18-D: 等待觸發狀態機（waiting → signal → open；waiting 6h 未觸 → expired）
# ===========================================================================
def get_waiting_trades() -> list[dict]:
    """所有 status='waiting' 的訊號（等價格回到進場區）"""
    init_db()
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, symbol, setup, direction, entry_price, stop_price, "
            "tp1, tp2, tp3, entry_at, fire_id, tags "
            "FROM trades WHERE status='waiting' ORDER BY entry_at",
        ).fetchall()
        return [{"id": r[0], "symbol": r[1], "setup": r[2], "direction": r[3],
                 "entry_price": r[4], "stop_price": r[5],
                 "tp1": r[6], "tp2": r[7], "tp3": r[8],
                 "entry_at": r[9], "fire_id": r[10], "tags": r[11] or ""}
                for r in rows]
    finally:
        conn.close()


def trigger_waiting(trade_id: int) -> dict:
    """價格回到進場區：waiting → signal（entry_at 重置，4h 按鈕窗口重新起算）"""
    init_db()
    conn = _conn()
    try:
        now_ms = int(time.time() * 1000)
        cur = conn.execute(
            "UPDATE trades SET status='signal', entry_at=?, updated_at=?, "
            "triggered_at=? WHERE id=? AND status='waiting'",   # v23-3: 觸發時點
            (now_ms, now_ms, now_ms, trade_id),
        )
        if cur.rowcount == 0:
            return {"ok": False, "msg": "not in waiting state"}
        return {"ok": True}
    finally:
        conn.close()


def expire_stale_waiting(max_age_hours: float = 6.0) -> list[dict]:
    """超過 N 小時價格沒回進場區的 waiting → expired"""
    init_db()
    conn = _conn()
    try:
        cutoff_ms = int(time.time() * 1000) - int(max_age_hours * 3600 * 1000)
        rows = conn.execute(
            "SELECT id, symbol, direction FROM trades "
            "WHERE status='waiting' AND entry_at < ?", (cutoff_ms,),
        ).fetchall()
        if rows:
            conn.execute(
                "UPDATE trades SET status='expired', updated_at=? "
                "WHERE status='waiting' AND entry_at < ?",
                (int(time.time() * 1000), cutoff_ms),
            )
        return [{"id": r[0], "symbol": r[1], "direction": r[2]} for r in rows]
    finally:
        conn.close()


def get_pending_signals() -> list[dict]:
    """所有等待確認的訊號（/status 儀表板用）"""
    init_db()
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, symbol, setup, direction, entry_price, entry_at, fire_id "
            "FROM trades WHERE status='signal' ORDER BY entry_at DESC",
        ).fetchall()
        return [{"id": r[0], "symbol": r[1], "setup": r[2], "direction": r[3],
                 "entry_price": r[4], "entry_at": r[5], "fire_id": r[6]} for r in rows]
    finally:
        conn.close()


# ===========================================================================
# 寫入：平倉（完全 / 部分）
# ===========================================================================
def record_leg(trade_id: int, leg_label: str, size_pct: float,
               exit_price: float, exit_at_ms: int | None = None) -> dict:
    """記錄一段 partial close（TP1/TP2/TP3/stop/manual）。

    自動算 pnl_usd 與 realized_r。
    所有 leg size_pct 累計達 1.0 後自動 mark trade as closed。
    """
    init_db()
    conn = _conn()
    try:
        if exit_at_ms is None:
            exit_at_ms = int(time.time() * 1000)

        # 拿 trade 原始資料（v15: 加 status 守衛 — 只有確認過的持倉能平倉）
        trade = conn.execute(
            "SELECT direction, entry_price, stop_price, risk_usd, status "
            "FROM trades WHERE id=?",
            (trade_id,),
        ).fetchone()
        if not trade:
            raise ValueError(f"trade {trade_id} not found")
        direction, entry_price, stop_price, risk_usd, status = trade
        if status != "open":
            raise ValueError(
                f"trade {trade_id} status={status}, only 'open' can record legs "
                f"(signal 未確認/已略過的訊號不能平倉)")
        sl_distance = abs(entry_price - stop_price)

        # 算 leg R
        if direction == "bull":
            leg_r = (exit_price - entry_price) / sl_distance
        else:
            leg_r = (entry_price - exit_price) / sl_distance

        # 算 leg PnL：size_pct × 風險 × leg_r
        leg_pnl = size_pct * risk_usd * leg_r

        conn.execute(
            """INSERT INTO trade_legs (
                trade_id, leg_label, size_pct, exit_price, exit_at,
                pnl_usd, realized_r
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (trade_id, leg_label, size_pct, exit_price, exit_at_ms,
             leg_pnl, leg_r),
        )

        # 算累計 size_pct
        total_pct, total_pnl = conn.execute(
            "SELECT COALESCE(SUM(size_pct), 0), COALESCE(SUM(pnl_usd), 0) "
            "FROM trade_legs WHERE trade_id=?",
            (trade_id,),
        ).fetchone()

        # 累計達 1.0 → 標 closed
        if total_pct >= 0.999:
            # 算加權出場價
            wavg_exit = conn.execute(
                "SELECT SUM(exit_price * size_pct) / SUM(size_pct) "
                "FROM trade_legs WHERE trade_id=?",
                (trade_id,),
            ).fetchone()[0]
            realized_r = total_pnl / risk_usd
            conn.execute(
                """UPDATE trades SET
                    status='closed', exit_at=?, exit_price=?, exit_reason=?,
                    pnl_usd=?, realized_r=?, updated_at=?
                WHERE id=?""",
                (exit_at_ms, wavg_exit, leg_label if total_pct < 1.001 else "mixed",
                 total_pnl, realized_r, exit_at_ms, trade_id),
            )
            # 更新當日 PnL 快照
            _update_daily_pnl(conn, exit_at_ms, total_pnl, leg_r > 0)

        return {
            "trade_id": trade_id,
            "leg_label": leg_label,
            "leg_pnl_usd": round(leg_pnl, 2),
            "leg_r": round(leg_r, 3),
            "cumulative_pct": round(total_pct, 2),
            "cumulative_pnl_usd": round(total_pnl, 2),
            "trade_status": "closed" if total_pct >= 0.999 else "open",
        }
    finally:
        conn.close()


def record_full_exit(trade_id: int, exit_price: float, exit_reason: str = "manual",
                     exit_at_ms: int | None = None) -> dict:
    """直接完全平倉（不分批）。"""
    return record_leg(trade_id, exit_reason, 1.0, exit_price, exit_at_ms)


def cancel_trade(trade_id: int, reason: str = "cancelled_by_user") -> None:
    """取消（未進場就 cancel）"""
    conn = _conn()
    try:
        conn.execute(
            "UPDATE trades SET status='cancelled', notes=?, updated_at=? WHERE id=?",
            (reason, int(time.time() * 1000), trade_id),
        )
    finally:
        conn.close()


# ===========================================================================
# 統計查詢
# ===========================================================================
def get_stats(days: int = 7, setup: str | None = None,
              symbol: str | None = None) -> dict:
    """統計 N 天內表現。

    Returns:
        {
            "window_days": N,
            "n_trades_total": ...,
            "n_trades_closed": ...,
            "n_trades_open": ...,
            "n_wins": ...,
            "n_losses": ...,
            "win_rate_pct": ...,
            "total_pnl_usd": ...,
            "avg_r": ...,
            "best_r": ..., "worst_r": ...,
            "max_consecutive_losses": ...,
            "max_drawdown_usd": ...,
            "by_setup": {setup: stats},
            "by_symbol": {symbol: stats},
        }
    """
    init_db()
    conn = _conn()
    try:
        cutoff_ms = int(time.time() * 1000) - days * 86400 * 1000
        sql = "SELECT * FROM trades WHERE entry_at >= ?"
        args: list = [cutoff_ms]
        if setup:
            sql += " AND setup=?"
            args.append(setup)
        if symbol:
            sql += " AND symbol=?"
            args.append(symbol)
        sql += " ORDER BY entry_at"
        rows = conn.execute(sql, args).fetchall()
        cols = [d[0] for d in conn.execute(sql, args).description]
        trades = [dict(zip(cols, r)) for r in rows]

        if not trades:
            return {
                "window_days": days, "n_trades_total": 0,
                "n_trades_closed": 0, "n_trades_open": 0,
                "n_wins": 0, "n_losses": 0, "win_rate_pct": 0.0,
                "total_pnl_usd": 0.0, "avg_r": 0.0,
                "best_r": 0.0, "worst_r": 0.0,
                "max_consecutive_losses": 0, "max_drawdown_usd": 0.0,
                "by_setup": {}, "by_symbol": {},
            }

        closed = [t for t in trades if t["status"] == "closed"]
        opens = [t for t in trades if t["status"] == "open"]
        # v15: cancelled 涵蓋 skipped/expired（未成交訊號，不汙染勝率統計）
        cancelled = [t for t in trades if t["status"] in ("cancelled", "skipped", "expired")]
        signals_pending = [t for t in trades if t["status"] == "signal"]

        wins = [t for t in closed if (t.get("realized_r") or 0) > 0]
        losses = [t for t in closed if (t.get("realized_r") or 0) < 0]
        scratch = [t for t in closed if (t.get("realized_r") or 0) == 0]

        total_pnl = sum(t.get("pnl_usd") or 0 for t in closed)
        rs = [t.get("realized_r") or 0 for t in closed]

        # 連虧
        max_consec, cur_consec = 0, 0
        for t in closed:
            if (t.get("realized_r") or 0) < 0:
                cur_consec += 1
                max_consec = max(max_consec, cur_consec)
            else:
                cur_consec = 0

        # 回撤
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in closed:
            equity += t.get("pnl_usd") or 0
            peak = max(peak, equity)
            max_dd = min(max_dd, equity - peak)

        # by setup / symbol
        def _group_stats(items):
            grp = {}
            for t in items:
                k = t["setup"] if items is closed else t["symbol"]
                grp.setdefault(k, []).append(t)
            out = {}
            for k, ts in grp.items():
                tw = [t for t in ts if (t.get("realized_r") or 0) > 0]
                pnl = sum(t.get("pnl_usd") or 0 for t in ts)
                out[k] = {
                    "trades": len(ts),
                    "wins": len(tw),
                    "win_rate": len(tw) / len(ts) * 100 if ts else 0,
                    "total_pnl_usd": round(pnl, 2),
                    "avg_r": round(mean([t.get("realized_r") or 0 for t in ts]), 3) if ts else 0,
                }
            return out

        by_setup = {}
        for t in closed:
            k = t["setup"]
            by_setup.setdefault(k, []).append(t)
        by_setup_stats = {k: {
            "trades": len(ts),
            "wins": sum(1 for x in ts if (x.get("realized_r") or 0) > 0),
            "win_rate": sum(1 for x in ts if (x.get("realized_r") or 0) > 0) / len(ts) * 100,
            "total_pnl_usd": round(sum(x.get("pnl_usd") or 0 for x in ts), 2),
            "avg_r": round(mean([x.get("realized_r") or 0 for x in ts]), 3),
        } for k, ts in by_setup.items()}

        by_sym = {}
        for t in closed:
            by_sym.setdefault(t["symbol"], []).append(t)
        by_sym_stats = {k: {
            "trades": len(ts),
            "wins": sum(1 for x in ts if (x.get("realized_r") or 0) > 0),
            "win_rate": sum(1 for x in ts if (x.get("realized_r") or 0) > 0) / len(ts) * 100,
            "total_pnl_usd": round(sum(x.get("pnl_usd") or 0 for x in ts), 2),
        } for k, ts in by_sym.items()}

        return {
            "window_days": days,
            # v15: n_trades_total 只算真實交易（open+closed），訊號/略過不汙染
            "n_trades_total": len(closed) + len(opens),
            "n_trades_closed": len(closed),
            "n_trades_open": len(opens),
            "n_trades_cancelled": len(cancelled),
            "n_signals_pending": len(signals_pending),
            "n_wins": len(wins),
            "n_losses": len(losses),
            "n_scratch": len(scratch),
            "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else 0,
            "total_pnl_usd": round(total_pnl, 2),
            "avg_r": round(mean(rs), 3) if rs else 0,
            "best_r": round(max(rs), 3) if rs else 0,
            "worst_r": round(min(rs), 3) if rs else 0,
            "max_consecutive_losses": max_consec,
            "max_drawdown_usd": round(max_dd, 2),
            "profit_factor": (sum(t.get("pnl_usd") or 0 for t in wins) /
                             abs(sum(t.get("pnl_usd") or 0 for t in losses))
                             if losses else float("inf") if wins else 0),
            "by_setup": by_setup_stats,
            "by_symbol": by_sym_stats,
        }
    finally:
        conn.close()


# ===========================================================================
# Daily PnL（給 risk_manager 熔斷判斷）
# ===========================================================================
def _update_daily_pnl(conn, exit_at_ms: int, pnl: float, is_win: bool) -> None:
    """每筆平倉自動更新當日 PnL 快照"""
    date_utc = dt.datetime.fromtimestamp(exit_at_ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%d")
    now_ms = int(time.time() * 1000)
    existing = conn.execute(
        "SELECT total_pnl_usd, n_trades_closed, n_wins, n_losses "
        "FROM daily_pnl WHERE date_utc=?", (date_utc,)
    ).fetchone()
    if existing:
        new_pnl = existing[0] + pnl
        new_n = existing[1] + 1
        new_w = existing[2] + (1 if is_win else 0)
        new_l = existing[3] + (0 if is_win else 1)
        conn.execute(
            "UPDATE daily_pnl SET total_pnl_usd=?, n_trades_closed=?, "
            "n_wins=?, n_losses=?, updated_at=? WHERE date_utc=?",
            (new_pnl, new_n, new_w, new_l, now_ms, date_utc),
        )
    else:
        conn.execute(
            "INSERT INTO daily_pnl (date_utc, total_pnl_usd, n_trades_closed, "
            "n_wins, n_losses, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (date_utc, pnl, 1, 1 if is_win else 0, 0 if is_win else 1, now_ms),
        )


def get_today_pnl(account_balance_usd: float | None = None) -> dict:
    """拿今日 PnL（給熔斷判斷用）"""
    if account_balance_usd is None:
        from botconfig import CONFIG  # v42: 單一來源（含 override、依預算分級）
        account_balance_usd = CONFIG.account_balance_usd
    init_db()
    conn = _conn()
    try:
        today = dt.datetime.now(tz=dt.timezone.utc).strftime("%Y-%m-%d")
        row = conn.execute(
            "SELECT total_pnl_usd, n_trades_closed, n_wins, n_losses "
            "FROM daily_pnl WHERE date_utc=?", (today,)
        ).fetchone()
        if not row:
            return {"date_utc": today, "total_pnl_usd": 0,
                    "n_trades_closed": 0, "n_wins": 0, "n_losses": 0,
                    "pnl_pct_of_account": 0.0}
        pnl_pct = (row[0] / account_balance_usd * 100) if account_balance_usd else 0
        return {
            "date_utc": today,
            "total_pnl_usd": round(row[0], 2),
            "n_trades_closed": row[1],
            "n_wins": row[2],
            "n_losses": row[3],
            "pnl_pct_of_account": round(pnl_pct, 2),
        }
    finally:
        conn.close()


def get_week_pnl(account_balance_usd: float | None = None) -> dict:
    """拿本週累計 PnL"""
    if account_balance_usd is None:
        from botconfig import CONFIG  # v42: 單一來源（含 override、依預算分級）
        account_balance_usd = CONFIG.account_balance_usd
    init_db()
    conn = _conn()
    try:
        now = dt.datetime.now(tz=dt.timezone.utc)
        # 本週一 00:00 UTC
        monday = now - dt.timedelta(days=now.weekday())
        monday_str = monday.strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT SUM(total_pnl_usd), SUM(n_trades_closed), SUM(n_wins), SUM(n_losses) "
            "FROM daily_pnl WHERE date_utc >= ?", (monday_str,)
        ).fetchone()
        pnl = rows[0] or 0
        pnl_pct = (pnl / account_balance_usd * 100) if account_balance_usd else 0
        return {
            "week_start_utc": monday_str,
            "total_pnl_usd": round(pnl, 2),
            "n_trades_closed": rows[1] or 0,
            "n_wins": rows[2] or 0,
            "n_losses": rows[3] or 0,
            "pnl_pct_of_account": round(pnl_pct, 2),
        }
    finally:
        conn.close()


def count_opens_today() -> int:
    """今日（UTC）實際開倉筆數（status in open/closed，依 entry_at 計）。

    給 risk_manager「每日最多開倉次數」閘門用 — 防情緒性連續開倉。
    只計真正成為部位的（open/closed）；未確認的 signal / 略過 / 取消不計。
    """
    init_db()
    conn = _conn()
    try:
        now = dt.datetime.now(tz=dt.timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_ms = int(start.timestamp() * 1000)
        row = conn.execute(
            "SELECT COUNT(*) FROM trades "
            "WHERE entry_at >= ? AND status IN ('open','closed')",
            (start_ms,),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    finally:
        conn.close()


# ===========================================================================
# v41: 紀律遵守率 KPI（task #8 ⑥）— 全部來自可觀測欄位，零臆測
# ===========================================================================
DISCIPLINE_MIN_SAMPLE = 5   # 每分項至少 N 筆才報百分比，否則「資料累積中」


def discipline_stats(days: int = 30) -> dict:
    """紀律遵守率：兩個客觀指標，皆由 trades 表直接觀測得出（不靠自我感覺）。

    A. 決斷率 decisiveness：對推送並已到終局的訊號，有意識處理（已下單 open/closed
       或 主動略過 skipped）占 (處理 + 放生過期 expired) 的比例。
       放生過期 = 看到訊號卻不做任何決定 = 不紀律。
    B. 不追高率 no_chase：真正成為部位的單（open/closed）中，在計畫進場區內成交
       （entry_kind direct_fire / wait_trigger）占全部進場（含 market_chase 追高）的比例。
       market_chase = 成交價落在計畫進場區外 = 追高 = 不紀律。

    分項樣本不足（< DISCIPLINE_MIN_SAMPLE）時對應 *_pct=None（顯示「資料累積中」），
    絕不用小樣本充當有意義的百分比。
    """
    init_db()
    conn = _conn()
    try:
        cutoff_ms = int(time.time() * 1000) - days * 86400 * 1000
        rows = conn.execute(
            "SELECT status, COALESCE(entry_kind, 'direct_fire') FROM trades "
            "WHERE created_at >= ?", (cutoff_ms,),
        ).fetchall()
    finally:
        conn.close()

    # A. 決斷率
    acted = sum(1 for s, _ in rows if s in ("open", "closed", "skipped"))
    ghosted = sum(1 for s, _ in rows if s == "expired")
    dec_n = acted + ghosted
    decisiveness = round(acted / dec_n * 100, 1) if dec_n >= DISCIPLINE_MIN_SAMPLE else None

    # B. 不追高率（只看真正成為部位的單）
    entries = [(s, k) for s, k in rows if s in ("open", "closed")]
    in_zone = sum(1 for _, k in entries if k in ("direct_fire", "wait_trigger"))
    chased = sum(1 for _, k in entries if k == "market_chase")
    nz_n = in_zone + chased
    no_chase = round(in_zone / nz_n * 100, 1) if nz_n >= DISCIPLINE_MIN_SAMPLE else None

    parts = [r for r in (decisiveness, no_chase) if r is not None]
    overall = round(sum(parts) / len(parts), 1) if parts else None

    return {
        "window_days": days,
        "decisiveness_pct": decisiveness, "acted": acted, "ghosted": ghosted,
        "no_chase_pct": no_chase, "in_zone": in_zone, "chased": chased,
        "overall_pct": overall, "min_sample": DISCIPLINE_MIN_SAMPLE,
    }


def _disc_grade(pct: float | None) -> str:
    if pct is None:
        return "—"
    if pct >= 90:
        return "🟢 優秀"
    if pct >= 75:
        return "🟡 良好"
    if pct >= 60:
        return "🟠 待加強"
    return "🔴 需警惕"


def render_discipline(d: dict) -> str:
    """文字化紀律 KPI（給 Telegram /discipline 與 CEO 簡報引用）。"""
    lines = [f"🎯 <b>紀律遵守率（近 {d['window_days']} 天）</b>",
             "━━━━━━━━━━━━━━━━"]
    if d["overall_pct"] is None:
        lines.append("資料累積中 —— 等你實際處理訊號／進場後才有足夠樣本可評分。")
        lines.append(f"<i>目前：決斷 {d['acted']} 處理 / {d['ghosted']} 放生　"
                     f"進場 {d['in_zone']} 區內 / {d['chased']} 追高</i>")
        lines.append("\n<i>紀律比勝率更早決定小資能不能活下來。</i>")
        return "\n".join(lines)

    lines.append(f"綜合：<b>{d['overall_pct']}%</b>　{_disc_grade(d['overall_pct'])}")
    if d["decisiveness_pct"] is not None:
        lines.append(f"• 決斷率 <code>{d['decisiveness_pct']}%</code>"
                     f"（{d['acted']} 有意識處理 / {d['ghosted']} 放生過期）"
                     f"　{_disc_grade(d['decisiveness_pct'])}")
    else:
        lines.append(f"• 決斷率：資料累積中（{d['acted']}/{d['ghosted']}）")
    if d["no_chase_pct"] is not None:
        lines.append(f"• 不追高率 <code>{d['no_chase_pct']}%</code>"
                     f"（{d['in_zone']} 區內進場 / {d['chased']} 追高）"
                     f"　{_disc_grade(d['no_chase_pct'])}")
    else:
        lines.append(f"• 不追高率：資料累積中（{d['in_zone']}/{d['chased']}）")
    lines.append("\n<i>兩項皆由系統客觀記錄，不靠自我感覺。紀律比勝率更早決定小資存活。</i>")
    return "\n".join(lines)


# ===========================================================================
# 取所有 open trades（給 risk_manager 看當前曝險）
# ===========================================================================
def get_open_trades() -> list[dict]:
    """回所有 status='open' 的 trades，附帶 tp1/2/3、已 hit legs、tg_message_id。"""
    init_db()
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, symbol, setup, direction, entry_price, stop_price, "
            "tp1, tp2, tp3, risk_usd, leverage, margin_usd, entry_at, tg_message_id, "
            "entry_kind "
            "FROM trades WHERE status='open' ORDER BY entry_at"
        ).fetchall()
        out = []
        for r in rows:
            trade_id = r[0]
            legs = conn.execute(
                "SELECT leg_label, size_pct FROM trade_legs WHERE trade_id=?",
                (trade_id,),
            ).fetchall()
            hit_labels = {leg[0] for leg in legs}
            total_size_hit = sum(leg[1] for leg in legs)
            out.append({
                "id": trade_id, "symbol": r[1], "setup": r[2], "direction": r[3],
                "entry_price": r[4], "stop_price": r[5],
                "tp1": r[6], "tp2": r[7], "tp3": r[8],
                "risk_usd": r[9], "leverage": r[10], "margin_usd": r[11],
                "entry_at": r[12], "tg_message_id": r[13],
                "entry_kind": r[14] or "direct_fire",   # v41: 教練追高偵測用
                "legs_hit": list(hit_labels),
                "size_remaining": round(1.0 - total_size_hit, 3),
            })
        return out
    finally:
        conn.close()


def render_stats_summary(stats: dict, label: str = "") -> str:
    """文字化統計報告（給 Telegram 推送用）"""
    if stats["n_trades_total"] == 0:
        return f"📊 {label}：過去 {stats['window_days']} 天無交易紀錄"

    lines = [
        f"📊 <b>{label} (過去 {stats['window_days']} 天)</b>",
        f"━━━━━━━━━━━━━━━━",
        f"總筆數：<code>{stats['n_trades_total']}</code> "
        f"（已平 {stats['n_trades_closed']} / 持倉 {stats['n_trades_open']} / 取消 {stats.get('n_trades_cancelled', 0)}）",
    ]
    if stats["n_trades_closed"] > 0:
        pf = stats.get("profit_factor", 0)
        pf_str = f"{pf:.2f}" if pf != float("inf") else "∞"
        lines.append(
            f"勝率：<code>{stats['win_rate_pct']}%</code> "
            f"（{stats['n_wins']} 勝 / {stats['n_losses']} 負 / {stats['n_scratch']} 平）"
        )
        # v22-3: R 倍數為主、金額為輔（開源訊號慣例 — 任何本金都能直接套用）
        lines.append(f"期望值：<code>{stats['avg_r']:+.3f}R</code>/筆  "
                     f"獲利因子：<code>{pf_str}</code>")
        lines.append(f"最佳：<code>{stats['best_r']:+.2f}R</code>  "
                     f"最差：<code>{stats['worst_r']:+.2f}R</code>  "
                     f"最大連虧：<code>{stats['max_consecutive_losses']}</code>")
        lines.append(f"<i>金額參考（單筆風險 100U 基準）："
                     f"PnL <code>${stats['total_pnl_usd']:+.2f}</code>  "
                     f"最大回撤 <code>${stats['max_drawdown_usd']:.2f}</code></i>")

        if stats["by_setup"]:
            lines.append("\n<b>各 setup 表現：</b>")
            for setup_name, st in stats["by_setup"].items():
                lines.append(f"  • {setup_name}: {st['trades']} 筆 / {st['win_rate']:.0f}% 勝 / ${st['total_pnl_usd']:+.0f}")

        if stats["by_symbol"]:
            lines.append("\n<b>各標的表現：</b>")
            for sym, st in sorted(stats["by_symbol"].items(), key=lambda x: -x[1]["total_pnl_usd"]):
                lines.append(f"  • {sym}: {st['trades']} 筆 / {st['win_rate']:.0f}% 勝 / ${st['total_pnl_usd']:+.0f}")

    return "\n".join(lines)


if __name__ == "__main__":
    # 自測：建表 + 模擬一筆完整流程
    init_db()
    print(f"DB initialized at {DB_PATH}")

    # 模擬一筆 trade
    e = EntryRecord(
        symbol="BTC", setup="intraday", direction="bull",
        entry_price=65000.0, stop_price=63700.0,
        tp1=66300.0, tp2=66950.0, tp3=67600.0,
        risk_usd=100.0, leverage=15, margin_usd=333.0, notional_usd=5000.0,
    )
    tid = record_entry(e)
    print(f"recorded entry, trade_id={tid}")

    # 模擬 TP1 觸及
    r1 = record_leg(tid, "tp1", 0.33, 66300.0)
    print(f"TP1 hit: {r1}")
    # 模擬 TP2 觸及
    r2 = record_leg(tid, "tp2", 0.33, 66950.0)
    print(f"TP2 hit: {r2}")
    # 模擬 TP3 觸及（剩餘 33% + 微調至 100%）
    r3 = record_leg(tid, "tp3", 0.34, 67600.0)
    print(f"TP3 hit: {r3}")

    # 7d 統計
    stats = get_stats(7)
    print(f"\n7d stats: {stats}")
    print(f"\nRendered:\n{render_stats_summary(stats, label='測試帳戶')}")

    # 今日 PnL
    today = get_today_pnl()
    print(f"\nToday PnL: {today}")
