"""🔬 交易覆盤／驗屍 Session（Session A，純讀、零新成本）。

界線（硬安全紅線）：
    本 worker **只純讀** trade_journal.db / scanner.db / ohlc_cache.db，
    永不下單、不寫交易路徑、不碰實盤。它只把已平倉的紙上(paper)交易做事後
    歸因驗屍，append 到本機 jsonl + 渲染 digest，每週一把賠錢模式彙整成
    「建議」推系統主題（僅建議，比照 auto_tuner 不自動套用）。

誠實（紅線③）：
    可覆盤樣本全為紙上模擬、未模擬滑點、demo/real 成交皆 0 →
    任何推播都帶誠實橫幅，不可作績效宣稱。

職責拆解：
    1. 純讀 paper_trades 找「新出現的已平倉列」（用 fire_id / id 去重，
       已處理清單記在 postmortem_notes.jsonl）。
    2. 逐筆驗屍：分類 exit_reason；用 scanner.db 的 breadth 回溯該筆 entry_at
       時刻的市場環境（up/down 廣度%、avg_funding）。
       ⚠️ entry_at=毫秒、breadth.ts=秒，對齊務必換算（ms//1000），見 _nearest_breadth。
       標記每筆是否「逆勢」（bull 進場 up% < 35 視為逆勢，bear 進場 up% > 65）。
    3. 滾動聚合：按 setup / regime / direction / breadth 桶分組算 avg_R / 勝率 / n，
       找賠錢模式。
    4. 輸出：每筆 append jsonl + 渲染 postmortem_digest.md；每週一彙整賠錢模式
       成建議，沿用 auto_tuner 的 TG 推送方式推系統主題（頂部誠實橫幅）。
    5. D1 反事實：回測「若 deepdive 加上『進場 breadth>=門檻 且 BTC 4h>200MA』閘」，
       歷史上會擋掉哪些已平倉 paper deepdive 單、被擋單合計 R 多少（供協調者判 D1）。
       純讀 ohlc_cache.db 的 BTC 4h，且嚴格無前視（200MA 只用 entry_at 當下及之前的 K）。
    6. worker 進入點 run_postmortem_loop：每 6 小時掃新平倉、每週一彙整；
       不吞例外成 busy-loop（讓 supervise 能退避重啟）。

純函式核心（分類 / 環境回溯 / 分桶EV / D1反事實 / jsonl去重）可離線單測，
見 tests/test_postmortem.py。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import sqlite3
import time
from pathlib import Path

from botpaths import data_dir as _data_dir
from botpaths import db_path as _db_path

# 純讀三個本機 DB（絕不寫交易路徑）
JOURNAL_DB = _db_path("trade_journal.db")
SCANNER_DB = _db_path("scanner.db")

NOTES_PATH = _data_dir() / "postmortem_notes.jsonl"      # 已驗屍紀錄帳本（也充當去重來源）
DIGEST_PATH = _data_dir() / "postmortem_digest.md"        # 人類可讀彙整

# 逆勢判定門檻（廣度 up% 百分比）
COUNTERTREND_UP_PCT_LO = 35.0     # bull 進場 up% < 35 → 逆勢
COUNTERTREND_UP_PCT_HI = 65.0     # bear 進場 up% > 65 → 逆勢（多頭環境放空）

# breadth 最近鄰對齊容忍（秒）— 紙上單 entry 多半在某輪掃描附近，給 2 小時窗
BREADTH_MATCH_TOL_S = 7200

# scanner.db breadth 只保留 7 天 → 超過此窗的舊單回溯不到環境（記為 unknown，不臆測）

# D1 反事實預設門檻（與 docs 規劃的 above_4h_200ma 同精神）
D1_BREADTH_GATE_DEFAULT = 45.0    # 進場 up% >= 45 才放行
EXIT_EXPIRED = "entry_expired"    # 掛單從未成交 → 非真實交易，全程排除


# =====================================================================
# 連線（皆唯讀；URI mode=ro 防任何意外寫入）
# =====================================================================
def _ro_conn(db: Path) -> sqlite3.Connection:
    """唯讀連線（mode=ro）。檔案不存在時 sqlite 會丟例外，呼叫端負責處理。"""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# =====================================================================
# 1) jsonl 去重核心（純函式，可離線測）
# =====================================================================
def _dedup_key(trade: dict) -> str:
    """每筆已平倉單的唯一去重鍵。

    優先用 fire_id（同一訊號只驗屍一次）；fire_id 缺時退回 paper id。
    刻意用前綴區分兩種鍵空間，避免 fire_id 與 id 數值碰撞。
    """
    fid = trade.get("fire_id")
    if fid is not None:
        return f"fire:{fid}"
    return f"id:{trade.get('id')}"


def load_processed_keys(notes_path: Path | None = None) -> set[str]:
    """讀 jsonl 已處理鍵集合。檔不存在 / 壞行一律寬容跳過（回已知集合）。"""
    p = notes_path or NOTES_PATH
    keys: set[str] = set()
    if not p.exists():
        return keys
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                k = rec.get("dedup_key")
                if k:
                    keys.add(k)
    except Exception:
        pass
    return keys


def filter_new_closed(trades: list[dict], processed: set[str]) -> list[dict]:
    """從已平倉清單剔除已處理過的（依 _dedup_key）。純函式。

    同一批內也去重（避免同 fire_id 多列重複 append）。
    """
    out: list[dict] = []
    seen = set(processed)
    for t in trades:
        k = _dedup_key(t)
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


def append_notes(records: list[dict], notes_path: Path | None = None) -> int:
    """把驗屍紀錄逐行 append 進 jsonl。回實際寫入筆數。"""
    p = notes_path or NOTES_PATH
    if not records:
        return 0
    p.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with p.open("a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    return n


# =====================================================================
# 2) exit_reason 分類 + 逆勢標記（純函式）
# =====================================================================
def classify_exit(exit_reason: str | None, realized_r: float | None,
                  legs_hit: str | None) -> str:
    """把 exit_reason 正規化成驗屍分類。

    回值 ∈ {win_full, win_partial, stop_loss, timeout, entry_expired, other}。
    - tp3（或 legs_hit 含 3 個 tp）→ win_full
    - tp1/tp2 但未到 tp3 → win_partial（部分止盈後出場）
    - stop 且 R<=0 → stop_loss
    - timeout → timeout
    - entry_expired → entry_expired（掛單從未成交，非真實交易）
    """
    er = (exit_reason or "").lower()
    legs = (legs_hit or "").lower()
    r = realized_r or 0.0
    if er == EXIT_EXPIRED:
        return "entry_expired"
    if "timeout" in er:
        return "timeout"
    if "stop" in er:
        # 移動停利打到的 stop 也可能 R>0；只有 R<=0 才算真止損
        return "stop_loss" if r <= 0 else "win_partial"
    if er in ("tp3",) or legs.count("tp") >= 3:
        return "win_full"
    if er in ("tp1", "tp2") or "tp" in legs:
        return "win_partial"
    # 沒有明確 exit_reason，用 R 兜底
    if r > 0:
        return "win_partial"
    if r < 0:
        return "stop_loss"
    return "other"


def is_countertrend(direction: str | None, up_pct: float | None) -> bool | None:
    """逆勢判定。up_pct=None（回溯不到環境）→ 回 None（未知，不臆測）。

    - bull 進場 up% < 35 → 逆勢（市場在跌時做多）
    - bear 進場 up% > 65 → 逆勢（市場在漲時做空）
    """
    if up_pct is None:
        return None
    d = (direction or "").lower()
    if d == "bull":
        return up_pct < COUNTERTREND_UP_PCT_LO
    if d == "bear":
        return up_pct > COUNTERTREND_UP_PCT_HI
    return None


# =====================================================================
# 3) 環境回溯：用 breadth 最近鄰（ms↔s 換算是最易錯處！）
# =====================================================================
def _up_pct_from_breadth(b: dict) -> float | None:
    """從一列 breadth 算「上漲廣度%」。

    優先用 24h 桶（n_up24h / (n_up24h + n_down24h)）——它在冷啟動時也有值
    （1h 桶需要前一小時快照基準，剛啟動會全 0）。分母 0 → None。
    """
    up = b.get("n_up24h") or 0
    dn = b.get("n_down24h") or 0
    tot = up + dn
    if tot <= 0:
        return None
    return round(up / tot * 100, 1)


def nearest_breadth(entry_at_ms: int, breadth_rows: list[dict],
                    tol_s: int = BREADTH_MATCH_TOL_S) -> dict | None:
    """在 breadth_rows（每列含秒級 'ts'）中找離 entry_at 最近的一列。

    ⚠️⚠️ entry_at 是毫秒、breadth.ts 是秒 → 先把 entry 換成秒再比。
    超過 tol_s 容忍窗 → 回 None（回溯不到，不硬湊）。純函式（rows 由呼叫端撈好傳入）。
    """
    if not breadth_rows:
        return None
    entry_s = entry_at_ms / 1000.0      # ← 關鍵換算：ms → s
    best = None
    best_dist = None
    for b in breadth_rows:
        ts = b.get("ts")
        if ts is None:
            continue
        dist = abs(ts - entry_s)
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best = b
    if best is None or best_dist is None or best_dist > tol_s:
        return None
    return best


def enrich_with_environment(trade: dict, breadth_rows: list[dict]) -> dict:
    """把單筆 trade 加上環境回溯欄位。純函式（breadth_rows 由呼叫端撈好）。

    回新 dict（不改輸入），含：
        exit_class, up_pct, avg_funding, countertrend, breadth_ts, R, setup, ...
    """
    out = dict(trade)
    exit_class = classify_exit(trade.get("exit_reason"), trade.get("realized_r"),
                               trade.get("legs_hit"))
    b = nearest_breadth(trade.get("entry_at") or 0, breadth_rows)
    up_pct = _up_pct_from_breadth(b) if b else None
    avg_funding = b.get("avg_funding") if b else None
    out["exit_class"] = exit_class
    out["up_pct"] = up_pct
    out["avg_funding"] = avg_funding
    out["countertrend"] = is_countertrend(trade.get("direction"), up_pct)
    out["breadth_ts"] = b.get("ts") if b else None
    out["dedup_key"] = _dedup_key(trade)
    return out


# =====================================================================
# 4) 分桶 EV 聚合（純函式）
# =====================================================================
def _breadth_bucket(up_pct: float | None) -> str:
    """把 up% 分成可讀桶：unknown / 偏空(<35) / 中性(35-65) / 偏多(>65)。"""
    if up_pct is None:
        return "unknown"
    if up_pct < COUNTERTREND_UP_PCT_LO:
        return "bearish"
    if up_pct > COUNTERTREND_UP_PCT_HI:
        return "bullish"
    return "neutral"


def _agg_one(rows: list[dict]) -> dict:
    """一桶內的 avg_R / 勝率 / n。rows 為 enrich 後 dict（含 realized_r, exit_class）。"""
    rs = [(r.get("realized_r") or 0.0) for r in rows]
    n = len(rs)
    wins = sum(1 for r in rs if r > 0)
    avg_r = round(sum(rs) / n, 3) if n else 0.0
    win_rate = round(wins / n * 100, 1) if n else 0.0
    return {"n": n, "avg_r": avg_r, "win_rate": win_rate, "sum_r": round(sum(rs), 3)}


MIN_PATTERN_N = 5     # 一個賠錢模式至少要這麼多筆才提（避免雜訊）


def bucket_ev(enriched: list[dict]) -> dict:
    """按 setup / regime / direction / breadth 桶分組算 EV。純函式。

    排除 entry_expired（非真實交易）。回 {dimension: {bucket_key: agg}}。
    另含 'losing_patterns'：avg_r < 0 且 n >= MIN_PATTERN_N 的桶清單（最賠的在前）。
    """
    real = [e for e in enriched if e.get("exit_class") != "entry_expired"]
    dims: dict[str, dict[str, list[dict]]] = {
        "setup": {}, "regime": {}, "direction": {}, "breadth": {},
        "countertrend": {},
    }
    for e in real:
        setup = e.get("setup") or "unknown"
        regime = e.get("regime") or "unknown"
        direction = e.get("direction") or "unknown"
        bbucket = _breadth_bucket(e.get("up_pct"))
        ct = e.get("countertrend")
        ct_key = "逆勢" if ct is True else ("順勢" if ct is False else "未知")
        dims["setup"].setdefault(setup, []).append(e)
        dims["regime"].setdefault(regime, []).append(e)
        dims["direction"].setdefault(direction, []).append(e)
        dims["breadth"].setdefault(bbucket, []).append(e)
        dims["countertrend"].setdefault(ct_key, []).append(e)

    result: dict = {"n_total": len(real)}
    for dim, buckets in dims.items():
        result[dim] = {k: _agg_one(v) for k, v in buckets.items()}

    # 賠錢模式：跨 setup×breadth×countertrend 的交叉桶，挑 avg_r<0 且樣本夠
    cross: dict[str, list[dict]] = {}
    for e in real:
        key = (f"{e.get('setup') or 'unknown'}｜"
               f"{_breadth_bucket(e.get('up_pct'))}｜"
               f"{'逆勢' if e.get('countertrend') is True else ('順勢' if e.get('countertrend') is False else '未知')}")
        cross.setdefault(key, []).append(e)
    losing = []
    for key, rows in cross.items():
        agg = _agg_one(rows)
        if agg["avg_r"] < 0 and agg["n"] >= MIN_PATTERN_N:
            losing.append({"pattern": key, **agg})
    losing.sort(key=lambda x: x["sum_r"])    # 合計虧損最大的在前
    result["losing_patterns"] = losing
    return result


# =====================================================================
# 5) D1 反事實：deepdive 若加「breadth 閘 + BTC 4h>200MA」會擋掉什麼？
# =====================================================================
def btc_above_200ma_4h(entry_at_ms: int, btc_4h_closes: list[tuple[int, float]],
                       period: int = 200) -> bool | None:
    """BTC 4h 收盤在進場時點是否 > 200 根 4h SMA。**嚴格無前視**。

    參數
    ----
    entry_at_ms: 進場毫秒。
    btc_4h_closes: [(ts_ms, close), ...] 升序，由呼叫端從 ohlc_cache 撈好（純讀）。
    period: MA 週期（預設 200）。

    回 True/False；資料不足（< period 根落在 entry 之前）→ None（未知，不臆測、不放行）。
    無前視保證：只取 ts <= entry_at_ms 的 K 計算，最後一根當「進場當下的價」。
    """
    if not btc_4h_closes:
        return None
    # 只用 entry 當下及之前的 K（升序 → 取 ts<=entry 的尾段）
    past = [c for (ts, c) in btc_4h_closes if ts <= entry_at_ms]
    if len(past) < period:
        return None
    window = past[-period:]
    ma = sum(window) / period
    last_close = past[-1]
    return last_close > ma


def d1_counterfactual(deepdive_enriched: list[dict],
                      btc_4h_closes: list[tuple[int, float]],
                      breadth_gate: float = D1_BREADTH_GATE_DEFAULT,
                      period: int = 200) -> dict:
    """反事實：若 deepdive 進場前必須通過『up% >= breadth_gate 且 BTC 4h>200MA』，
    歷史上會擋掉哪些已平倉 deepdive 單、被擋單合計 R 多少。純函式。

    閘判定（任一不過即「會被擋」）：
        - up_pct is None（回溯不到環境）→ 視為「未知，保守當被擋」（gate 無法確認通過）
        - up_pct < breadth_gate → 被擋
        - btc_above_200ma is None / False → 被擋
    回 dict：blocked_n / blocked_sum_r / passed_n / passed_sum_r / verdict 文案。
    被擋單若合計 R 為負（擋掉的多是賠錢單）→ 閘「有幫助」；正 → 閘「反而擋掉賺的」。
    """
    real = [e for e in deepdive_enriched
            if e.get("exit_class") != "entry_expired"
            and (e.get("setup") or "") == "deepdive"]
    blocked, passed = [], []
    for e in real:
        up_pct = e.get("up_pct")
        entry_at = e.get("entry_at") or 0
        btc_ok = btc_above_200ma_4h(entry_at, btc_4h_closes, period)
        gate_pass = (up_pct is not None and up_pct >= breadth_gate and btc_ok is True)
        (passed if gate_pass else blocked).append(e)

    def _sum_r(rows):
        return round(sum((r.get("realized_r") or 0.0) for r in rows), 3)

    blocked_sum_r = _sum_r(blocked)
    passed_sum_r = _sum_r(passed)
    if not real:
        verdict = "無已平倉 deepdive 單可供反事實評估"
    elif blocked_sum_r < 0:
        verdict = (f"此閘歷史上會擋掉 {len(blocked)} 筆、合計 {blocked_sum_r:+.2f}R "
                   f"（擋掉的整體是賠錢的 → 閘可能有幫助，惟樣本不足，僅供 D1 參考）")
    elif blocked_sum_r > 0:
        verdict = (f"此閘歷史上會擋掉 {len(blocked)} 筆、合計 {blocked_sum_r:+.2f}R "
                   f"（擋掉的整體是賺錢的 → 閘反而誤殺盈利單，不宜採納）")
    else:
        verdict = f"此閘會擋掉 {len(blocked)} 筆、合計 0R（中性，無證據）"

    return {
        "breadth_gate": breadth_gate,
        "ma_period": period,
        "n_eval": len(real),
        "blocked_n": len(blocked),
        "blocked_sum_r": blocked_sum_r,
        "passed_n": len(passed),
        "passed_sum_r": passed_sum_r,
        "verdict": verdict,
    }


# =====================================================================
# 誠實橫幅（紅線③）
# =====================================================================
def _honesty_banner_html() -> str:
    return ("⚠️ <i>誠實聲明：以下全為『紙上模擬』覆盤，未模擬滑點，"
            "demo/real 成交皆 0、樣本 &lt;100，不可作績效宣稱，僅供引擎自我檢討。</i>")


def _honesty_banner_md() -> str:
    return ("> ⚠️ 誠實聲明：全為紙上模擬覆盤，未模擬滑點，demo/real 成交皆 0、"
            "樣本 <100，不可作績效宣稱，僅供引擎自我檢討。")


# =====================================================================
# DB 讀取層（薄、純讀；核心邏輯都在上面的純函式）
# =====================================================================
def _fetch_closed_paper(conn: sqlite3.Connection) -> list[dict]:
    """純讀所有 closed paper_trades（含 entry_expired，後續分類時再排）。"""
    rows = conn.execute(
        "SELECT id, symbol, setup, direction, entry_price, stop_price, "
        "tp1, tp2, tp3, status, legs_hit, pnl_usd, realized_r, exit_reason, "
        "entry_at, exit_at, fire_id, regime "
        "FROM paper_trades WHERE status='closed' ORDER BY exit_at"
    ).fetchall()
    cols = ["id", "symbol", "setup", "direction", "entry_price", "stop_price",
            "tp1", "tp2", "tp3", "status", "legs_hit", "pnl_usd", "realized_r",
            "exit_reason", "entry_at", "exit_at", "fire_id", "regime"]
    return [dict(zip(cols, r)) for r in rows]


def _fetch_breadth_rows() -> list[dict]:
    """純讀 scanner.db breadth（秒級 ts）。檔不存在 / 表缺 → 回空（不臆測環境）。"""
    try:
        conn = _ro_conn(SCANNER_DB)
    except Exception:
        return []
    try:
        rows = conn.execute(
            "SELECT ts, n_total, n_up24h, n_down24h, n_up1h, n_down1h, "
            "n_overheat, n_oversold, avg_funding FROM breadth ORDER BY ts"
        ).fetchall()
        cols = ["ts", "n_total", "n_up24h", "n_down24h", "n_up1h", "n_down1h",
                "n_overheat", "n_oversold", "avg_funding"]
        return [dict(zip(cols, r)) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def _fetch_btc_4h_closes(days: int = 1200) -> list[tuple[int, float]]:
    """純讀 ohlc_cache.db 的 BTC 4h 收盤（升序，(ts_ms, close)）。

    供 D1 反事實算 200MA。檔/表缺 → 回空（D1 無法評估，回 None verdict）。
    """
    try:
        from backtest.data_loader import _read_cache  # 既有純讀快取函式
    except Exception:
        return []
    try:
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - days * 86_400_000
        bars = _read_cache("BTC", "4h", start_ms, end_ms)
        return [(int(b["ts"]), float(b["close"])) for b in bars if b.get("close") is not None]
    except Exception:
        return []


# =====================================================================
# digest 渲染（人類可讀 .md）
# =====================================================================
def render_digest_md(ev: dict, d1: dict, n_new: int) -> str:
    """把分桶 EV + D1 渲染成 markdown。"""
    lines = [
        "# 🔬 皮諾丘交易覆盤（驗屍）摘要",
        "",
        _honesty_banner_md(),
        "",
        f"更新時間：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}",
        f"本輪新驗屍：{n_new} 筆　｜　累積已平倉樣本：{ev.get('n_total', 0)} 筆",
        "",
        "## 各 setup 期望值",
        "",
        "| setup | n | 勝率% | avg_R | 合計R |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for k, a in sorted(ev.get("setup", {}).items()):
        lines.append(f"| {k} | {a['n']} | {a['win_rate']} | {a['avg_r']:+.2f} | {a['sum_r']:+.2f} |")

    lines += ["", "## 順勢 vs 逆勢", "",
              "| 類別 | n | 勝率% | avg_R | 合計R |",
              "| --- | ---: | ---: | ---: | ---: |"]
    for k, a in sorted(ev.get("countertrend", {}).items()):
        lines.append(f"| {k} | {a['n']} | {a['win_rate']} | {a['avg_r']:+.2f} | {a['sum_r']:+.2f} |")

    lines += ["", "## 廣度環境分桶", "",
              "| 廣度 | n | 勝率% | avg_R | 合計R |",
              "| --- | ---: | ---: | ---: | ---: |"]
    for k, a in sorted(ev.get("breadth", {}).items()):
        lines.append(f"| {k} | {a['n']} | {a['win_rate']} | {a['avg_r']:+.2f} | {a['sum_r']:+.2f} |")

    lp = ev.get("losing_patterns", [])
    lines += ["", "## ⚠️ 賠錢模式（avg_R<0 且樣本足）", ""]
    if lp:
        lines += ["| 模式（setup｜廣度｜順逆） | n | 勝率% | avg_R | 合計R |",
                  "| --- | ---: | ---: | ---: | ---: |"]
        for p in lp:
            lines.append(f"| {p['pattern']} | {p['n']} | {p['win_rate']} | "
                         f"{p['avg_r']:+.2f} | {p['sum_r']:+.2f} |")
    else:
        lines.append("（暫無達門檻的賠錢模式）")

    lines += ["", "## D1 反事實（deepdive 加 breadth 閘 + BTC 4h>200MA）", "",
              f"- 評估筆數：{d1.get('n_eval', 0)}",
              f"- 會被擋：{d1.get('blocked_n', 0)} 筆，合計 {d1.get('blocked_sum_r', 0):+.2f}R",
              f"- 會放行：{d1.get('passed_n', 0)} 筆，合計 {d1.get('passed_sum_r', 0):+.2f}R",
              f"- 判讀：{d1.get('verdict', '')}",
              ""]
    return "\n".join(lines)


def render_weekly_tg(ev: dict, d1: dict) -> str:
    """每週一彙整賠錢模式成建議，HTML 推系統主題（頂部誠實橫幅）。"""
    lp = ev.get("losing_patterns", [])
    blocks = [_honesty_banner_html(),
              "━━━━━━━━━━━━━━━━",
              f"🔬 <b>每週覆盤 Session</b>（紙上帳驗屍，僅建議不自動套用）",
              f"累積已平倉樣本 <code>{ev.get('n_total', 0)}</code> 筆"]

    # setup 期望值速覽
    setup_lines = []
    for k, a in sorted(ev.get("setup", {}).items()):
        setup_lines.append(f"  <b>{k}</b>：{a['n']} 筆｜勝率 {a['win_rate']}%｜"
                           f"期望值 <code>{a['avg_r']:+.2f}R</code>")
    if setup_lines:
        blocks.append("各引擎期望值：\n" + "\n".join(setup_lines))

    if lp:
        tip_lines = []
        for p in lp[:4]:
            tip_lines.append(f"  💡 <b>{p['pattern']}</b>：{p['n']} 筆、"
                             f"avg <code>{p['avg_r']:+.2f}R</code>、合計 "
                             f"<code>{p['sum_r']:+.2f}R</code> → 建議收緊此情境進場條件")
        blocks.append("⚠️ <b>偵測到賠錢模式</b>（建議檢視）：\n" + "\n".join(tip_lines))
    else:
        blocks.append("✅ 本週無達門檻的賠錢模式（樣本仍 &lt;100，續觀察）")

    blocks.append(f"🧪 <b>D1 反事實</b>（deepdive 加 breadth&ge;{d1.get('breadth_gate', '')} "
                  f"且 BTC 4h&gt;200MA）：\n  {d1.get('verdict', '')}")
    blocks.append("<i>採納方式：到 /settings 或 .env 調整對應參數。"
                  "AI 持續評估，參數變更權保留給你。</i>")
    return "\n\n".join(blocks)


# =====================================================================
# 編排：掃新平倉 → 驗屍 → append jsonl + 寫 digest（純讀，不下單）
# =====================================================================
def run_scan_once(notes_path: Path | None = None,
                  digest_path: Path | None = None) -> dict:
    """掃一輪：找新平倉 → 驗屍 → append jsonl + 寫 digest。回統計 dict。

    全程純讀 DB；只寫 jsonl/digest 兩個本機檔案（非交易路徑）。
    trade_journal.db 不存在 → 視為尚無資料，回 n_new=0（不丟例外，讓 loop 安靜過）。
    """
    np_ = notes_path or NOTES_PATH
    dp_ = digest_path or DIGEST_PATH
    try:
        conn = _ro_conn(JOURNAL_DB)
    except Exception as e:
        return {"n_new": 0, "n_total": 0, "note": f"journal not ready: {e}"}
    try:
        closed = _fetch_closed_paper(conn)
    finally:
        conn.close()

    breadth_rows = _fetch_breadth_rows()
    enriched_all = [enrich_with_environment(t, breadth_rows) for t in closed]

    processed = load_processed_keys(np_)
    # 新平倉（去重）— 用未 enrich 的原列判鍵即可，但 enrich 後 dict 也含 dedup_key
    new_closed = filter_new_closed(closed, processed)
    new_enriched = [enrich_with_environment(t, breadth_rows) for t in new_closed]

    # append 新筆（含驗屍欄）
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
    records = []
    for e in new_enriched:
        records.append({
            "dedup_key": e["dedup_key"],
            "processed_at": now_iso,
            "symbol": e.get("symbol"),
            "setup": e.get("setup"),
            "direction": e.get("direction"),
            "regime": e.get("regime"),
            "fire_id": e.get("fire_id"),
            "paper_id": e.get("id"),
            "realized_r": e.get("realized_r"),
            "exit_reason": e.get("exit_reason"),
            "exit_class": e.get("exit_class"),
            "up_pct": e.get("up_pct"),
            "avg_funding": e.get("avg_funding"),
            "countertrend": e.get("countertrend"),
            "entry_at": e.get("entry_at"),
            "exit_at": e.get("exit_at"),
        })
    n_written = append_notes(records, np_)

    # 滾動聚合（用全部已平倉，非只新筆）+ D1 反事實
    ev = bucket_ev(enriched_all)
    btc_4h = _fetch_btc_4h_closes()
    d1 = d1_counterfactual(enriched_all, btc_4h)

    # 寫 digest（覆寫；它是「最新狀態」快照）
    try:
        dp_.parent.mkdir(parents=True, exist_ok=True)
        dp_.write_text(render_digest_md(ev, d1, n_written), encoding="utf-8")
    except Exception as e:
        print(f"[postmortem] digest write failed: {type(e).__name__}: {e}")

    return {"n_new": n_written, "n_total": ev.get("n_total", 0),
            "ev": ev, "d1": d1, "losing_patterns": ev.get("losing_patterns", [])}


def build_weekly_report() -> str | None:
    """每週一彙整：跑一次聚合 + D1，產 TG 文案。無資料回 None。"""
    try:
        conn = _ro_conn(JOURNAL_DB)
    except Exception:
        return None
    try:
        closed = _fetch_closed_paper(conn)
    finally:
        conn.close()
    if not closed:
        return None
    breadth_rows = _fetch_breadth_rows()
    enriched = [enrich_with_environment(t, breadth_rows) for t in closed]
    ev = bucket_ev(enriched)
    if ev.get("n_total", 0) == 0:
        return None
    btc_4h = _fetch_btc_4h_closes()
    d1 = d1_counterfactual(enriched, btc_4h)
    return render_weekly_tg(ev, d1)


# =====================================================================
# 6) worker 進入點
# =====================================================================
async def run_postmortem_loop(tg, scan_interval_seconds: int = 21600,
                              weekly_hour_utc: int = 3):
    """覆盤 Session worker。

    - 每 scan_interval_seconds（預設 6h=21600）掃一次新平倉 → 驗屍 → 寫 jsonl/digest。
    - 每週一（UTC）weekly_hour_utc 時把賠錢模式彙整成建議推系統主題（頂部誠實橫幅）。
    - 例外只 log 不吞成 busy-loop：真正掛掉時讓 supervise() 退避重啟。

    tg：系統主題 client（可 None，無 token 時優雅待命，只寫本機檔）。
    """
    print("[postmortem] loop online（每 6h 驗屍 + 每週一彙整賠錢模式）")
    await asyncio.sleep(300)     # 啟動緩衝，避開開機洗版與首輪掃描尖峰
    last_weekly_date: str | None = None
    while True:
        try:
            stats = run_scan_once()
            print(f"[postmortem] scan: new={stats['n_new']} total={stats['n_total']}")

            now = dt.datetime.now(tz=dt.timezone.utc)
            today_key = now.strftime("%Y-%m-%d")
            # 週一且到點且今天還沒推過 → 推週報
            if now.weekday() == 0 and now.hour >= weekly_hour_utc and last_weekly_date != today_key:
                rep = build_weekly_report()
                if rep and tg is not None:
                    await tg.send_message(rep, parse_mode="HTML")
                    print("[postmortem] weekly report sent")
                else:
                    print("[postmortem] 週報：無資料或無 tg，略過")
                last_weekly_date = today_key     # 不論有無推都標記，避免同日重試洗版
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 不吞成 busy-loop：log 後睡一個間隔再續；連續硬錯由 supervise 退避處理
            print(f"[postmortem] loop error: {type(e).__name__}: {e}")
        await asyncio.sleep(scan_interval_seconds)


if __name__ == "__main__":
    import re
    import sys as _sys
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    # cp950 主控台無法印 emoji → 強制 UTF-8 輸出（僅 CLI 演示用，worker 走 tg 不受影響）
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    s = run_scan_once()
    print(f"new={s['n_new']} total={s['n_total']}")
    rep = build_weekly_report()
    print(re.sub(r"<[^>]+>", "", rep) if rep else "（無週報資料）")
    print(f"digest -> {DIGEST_PATH}")
