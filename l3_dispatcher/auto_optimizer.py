"""復盤引擎 step8（task#53）── 自動優化器（編排層）。

職責（把零散模組串成一條閉環）：
    已平倉 paper_trades  ──分桶(symbol×quadrant)──▶  champion/challenger 離線回放
        ──過 L2 四關(minTRL/DSR/PBO/FDR)──▶  verdict.promote?
            是 → auto_param_store 寫「活躍 TP 分配覆寫」→ 模擬盤下一筆同桶進場即生效
            否 → 只寫稽核（hold 留痕），活躍表不動

把關＝統計嚴謹度（L2），非人工逐次點頭（INTENT #1）。每日由 auto_tuner 迴圈呼叫。

安全紅線落點：
    紅線①：本層只動模擬盤 paper／demo 的 TP 分配；真錢執行層完全不讀（config 只驅動
            signal/paper/demo）。本檔不 import、不呼叫任何下單 API。
    紅線③：不捏造。今日對齊樣本 <30 → L2 minTRL fail-closed → 0 晉升 → 活躍表恆空 →
            零行為改變（由 test_auto_optimizer 驗證）；報告誠實標「樣本不足、未晉升」。

L2 家族（multiple-testing）誠實性：
    每桶把固定 CANDIDATE_GRID 的每個挑戰者各送一次 evaluate_candidate（同 bucket_key、
    不同 hypothesis → 不同 trial_id）。家族大小＝grid 大小（固定小，避免 FDR/PBO 失真）。
    用**穩定 hypothesis + 固定 registered_at_ms**（_LEDGER_EPOCH_MS）→ trial_id 穩定 →
    每日重跑在帳本中「就地更新」而非新增 → n_trials 不會隨天數灌水（idempotent）。
"""
from __future__ import annotations

import sqlite3
import time

from botpaths import db_path as _db_path
from l3_dispatcher import auto_param_store as aps
from l3_dispatcher.champion_challenger import AllocPolicy, champion_alloc, compare_allocation
from l3_dispatcher.plan_snapshot import SNAP_UNREADABLE, read_plan_snapshot

DB_PATH = _db_path("trade_journal.db")

# 固定 epoch：讓同 (bucket, champ, chal) 的 trial_id 跨日穩定 → 重跑不灌水 n_trials。
_LEDGER_EPOCH_MS = 1_700_000_000_000

# 候選網格（刻意小：grid 大小＝L2 多重檢定家族大小，越大 FDR/PBO 校正越嚴）。
# 預設 champion＝(0.5,0.3,0.2)；以下皆與其相異，涵蓋「提早收/均衡/讓利潤奔跑」三種傾向。
CANDIDATE_GRID: list[AllocPolicy] = [
    AllocPolicy("front_heavy(0.6/0.25/0.15)", (0.6, 0.25, 0.15)),  # 更早落袋
    AllocPolicy("balanced(0.4/0.3/0.3)", (0.4, 0.3, 0.3)),        # 均衡（≈demo_trader 權重）
    AllocPolicy("even(0.34/0.33/0.33)", (0.34, 0.33, 0.33)),       # 平均三段
    AllocPolicy("let_run(0.2/0.3/0.5)", (0.2, 0.3, 0.5)),         # 讓利潤奔跑
]

# 與 paper_audit.load_closed 對齊的欄位（多帶 tp_alloc，供 alloc-aware 回放對帳）。
_COLS = ["id", "symbol", "setup", "direction", "entry_price", "stop_price",
         "tp1", "tp2", "tp3", "entry_at", "exit_at", "legs_hit", "exit_reason",
         "realized_r", "pnl_usd", "entry_filled_pct", "plan_snapshot", "tp_alloc"]


# ── 載入（自含；tp_alloc 欄未遷移時自動退回） ────────────────────────
def _load_closed_paper(days: int = 120, db=None) -> list[dict]:
    cutoff = int(time.time() * 1000) - days * 86400 * 1000
    conn = sqlite3.connect(str(db or DB_PATH))
    conn.execute("PRAGMA busy_timeout=5000")
    # v114(稽核rank3治本)：只收加密 deepdive——池化桶(POOL,q)/(POOL,POOL)過去混入美股
    #   us_breakout 樣本＝拿另一個引擎的成交行為替加密參數背書（統計污染）。TP 政策的
    #   消費端是加密路徑；美股引擎日後要優化須另立自己的池，不與加密共池。
    where = ("WHERE status='closed' AND setup='deepdive' "
             "AND IFNULL(exit_reason,'')!='entry_expired' AND entry_at>=?")
    try:
        cols = _COLS
        try:
            rows = conn.execute(
                f"SELECT {', '.join(cols)} FROM paper_trades {where}", (cutoff,)).fetchall()
        except sqlite3.OperationalError:
            # tp_alloc 欄尚未遷移（新模組先於 daemon 遷移時）→ 退回不含該欄
            cols = [c for c in _COLS if c != "tp_alloc"]
            rows = conn.execute(
                f"SELECT {', '.join(cols)} FROM paper_trades {where}", (cutoff,)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        d.setdefault("tp_alloc", None)
        out.append(d)
    return out


# ── 宇宙世代同代化（v144，監督員 r29 治本） ──────────────────────────
def _same_generation_only(rows: list[dict]) -> tuple[list[dict], dict]:
    """只留「與現行生產宇宙同一代」的樣本，回 (rows, cohort 摘要)。

    為什麼（與 v114 排除 us_breakout 同一條理）：CoinGlass 訂閱 2026-07-08 到期後
    宇宙降級到免費 OKX 源，活線 topN_agreement=0.0＝選出來的標的實質換掉。120 天
    窗把兩代混在一桶算晉升，等於拿上一代的成交行為替這一代的參數背書；一旦樣本
    跨過 n≥30 就會餵出錯誤晉升。這裡在**進統計之前**切乾淨，不動 L2 任何門檻。

    分寸：只做同代化，不降也不加門檻；同代樣本不足時由既有 minTRL(30) 自然
    fail-closed（＝0 晉升、覆寫表恆空），這是誠實答案而非退化。世代標籤取不到時
    （全庫皆無留痕）active_generation 回 "unknown"＝維持既有行為，不製造新阻斷。
    """
    from l3_dispatcher import universe_provenance as up
    rows = list(rows or [])
    mix = up.cohort_mix(rows)
    gen = up.active_generation(rows)
    # v178：同代鍵擴成 (宇宙源, 數據面版本)——dp1 舊樣本不替 dp2 參數背書；
    # 全庫無 dp 標記時維持現況（與宇宙世代同分寸，不製造新阻斷）
    kept = [r for r in rows if up.generation_of_row(r) == gen]
    kept = up.data_plane_filter(kept)
    return kept, {"active_generation": gen, "data_plane": up.DATA_PLANE,
                  "mix": mix,
                  "n_in": len(rows), "n_kept": len(kept),
                  "n_excluded_other_generation": len(rows) - len(kept)}


def _render_cohort_line(result: dict) -> str:
    """報告用一行：這批統計樣本是哪一代宇宙、排除了幾筆別代（誠實揭露樣本被縮小）。"""
    c = (result or {}).get("cohort") or {}
    if not c:
        return ""
    mix = "、".join(f"{k}={v}" for k, v in sorted(c.get("mix", {}).items()))
    return (f"🌐 宇宙世代：只採 <b>{c.get('active_generation')}</b> 代樣本 "
            f"{c.get('n_kept')} 筆（排除別代 {c.get('n_excluded_other_generation')} 筆；"
            f"全窗分佈 {mix or '—'}）")


def _render_unreadable_line(result: dict) -> str:
    """v202：被排除的『快照讀不出來』筆數必須出聲——靜默排除會讓報告看起來像全掃過了。"""
    n = (result or {}).get("n_excluded_unreadable_snapshot") or 0
    if not n:
        return ""
    return (f"⛔ 另有 <b>{n}</b> 筆進場快照讀不出來（壞檔／型別不對）＝當時 regime 未知，"
            f"已排除於分桶之外（不併入 unknown 桶、不計入 minTRL 樣本）")


def _quadrant_of(row: dict) -> str:
    """與 lessons_store.distill 同規則：plan_snapshot.regime_at_entry.oi_price_quadrant。
    ⚠️ 讀不出快照者回 None（不是 'unknown'）——呼叫端必須把它排除在分桶之外。"""
    snap, status = read_plan_snapshot(row.get("plan_snapshot"))
    if status == SNAP_UNREADABLE:
        return None
    regime = (snap or {}).get("regime_at_entry") or {}
    return regime.get("oi_price_quadrant") or "unknown"


# ── 單桶優化 ──────────────────────────────────────────────────────────
def optimize_bucket(symbol: str, quadrant: str, trades: list[dict], *,
                    at_ms: int, ledger, active_path=None, audit_path=None) -> dict:
    """單一 (symbol, quadrant) 桶：跑全網格挑戰，過 L2 者由 auto_param_store 落地。"""
    bkey = aps.bucket_key(symbol, quadrant)

    # champion＝此桶「現行生效」分配：活躍覆寫優先，否則 CONFIG 預設。
    override = aps.resolve_tp_alloc(symbol, quadrant, active_path=active_path)
    if override is not None:
        champ = AllocPolicy("champion(現行覆寫)", tuple(override))
    else:
        champ = champion_alloc()
    champ_key = tuple(round(float(x), 6) for x in champ.tp_alloc)

    evaluated: list[tuple[AllocPolicy, object]] = []
    for chal in CANDIDATE_GRID:
        if tuple(round(float(x), 6) for x in chal.tp_alloc) == champ_key:
            continue  # 與 champion 同 → 無意義，跳過
        hyp = f"alloc{tuple(champ.tp_alloc)}→{tuple(chal.tp_alloc)}"
        v = compare_allocation(trades, chal, bucket_key=bkey, ledger=ledger,
                               champion=champ, hypothesis=hyp,
                               registered_at_ms=_LEDGER_EPOCH_MS)
        evaluated.append((chal, v))

    # 選定：優先「已晉升且平均 R 最高」者；皆未晉升 → 取平均 R 最高者當 hold 留痕。
    def _mean(v):
        return v.chal_mean_r if getattr(v, "chal_mean_r", None) is not None else -1e9

    promoted = [(c, v) for c, v in evaluated if v.promote]
    chosen = (max(promoted, key=lambda cv: _mean(cv[1])) if promoted
              else (max(evaluated, key=lambda cv: _mean(cv[1])) if evaluated else None))

    applied = None
    if chosen is not None:
        chal_pol, verdict = chosen
        applied = aps.apply_verdict(
            verdict, symbol=symbol, quadrant=quadrant,
            challenger_alloc=chal_pol.tp_alloc, champion_alloc=champ.tp_alloc,
            at_ms=at_ms, active_path=active_path, audit_path=audit_path)

    return {"bucket": bkey, "symbol": symbol, "quadrant": quadrant,
            "n_trades": len(trades), "champion_alloc": list(champ.tp_alloc),
            "evaluated": evaluated, "chosen": chosen, "applied": applied}


# ── 全跑 ──────────────────────────────────────────────────────────────
def run_optimization(*, days: int = 120, at_ms: int | None = None,
                     rows: list[dict] | None = None, ledger=None,
                     active_path=None, audit_path=None, db=None) -> dict:
    """載入→（v144 宇宙世代同代化）→分桶(symbol×quadrant)→逐桶優化。rows 可注入（測試）。"""
    if at_ms is None:
        at_ms = int(time.time() * 1000)
    if rows is None:
        rows = _load_closed_paper(days=days, db=db)
    rows, cohort = _same_generation_only(rows)
    if ledger is None:
        from backtest.l2_stat_gates import TrialLedger, default_ledger_path
        ledger = TrialLedger(default_ledger_path())

    # task#62 階層部分池化（鏡像 entry_policy）：每筆同時進 per-symbol×象限、象限池(跨symbol)、
    #   全域池(跨一切)。讓碎裂的 per-symbol 桶湊不到 n≥30 時，較一般的池仍可合法達門檻過 L2。
    #   不降門檻（只合法匯集樣本；FDR 多重比較隨桶數變更嚴）；無覆寫時 resolve 階梯恆回 None。
    groups: dict[tuple[str, str], list[dict]] = {}
    n_unreadable = 0
    for r in rows:
        sym, q = r.get("symbol"), _quadrant_of(r)
        if q is None:
            # v202：快照讀不出來＝我們不知道這筆單當時的 regime。放進 unknown 桶等於
            #   拿「不知道」去墊高樣本數，而樣本數正是 minTRL≥30 晉升閘的判準 ⇒ 誠實排除。
            #   ⛔ 不可靜默丟棄：計數並在報告出聲（掃了幾筆 vs 用了幾筆要對得起來）。
            n_unreadable += 1
            continue
        groups.setdefault((sym, q), []).append(r)               # per-symbol × regime（最具體）
        groups.setdefault((aps.POOL, q), []).append(r)          # 象限池（跨 symbol、同 regime）
        groups.setdefault((aps.POOL, aps.POOL), []).append(r)   # 全域池（跨一切）

    def _rank(sym, q):
        if sym == aps.POOL and q == aps.POOL:
            return 0   # 全域池（最一般，先處理→具體桶可繼承本輪池化覆寫）
        return 1 if sym == aps.POOL else 2

    buckets = []
    for (sym, q), trs in sorted(groups.items(),
                                key=lambda kv: (_rank(*kv[0]), kv[0][0] or "", kv[0][1] or "")):
        buckets.append(optimize_bucket(sym, q, trs, at_ms=at_ms, ledger=ledger,
                                       active_path=active_path, audit_path=audit_path))
    n_promoted = sum(1 for b in buckets
                     if b["applied"] and b["applied"]["action"] == "promote")
    n_pooled = sum(1 for b in buckets if b["symbol"] == aps.POOL)
    return {"at_ms": at_ms, "n_buckets": len(buckets), "n_trades": len(rows),
            "n_pooled": n_pooled, "n_promoted": n_promoted, "buckets": buckets,
            "cohort": cohort,
            "n_excluded_unreadable_snapshot": n_unreadable}


# ── 繁中報告（CEO/調參 Session 透明化） ──────────────────────────────
def render_report(result: dict | None = None, *, active_path=None) -> str | None:
    """純描述報告：每桶 champion／最佳挑戰者／是否晉升 + 活躍覆寫表摘要。無資料回 None。"""
    if result is None:
        result = run_optimization(active_path=active_path)
    if not result["buckets"]:
        # v202：「一桶都沒有」有兩種成因，不可折成同一個 None（靜音）：
        #   真的沒樣本 → 照舊安靜；全部樣本的快照都讀不出來 → 必須出聲，否則
        #   優化器整輪空轉會長得跟「今天沒單」一模一樣。
        if result.get("n_excluded_unreadable_snapshot"):
            return ("🤖 <b>自動優化器</b>（TP 分配）\n"
                    f"掃 {result['n_trades']} 筆，但**可用桶數為 0**：\n"
                    + _render_unreadable_line(result) + "\n"
                    "<i>這不是『今天沒樣本』——是樣本讀不出來。請查 paper_trades."
                    "plan_snapshot 欄位是否被寫壞。</i>")
        return None
    # v82：0 晉升日（常態）每桶皆「⏸️維持」零資訊 → 收斂為結論一行＋收斂進度，
    #   把每日 ~4800 字洗版牆壓成可掃讀的一則；全文明細仍可由 active 覆寫表/帳本回溯。
    #   誠實標籤（紅線①、樣本未達門檻）保留。有晉升才展開全文。
    if result["n_promoted"] == 0:
        # v83(6)：進度用 L2 實際把關的「對齊樣本 n_aligned」估距門檻（raw 桶筆數會樂觀高估）
        _aligned = [getattr(b["chosen"][1], "n_aligned", 0) or 0
                    for b in result["buckets"] if b.get("chosen")]
        max_n = max(_aligned) if _aligned else 0
        prog = (f"，最大對齊樣本 n_aligned={max_n}（差 {30 - max_n} 筆達門檻 30）"
                if max_n < 30 else f"，最大對齊樣本 n_aligned={max_n}")
        return ("🤖 <b>自動優化器</b>（TP 分配，過 L2 才寫模擬盤）\n"
                f"掃 {result['n_trades']} 筆／{result['n_buckets']} 桶｜"
                f"本輪晉升 <b>0</b> 桶（皆未過 L2{prog}）\n"
                + _render_cohort_line(result) + "\n"
                + (_render_unreadable_line(result) + "\n"
                   if _render_unreadable_line(result) else "")
                + aps.render_active(active_path) + "\n"
                "<i>只驅動模擬盤（紅線①）；覆寫表恆空＝零行為變更，可事後 rollback。</i>")
    lines = ["🤖 <b>自動優化器</b>（step8：過 L2 後直接寫模擬盤 TP 分配，即時生效）",
             "━━━━━━━━━━━━━━━━",
             f"掃 {result['n_trades']} 筆已平倉紙上單，分 {result['n_buckets']} 桶"
             f"（symbol×regime）｜本輪晉升 {result['n_promoted']} 桶",
             _render_cohort_line(result),
             _render_unreadable_line(result)]
    for b in result["buckets"]:
        champ = "/".join(f"{x*100:.0f}%" for x in b["champion_alloc"])
        head = f"<b>[{b['bucket']}]</b> {b['n_trades']} 筆｜champion {champ}"
        if not b["evaluated"]:
            lines.append(head + "｜（無可比挑戰者）")
            continue
        cv = b["chosen"]
        chal_pol, v = cv
        cstr = "/".join(f"{x*100:.0f}%" for x in chal_pol.tp_alloc)
        verb = "✅晉升" if v.promote else "⏸️維持"
        mr = (f"{v.champ_mean_r:+.3f}→{v.chal_mean_r:+.3f}R"
              if v.champ_mean_r is not None and v.chal_mean_r is not None else "—")
        lines.append(f"{head}\n  最佳挑戰者 {cstr}：{verb}（對齊 n={v.n_aligned}，{mr}）"
                     f"\n  L2：{v.l2_summary}")
    lines.append("")
    lines.append(aps.render_active(active_path))
    lines.append("<i>動參數唯一合法路徑＝過 L2 四關（minTRL/DSR/PBO/FDR）；只驅動模擬盤"
                 "（紅線①），可事後 rollback。樣本未達門檻前不會有任何覆寫。</i>")
    return "\n".join(lines)


def _selftest() -> bool:
    """離線、合成樣本、暫存帳本/覆寫檔；驗『小樣本→0 晉升、活躍表恆空』的誠實答案。"""
    import tempfile
    from pathlib import Path
    from backtest.l2_stat_gates import TrialLedger

    def _mk(tid, r, q="price_up_oi_up", sym="BTC"):
        import json
        snap = json.dumps({"regime_at_entry": {"oi_price_quadrant": q}})
        return {"id": tid, "symbol": sym, "setup": "intraday", "direction": "bull",
                "entry_price": 100.0, "stop_price": 90.0, "tp1": 110.0, "tp2": 120.0,
                "tp3": 140.0, "entry_at": 0, "exit_at": 10, "legs_hit": "tp1,tp2,stop",
                "exit_reason": "stop", "realized_r": r, "pnl_usd": r * 100,
                "entry_filled_pct": 1.0, "plan_snapshot": snap, "tp_alloc": None}

    with tempfile.TemporaryDirectory() as td:
        ap = Path(td) / "active.json"
        au = Path(td) / "audit.jsonl"
        led = TrialLedger(Path(td) / "ledger.jsonl")

        # champion 帳本一致的 R（用預設 0.5/0.3/0.2 回放）：tp1,tp2 然後 stop 剩餘。
        champ = champion_alloc()
        a1, a2, _ = champ.tp_alloc
        rem = 1.0 - a1 - a2
        r = round(a1 * 1.0 + a2 * 2.0 + rem * (-1.0), 6)

        # (a) 小樣本（<30）→ minTRL fail-closed → 0 晉升、活躍表恆空
        rows_small = [_mk(i, r) for i in range(10)]
        res = run_optimization(rows=rows_small, at_ms=1, ledger=led,
                               active_path=ap, audit_path=au)
        assert res["n_promoted"] == 0
        assert aps.resolve_tp_alloc("BTC", "price_up_oi_up", active_path=ap) is None
        assert not ap.exists()  # 從未寫活躍表

        # (b) 同質大樣本（每筆 R 相同 → 離散=0）→ L2 仍擋 → 0 晉升（誠實）
        rows_homo = [_mk(100 + i, r) for i in range(40)]
        res = run_optimization(rows=rows_homo, at_ms=2, ledger=led,
                               active_path=ap, audit_path=au)
        assert res["n_promoted"] == 0
        assert aps.resolve_tp_alloc("BTC", "price_up_oi_up", active_path=ap) is None

        # (c) 分桶正確：2 quadrant × 1 symbol → per-symbol 2 + 象限池 2 + 全域池 1 = 5 桶（task#62 池化）
        rows_two = [_mk(200 + i, r, q="price_up_oi_up") for i in range(5)] + \
                   [_mk(300 + i, r, q="price_down_oi_up") for i in range(5)]
        res = run_optimization(rows=rows_two, at_ms=3, ledger=led,
                               active_path=ap, audit_path=au)
        assert res["n_buckets"] == 5, f"應 5 桶(2 per-symbol+2 象限池+1 全域池)，實得 {res['n_buckets']}"
        assert res["n_pooled"] == 3, f"池化桶應 3，實得 {res['n_pooled']}"

        # (d) 帳本鏈完整（未被自動優化器破壞）
        ok, _ = led.verify_chain()
        assert ok

        # (e) 報告不炸
        assert render_report(res, active_path=ap) is not None

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
    print(_re.sub(r"<[^>]+>", "", rep) if rep else "（無紙上資料）")
