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
    # #48 計畫 vs 結果歸因（消費進場凍結的 plan_snapshot；無快照→誠實標 unknown）
    out["plan_attribution"] = attribute_plan_vs_result(out)
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
# 4.5) 計畫 vs 結果歸因（#48 復盤三問）— 純函式、零策略數學、不 import strength
# =====================================================================
# 計畫對比容忍帶（R 的相對誤差）：進出場有滑價、部位分批，給合理帶寬避免吹毛求疵。
PLAN_R_BAND = 0.15

# 與計畫吻合度 / 止損劇本驗證的中文標籤（digest 與 TG 共用，集中維護）
_ADH_LABEL = {
    "as_planned_win": "照計畫達標獲利",
    "exceeded_plan": "超出計畫（賺更多）",
    "under_plan_win": "獲利但不及計畫目標（提早/部分出場）",
    "as_planned_loss": "照計畫認賠（止損上限內）",
    "worse_than_plan": "⚠️ 虧損超出計畫（關鍵教訓）",
    "win_no_plan_r": "獲利（計畫未含目標R）",
    "flat": "打平出場（≈0R）",
    "not_traded": "掛單未成交",
    "unknown": "無法判定",
}
_STOP_LABEL = {
    "stop_as_expected": "止損如預期",
    "stop_worse_than_expected": "⚠️ 止損比預期更糟（滑價/跳空）",
    "stop_not_triggered": "止損未觸發（以其他方式出場）",
    "no_stop_plan": "計畫未含止損劇本",
    "unknown": "無法判定",
}


def _planned_r_targets(plan: dict) -> tuple[float | None, float | None]:
    """從 plan 取 (expected_r=首要目標R, max_planned_r=最遠目標R)。容錯回 (None, None)。"""
    try:
        exp = plan.get("expected_r")
        exp = float(exp) if exp is not None else None
    except Exception:
        exp = None
    mx = None
    rr = plan.get("rr_to_tp")
    if isinstance(rr, dict):
        vals = []
        for k in ("tp1", "tp2", "tp3"):
            v = rr.get(k)
            try:
                if v is not None:
                    vals.append(float(v))
            except Exception:
                pass
        if vals:
            mx = max(vals)
    if exp is None:
        exp = mx
    return exp, mx


def _stop_ceiling_r(plan: dict) -> float:
    """預期止損上限（R 正值幅度）。缺值 → 預設 1.0（與 plan_snapshot 預設劇本一致）。"""
    try:
        ess = plan.get("expected_stop_scenario") or {}
        c = ess.get("expected_mae_ceiling_r")
        return float(c) if c is not None else 1.0
    except Exception:
        return 1.0


def _stop_trigger_desc(ess: dict) -> str:
    """把止損劇本渲染成短描述（給『未觸發』時回顧當初預期）。"""
    tt = ess.get("trigger_type") or "invalidation_level"
    lvl = ess.get("trigger_level")
    return f"{tt}@{lvl}" if lvl is not None else str(tt)


def attribute_plan_vs_result(trade: dict) -> dict:
    """計畫 vs 結果歸因（純函式，零策略數學、不 import strength／不呼叫 evaluate）。

    消費進場時凍結的 plan_snapshot（trade['plan_snapshot']，dict 或 None），對照真實
    出場結果，回答使用者要的復盤三問：
      ① 結果原因（cause）：哪一段劇本上演了（達標／部分／止損／超時）。
      ② 與計畫吻合度（plan_adherence）：照計畫達標 / 超出 / 不及 / 比計畫更糟。
      ③ 止損劇本驗證（stop_scenario_check）：當初預期『觸及失效價約 1R 出場』，
         真止損時是否吻合（吻合 / 比預期更糟＝滑價跳空 / 本次未觸發）。

    無 plan_snapshot（引擎上線前的舊單）→ has_plan=False，比較欄誠實標 unknown、
    不臆測（紅線③）。隨引擎前向累積新單，覆蓋率會上升。回新 dict，不改輸入。
    """
    exit_class = trade.get("exit_class") or classify_exit(
        trade.get("exit_reason"), trade.get("realized_r"), trade.get("legs_hit"))
    try:
        realized_r = trade.get("realized_r")
        realized_r = float(realized_r) if realized_r is not None else None
    except Exception:
        realized_r = None

    cause = {
        "win_full": "達成完整計畫（tp3 全打）",
        "win_partial": "部分止盈後出場（未達最終目標）",
        "stop_loss": "觸及止損出場",
        "timeout": "超時出場（既未達標也未止損）",
        "entry_expired": "掛單未成交（非真實交易）",
        "other": "其他/未明確出場",
    }.get(exit_class, "未知")

    plan = trade.get("plan_snapshot")
    if not isinstance(plan, dict):
        return {
            "has_plan": False, "plan_source": None, "exit_class": exit_class,
            "cause": cause, "plan_adherence": "unknown",
            "plan_adherence_note": "進場時未凍結計畫快照（引擎上線前的舊單）→ 無法做計畫對比",
            "stop_scenario_check": "unknown",
            "stop_scenario_note": "無計畫快照，止損劇本無從驗證",
            "expected_r": None, "max_planned_r": None,
            "realized_r": realized_r, "r_vs_expected": None,
            "expected_mae_ceiling_r": None,
        }

    exp_r, max_r = _planned_r_targets(plan)
    ceiling = _stop_ceiling_r(plan)
    r_vs_exp = (round(realized_r - exp_r, 3)
                if (realized_r is not None and exp_r is not None) else None)

    # ② 與計畫吻合度
    if realized_r is None:
        adh, adh_note = "unknown", "無真實 R，無法對比計畫"
    elif exit_class == "entry_expired":
        adh, adh_note = "not_traded", "掛單未成交，計畫未執行"
    elif realized_r > 0:
        if max_r is not None and realized_r > max_r * (1 + PLAN_R_BAND):
            adh = "exceeded_plan"
            adh_note = f"獲利 {realized_r:+.2f}R 超出最遠計畫目標 {max_r:+.2f}R（賺得比計畫多）"
        elif exp_r is not None and realized_r >= exp_r * (1 - PLAN_R_BAND):
            adh = "as_planned_win"
            adh_note = f"獲利 {realized_r:+.2f}R 達計畫目標區（首要目標 {exp_r:+.2f}R）"
        elif exp_r is not None:
            adh = "under_plan_win"
            adh_note = f"獲利 {realized_r:+.2f}R 但不及計畫首要目標 {exp_r:+.2f}R（提早/部分出場）"
        else:
            adh = "win_no_plan_r"
            adh_note = f"獲利 {realized_r:+.2f}R（計畫未含目標 R，無對比基準）"
    elif realized_r == 0:
        adh, adh_note = "flat", "打平出場（≈0R）"
    else:
        if realized_r >= -ceiling * (1 + PLAN_R_BAND):
            adh = "as_planned_loss"
            adh_note = f"虧損 {realized_r:+.2f}R 在計畫止損上限（約 -{ceiling:.2f}R）內（照計畫認賠）"
        else:
            adh = "worse_than_plan"
            adh_note = (f"虧損 {realized_r:+.2f}R 超出計畫止損上限 -{ceiling:.2f}R"
                        "（滑價/跳空/止損未如期保護 → 關鍵教訓）")

    # ③ 止損劇本驗證
    ess = plan.get("expected_stop_scenario")
    ess = ess if isinstance(ess, dict) else None
    if ess is None:
        stop_chk, stop_note = "no_stop_plan", "計畫未含止損劇本"
    elif exit_class != "stop_loss":
        stop_chk = "stop_not_triggered"
        stop_note = (f"本次以 {exit_class} 出場，未觸發止損 → 當初『{_stop_trigger_desc(ess)}』"
                     "的止損劇本本次未上演")
    elif realized_r is None:
        stop_chk, stop_note = "unknown", "止損出場但無真實 R，無法驗證"
    elif realized_r >= -ceiling * (1 + PLAN_R_BAND):
        stop_chk = "stop_as_expected"
        stop_note = (f"止損如預期：觸及失效價出場、虧損 {realized_r:+.2f}R "
                     f"在預期上限 -{ceiling:.2f}R 內（劇本吻合）")
    else:
        stop_chk = "stop_worse_than_expected"
        stop_note = (f"止損但比預期更糟：虧損 {realized_r:+.2f}R 超出預期上限 -{ceiling:.2f}R"
                     "（疑似滑價/跳空，止損未如期保護 → 關鍵教訓）")

    return {
        "has_plan": True,
        "plan_source": plan.get("source"),
        "exit_class": exit_class,
        "cause": cause,
        "plan_adherence": adh,
        "plan_adherence_note": adh_note,
        "stop_scenario_check": stop_chk,
        "stop_scenario_note": stop_note,
        "expected_r": exp_r,
        "max_planned_r": max_r,
        "realized_r": realized_r,
        "r_vs_expected": r_vs_exp,
        "expected_mae_ceiling_r": ceiling,
    }


def bucket_plan_attribution(enriched: list[dict]) -> dict:
    """聚合計畫對比結果（純函式）。回快照覆蓋率 + adherence/stop_check 計數。

    排除 entry_expired（非真實交易）。覆蓋率＝有快照的真實單 / 全部真實單；
    引擎上線前的舊單無快照，覆蓋率會隨新單前向累積而上升（誠實呈現，不臆測舊單）。
    """
    real = [e for e in enriched if e.get("exit_class") != "entry_expired"]
    n_real = len(real)
    adh_counts: dict[str, int] = {}
    stop_counts: dict[str, int] = {}
    n_with_plan = 0
    for e in real:
        pa = e.get("plan_attribution") or {}
        if not pa.get("has_plan"):
            continue
        n_with_plan += 1
        a = pa.get("plan_adherence", "unknown")
        s = pa.get("stop_scenario_check", "unknown")
        adh_counts[a] = adh_counts.get(a, 0) + 1
        stop_counts[s] = stop_counts.get(s, 0) + 1
    return {
        "n_real": n_real,
        "n_with_plan": n_with_plan,
        "coverage_pct": round(n_with_plan / n_real * 100, 1) if n_real else 0.0,
        "adherence_counts": adh_counts,
        "stop_check_counts": stop_counts,
    }


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
    """純讀所有 closed paper_trades（含 entry_expired，後續分類時再排）。

    含 plan_snapshot 欄（#47 進場凍結的計畫快照，TEXT/JSON）：在此反序列化成 dict，
    供 #48 計畫 vs 結果歸因消費。舊單（引擎上線前）此欄為 NULL → 回 None（誠實標未捕捉）。
    """
    rows = conn.execute(
        "SELECT id, symbol, setup, direction, entry_price, stop_price, "
        "tp1, tp2, tp3, status, legs_hit, pnl_usd, realized_r, exit_reason, "
        "entry_at, exit_at, fire_id, regime, plan_snapshot "
        "FROM paper_trades WHERE status='closed' ORDER BY exit_at"
    ).fetchall()
    cols = ["id", "symbol", "setup", "direction", "entry_price", "stop_price",
            "tp1", "tp2", "tp3", "status", "legs_hit", "pnl_usd", "realized_r",
            "exit_reason", "entry_at", "exit_at", "fire_id", "regime", "plan_snapshot"]
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        ps = d.get("plan_snapshot")
        if isinstance(ps, str) and ps:
            try:
                d["plan_snapshot"] = json.loads(ps)
            except Exception:
                d["plan_snapshot"] = None      # 壞 JSON → 視同未捕捉，不臆測
        else:
            d["plan_snapshot"] = None
        out.append(d)
    return out


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
def render_digest_md(ev: dict, d1: dict, n_new: int, pa: dict | None = None,
                     mci: dict | None = None, tai: dict | None = None) -> str:
    """把分桶 EV + 計畫對比 + 缺數據誤判(item-d) + 大盤方向濾網追蹤(#4) + D1 渲染成 md。"""
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

    if pa:
        cov_warn = ("　⚠️ 引擎上線前的舊單無快照，覆蓋率會隨新單前向累積而上升"
                    if pa.get("coverage_pct", 0) < 100 else "")
        lines += ["", "## 📋 計畫 vs 結果歸因（復盤三問）", "",
                  f"- 計畫快照覆蓋率：{pa.get('n_with_plan', 0)}/{pa.get('n_real', 0)} 筆"
                  f"（{pa.get('coverage_pct', 0)}%）{cov_warn}"]
        adh = pa.get("adherence_counts", {})
        if adh:
            lines += ["", "**② 與計畫吻合度：**"]
            for k, v in sorted(adh.items(), key=lambda x: -x[1]):
                lines.append(f"- {_ADH_LABEL.get(k, k)}：{v} 筆")
        sc = pa.get("stop_check_counts", {})
        if sc:
            lines += ["", "**③ 止損劇本驗證：**"]
            for k, v in sorted(sc.items(), key=lambda x: -x[1]):
                lines.append(f"- {_STOP_LABEL.get(k, k)}：{v} 筆")

    # 🧩 item-d（v102 治本）：缺數據誤判排行——接 review_attribution.missing_context_impact，
    #   把『缺哪個數據時平均 R 最差』自動帶進每輪 digest，讓復盤閉環自動指出下一個最該回補的
    #   因子（過去此分析只在 CLI、未進 daemon）。純觀測、小樣本標未達顯著（紅線③）。
    if mci:
        ranked = sorted(
            (kv for kv in mci.items()
             if kv[1].get("gap_absent_minus_present") is not None
             and kv[1].get("n_absent", 0) >= 3 and kv[1].get("n_present", 0) >= 3),
            key=lambda kv: kv[1]["gap_absent_minus_present"])   # 最負(缺它最傷)在前
        lines += ["", "## 🧩 缺數據誤判排行（item-d：缺哪個數據時結果最差 → 最該回補）", ""]
        if ranked:
            lines += ["| 數據因子 | 缺席n | 在場n | 缺席avgR | 在場avgR | 差距(缺−在) |",
                      "| --- | ---: | ---: | ---: | ---: | ---: |"]
            for k, v in ranked[:8]:
                lines.append(f"| {k} | {v['n_absent']} | {v['n_present']} | "
                             f"{v['mean_r_when_absent']:+.2f} | {v['mean_r_when_present']:+.2f} | "
                             f"{v['gap_absent_minus_present']:+.2f} |")
            lines += ["", "_差距為負＝該數據缺席時平均 R 較差＝引擎下一個最該自動回補的因子；"
                      "樣本小僅供觀察、未達統計顯著（紅線③）。_"]
        else:
            lines.append("（缺席/在場樣本皆未達 3 筆，暫無可比；待樣本累積自動充實）")

    # 🧭 #4：大盤方向濾網假說追蹤——順勢(進場與 BTC 4h200MA 同向) vs 逆勢 的 EV。
    #   止損復盤 n=6 之全樣本放大；複用既有 plan_snapshot、零新請求。樣本足(t≥2)才走 L2 晉升濾網。
    if tai and ((tai.get("aligned") or {}).get("n") or (tai.get("counter") or {}).get("n")):
        a = tai.get("aligned") or {}
        c = tai.get("counter") or {}
        gap = tai.get("gap")
        lines += ["", "## 🧭 大盤方向濾網假說追蹤（順勢 vs 逆勢，#4）", "",
                  f"- 順勢（與 BTC 4h200MA 同向）：n={a.get('n', 0)}　"
                  f"EV={a.get('ev')}R　t={a.get('t')}",
                  f"- 逆勢（與大盤相反）：n={c.get('n', 0)}　"
                  f"EV={c.get('ev')}R　t={c.get('t')}"]
        if gap is not None:
            lines.append(f"- **EV 差距（順勢−逆勢）：{gap:+.3f}R**"
                         + ("　← 順勢較優；待樣本足(t≥2)過 L2 即可晉升『大盤方向濾網』"
                            if gap > 0 else ""))
        at = a.get("t")
        if at is None or abs(at) < 2:
            lines.append("　_樣本未達統計顯著(t<2)，僅供觀察；不據此硬改進場，待 "
                         "champion/challenger→L2（紅線③）。_")

    lines += ["", "## D1 反事實（deepdive 加 breadth 閘 + BTC 4h>200MA）", "",
              f"- 評估筆數：{d1.get('n_eval', 0)}",
              f"- 會被擋：{d1.get('blocked_n', 0)} 筆，合計 {d1.get('blocked_sum_r', 0):+.2f}R",
              f"- 會放行：{d1.get('passed_n', 0)} 筆，合計 {d1.get('passed_sum_r', 0):+.2f}R",
              f"- 判讀：{d1.get('verdict', '')}",
              ""]
    return "\n".join(lines)


def render_weekly_tg(ev: dict, d1: dict, pa: dict | None = None) -> str:
    """每週一彙整賠錢模式成建議，HTML 推系統主題（頂部誠實橫幅）。"""
    lp = ev.get("losing_patterns", [])
    blocks = [_honesty_banner_html(),
              "━━━━━━━━━━━━━━━━",
              f"🔬 <b>每週覆盤 Session</b>（紙上帳驗屍，純描述觀測；任何調整一律走 L2 四關，不自動套用）",
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
            tip_lines.append(f"  • <b>{p['pattern']}</b>：{p['n']} 筆、"
                             f"avg <code>{p['avg_r']:+.2f}R</code>、合計 "
                             f"<code>{p['sum_r']:+.2f}R</code>（純描述）")
        blocks.append("⚠️ <b>偵測到賠錢模式</b>（純描述；是否調整由 champion/challenger→L2 "
                      "四關以統計嚴謹度判定，非此報告口語建議）：\n" + "\n".join(tip_lines))
    else:
        blocks.append("✅ 本週無達門檻的賠錢模式（樣本仍 &lt;100，續觀察）")

    if pa and pa.get("n_real"):
        cov = f"{pa.get('n_with_plan', 0)}/{pa.get('n_real', 0)}（{pa.get('coverage_pct', 0)}%）"
        wtp = pa.get("adherence_counts", {}).get("worse_than_plan", 0)
        sworse = pa.get("stop_check_counts", {}).get("stop_worse_than_expected", 0)
        blocks.append(f"📋 <b>計畫 vs 結果</b>：快照覆蓋 {cov}｜"
                      f"虧損超出計畫 <code>{wtp}</code> 筆｜"
                      f"止損比預期更糟 <code>{sworse}</code> 筆")

    blocks.append(f"🧪 <b>D1 反事實</b>（deepdive 加 breadth&ge;{d1.get('breadth_gate', '')} "
                  f"且 BTC 4h&gt;200MA）：\n  {d1.get('verdict', '')}")
    blocks.append("<i>此為唯讀紙上帳描述。任何參數調整一律走 champion/challenger→L2 四關"
                  "（minTRL/DSR/PBO/FDR）以統計嚴謹度自動把關，不依此報告口語建議手動改"
                  "（避免繞過 L2）；真錢永遠人工（紅線①）。</i>")
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
        pa_e = e.get("plan_attribution") or {}
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
            # #48 計畫 vs 結果歸因（復盤三問留痕；無快照→has_plan=False）
            "has_plan": pa_e.get("has_plan"),
            "cause": pa_e.get("cause"),
            "plan_adherence": pa_e.get("plan_adherence"),
            "plan_adherence_note": pa_e.get("plan_adherence_note"),
            "stop_scenario_check": pa_e.get("stop_scenario_check"),
            "stop_scenario_note": pa_e.get("stop_scenario_note"),
            "r_vs_expected": pa_e.get("r_vs_expected"),
        })
    n_written = append_notes(records, np_)

    # 滾動聚合（用全部已平倉，非只新筆）+ 計畫對比 + D1 反事實
    ev = bucket_ev(enriched_all)
    pa = bucket_plan_attribution(enriched_all)
    btc_4h = _fetch_btc_4h_closes()
    d1 = d1_counterfactual(enriched_all, btc_4h)

    # item-d（v102 治本）：缺數據誤判分析接進 daemon——過去 review_attribution.missing_context_impact
    #   只在 CLI、digest 只報覆蓋率不報「哪個漏看因子最傷 EV」。純讀、失敗不致命（不阻塞驗屍）。
    mci = tai = None
    try:
        from backtest import review_attribution as _ra
        _rows = _ra.load_closed()
        mci = _ra.analyze(_rows).get("missing_context_impact")
        tai = _ra.trend_alignment_impact(_rows)   # #4：順勢 vs 逆勢 EV 追蹤
    except Exception as e:  # noqa: BLE001
        print(f"[postmortem] 缺數據/大盤方向分析略過：{type(e).__name__}: {e}")

    # 寫 digest（覆寫；它是「最新狀態」快照）
    try:
        dp_.parent.mkdir(parents=True, exist_ok=True)
        dp_.write_text(render_digest_md(ev, d1, n_written, pa, mci, tai), encoding="utf-8")
    except Exception as e:
        print(f"[postmortem] digest write failed: {type(e).__name__}: {e}")

    return {"n_new": n_written, "n_total": ev.get("n_total", 0),
            "ev": ev, "d1": d1, "plan_attribution": pa,
            "losing_patterns": ev.get("losing_patterns", [])}


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
    pa = bucket_plan_attribution(enriched)
    btc_4h = _fetch_btc_4h_closes()
    d1 = d1_counterfactual(enriched, btc_4h)
    return render_weekly_tg(ev, d1, pa)


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
