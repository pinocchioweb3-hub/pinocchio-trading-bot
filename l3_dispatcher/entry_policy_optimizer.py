"""復盤引擎 step9-c（task#61）── 入場積極度自動優化器（編排層）。

把零散模組串成一條閉環（與 auto_optimizer.py 同骨架，差別在『需要逐根 K 線重放』）：
    已平倉/逾時 paper_trades ──建 EntryPlan＋取真實後續 K 線──▶ entry_policy_cc 重放
        champion(現行深限價可到期) vs challenger(D 深限價到期轉市價／市價即進)
        ──過 L2 四關(minTRL/DSR/PBO/FDR)──▶ verdict.promote?
            是 → entry_policy_store 寫「活躍入場積極度覆寫」→ 模擬盤下一筆同桶進場即生效
            否 → 只寫稽核（hold 留痕），活躍表不動（但 coverage_delta_pp 仍揭示救回多少涵蓋率）

為何與 auto_optimizer 不同需要新編排：
    auto_optimizer 只重放『已記錄的 TP 分批腿』（champion_challenger.replay_trade_r，不需 K 線）。
    入場積極度要回答『限價會不會成交、到期追市價會怎樣』＝必須逐根 K 線重放（candle-replay）。
    本檔的核心新增＝**K 線資料配接器**：把每筆計畫凍結的 (symbol, entry_at, planned_*) 接上
    data_loader 的快取 OHLC，定位訊號根 signal_idx，餵給 entry_policy_cc.compare_entry_policy。

把關＝統計嚴謹度（L2），非人工逐次點頭（INTENT #1）。可由 auto_tuner/復盤官每日呼叫。

安全紅線落點（與 entry_policy_cc／entry_policy_store 一致）：
    紅線①：全程唯讀快取 OHLC、零下單、零訊號數學變更；產出只寫模擬盤入場積極度覆寫表，
            真錢執行層完全不讀。本檔不 import、不呼叫任何下單 API。
    紅線③：未成交誠實計 0R（per-proposed）；今日對齊樣本 <30 → L2 minTRL fail-closed →
            0 晉升 → 活躍表恆空 → 零行為改變（由 _selftest 驗證）。**只用真實後續價格重放
            反事實成交結果**（合法），不回填任何進場當下未捕捉的 regime 快照（禁 look-ahead 污染）。

重要納入範圍（與 auto_optimizer 相反）：
    auto_optimizer **排除** entry_expired；本檔**刻意納入** entry_expired——那些正是 champion
    沒成交、D 可能救回的『缺的數據』。納入後 reality_filled=False，只進重放與涵蓋率統計，
    不進 self-check（self-check 只查『現實真成交者 champion 重放也必須成交』）。
"""
from __future__ import annotations

import asyncio
import bisect
import json
import sqlite3
import time

from botpaths import db_path as _db_path
from l3_dispatcher import entry_policy_store as eps
from l3_dispatcher.entry_policy_cc import (
    CHAMPION, CHALLENGER_CONVERT, CHALLENGER_MARKET,
    EntryPlan, EntryPolicy, bucket_key as cc_bucket_key, compare_entry_policy,
)

# 與重放原語同一真相來源（窗口尺度必須一致）
from backtest.entry_placement_ab import FILL_EXPIRY
from backtest.stop_placement_ab import TF, HOLD_MAX

DB_PATH = _db_path("trade_journal.db")
_STEP_MS = 3_600_000                      # 1h（TF 固定 1h，deepdive 限價單與 live PENDING 同尺度）
_FORWARD_BARS = 1 + FILL_EXPIRY + HOLD_MAX  # 完整重放所需的訊號後根數（133）；不足者誠實略過
_LOOKBACK_DAYS = 3                          # 訊號前緩衝（只為定位 signal_idx，重放不看前方）

# 固定 epoch：讓同 (bucket, champ, chal) 的 trial_id 跨日穩定 → 重跑不灌水 n_trials（同 auto_optimizer）。
_LEDGER_EPOCH_MS = 1_700_000_000_000

# kind → 政策物件（單一真相；resolve 出的 kind 對映回政策）
_KIND_TO_POLICY: dict[str, EntryPolicy] = {
    CHAMPION.kind: CHAMPION,
    CHALLENGER_CONVERT.kind: CHALLENGER_CONVERT,
    CHALLENGER_MARKET.kind: CHALLENGER_MARKET,
}
# 全政策池：champion 之外者即為挑戰者（家族大小＝2，刻意小以免 FDR/PBO 失真）
_ALL_POLICIES = (CHAMPION, CHALLENGER_CONVERT, CHALLENGER_MARKET)


# ════════════════════════════════════════════════════════════════════════
#  載入（含 entry_expired；自含查詢）
# ════════════════════════════════════════════════════════════════════════
_COLS = ["id", "symbol", "setup", "direction", "entry_price", "stop_price", "tp1",
         "entry_at", "exit_reason", "plan_snapshot", "entry_splits"]


def _load_paper_for_entry(days: int = 120, db=None) -> list[dict]:
    """載入近 days 天『所有已平倉（含 entry_expired 逾時未成交）』紙上單。

    與 auto_optimizer 相反：**納入** entry_expired（那是 champion 沒成交、D 要救的樣本）。
    v114(稽核rank3)：只收加密 deepdive——池化桶過去混入美股 us_breakout（1h 突破、
    成交機制不同）＝跨引擎統計污染；晉升決策必須建立在引擎純淨樣本上。"""
    cutoff = int(time.time() * 1000) - days * 86400 * 1000
    conn = sqlite3.connect(str(db or DB_PATH))
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        rows = conn.execute(
            f"SELECT {', '.join(_COLS)} FROM paper_trades "
            "WHERE status='closed' AND setup='deepdive' AND entry_at>=? "
            "ORDER BY symbol, entry_at",
            (cutoff,)).fetchall()
    finally:
        conn.close()
    return [dict(zip(_COLS, r)) for r in rows]


def _quadrant_of(row: dict) -> str:
    """plan_snapshot.regime_at_entry.oi_price_quadrant（與 lessons_store/auto_optimizer 同切面）。"""
    try:
        snap = json.loads(row.get("plan_snapshot") or "") or {}
    except Exception:
        snap = {}
    return ((snap or {}).get("regime_at_entry") or {}).get("oi_price_quadrant") or "unknown"


def _plan_prices(row: dict) -> tuple[str, float, float, float] | None:
    """從 plan_snapshot（優先，凍結於訊號當下）抽 (direction, limit_px, stop_px, tp_px)；
    缺則退回欄位。任何缺值/退化 → None（誠實略過，不臆造）。"""
    try:
        snap = json.loads(row.get("plan_snapshot") or "") or {}
    except Exception:
        snap = {}
    direction = (snap.get("direction") or row.get("direction") or "").strip()
    limit_px = snap.get("planned_entry", row.get("entry_price"))
    # v114(稽核rank1治本)：champion 重放的限價必須忠實於「實際首格」——紙上掛的是
    #   分段限價(entry_splits)，先成交的是最淺格(bull=最高格、bear=最低格)；舊碼用
    #   中點 planned_entry 重放＝比首格更深 → 現實已成交的單被重放判「未成交」→
    #   self-check 永久打假 → 已過 L2 四關的晉升被卡死。有 splits 用首格，缺→退回中點。
    try:
        _splits = json.loads(row.get("entry_splits") or "")
        _sp = [float(s["price"]) for s in _splits
               if isinstance(s, dict) and s.get("price") is not None]
    except Exception:
        _sp = []
    if _sp:
        limit_px = max(_sp) if direction in ("bull", "long") else min(_sp)
    stop_px = snap.get("planned_stop", row.get("stop_price"))
    tp_px = ((snap.get("planned_tp") or {}).get("tp1")
             if isinstance(snap.get("planned_tp"), dict) else None)
    if tp_px is None:
        tp_px = row.get("tp1")
    try:
        limit_px, stop_px, tp_px = float(limit_px), float(stop_px), float(tp_px)
    except (TypeError, ValueError):
        return None
    if direction not in ("bull", "bear", "long", "short"):
        return None
    if abs(limit_px - stop_px) <= 0:          # 退化風險距離
        return None
    return direction, limit_px, stop_px, tp_px


# ════════════════════════════════════════════════════════════════════════
#  K 線資料配接器（每 symbol 取一段共用序列，per-trade 定位 signal_idx）
# ════════════════════════════════════════════════════════════════════════
def _signal_idx(ts_list: list[int], entry_at: int) -> int:
    """訊號根＝ts ≤ entry_at 的最後一根索引；無則 -1。"""
    i = bisect.bisect_right(ts_list, int(entry_at)) - 1
    return i


async def load_plans_and_candles(rows: list[dict], *, get_ohlc=None
                                 ) -> tuple[list[EntryPlan], dict[str, str],
                                            dict[str, list[dict]]]:
    """把資料列轉成 (plans, quadrant_by_pid, candles_by_pid)。

    效率：每個 symbol 只抓**一段**涵蓋其所有訊號的 K 線序列，所有該 symbol 的計畫共用同一 list
    （以絕對 signal_idx 索引）。完整重放需訊號後 _FORWARD_BARS 根；不足（如太新）者誠實略過。
    """
    if get_ohlc is None:
        from backtest.data_loader import get_ohlc as _g
        get_ohlc = _g

    # 先解析價格/象限/方向，按 symbol 分組（只留可建計畫者）
    by_symbol: dict[str, list[tuple[dict, tuple]]] = {}
    for row in rows:
        prices = _plan_prices(row)
        if prices is None:
            continue
        by_symbol.setdefault(row["symbol"], []).append((row, prices))

    plans: list[EntryPlan] = []
    quad_by_pid: dict[str, str] = {}
    candles_by_pid: dict[str, list[dict]] = {}
    fwd_ms = _FORWARD_BARS * _STEP_MS

    for sym, items in by_symbol.items():
        entries = [int(r["entry_at"]) for r, _ in items]
        end_ms = max(entries) + fwd_ms
        start_desired = min(entries) - _LOOKBACK_DAYS * 86_400_000
        days = max(1, (end_ms - start_desired) // 86_400_000 + 1)
        try:
            bars = await get_ohlc(sym, TF, int(days), end_ms=end_ms)
        except Exception:
            bars = []
        if not bars:
            continue                          # 無源（如美股/黃金 OKX 無永續）→ 整 symbol 略過
        ts_list = [int(b["ts"]) for b in bars]
        n = len(bars)
        for row, (direction, limit_px, stop_px, tp_px) in items:
            si = _signal_idx(ts_list, int(row["entry_at"]))
            if si < 0:
                continue                      # 訊號早於序列起點
            if n - si - 1 < _FORWARD_BARS:    # 訊號後完整窗不足（多為太新）→ 公平起見略過
                continue
            pid = str(row["id"])
            reality_filled = (row.get("exit_reason") or "") != "entry_expired"
            plans.append(EntryPlan(pid=pid, symbol=sym, direction=direction,
                                   signal_idx=si, limit_px=limit_px, stop_px=stop_px,
                                   tp_px=tp_px, reality_filled=reality_filled))
            quad_by_pid[pid] = _quadrant_of(row)
            candles_by_pid[pid] = bars        # 共用參照（同 symbol 同序列）
    return plans, quad_by_pid, candles_by_pid


# ════════════════════════════════════════════════════════════════════════
#  優化核心（同步、可注入；不碰 DB/網路）
# ════════════════════════════════════════════════════════════════════════
# ── task#62 池化層級（rank 決定處理順序＝由一般到具體） ───────────────────
_LEVEL_GLOBAL = "global"
_LEVEL_QUAD = "quadrant-pool"
_LEVEL_SYMBOL = "per-symbol"
_LEVEL_RANK = {_LEVEL_GLOBAL: 0, _LEVEL_QUAD: 1, _LEVEL_SYMBOL: 2}


def _level_of(symbol: str, quadrant: str) -> str:
    if symbol == eps.POOL and quadrant == eps.POOL:
        return _LEVEL_GLOBAL
    if symbol == eps.POOL:
        return _LEVEL_QUAD
    return _LEVEL_SYMBOL


def optimize_entry(plans: list[EntryPlan], quad_by_pid: dict[str, str],
                   candles_by_pid: dict[str, list[dict]], *, at_ms: int,
                   ledger, active_path=None, audit_path=None) -> dict:
    """task#62 階層式部分池化分桶：每筆計畫同時餵進三個桶
        (symbol, quadrant)  per-symbol×regime（最具體；使用者本意的特化層）
        (POOL,   quadrant)  象限池（跨 symbol、同 regime）
        (POOL,   POOL)      全域池（跨一切；task#59 regime-invariant → 最有據、學最快）
    各桶獨立過 L2 晉升；消費端 resolve_entry_policy 取最具體有覆寫者（見 store ladder）。

    處理順序＝由一般到具體（全域→象限→per-symbol），讓具體桶的 champion 能**繼承**本輪
    已晉升的池化覆寫（階層繼承一致：具體層只在能勝過繼承來的池化政策時才特化）。

    統計安全：家族每桶仍＝2 挑戰者；L2 的 n_trials／FDR 族群／SR 變異**per-bucket_key 隔離**
    （見 l2_stat_gates.evaluate_candidate 的 distinct_trials(bucket_key)），故新增池化桶
    （鍵為 *|q、*|*，與 per-symbol 鍵不同）**不抬高既有 per-symbol 桶的門檻**。
    """
    groups: dict[tuple[str, str], list[EntryPlan]] = {}
    for p in plans:
        q = quad_by_pid.get(p.pid, "unknown")
        groups.setdefault((p.symbol, q), []).append(p)            # per-symbol × regime
        groups.setdefault((eps.POOL, q), []).append(p)            # 象限池（跨 symbol）
        groups.setdefault((eps.POOL, eps.POOL), []).append(p)     # 全域池（跨一切）

    def _order(kv):
        (sym, q), _ps = kv
        return (_LEVEL_RANK[_level_of(sym, q)], sym or "", q or "")

    buckets = []
    for (sym, q), ps in sorted(groups.items(), key=_order):
        buckets.append(_optimize_bucket(sym, q, ps, candles_by_pid, at_ms=at_ms,
                                        ledger=ledger, active_path=active_path,
                                        audit_path=audit_path))
    n_promoted = sum(1 for b in buckets
                     if b["applied"] and b["applied"]["action"] == "promote")
    n_pooled = sum(1 for b in buckets if b["level"] != _LEVEL_SYMBOL)
    return {"at_ms": at_ms, "n_buckets": len(buckets), "n_pooled": n_pooled,
            "n_plans": len(plans), "n_promoted": n_promoted, "buckets": buckets}


def _optimize_bucket(symbol: str, quadrant: str, plans: list[EntryPlan],
                     candles_by_pid: dict[str, list[dict]], *, at_ms: int,
                     ledger, active_path=None, audit_path=None) -> dict:
    bkey = eps.bucket_key(symbol, quadrant)
    cc_bkey = cc_bucket_key(symbol, quadrant)

    # champion＝此桶現行生效政策：活躍覆寫優先，否則預設深限價可到期。
    cur_kind = eps.resolve_entry_policy(symbol, quadrant, active_path=active_path)
    champ = _KIND_TO_POLICY.get(cur_kind or CHAMPION.kind, CHAMPION)
    challengers = [p for p in _ALL_POLICIES if p.kind != champ.kind]

    evaluated: list[tuple[EntryPolicy, object]] = []
    for chal in challengers:
        hyp = f"entry:{champ.kind}->{chal.kind}"
        v = compare_entry_policy(plans, candles_by_pid, chal, bucket_key=cc_bkey,
                                 ledger=ledger, champion=champ, hypothesis=hyp,
                                 registered_at_ms=_LEDGER_EPOCH_MS)
        evaluated.append((chal, v))

    # 選定（task#63 三層優先序）：
    #   ① EV 超越性晉升者中 per-proposed 平均 R 最高（promoted）。
    #   ② 否則涵蓋率非劣性晉升者中 涵蓋率回補最大（cov_promoted，僅 D／limit_convert）——
    #      解 D 因 EV-neutral 永過不了①、導致 live trade_monitor 到期轉市價分支卡死。
    #   ③ 皆未晉升 → 取涵蓋率回補最大者當 hold 留痕（揭示餵桶價值，活躍表不動）。
    def _mean(v):
        return v.chal_mean_r if getattr(v, "chal_mean_r", None) is not None else -1e9

    def _cov(v):
        return v.coverage_delta_pp if getattr(v, "coverage_delta_pp", None) is not None else -1e9

    promoted = [(c, v) for c, v in evaluated if v.promote]
    cov_promoted = [(c, v) for c, v in evaluated if getattr(v, "coverage_promote", False)]
    if promoted:
        chosen = max(promoted, key=lambda cv: _mean(cv[1]))
    elif cov_promoted:
        chosen = max(cov_promoted, key=lambda cv: _cov(cv[1]))
    elif evaluated:
        chosen = max(evaluated, key=lambda cv: _cov(cv[1]))
    else:
        chosen = None

    applied = None
    if chosen is not None:
        chal_pol, verdict = chosen
        applied = eps.apply_verdict(
            verdict, symbol=symbol, quadrant=quadrant,
            challenger_kind=chal_pol.kind, champion_kind=champ.kind,
            at_ms=at_ms, active_path=active_path, audit_path=audit_path)

    return {"bucket": bkey, "symbol": symbol, "quadrant": quadrant,
            "level": _level_of(symbol, quadrant),
            "n_plans": len(plans), "champion_kind": champ.kind,
            "evaluated": evaluated, "chosen": chosen, "applied": applied}


# ════════════════════════════════════════════════════════════════════════
#  全跑（async：載入＋取 K 線＋優化）
# ════════════════════════════════════════════════════════════════════════
async def run_entry_optimization(*, days: int = 120, at_ms: int | None = None,
                                 rows: list[dict] | None = None, ledger=None,
                                 active_path=None, audit_path=None, db=None,
                                 get_ohlc=None) -> dict:
    """載入(含 entry_expired)→取 K 線→建計畫→分桶→逐桶優化。rows/get_ohlc 可注入（測試）。"""
    if at_ms is None:
        at_ms = int(time.time() * 1000)
    if rows is None:
        rows = _load_paper_for_entry(days=days, db=db)
    if ledger is None:
        from backtest.l2_stat_gates import TrialLedger, default_ledger_path
        ledger = TrialLedger(default_ledger_path())

    plans, quad_by_pid, candles_by_pid = await load_plans_and_candles(rows, get_ohlc=get_ohlc)
    # v130（獵捕workflow）：optimize_entry=119桶×逐根重放+trial_ledger全檔re-parse，實測
    #   72.5s 純同步——v129 只包了 auto_tuner 的 3 段、漏了這段（它在 await 皮下面）。
    #   丟 thread 讓事件迴圈/心跳照轉（async 取 K 線段留在迴圈上）。
    res = await asyncio.to_thread(
        optimize_entry, plans, quad_by_pid, candles_by_pid, at_ms=at_ms, ledger=ledger,
        active_path=active_path, audit_path=audit_path)
    res["n_rows"] = len(rows)
    res["n_eligible"] = len(plans)
    return res


# ════════════════════════════════════════════════════════════════════════
#  繁中報告（CEO/復盤 Session 透明化）
# ════════════════════════════════════════════════════════════════════════
def render_report(result: dict | None = None, *, active_path=None) -> str | None:
    if result is None:
        result = asyncio.run(run_entry_optimization(active_path=active_path))
    if not result["buckets"]:
        return None
    # v82：0 晉升日（常態）每桶皆「⏸️維持」零資訊 → 收斂為結論一行＋收斂進度，
    #   把每日 ~4800 字洗版牆壓成可掃讀的一則；全文明細仍可由 active 覆寫表/帳本回溯。
    if result["n_promoted"] == 0:
        # v83(6)：進度用 L2 實際把關的「對齊樣本 n_aligned」估距門檻（raw 桶筆數會樂觀高估）
        _aligned = [getattr(b["chosen"][1], "n_aligned", 0) or 0
                    for b in result["buckets"] if b.get("chosen")]
        max_n = max(_aligned) if _aligned else 0
        prog = (f"，最大對齊樣本 n_aligned={max_n}（差 {30 - max_n} 筆達門檻 30）"
                if max_n < 30 else f"，最大對齊樣本 n_aligned={max_n}")
        return ("🎚️ <b>入場積極度自動優化器</b>\n"
                f"掃 {result.get('n_rows', '?')} 筆／{result['n_buckets']} 桶｜"
                f"本輪晉升 <b>0</b> 桶（最佳挑戰者皆未過 L2{prog}）\n"
                + eps.render_active(active_path) + "\n"
                "<i>純驅動模擬盤 paper／demo，真錢執行層永不讀（紅線①）；"
                "覆寫表恆空＝零行為變更，透明可事後 rollback。</i>")
    lines = ["🎚️ <b>入場積極度自動優化器</b>（過 L2 四關後寫入模擬盤入場政策覆寫表）",
             "━━━━━━━━━━━━━━━━",
             f"掃 {result.get('n_rows', '?')} 筆已平倉/逾時紙上單"
             f"（含 entry_expired 缺料樣本），符合完整重放窗 {result.get('n_eligible', '?')} 筆，"
             f"分 {result['n_buckets']} 桶（per-symbol×regime + 象限池 + 全域池，"
             f"其中池化桶 {result.get('n_pooled', '?')}）｜本輪晉升 {result['n_promoted']} 桶"]
    for b in result["buckets"]:
        ck = eps._KIND_ZH.get(b["champion_kind"], b["champion_kind"])
        head = f"<b>[{eps._bucket_label(b['bucket'])}]</b> {b['n_plans']} 筆｜champion {ck}"
        if not b["evaluated"]:
            lines.append(head + "｜（無可比挑戰者）")
            continue
        cv = b["chosen"]
        chal_pol, v = cv
        kzh = eps._KIND_ZH.get(chal_pol.kind, chal_pol.kind)
        verb = ("✅晉升(EV超越性)" if v.promote
                else ("✅晉升(涵蓋率非劣性)" if getattr(v, "coverage_promote", False)
                      else "⏸️維持"))
        cov = (f"，成交率 {v.champ_fill_rate}%→{v.chal_fill_rate}%(+{v.coverage_delta_pp}pp)"
               if v.coverage_delta_pp is not None else "")
        mr = (f"{v.champ_mean_r:+.3f}→{v.chal_mean_r:+.3f}R"
              if v.champ_mean_r is not None and v.chal_mean_r is not None else "—")
        lines.append(f"{head}\n  最佳挑戰者 {kzh}：{verb}（對齊 n={v.n_aligned}，{mr}{cov}）"
                     f"\n  L2：{v.l2_summary}")
    lines.append("")
    lines.append(eps.render_active(active_path))
    lines.append("<i>晉升此覆寫表有兩條合法路徑：①EV 超越性（過 L2 四關 minTRL/DSR/PBO/FDR ∧ 配對"
                 "更好）②涵蓋率非劣性（task#63，僅 D／limit_convert：EV 對 champion 統計非劣 ∧ 實質"
                 "回補涵蓋率 ∧ n≥30）。消費端**已接線生效**（paper_journal 進場自解析覆寫、trade_monitor"
                 " 到期轉市價）——但**只驅動模擬盤 paper／demo，真錢執行層永不讀**（紅線①）。今日對齊"
                 "樣本未達門檻 → 覆寫表恆空 → resolve 回 None → 模擬盤維持現行深限價可到期行為"
                 "（byte-identical，零行為變更）。entry_expired 樣本即使未晉升，重放也已回補其"
                 "『若用 D 會怎樣』的標籤＝餵飽餓死桶。透明可事後 rollback。</i>")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════
#  離線自測（合成 K 線＋注入 rows/get_ohlc，零網路/DB）
# ════════════════════════════════════════════════════════════════════════
def _selftest() -> bool:
    import tempfile
    from pathlib import Path
    from backtest.l2_stat_gates import TrialLedger

    ts0 = 1_700_000_000_000

    def _series(kind: str) -> list[dict]:
        """造一段夠長（> _FORWARD_BARS+緩衝）的 1h 序列。
        kind='pullback'：訊號後**成交窗內**（前 FILL_EXPIRY 根）即回踩到 94 ≤ 95 限價成交，
                         續漲過 110 打 TP（故 champion 深限價會成交，self-check 一致）。
        kind='trend'   ：一路單調上漲、low 永遠 > 95（champion 永不成交、D 到期追市價救回）。"""
        bars = []
        nbars = _FORWARD_BARS + 80
        for i in range(nbars):
            if kind == "pullback":
                if i == 0:
                    hi, lo, cl = 101, 99, 100        # 訊號根（限價 95 在其下方）
                elif 1 <= i <= 5:
                    hi, lo, cl = 99, 94, 96          # 窗內回踩：low 94 ≤ 95 → 限價成交
                elif 6 <= i <= 40:
                    base = 96 + (i - 6) * 1.0        # 續漲：i=19 時 high=110 ≥ 110 打 TP
                    hi, lo, cl = base + 1, base - 1, base
                else:
                    hi, lo, cl = 130, 128, 129
            else:  # trend：單調漲，low 永遠 > 95
                base = 100 + i * 0.7
                hi, lo, cl = base + 0.5, base - 0.2, base
            bars.append({"ts": ts0 + i * _STEP_MS, "open": cl,
                         "high": float(hi), "low": float(lo), "close": float(cl)})
        return bars

    series_pull = _series("pullback")
    series_trend = _series("trend")

    async def _fake_ohlc(symbol, tf, days, end_ms=None):
        # 依 symbol 回不同情境；signal 永遠落在序列第 0 根（entry_at=ts0）
        return series_pull if symbol == "PULL" else series_trend

    def _row(tid, sym, exit_reason, q="price_up_oi_up"):
        snap = json.dumps({"direction": "bull", "planned_entry": 95.0,
                           "planned_stop": 90.0, "planned_tp": {"tp1": 110.0},
                           "regime_at_entry": {"oi_price_quadrant": q}})
        return {"id": tid, "symbol": sym, "direction": "bull", "entry_price": 95.0,
                "stop_price": 90.0, "tp1": 110.0, "entry_at": ts0,
                "exit_reason": exit_reason, "plan_snapshot": snap}

    with tempfile.TemporaryDirectory() as td:
        ap = Path(td) / eps.ACTIVE_NAME
        au = Path(td) / eps.AUDIT_NAME
        led = TrialLedger(Path(td) / "ledger.jsonl")

        # (a) 配接器：PULL 的限價會成交、TREND 永不回踩 → 建計畫＋定位 signal_idx
        rows = ([_row(i, "PULL", "tp1") for i in range(6)]            # 現實真成交
                + [_row(100 + i, "TREND", "entry_expired") for i in range(6)])  # 逾時未成交
        plans, quad, cbp = asyncio.run(load_plans_and_candles(rows, get_ohlc=_fake_ohlc))
        assert len(plans) == 12, f"應建 12 計畫，實得 {len(plans)}"
        assert all(p.signal_idx == 0 for p in plans), "entry_at=ts0 → signal_idx 應為 0"
        # 現實真成交者 reality_filled=True；entry_expired 者 False
        assert sum(1 for p in plans if p.reality_filled) == 6

        # (b) 小樣本（每桶 <30）→ L2 minTRL fail-closed → 0 晉升、活躍表恆空（誠實答案）
        res = optimize_entry(plans, quad, cbp, at_ms=1, ledger=led,
                             active_path=ap, audit_path=au)
        assert res["n_promoted"] == 0
        assert eps.resolve_entry_policy("PULL", "price_up_oi_up", active_path=ap) is None
        assert eps.resolve_entry_policy("TREND", "price_up_oi_up", active_path=ap) is None
        # 池化桶同樣 inert（樣本 <30）→ resolve 經 ladder 退回仍 None
        assert eps.resolve_entry_policy(eps.POOL, eps.POOL, active_path=ap) is None
        assert eps.resolve_entry_policy(eps.POOL, "price_up_oi_up", active_path=ap) is None
        assert not ap.exists()                # 從未寫活躍表

        # (c) 涵蓋率回補在 TREND 桶必 > 0（champion 不成交、D 救回）
        trend_bucket = next(b for b in res["buckets"] if b["symbol"] == "TREND")
        # 取 D（limit_convert）那個 verdict 來看 coverage
        d_eval = [v for (pol, v) in trend_bucket["evaluated"]
                  if pol.kind == CHALLENGER_CONVERT.kind]
        assert d_eval and d_eval[0].coverage_delta_pp is not None
        assert d_eval[0].coverage_delta_pp > 0, "TREND 桶 D 應救回涵蓋率"

        # (d) self-check：PULL（現實真成交）champion 重放也成交 → self_check_ok=True
        pull_bucket = next(b for b in res["buckets"] if b["symbol"] == "PULL")
        for (_pol, v) in pull_bucket["evaluated"]:
            assert v.self_check_ok, "PULL 現實真成交者 champion 重放應一致成交"

        # (e) task#62 階層分桶：per-symbol(PULL/TREND) + 象限池(*) + 全域池(*,*) = 4 桶
        assert res["n_buckets"] == 4, f"應 4 桶（2 per-symbol + 象限池 + 全域池），實得 {res['n_buckets']}"
        assert res["n_pooled"] == 2, f"池化桶應 2（象限池+全域池），實得 {res['n_pooled']}"
        levels = [b["level"] for b in res["buckets"]]
        # 處理順序＝由一般到具體（level rank 非遞減）：全域→象限→per-symbol
        #   （讓具體桶 champion 能繼承本輪已晉升的池化覆寫）
        ranks = [_LEVEL_RANK[lv] for lv in levels]
        assert ranks == sorted(ranks), f"桶須由一般到具體排序，實得 {levels}"
        assert levels[0] == _LEVEL_GLOBAL and levels[1] == _LEVEL_QUAD
        # 全域池/象限池確實存在且含跨 symbol 全樣本（12 筆）
        gbucket = next(b for b in res["buckets"] if b["level"] == _LEVEL_GLOBAL)
        qbucket = next(b for b in res["buckets"] if b["level"] == _LEVEL_QUAD)
        assert gbucket["n_plans"] == 12 and qbucket["n_plans"] == 12, \
            f"池化桶應含全 12 筆，實得 全域={gbucket['n_plans']} 象限={qbucket['n_plans']}"
        assert gbucket["bucket"] == eps.bucket_key(eps.POOL, eps.POOL)

        # (f) 帳本鏈完整（未被破壞）
        ok, _ = led.verify_chain()
        assert ok

        # (g) 報告不炸
        rep = render_report(res, active_path=ap)
        assert isinstance(rep, str) and "入場積極度自動優化器" in rep

        # (h) 太新（訊號後窗不足）→ 略過。把 entry_at 設到序列尾端附近。
        late_ts = ts0 + (len(series_pull) - 5) * _STEP_MS
        row_late = dict(_row(999, "PULL", "tp1"))
        row_late["entry_at"] = late_ts
        plans2, _, _ = asyncio.run(load_plans_and_candles([row_late], get_ohlc=_fake_ohlc))
        assert plans2 == [], "訊號後完整窗不足者應誠實略過"

        # (i) 無 K 線源（如美股）→ 整 symbol 略過，不崩
        async def _empty_ohlc(symbol, tf, days, end_ms=None):
            return []
        plans3, _, _ = asyncio.run(
            load_plans_and_candles([_row(1, "MU", "tp1")], get_ohlc=_empty_ohlc))
        assert plans3 == []

    print("  自測通過：配接器(定位/略過太新/無源) + 優化(小樣本0晉升/涵蓋率回補/self-check) "
          "+ task#62階層分桶(per-symbol+象限池+全域池/由一般到具體/池化inert) + L2鏈 + 報告 ✅")
    return True


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        ok = _selftest()
        print("selftest:", "PASS" if ok else "FAIL")
        sys.exit(0 if ok else 1)
    from dotenv import load_dotenv
    from pathlib import Path
    import re as _re
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    rep = render_report()
    print(_re.sub(r"<[^>]+>", "", rep) if rep else "（無符合完整重放窗的紙上資料）")
