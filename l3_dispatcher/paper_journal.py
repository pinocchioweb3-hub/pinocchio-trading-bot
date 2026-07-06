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

# task#77：由 trade_monitor 寫入的出場 leg 標籤（每筆每型只該記一次）。用於
# apply_paper_event 的冪等去重——at-least-once 輪詢若同一根 bar 被重抓，
# 同一 leg 不得重複 append 進 legs_hit（雙倍扣 size/PnL）。
_MONITOR_LEG_LABELS = ("tp1", "tp2", "tp3", "stop", "timeout")


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
            # v56: 進場計畫快照 JSON（復盤引擎 L1 前置——凍結預期劇本/止損劇本/當下上下文）
            ("plan_snapshot", "ALTER TABLE paper_trades ADD COLUMN plan_snapshot TEXT"),
            # task#53(step8): 進場時凍結的 TP 分配覆寫（auto_param_store 晉升的活躍值），
            # JSON 三元組如 [0.6,0.25,0.15]；None=用預設 CONFIG.tp_size_split。只驅動 paper/demo。
            ("tp_alloc", "ALTER TABLE paper_trades ADD COLUMN tp_alloc TEXT"),
            # task#61(step B): 進場時凍結此桶 (symbol×象限) 的活躍入場積極度覆寫
            #   （entry_policy_store 晉升的活躍 kind）。目前只用 'limit_convert'（深限價到期轉市價）；
            #   None / 'limit_expire'=現行深限價可到期(預設)。只驅動 paper/demo 掛單行為，
            #   真錢執行層永不讀（紅線①）。供 trade_monitor 在掛單到期時決定「轉市價或作廢」。
            ("entry_policy_kind", "ALTER TABLE paper_trades ADD COLUMN entry_policy_kind TEXT"),
            # task#77: 出場偵測檢查點 — 上次已掃到的「最後一根已確認 5m bar」的 ts（epoch ms）。
            ("last_checked_ts", "ALTER TABLE paper_trades ADD COLUMN last_checked_ts INTEGER"),
            # v118（稽核rank6）：淨值口徑——realized_r 是毛利（零費用/滑價建模），demo 配對
            #   實測止損中位 ~−1.05R 證明真實成本存在。net_r=毛R−費用R−止損滑價R（保守
            #   taker雙邊口徑），平倉時並行寫入；毛 realized_r 歷史口徑不動、舊列 NULL 永不回填（紅③）。
            ("net_r", "ALTER TABLE paper_trades ADD COLUMN net_r REAL"),
            # v121（使用者回饋「重覆開單」）：進場當下同幣同向已在場筆數。疊倉＝「紙上記全」
            #   設計的自然結果（deepdive 4-6h 重評、論點續存再記一筆），但疊倉樣本高度相關＝
            #   n 灌水根源之一——落帳供雙口徑統計（首筆vs全部）與「疊單訊號EV」分析。舊列 NULL。
            ("stack_depth", "ALTER TABLE paper_trades ADD COLUMN stack_depth INTEGER"),
            # v124（競品採納#1 Freqtrade StoplossGuard，shadow半）：進場當下「同方向近24h
            #   已有幾筆止損出場」。連續止損＝方向錯誤的市場證據（止損復盤:空頭大盤連開逆勢
            #   多單）——先落帳累積，離線分析『guard_stops≥K 的單 EV 是否顯著更差』過閘後
            #   才把「鎖方向」變真閘。純觀測不擋單。舊列 NULL 永不回填（紅③）。
            ("dir_stops_24h", "ALTER TABLE paper_trades ADD COLUMN dir_stops_24h INTEGER"),
        ):
            if col not in existing:
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError as e:
                    # 跨進程一次性遷移視窗的良性競態：另一進程已先加同欄
                    # （單進程 daemon 內因 init_db 無 await 不會交錯，這只防外部 CLI 同跑）。
                    # 只吞「duplicate column name」，其餘 OperationalError 照常拋出。
                    if "duplicate column name" not in str(e).lower():
                        raise
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


def _quadrant_from_snapshot(plan_snapshot: dict | None) -> str:
    """從進場計畫快照取 OI×價象限（與 lessons_store/auto_optimizer 解析一致）。
    任何缺失/型別錯 → 'unknown'（fail-safe，永不阻塞建單）。"""
    try:
        regime = (plan_snapshot or {}).get("regime_at_entry") or {}
        return regime.get("oi_price_quadrant") or "unknown"
    except Exception:
        return "unknown"


def record_paper_entry(symbol: str, setup: str, direction: str,
                       entry_price: float, stop_price: float,
                       tp1: float, tp2: float, tp3: float,
                       fire_id: int | None = None,
                       regime: str | None = None,
                       zone_lo: float | None = None,
                       zone_hi: float | None = None,
                       split_mode: bool = False,
                       signal_msg_id: int | None = None,
                       skip_cooldown: bool = False,
                       plan_snapshot: dict | None = None,
                       entry_policy_kind: str | None = None) -> int:
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
    # v56: 進場計畫快照（復盤引擎前置）。失敗回 None 不阻塞建單；序列化失敗也吞掉。
    try:
        ps_json = json.dumps(plan_snapshot, ensure_ascii=False) if plan_snapshot else None
    except Exception:
        ps_json = None
    # task#53(step8): 進場時凍結此桶 (symbol×象限) 的活躍 TP 分配覆寫（若 auto_param_store
    # 已晉升過）。fail-safe：任何錯誤 → None（沿用預設分配＝今日行為）。只驅動 paper/demo。
    try:
        from l3_dispatcher.auto_param_store import resolve_tp_alloc
        _ov = resolve_tp_alloc(symbol, _quadrant_from_snapshot(plan_snapshot))
        tp_alloc_json = json.dumps(list(_ov)) if _ov else None
    except Exception:
        tp_alloc_json = None
    # task#61(step B): 解析此桶 (symbol×象限) 的活躍入場積極度覆寫（entry_policy_store 晉升值）。
    #   呼叫端顯式給 entry_policy_kind → 用之（測試/未來 caller 級落地）；否則自解析（與 tp_alloc 同模式）。
    #   只在 split_mode（限價分批單）下有意義：market 進場無待成交掛單可轉，故只記 'limit_convert'。
    #   "market" 覆寫＝訊號當下市價即進，需呼叫端改 split_mode/entry_price 配合，record 層
    #     無從追溯改寫 → 暫不落地，誠實留痕（NO silent cap）後沿用現行深限價可到期。
    #   fail-safe：任何錯誤 → None（沿用今日行為）。只驅動 paper/demo（紅線①，真錢永不讀）。
    eff_entry_policy = None
    try:
        _ek = entry_policy_kind
        if _ek is None:
            from l3_dispatcher.entry_policy_store import resolve_entry_policy
            _ek = resolve_entry_policy(symbol, _quadrant_from_snapshot(plan_snapshot))
        if _ek == "market":
            print(f"[paper] {symbol} 入場政策覆寫=market 尚未落地（需呼叫端配合），"
                  f"本筆沿用現行深限價可到期行為（已留痕，紅線③不靜默丟棄）")
            _ek = None
        if (_ek == "limit_convert" and split_mode
                and zone_lo is not None and zone_hi is not None):
            eff_entry_policy = "limit_convert"
    except Exception:
        eff_entry_policy = None
    conn = _conn()
    try:
        now_ms = int(time.time() * 1000)
        # v121：進場當下同幣同向在場筆數（0=首筆）。落帳供統計雙口徑與疊單EV分析。
        try:
            stack_depth = conn.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE symbol=? AND direction=? "
                "AND status='open'", (symbol, direction)).fetchone()[0]
        except Exception:  # noqa: BLE001
            stack_depth = None
        # v124（StoplossGuard shadow）：同方向近 24h 止損出場筆數（連續止損=方向錯的證據）。
        try:
            dir_stops_24h = conn.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE direction=? "
                "AND exit_reason='stop' AND exit_at > ?",
                (direction, now_ms - 86400_000)).fetchone()[0]
        except Exception:  # noqa: BLE001
            dir_stops_24h = None
        # v122（使用者質疑「同標的重複開超多單」查明根因）：intraday 突破引擎沉睡數月
        #   （direct_fire 歷史吞吐≈0，稽核實證）後於 2026-07-04 強趨勢中甦醒，每 4h 冷卻
        #   一到就對同一標的再開一單（3 天 62 筆、ETH 空疊 10 筆）——冷卻閘只防窗內重複、
        #   不查在場＝設計漏洞。同幣同向在場 ≥MAX 即不再疊（同一突破論點的第 N 次重發
        #   ≈同一訊號，擋重複屬 v47 symbol_gate 去重精神的補洞，非策略變更）。
        #   deepdive 不受此限（LLM 逐次重新分析、疊倉輕微、且是已驗證軌道）。
        if (setup == "intraday" and stack_depth is not None
                and stack_depth >= _INTRADAY_MAX_STACK):
            print(f"[paper] {symbol}/{setup}/{direction} 跳過建單：同幣同向已在場 "
                  f"{stack_depth} 筆 ≥ 上限 {_INTRADAY_MAX_STACK}（v122 疊倉閘，"
                  f"同一突破論點不重複入帳）")
            return -1
        if split_mode and zone_lo is not None and zone_hi is not None:
            splits = _compute_entry_splits(direction, zone_lo, zone_hi)
            cur = conn.execute(
                """INSERT INTO paper_trades
                   (symbol, setup, direction, entry_price, stop_price, tp1, tp2, tp3,
                    entry_at, fire_id, regime, created_at,
                    entry_splits, entry_filled_pct, entry_state, signal_msg_id, plan_snapshot,
                    tp_alloc, entry_policy_kind, stack_depth, dir_stops_24h)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0, 'pending', ?, ?, ?, ?, ?, ?)""",
                (symbol, setup, direction, entry_price, stop_price, tp1, tp2, tp3,
                 now_ms, fire_id, regime, now_ms, json.dumps(splits), signal_msg_id, ps_json,
                 tp_alloc_json, eff_entry_policy, stack_depth, dir_stops_24h),
            )
        else:
            cur = conn.execute(
                """INSERT INTO paper_trades
                   (symbol, setup, direction, entry_price, stop_price, tp1, tp2, tp3,
                    entry_at, fire_id, regime, created_at, entry_filled_pct, entry_state,
                    signal_msg_id, plan_snapshot, tp_alloc, entry_policy_kind, stack_depth,
                    dir_stops_24h)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0, 'full', ?, ?, ?, ?, ?, ?)""",
                (symbol, setup, direction, entry_price, stop_price, tp1, tp2, tp3,
                 now_ms, fire_id, regime, now_ms, signal_msg_id, ps_json, tp_alloc_json,
                 eff_entry_policy, stack_depth, dir_stops_24h),
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
            "entry_at, setup, entry_policy_kind FROM paper_trades "
            "WHERE status='open' AND entry_state IN ('pending','partial')"
        ).fetchall()
        return [{"id": r[0], "symbol": r[1], "direction": r[2],
                 "splits": json.loads(r[3]) if r[3] else [],
                 "filled_pct": r[4], "entry_state": r[5], "entry_at": r[6],
                 "setup": r[7], "entry_policy_kind": r[8]} for r in rows]
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


def convert_pending_to_market(paper_id: int, market_px: float) -> dict | None:
    """task#61 D「深限價到期轉市價」：把一張『仍 0% 成交（pending）』的限價分批單，在掛單
    到期那刻改為以市價 market_px 成交（救涵蓋率 35%→94%，見 entry-depth-ab 定案）。

    嚴格對齊 backtest.entry_placement_ab._limit_variant(convert=True) 的兩道理性閘：
      ① 追價無意義閘：若市價已穿越 tp1（bull: market_px≥tp1／bear: market_px≤tp1）→ 放棄轉換
         （價已到目標，追進去等於沒有上行空間）。
      ② 風險為零/負閘：risk=|market_px−stop|；若 risk≤0（市價已過止損）→ 放棄轉換。
    放棄轉換 → 回 None（呼叫端續走原 expire_pending 作廢流程，＝per-proposed 計 0R）。

    成交（轉換）→ 改寫 entry_price=market_px、entry_state='full'、entry_filled_pct=1.0、
    所有 split 標記 filled（附 converted_market 旗標）。**永不動 entry_at**：優化器以『訊號
    時刻』對齊 K 線重放，動 entry_at 會錯位 ~12h。R 數學：apply_paper_event 以 entry_price
    欄計 sl_dist，改寫 entry_price 即忠實對映 backtest 的 risk_c=|conv_px−stop_abs|。
    只動模擬盤掛單行為，真錢執行層永不讀（紅線①）。

    SQL 內建 status='open' AND entry_state='pending' 護欄：partial/full/closed 一律不碰。
    回 {market_px, risk, prev_entry} 或 None（未轉換）。
    """
    init_db()
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT direction, entry_price, stop_price, tp1, entry_splits, entry_state, status "
            "FROM paper_trades WHERE id=?", (paper_id,)).fetchone()
        if not row:
            return None
        direction, entry_price, stop_price, tp1, splits_json, state, status = row
        if status != "open" or state != "pending":
            return None
        # 閘①：追價無意義（市價已穿越 tp1）。tp1 為 None（理論上不會）時跳過此閘。
        if tp1 is not None and (
                (direction == "bull" and market_px >= tp1) or
                (direction == "bear" and market_px <= tp1)):
            return None
        # 閘②：風險為零/負（市價已過止損）
        risk = abs(market_px - stop_price)
        if risk <= 0:
            return None
        now_ms = int(time.time() * 1000)
        try:
            splits = json.loads(splits_json) if splits_json else []
        except Exception:
            splits = []
        for s in splits:
            s["filled"] = 1
            s["filled_at"] = now_ms
            s["converted_market"] = 1
        cur = conn.execute(
            "UPDATE paper_trades SET entry_price=?, entry_state='full', "
            "entry_filled_pct=1.0, entry_splits=? "
            "WHERE id=? AND status='open' AND entry_state='pending'",
            (market_px, json.dumps(splits), paper_id))
        if cur.rowcount <= 0:
            return None   # 競態：另一路徑已改狀態 → 不轉換（fail-safe）
        return {"market_px": market_px, "risk": risk, "prev_entry": entry_price}
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
            "signal_msg_id, tp_alloc, last_checked_ts, entry_splits "
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
                "tp_alloc": r[14],
                "last_checked_ts": r[15],   # task#77 出場偵測檢查點
                "entry_splits": r[16],      # task#77 split 單 fill-start 退路用
            })
        return out
    finally:
        conn.close()


def get_latest_deepdive_plan(symbol: str | None = None,
                             direction: str | None = None) -> dict | None:
    """task#10(2d)：把「最近一筆 deepdive 紙上計畫」重建成 LLM-plan 形狀，供 deepdive 卡的
    「📋 複製 JSON」按鈕 / /intent 後備產出機器可讀 trade-intent。

    回 dict（與 synthesizer._extract_plan_block 同形狀，餵得進 plan.canonical_from_deepdive）：
        {actionable, symbol, direction, entry_type(market/limit),
         entry, entry_lo, entry_hi, stop, tp1, tp2, tp3, entry_at}
    限價區間從 entry_splits 的 price min/max 還原；entry_type 由 entry_splits 有無判定。
    找不到（無 deepdive 列 / symbol/direction 不符）→ None，呼叫端安全降級。

    ⛔ 紅線①：只讀 paper_trades（模擬盤帳），永不碰真錢帳本 `trades`。
    """
    init_db()
    conn = _conn()
    try:
        sql = ("SELECT symbol, direction, entry_price, stop_price, tp1, tp2, tp3, "
               "entry_at, entry_splits FROM paper_trades WHERE setup='deepdive'")
        args: list = []
        if symbol:
            sql += " AND symbol=?"
            args.append(symbol)
        if direction:
            sql += " AND direction=?"
            args.append(direction)
        sql += " ORDER BY id DESC LIMIT 1"
        row = conn.execute(sql, args).fetchone()
        if not row:
            return None
        sym, dirn, entry_price, stop_price, tp1, tp2, tp3, entry_at, splits_json = row
        zone_lo = zone_hi = None
        entry_type = "market"
        if splits_json:
            try:
                prices = [s["price"] for s in json.loads(splits_json)
                          if s.get("price") is not None]
                if prices:
                    zone_lo, zone_hi = min(prices), max(prices)
                    entry_type = "limit"
            except Exception:
                zone_lo = zone_hi = None
                entry_type = "market"
        return {
            "actionable": True,
            "symbol": sym,
            "direction": dirn,
            "entry_type": entry_type,
            "entry": entry_price,
            "entry_lo": zone_lo,
            "entry_hi": zone_hi,
            "stop": stop_price,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "entry_at": entry_at,
        }
    finally:
        conn.close()


def set_paper_checkpoint(paper_id: int, ts_ms: int) -> None:
    """task#77：推進紙上倉的出場偵測檢查點到「最後一根已確認 5m bar」的 ts。

    僅在本輪成功抓到 bar 後呼叫；抓取失敗不呼叫（下輪自然重抓同段窗，no silent loss）。
    寫前比大小，永不倒退。
    """
    conn = _conn()
    try:
        conn.execute(
            "UPDATE paper_trades SET last_checked_ts=? "
            "WHERE id=? AND (last_checked_ts IS NULL OR last_checked_ts < ?)",
            (int(ts_ms), paper_id, int(ts_ms)),
        )
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

        # task#77 冪等保險：監控腿每筆每型只該記一次。正常流程已由 legs_hit（get_open_paper
        #   從 legs_hit csv 重建）阻止重抓重記；此為「檢查點式 gap-fill」時代的縱深防禦——
        #   萬一同一根已確認 bar 被下輪重抓，同一 leg 不會二次 append（雙倍扣 size/PnL）。
        if leg_label in _MONITOR_LEG_LABELS:
            existing_legs = [x for x in (legs_csv or "").split(",") if x]
            if leg_label in existing_legs:
                return {"closed": status != "open", "total_pnl": round(pnl, 2),
                        "leg_r": 0.0, "leg_pnl": 0.0, "duplicate_skipped": True}

        sl_dist = abs(entry - stop)
        leg_r = ((exit_price - entry) if direction == "bull"
                 else (entry - exit_price)) / sl_dist
        # （net_r 由 compute_net_r 於全平時計算——見該函式；此處先保留毛利口徑不動）
        # v26: PnL 按實際成交比例縮放（只進 70% 就只賺/賠 70%）
        leg_pnl = size_pct * RISK_USD * leg_r * (filled_pct if filled_pct else 1.0)

        new_legs = ",".join([x for x in (legs_csv or "").split(",") if x] + [leg_label])
        new_size = round(size_rem - size_pct, 3)
        new_pnl = pnl + leg_pnl
        now_ms = int(time.time() * 1000)

        if new_size <= 0.001 or leg_label in ("stop", "timeout"):
            gross_r = new_pnl / RISK_USD
            net_r = compute_net_r(gross_r, entry, stop, leg_label)
            conn.execute(
                """UPDATE paper_trades SET status='closed', legs_hit=?, size_remaining=0,
                   pnl_usd=?, realized_r=?, net_r=?, exit_reason=?, exit_at=? WHERE id=?""",
                (new_legs, new_pnl, gross_r, net_r, leg_label, now_ms, paper_id),
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


# v118（稽核rank6）：淨值口徑常數——保守 taker 雙邊 + 止損滑價（demo 配對實測校準）。
_FEE_RATE = float(os.getenv("PAPER_FEE_RATE", "0.0005"))        # OKX taker 0.05%/邊
_STOP_SLIP_R = float(os.getenv("PAPER_STOP_SLIP_R", "0.05"))    # demo 配對止損中位 ~−1.05R
# v122：intraday 同幣同向在場疊倉上限（≥此數即不再入帳；deepdive 不受限）。
_INTRADAY_MAX_STACK = int(os.getenv("PAPER_INTRADAY_MAX_STACK", "2"))


def compute_net_r(gross_r: float, entry: float, stop: float,
                  exit_reason: str | None,
                  fee_rate: float | None = None,
                  stop_slip_r: float | None = None) -> float | None:
    """毛 R → 淨 R（純函式）。net = gross − 費用R − 止損滑價R。

    費用R = 2×taker費率×進場價/|進場−止損|（雙邊、全名目；R 單位下張數消掉）。
    刻意保守：實際進場/TP 多為 maker（更便宜），用 taker 高估成本——若淨 EV 在保守
    口徑下仍為正，真錢只會更好（真錢閘的誠實方向）。滑價只計 stop 出場（市價滑過）。
    缺料/退化 → None 誠實留空（紅線③），永不回填舊列。"""
    fr = _FEE_RATE if fee_rate is None else fee_rate
    sr = _STOP_SLIP_R if stop_slip_r is None else stop_slip_r
    try:
        entry, stop = float(entry), float(stop)
    except (TypeError, ValueError):
        return None
    dist = abs(entry - stop)
    if dist <= 0 or entry <= 0:
        return None
    fee_r = 2 * fr * entry / dist
    slip_r = sr if (exit_reason or "") == "stop" else 0.0
    return round(gross_r - fee_r - slip_r, 4)


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
        _rmean = (sum(rs) / len(rs)) if rs else 0.0
        r_std = (((sum((x - _rmean) ** 2 for x in rs) / (len(rs) - 1)) ** 0.5)
                 if len(rs) > 1 else 0.0)   # 樣本標準差（task#4：供 EV 信賴區間）
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
            "r_std": round(r_std, 3),
            "profit_factor": round(pf, 2) if pf != float("inf") else None,
            "stage0_progress": f"{len(closed)}/100",  # 自動交易 Stage 1 門檻
        }
    finally:
        conn.close()


# L2 學習單位門檻鏡像：每個 (symbol×regime) 桶需 ≥30 筆「成交」單才能過 minTRL 閘。
# 與 backtest.l2_stat_gates.MIN_BUCKET_N 對齊；此處只用於誠實推估「還缺多少提案」，
# 不參與任何下單／訊號數學（純顯示）。刻意不 import 以免 daemon 熱路徑拉進回測相依。
MIN_BUCKET_N_MIRROR = 30


def get_paper_funnel(days: int = 30, setup_not: str | None = None) -> dict:
    """v26: 訂單漏斗 — 提出/真正進場/未成交/部分倉/止盈止損/進行中。
    task#57: 額外揭露 entry_expired（限價掛單逾時未成交）、有效成交率、與依現行成交率
    推估湊滿一個 L2 學習桶所需的提案數——讓「限價未成交拖累」在日報上可見而非被埋。"""
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
        # entry_expired 是 never_filled 的子集（限價單到期未成交），單獨揭露其拖累佔比。
        entry_expired = sum(1 for r in rows if (r[3] or "") == "entry_expired")
        partial = sum(1 for r in rows if 0 < (r[2] or 0) < 0.999)
        in_progress = sum(1 for r in rows if r[0] == "open" and (r[2] or 0) > 0)
        # v33: entry_expired 已計入 never_filled（filled_pct==0），不重複算進「已平倉」
        closed = [r for r in rows if r[0] == "closed" and (r[3] or "") != "entry_expired"]
        tp_wins = sum(1 for r in closed if (r[4] or 0) > 0)
        sl_losses = sum(1 for r in closed if (r[4] or 0) < 0)
        timeouts = sum(1 for r in closed if "timeout" in (r[3] or ""))
        # 有效成交率＝真正進場 / 提出（None 表示尚無提案，誠實不假裝 0%）。
        fill_rate = round(100.0 * entered / proposed, 1) if proposed else None
        # 依現行成交率推估：還需多少提案才能湊滿一個 L2 學習桶（30 筆成交）。
        # 成交率為 0（全數未成交）時誠實回 None，不捏造一個有限數字。
        est_proposals_per_bucket = (
            int(-(-MIN_BUCKET_N_MIRROR * proposed // entered)) if entered else None)
        return {"proposed": proposed, "entered": entered, "never_filled": never_filled,
                "entry_expired": entry_expired, "partial": partial,
                "in_progress": in_progress, "closed": len(closed),
                "tp_wins": tp_wins, "sl_losses": sl_losses, "timeouts": timeouts,
                "fill_rate_pct": fill_rate,
                "est_proposals_per_bucket": est_proposals_per_bucket,
                "min_bucket_n": MIN_BUCKET_N_MIRROR}
    finally:
        conn.close()


def most_recent_activity_ms() -> int | None:
    """最近一筆紙上活動（進場或出場）的 epoch ms；無資料回 None。
    供 CEO 監督判『實質產出』（task#7）：有新進場/平倉＝引擎真在動，非只看 git commit。"""
    init_db()
    conn = _conn()
    try:
        row = conn.execute("SELECT MAX(entry_at), MAX(exit_at) FROM paper_trades").fetchone()
        cands = [int(x) for x in (row or []) if x]
        return max(cands) if cands else None
    finally:
        conn.close()


def render_paper_funnel(days: int = 30, setup_not: str | None = None) -> str:
    f = get_paper_funnel(days, setup_not)
    if f["proposed"] == 0:
        return "🔄 <b>訂單漏斗</b>（{}d）：尚無訊號".format(days)
    fr = f.get("fill_rate_pct")
    fr_txt = f"{fr}%" if fr is not None else "—"
    est = f.get("est_proposals_per_bucket")
    # 誠實揭露限價未成交拖累 + 湊滿 L2 學習桶（30 筆成交）所需提案推估。
    honest = (f"  限價未成交 <code>{f.get('entry_expired', 0)}</code>　"
              f"有效成交率 <code>{fr_txt}</code>")
    if est is not None:
        honest += (f"　→ 依此率約需 <code>{est}</code> 筆提案"
                   f"才湊滿 1 個 L2 學習桶（{f.get('min_bucket_n', MIN_BUCKET_N_MIRROR)} 筆成交）")
    return (f"🔄 <b>訂單漏斗</b>（{days}d，紙上）\n"
            f"  提出訊號 <code>{f['proposed']}</code> → "
            f"真正進場 <code>{f['entered']}</code>"
            f"（部分倉 {f['partial']}）→ 進行中 <code>{f['in_progress']}</code>\n"
            f"  無效（掛單未觸及）<code>{f['never_filled']}</code>　"
            f"已平倉 <code>{f['closed']}</code>"
            f"（止盈 {f['tp_wins']} / 止損 {f['sl_losses']} / 逾時 {f['timeouts']}）\n"
            f"{honest}")


def _display_mode_pj() -> str:
    """讀 DISPLAY_MODE（novice/expert，預設 novice）。純呈現層，不碰訊號/帳本數學。
    內嵌 import 避免與 botconfig 的載入順序耦合；任何錯誤落回 novice。"""
    try:
        from botconfig import get_str
        m = (get_str("DISPLAY_MODE", "novice") or "novice").strip().lower()
        return m if m in ("novice", "expert") else "novice"
    except Exception:
        return "novice"


def _wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """勝率 Wilson 95% 信賴區間（純數學、零相依/零網路）。小樣本會很寬＝誠實顯示不確定。"""
    if n <= 0:
        return (0.0, 0.0)
    k = max(0, min(k, n))   # v83(6) 縱深防禦：夾定義域，p>1 會丟 complex 而崩
    p = k / n
    denom = 1.0 + z * z / n
    centre = p + z * z / (2 * n)
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return (max(0.0, (centre - half) / denom), min(1.0, (centre + half) / denom))


def _paper_summary_stat_note(stats: dict) -> str:
    """task#83(B) 雙向顯示：治本「統計裸奔」——勝率/EV 不再以裸點估計假裝精確。
    novice＝白話 caveat（過去≠未來、紙上非真錢）；expert＝勝率 95%CI＋n<門檻不予判讀。
    純附加層：絕不改任何既有數字（與 parity 不變量一致）；誠實標籤永不省略（紅線③）。"""
    n = int(stats.get("n_closed", 0) or 0)
    if n <= 0:
        return ""
    if _display_mode_pj() == "expert":
        wr = (stats.get("win_rate_pct", 0) or 0) / 100.0
        lo, hi = _wilson_ci(round(wr * n), n)
        # EV 95%CI（常態近似 avg_r ± 1.96·SE；小樣本很寬＝誠實顯示不確定；含0＝未證實正期望值）
        avg_r = stats.get("avg_r", 0.0) or 0.0
        se = (stats.get("r_std", 0.0) or 0.0) / (n ** 0.5) if n > 0 else 0.0
        ev_lo, ev_hi = avg_r - 1.96 * se, avg_r + 1.96 * se
        ev_note = (f"｜EV 95%CI [{ev_lo:+.2f}, {ev_hi:+.2f}]R"
                   + ("（含0＝未證實有正期望值）" if ev_lo <= 0 <= ev_hi else ""))
        tail = ("（n<30：勝率與 EV 皆未達顯著門檻，僅描述非結論）" if n < 30
                else "（原始 n；同日叢聚下有效樣本更低，顯著性另見 crypto-EV 工具）")
        return (f"\n🎓 <i>勝率 95%CI [{lo*100:.0f}%–{hi*100:.0f}%]{ev_note}｜n={n}{tail}"
                "；紙上非真錢、過去≠未來</i>")
    return ("\n🔰 <i>勝率＝過去這些紙上單的命中比例，<b>不代表未來、樣本量也還沒到可下結論</b>；"
            "這是模擬盤紀錄、非真錢績效</i>")


def render_paper_summary(stats: dict) -> str:
    """紙上帳一行摘要（嵌進 /status 與每日績效）"""
    if stats["n_closed"] == 0 and stats["n_open"] == 0:
        return "📜 紙上驗證：尚無紀錄（每筆訊號自動追蹤中）"
    base = (f"📜 紙上驗證（{stats['window_days']}d）："
            f"已平 <code>{stats['n_closed']}</code> 筆 "
            f"勝率 <code>{stats['win_rate_pct']}%</code> "
            f"期望值 <code>{stats['avg_r']:+.2f}R</code>/筆　"
            f"Stage1 門檻 <code>{stats['stage0_progress']}</code>"
            f"　<i>(100U 風險基準 PnL ${stats['total_pnl_usd']:+.0f})</i>")
    return base + _paper_summary_stat_note(stats)


if __name__ == "__main__":
    init_db()
    print(f"paper journal at {DB_PATH}")
    print(render_paper_summary(get_paper_stats(30)))
