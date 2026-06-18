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

import json
import os
import sqlite3
import time

from botpaths import db_path as _db_path

DB_PATH = _db_path("trade_journal.db")

# v23-2: 與實倉同源（botconfig）— 紙上 1R 跟著用戶設定走
from botconfig import CONFIG as _CFG

RISK_USD = _CFG.risk_per_trade_usd  # 紙上 1R，與訊號設計一致


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
        # v26 idempotent migration：分批進場追蹤
        existing = {r[1] for r in conn.execute("PRAGMA table_info(paper_trades)").fetchall()}
        for col, ddl in (
            # entry_splits JSON: [{"price":x,"frac":0.7,"filled":0,"filled_at":null}]
            ("entry_splits", "ALTER TABLE paper_trades ADD COLUMN entry_splits TEXT"),
            ("entry_filled_pct", "ALTER TABLE paper_trades ADD COLUMN entry_filled_pct REAL NOT NULL DEFAULT 1.0"),
            ("entry_state", "ALTER TABLE paper_trades ADD COLUMN entry_state TEXT NOT NULL DEFAULT 'full'"),
            #   'pending'(掛單未成) / 'partial'(部分成交) / 'full'(全部成交)
            # v33: 發訊號當下的 Telegram message_id，供持倉訊息回連原始訊號
            ("signal_msg_id", "ALTER TABLE paper_trades ADD COLUMN signal_msg_id INTEGER"),
        ):
            if col not in existing:
                conn.execute(ddl)
    finally:
        conn.close()


# v26: 預設兩段限價分批（較近價位 60%、較遠價位 40%）
ENTRY_SPLIT_FRACS = (0.6, 0.4)


def _compute_entry_splits(direction: str, zone_lo: float, zone_hi: float) -> list[dict]:
    """從進場區算分批限價。
    long: 先填較高價（zone_hi，價跌入區先成），再填較低價（zone_lo）。
    short: 對稱。回 [{price, frac, filled, filled_at}]。"""
    if direction == "bull":
        prices = [zone_hi, zone_lo]
    else:
        prices = [zone_lo, zone_hi]
    return [{"price": round(p, 8), "frac": f, "filled": 0, "filled_at": None}
            for p, f in zip(prices, ENTRY_SPLIT_FRACS)]


# =====================================================================
# v47-2 post-close 冷卻：修「同幣同向同 setup 剛平倉就立刻重開同樣的單」重複單
# ---------------------------------------------------------------------
# 為什麼既有兩道閘擋不住這種重複：
#   • symbol_gate 的送出窗預設 1h << 持倉動輒 24–48h，且「平倉」時不會刷新它，
#     於是「timeout 平倉 → 十幾分鐘後同向同止損重開」這種貼身重複漏網。
#   • deepdive 的 open_paper_symbols 只擋「still-open」，已平倉的舊單看不到。
# 設計刻意保守，只有「全部硬條件成立」才擋，絕不誤殺正當新訊號：
#   ① 最近一筆同 (symbol, direction, setup) 的『已平倉』trade
#   ② exit_reason ∈ 白名單(timeout/stop/tp1/tp2/tp3)——entry_expired(掛單從未成交)不算，
#      重掛限價是正當的
#   ③ 平倉距今 < POST_CLOSE_COOLDOWN_S（預設 6h，可 .env 覆寫）
#   ④ 新止損與舊止損近似（相對差 < POST_CLOSE_STOP_EPS，預設 0.5%）——硬性 AND 條件，
#      差很多代表新論述/新結構，放行（如 BTC id21→id39 止損差 1.42% 是正當再進場）
# 方向化：只比同方向 → 反轉訊號(bull↔bear)天然放行（對持倉出場有參考價值）。
# setup 化：只比同 setup → 跨引擎(deepdive vs us_breakout)天然不互擋。
_POST_CLOSE_DEFAULT_S = 21600          # 6h
_POST_CLOSE_STOP_EPS_DEFAULT = 0.005   # 0.5%
_EXIT_COOLDOWN_WHITELIST = ("timeout", "stop", "tp1", "tp2", "tp3")


def _post_close_cooldown_s() -> int:
    try:
        v = int(float(os.getenv("POST_CLOSE_COOLDOWN_S", str(_POST_CLOSE_DEFAULT_S))))
        return v if v > 0 else _POST_CLOSE_DEFAULT_S
    except Exception:
        return _POST_CLOSE_DEFAULT_S


def _post_close_stop_eps() -> float:
    try:
        v = float(os.getenv("POST_CLOSE_STOP_EPS", str(_POST_CLOSE_STOP_EPS_DEFAULT)))
        return v if v > 0 else _POST_CLOSE_STOP_EPS_DEFAULT
    except Exception:
        return _POST_CLOSE_STOP_EPS_DEFAULT


def _recently_closed_dup(symbol: str, setup: str, direction: str,
                         new_stop: float, *, now_ms: int | None = None) -> dict | None:
    """判斷新單是否為『剛平倉的同向同 setup 同止損單』的貼身重複。

    回 dict（被擋細節，供 log）= 應擋；回 None = 放行。任何 DB 錯誤一律回 None
    （fail-open，絕不因閘故障漏真訊號）。
    """
    if new_stop is None or new_stop <= 0:
        return None
    t = int(now_ms if now_ms is not None else time.time() * 1000)
    window_ms = _post_close_cooldown_s() * 1000
    eps = _post_close_stop_eps()
    try:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT id, stop_price, exit_reason, exit_at FROM paper_trades "
                "WHERE symbol=? AND setup=? AND direction=? AND status='closed' "
                "AND exit_at IS NOT NULL "
                "ORDER BY exit_at DESC LIMIT 1",
                (symbol, setup, direction),
            ).fetchone()
            if not row:
                return None
            last_id, last_stop, exit_reason, exit_at = row
            if (exit_reason or "") not in _EXIT_COOLDOWN_WHITELIST:
                return None                       # entry_expired 等 → 重下正當
            if exit_at is None or (t - exit_at) >= window_ms:
                return None                       # 已過冷卻窗 → 放行
            if not last_stop or last_stop <= 0:
                return None
            stop_diff = abs(new_stop - last_stop) / last_stop
            if stop_diff >= eps:
                return None                       # 止損差很多 → 新論述，放行
            return {"last_id": last_id, "exit_reason": exit_reason,
                    "age_s": int((t - exit_at) / 1000), "stop_diff": stop_diff}
        finally:
            conn.close()
    except Exception:
        return None


def record_paper_entry(symbol: str, setup: str, direction: str,
                       entry_price: float, stop_price: float,
                       tp1: float, tp2: float, tp3: float,
                       fire_id: int | None = None,
                       regime: str | None = None,
                       zone_lo: float | None = None,
                       zone_hi: float | None = None,
                       split_mode: bool = False,
                       signal_msg_id: int | None = None,
                       skip_cooldown: bool = False) -> int:
    """v26: split_mode=True 時建分批限價單（entry_state='pending'，等價格逐格成交）；
    否則維持原行為（直接全額成交，entry_state='full'）。

    v47-2: 進場前先過 post-close 冷卻去重（見 _recently_closed_dup）。
    skip_cooldown=True 豁免（waiting-trigger 觸發是先前已承諾的等待單兌現，非新單）。
    被擋時回 -1（不建單）；呼叫端只把回傳值用於 log，回 -1 安全。"""
    init_db()
    if not skip_cooldown:
        dup = _recently_closed_dup(symbol, setup, direction, stop_price)
        if dup is not None:
            print(f"[paper] {symbol}/{setup}/{direction} 跳過建單：post-close 冷卻中"
                  f"（{dup['age_s']}s 前以 {dup['exit_reason']} 平倉、止損僅差 "
                  f"{dup['stop_diff']*100:.2f}% < {_post_close_stop_eps()*100:.2f}% 門檻，"
                  f"視為重複單；舊 id={dup['last_id']}）")
            return -1
    conn = _conn()
    try:
        now_ms = int(time.time() * 1000)
        if split_mode and zone_lo is not None and zone_hi is not None:
            splits = _compute_entry_splits(direction, zone_lo, zone_hi)
            cur = conn.execute(
                """INSERT INTO paper_trades
                   (symbol, setup, direction, entry_price, stop_price, tp1, tp2, tp3,
                    entry_at, fire_id, regime, created_at,
                    entry_splits, entry_filled_pct, entry_state, signal_msg_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0, 'pending', ?)""",
                (symbol, setup, direction, entry_price, stop_price, tp1, tp2, tp3,
                 now_ms, fire_id, regime, now_ms, json.dumps(splits), signal_msg_id),
            )
        else:
            cur = conn.execute(
                """INSERT INTO paper_trades
                   (symbol, setup, direction, entry_price, stop_price, tp1, tp2, tp3,
                    entry_at, fire_id, regime, created_at, entry_filled_pct, entry_state,
                    signal_msg_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0, 'full', ?)""",
                (symbol, setup, direction, entry_price, stop_price, tp1, tp2, tp3,
                 now_ms, fire_id, regime, now_ms, signal_msg_id),
            )
        return cur.lastrowid
    finally:
        conn.close()


def get_pending_entries() -> list[dict]:
    """回所有尚未全部成交的分批單（entry_state in pending/partial）。"""
    init_db()
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, symbol, direction, entry_splits, entry_filled_pct, entry_state, "
            "entry_at, setup FROM paper_trades WHERE status='open' AND entry_state IN ('pending','partial')"
        ).fetchall()
        return [{"id": r[0], "symbol": r[1], "direction": r[2],
                 "splits": json.loads(r[3]) if r[3] else [],
                 "filled_pct": r[4], "entry_state": r[5], "entry_at": r[6],
                 "setup": r[7]} for r in rows]
    finally:
        conn.close()


def apply_entry_fill(paper_id: int, live_price: float) -> dict | None:
    """檢查分批單在現價下有哪些格成交。回 {newly_filled:[...], filled_pct, state} 或 None（無變化）。"""
    init_db()
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT direction, entry_splits, entry_state FROM paper_trades "
            "WHERE id=? AND status='open'", (paper_id,)).fetchone()
        if not row or not row[1]:
            return None
        direction, splits_json, state = row
        if state == "full":
            return None
        splits = json.loads(splits_json)
        now_ms = int(time.time() * 1000)
        newly = []
        for s in splits:
            if s["filled"]:
                continue
            hit = (live_price <= s["price"]) if direction == "bull" else (live_price >= s["price"])
            if hit:
                s["filled"] = 1
                s["filled_at"] = now_ms
                newly.append(s)
        if not newly:
            return None
        filled_pct = round(sum(s["frac"] for s in splits if s["filled"]), 4)
        new_state = "full" if filled_pct >= 0.999 else "partial"
        conn.execute(
            "UPDATE paper_trades SET entry_splits=?, entry_filled_pct=?, entry_state=? WHERE id=?",
            (json.dumps(splits), filled_pct, new_state, paper_id))
        return {"newly_filled": newly, "filled_pct": filled_pct, "state": new_state}
    finally:
        conn.close()


def expire_pending(paper_id: int) -> bool:
    """v33: 掛單逾時作廢 — 從未成交（entry_state='pending'，0% filled）的分批限價單
    超過時限，標記為 status='closed'、exit_reason='entry_expired'、0R，避免未成交掛單
    永久佔用 open/pending 計數。
    SQL 內建 entry_state='pending' 護欄：partial/full 一律不碰（交給 TP/SL/timeout 流程）。
    回 True 表示確實作廢了一筆（找不到或非 pending 則回 False）。"""
    init_db()
    conn = _conn()
    try:
        now_ms = int(time.time() * 1000)
        cur = conn.execute(
            "UPDATE paper_trades SET status='closed', exit_reason='entry_expired', "
            "realized_r=0, pnl_usd=0, size_remaining=0, exit_at=? "
            "WHERE id=? AND status='open' AND entry_state='pending'",
            (now_ms, paper_id))
        return cur.rowcount > 0
    finally:
        conn.close()


def open_paper_symbols(setup: str | None = None) -> set:
    """v33：回目前 open（含 pending 等待觸發）的紙上倉位 symbol 集合，供 deepdive 去重。"""
    init_db()
    conn = _conn()
    try:
        if setup:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM paper_trades WHERE status='open' AND setup=?",
                (setup,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM paper_trades WHERE status='open'").fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def get_open_paper() -> list[dict]:
    """回所有 open 紙上倉位，dict 形狀與 trade_monitor._check_trade 相容。"""
    init_db()
    conn = _conn()
    try:
        # v26: 只對「已成交（部分或全部）」的單檢 TP/SL；pending（掛單未成）不檢
        rows = conn.execute(
            "SELECT id, symbol, setup, direction, entry_price, stop_price, "
            "tp1, tp2, tp3, entry_at, legs_hit, size_remaining, entry_filled_pct, "
            "signal_msg_id "
            "FROM paper_trades WHERE status='open' AND entry_state != 'pending' ORDER BY entry_at",
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
                "entry_filled_pct": r[12] if r[12] is not None else 1.0,
                "risk_usd": RISK_USD,
                "tg_message_id": None,
                "signal_msg_id": r[13],
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
            "pnl_usd, status, entry_filled_pct FROM paper_trades WHERE id=?",
            (paper_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"paper trade {paper_id} not found")
        direction, entry, stop, legs_csv, size_rem, pnl, status, filled_pct = row
        if status != "open":
            return {"closed": True, "total_pnl": pnl, "leg_r": 0, "leg_pnl": 0}

        sl_dist = abs(entry - stop)
        leg_r = ((exit_price - entry) if direction == "bull"
                 else (entry - exit_price)) / sl_dist
        # v26: PnL 按實際成交比例縮放（只進 70% 就只賺/賠 70%）
        leg_pnl = size_pct * RISK_USD * leg_r * (filled_pct if filled_pct else 1.0)

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


def get_signals_after(after_id: int, limit: int = 200) -> list[dict]:
    """task #39（OKX 模擬盤操盤手鏡像用）：回 id > after_id 的新紙上訊號，按 id 升冪。
    只回鏡像下單所需欄位（entry/stop/tp/方向/setup/狀態/fire_id）。純讀、無副作用。"""
    init_db()
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, symbol, setup, direction, entry_price, stop_price, "
            "tp1, tp2, tp3, status, entry_state, fire_id, regime, entry_at "
            "FROM paper_trades WHERE id > ? ORDER BY id ASC LIMIT ?",
            (int(after_id), int(limit)),
        ).fetchall()
        return [{"id": r[0], "symbol": r[1], "setup": r[2], "direction": r[3],
                 "entry_price": r[4], "stop_price": r[5],
                 "tp1": r[6], "tp2": r[7], "tp3": r[8],
                 "status": r[9], "entry_state": r[10], "fire_id": r[11],
                 "regime": r[12], "entry_at": r[13]} for r in rows]
    finally:
        conn.close()


def max_paper_id() -> int:
    """目前 paper_trades 最大 id（操盤手首次啟動設高水位用，略過歷史回補）。"""
    init_db()
    conn = _conn()
    try:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM paper_trades").fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def get_paper_stats(days: int = 30, setup: str | None = None,
                    setup_not: str | None = None) -> dict:
    """紙上帳統計（引擎期望值驗證用）。setup 指定時只統計該引擎；
    setup_not 排除指定引擎（v23-2: Stage 0 門檻只算加密，排除 us_breakout）。"""
    init_db()
    conn = _conn()
    try:
        cutoff = int(time.time() * 1000) - days * 86400 * 1000
        sql = "SELECT status, pnl_usd, realized_r, exit_reason FROM paper_trades WHERE entry_at >= ?"
        args: list = [cutoff]
        if setup:
            sql += " AND setup=?"
            args.append(setup)
        if setup_not:
            sql += " AND setup != ?"
            args.append(setup_not)
        rows = conn.execute(sql, args).fetchall()
        # v33: entry_expired（掛單從未成交的逾時作廢）不是真實交易 —
        #   排除於期望值/勝率/Stage1 門檻；它只該出現在漏斗的 never_filled。
        closed = [r for r in rows if r[0] == "closed" and (r[3] or "") != "entry_expired"]
        opens = [r for r in rows if r[0] == "open"]
        wins = [r for r in closed if (r[2] or 0) > 0]
        total_pnl = sum(r[1] or 0 for r in closed)
        rs = [r[2] or 0 for r in closed]
        gain = sum(r for r in rs if r > 0)
        loss = abs(sum(r for r in rs if r < 0))
        pf = (gain / loss) if loss > 0 else (float("inf") if gain > 0 else 0.0)
        return {
            "window_days": days,
            "n_closed": len(closed),
            "n_open": len(opens),
            "n_wins": len(wins),
            "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
            "total_pnl_usd": round(total_pnl, 2),
            "avg_r": round(sum(rs) / len(rs), 3) if rs else 0.0,
            "profit_factor": round(pf, 2) if pf != float("inf") else None,
            "stage0_progress": f"{len(closed)}/100",  # 自動交易 Stage 1 門檻
        }
    finally:
        conn.close()


def get_paper_funnel(days: int = 30, setup_not: str | None = None) -> dict:
    """v26: 訂單漏斗 — 提出/真正進場/未成交/部分倉/止盈止損/進行中。"""
    init_db()
    conn = _conn()
    try:
        cutoff = int(time.time() * 1000) - days * 86400 * 1000
        sql = "SELECT status, entry_state, entry_filled_pct, exit_reason, realized_r FROM paper_trades WHERE entry_at >= ?"
        args: list = [cutoff]
        if setup_not:
            sql += " AND setup != ?"
            args.append(setup_not)
        rows = conn.execute(sql, args).fetchall()
        proposed = len(rows)
        entered = sum(1 for r in rows if (r[2] or 0) > 0)
        never_filled = sum(1 for r in rows if (r[2] or 0) == 0)      # 掛單從未觸及=無效
        partial = sum(1 for r in rows if 0 < (r[2] or 0) < 0.999)
        in_progress = sum(1 for r in rows if r[0] == "open" and (r[2] or 0) > 0)
        # v33: entry_expired 已計入 never_filled（filled_pct==0），不重複算進「已平倉」
        closed = [r for r in rows if r[0] == "closed" and (r[3] or "") != "entry_expired"]
        tp_wins = sum(1 for r in closed if (r[4] or 0) > 0)
        sl_losses = sum(1 for r in closed if (r[4] or 0) < 0)
        timeouts = sum(1 for r in closed if "timeout" in (r[3] or ""))
        return {"proposed": proposed, "entered": entered, "never_filled": never_filled,
                "partial": partial, "in_progress": in_progress, "closed": len(closed),
                "tp_wins": tp_wins, "sl_losses": sl_losses, "timeouts": timeouts}
    finally:
        conn.close()


def render_paper_funnel(days: int = 30, setup_not: str | None = None) -> str:
    f = get_paper_funnel(days, setup_not)
    if f["proposed"] == 0:
        return "🔄 <b>訂單漏斗</b>（{}d）：尚無訊號".format(days)
    return (f"🔄 <b>訂單漏斗</b>（{days}d，紙上）\n"
            f"  提出訊號 <code>{f['proposed']}</code> → "
            f"真正進場 <code>{f['entered']}</code>"
            f"（部分倉 {f['partial']}）→ 進行中 <code>{f['in_progress']}</code>\n"
            f"  無效（掛單未觸及）<code>{f['never_filled']}</code>　"
            f"已平倉 <code>{f['closed']}</code>"
            f"（止盈 {f['tp_wins']} / 止損 {f['sl_losses']} / 逾時 {f['timeouts']}）")


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
