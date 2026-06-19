"""review_attribution.py — 復盤引擎 step2：進場計畫 vs 真實結果 歸因分析（離線、唯讀）。

定位（接 l3_dispatcher/plan_snapshot.py step1 的下一層）：
    step1 在『進場那一刻』把預期劇本／止損劇本／當下抓到的上下文凍結成 plan_snapshot，
    存進 paper_trades.plan_snapshot 欄。本模組（step2）在『平倉之後』把那份凍結計畫
    與真實結果擺在一起，回答使用者要的真復盤三問：
      ① 結果原因是什麼？（exit_reason × realized_r）
      ② 跟進場計畫一樣，還是超出計畫？（expected_r vs realized_r、出場是否吻合止損劇本）
      ③ 當初『沒抓到的數據』是不是誤判主因？（missing_context_keys × 結果好壞）
    第③問正是 plan_snapshot 的 _CONTEXT_KEYS 設計初衷：缺哪個數據時最容易賠，
    就是引擎下一個最該回補的因素——把復盤從「結果論」變成「可回測的待補清單」。

安全鐵則（與三紅線並存）：
    • 純離線唯讀：只對 trade_journal.db 下 SELECT，從不 UPDATE/INSERT；不碰 daemon、
      不 import strength / evaluate / eval_cvd_divergence、零訊號數學、零下單。
    • 全是『模擬盤（paper）』樣本：任何輸出一律標明模擬盤，永不當真實績效宣稱（紅線③）。
    • 樣本誠實：前向樣本（plan_captured_at_ms ≥ engine_epoch_ms）與歷史回補單分開計；
      樣本數小一律標「樣本不足、僅供觀察、未達統計顯著」，不輸出勝率/年化等宣稱。

用法：
    set PYTHONIOENCODING=utf-8
    python -X utf8 backtest\\review_attribution.py            # 繁中報告
    python -X utf8 backtest\\review_attribution.py --json     # 機器可讀 JSON
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

# 允許從 backtest/ 直接以檔案執行時仍找得到專案根的 botpaths
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from botpaths import db_path

# 出場原因分類：計畫劇本是否吻合
_PLAN_WORKED = {"tp1", "tp2", "tp3"}        # 走到目標＝base/bull case 成立
_PLAN_STOP = {"stop"}                        # 觸及失效價＝止損劇本如預期觸發
_INCONCLUSIVE = {"timeout", "entry_expired"}  # 計畫未被市場裁決（逾時/掛單沒成交）


def _ro_conn() -> sqlite3.Connection:
    """唯讀連線。優先用 URI mode=ro（DB 層級擋寫）；失敗則退回一般連線但本模組只下 SELECT。"""
    p = db_path("trade_journal.db")
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    except Exception:
        conn = sqlite3.connect(str(p))
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _classify_exit(exit_reason: str | None) -> str:
    er = (exit_reason or "").lower()
    if er in _PLAN_WORKED:
        return "plan_worked"
    if er in _PLAN_STOP:
        return "stop_as_planned"
    if er in _INCONCLUSIVE:
        return "inconclusive"
    return "other"


def _mean(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def load_closed() -> list[dict]:
    """讀所有已平倉模擬單，解出 plan_snapshot（若有）。唯讀。"""
    conn = _ro_conn()
    try:
        cur = conn.execute(
            "SELECT id, symbol, setup, direction, realized_r, exit_reason, "
            "       exit_at, regime, plan_snapshot "
            "FROM paper_trades WHERE status='closed' ORDER BY exit_at"
        )
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    for r in rows:
        snap = None
        raw = r.get("plan_snapshot")
        if raw:
            try:
                snap = json.loads(raw)
            except Exception:
                snap = None
        r["_snap"] = snap
    return rows


def analyze(rows: list[dict]) -> dict:
    n_total = len(rows)
    with_snap = [r for r in rows if r.get("_snap")]
    # 前向樣本：進場計畫凍結時刻 ≥ 引擎上線時刻（避免拿被優化汙染的歷史單當前向證據）
    forward = []
    for r in with_snap:
        snap = r["_snap"]
        epoch = snap.get("engine_epoch_ms") or 0
        cap = snap.get("plan_captured_at_ms") or 0
        if epoch and cap and cap >= epoch:
            forward.append(r)

    # ── 全體出場分佈（含無快照的歷史單；只用已存欄位，仍是真實資料）──
    exit_dist: dict[str, int] = {}
    by_class: dict[str, list[float]] = {}
    for r in rows:
        er = (r.get("exit_reason") or "unknown")
        exit_dist[er] = exit_dist.get(er, 0) + 1
        cls = _classify_exit(er)
        by_class.setdefault(cls, []).append(r.get("realized_r"))

    overall = {
        "n_closed_total": n_total,
        "n_with_snapshot": len(with_snap),
        "n_forward_sample": len(forward),
        "mean_realized_r_all": _mean([r.get("realized_r") for r in rows]),
        "exit_reason_dist": dict(sorted(exit_dist.items(), key=lambda kv: -kv[1])),
        "mean_r_by_class": {k: _mean(v) for k, v in by_class.items()},
    }

    # ── 計畫 vs 結果（僅含快照單）──
    plan_vs_result: list[dict] = []
    for r in with_snap:
        snap = r["_snap"]
        exp_r = snap.get("expected_r")
        real_r = r.get("realized_r")
        delta = None
        if isinstance(exp_r, (int, float)) and isinstance(real_r, (int, float)):
            delta = round(real_r - exp_r, 3)
        plan_vs_result.append({
            "id": r["id"], "symbol": r["symbol"], "source": snap.get("source"),
            "expected_r": exp_r, "realized_r": real_r, "delta_r": delta,
            "exit_reason": r.get("exit_reason"),
            "exit_class": _classify_exit(r.get("exit_reason")),
            "missing_context_keys": snap.get("missing_context_keys", []),
        })

    # ── 第③問：缺哪個數據時最容易誤判 ──
    # 對每個 context key，比較『該筆進場時此 key 缺席 vs 在場』兩群的 mean realized_r。
    # 缺席群明顯較差＝那個數據是當下最該回補的因素。樣本小一律標註，不下結論。
    missing_impact: dict[str, dict] = {}
    if with_snap:
        all_keys = set()
        for r in with_snap:
            for k in (r["_snap"].get("context_at_entry") or {}):
                all_keys.add(k)
        for k in sorted(all_keys):
            absent_r, present_r = [], []
            for r in with_snap:
                snap = r["_snap"]
                missing = set(snap.get("missing_context_keys") or [])
                rr = r.get("realized_r")
                (absent_r if k in missing else present_r).append(rr)
            ma, mp = _mean(absent_r), _mean(present_r)
            gap = round(ma - mp, 4) if (ma is not None and mp is not None) else None
            missing_impact[k] = {
                "n_absent": len([x for x in absent_r if x is not None]),
                "n_present": len([x for x in present_r if x is not None]),
                "mean_r_when_absent": ma, "mean_r_when_present": mp,
                "gap_absent_minus_present": gap,
            }

    return {
        "overall": overall,
        "plan_vs_result": plan_vs_result,
        "missing_context_impact": missing_impact,
    }


def render_report(res: dict) -> str:
    o = res["overall"]
    L: list[str] = []
    L.append("═══ 復盤引擎 step2 · 進場計畫 vs 真實結果 歸因（全部為【模擬盤】樣本）═══")
    L.append("")
    L.append(f"已平倉模擬單總數：{o['n_closed_total']}")
    L.append(f"  其中含進場快照（v56 後捕捉）：{o['n_with_snapshot']}")
    L.append(f"  其中屬前向樣本（引擎上線後凍結）：{o['n_forward_sample']}")
    mr = o["mean_realized_r_all"]
    L.append(f"全體平均實現 R：{mr if mr is not None else 'N/A'}（模擬盤，非真實績效）")
    L.append("")
    L.append("出場原因分佈：")
    for er, n in o["exit_reason_dist"].items():
        L.append(f"  {er:<16} {n}")
    L.append("")
    L.append("依劇本分類的平均 R：")
    labels = {"plan_worked": "走到目標(計畫成立)", "stop_as_planned": "止損(劇本如預期)",
              "inconclusive": "未被裁決(逾時/未成交)", "other": "其他"}
    for cls, m in o.get("mean_r_by_class", {}).items():
        L.append(f"  {labels.get(cls, cls):<22} 平均R={m}")
    L.append("")

    if o["n_with_snapshot"] == 0:
        L.append("【尚無含快照的已平倉樣本】")
        L.append("  進場快照捕捉層 v56 才上線，現有已平倉單多在那之前開倉，故無凍結計畫可比對。")
        L.append("  待 v56 後開倉的單陸續平倉，下列『計畫 vs 結果』與『缺數據誤判』分析會自動充實。")
        L.append("  本工具已就位＝復盤引擎的歸因骨架已搭好，零訊號數學、純離線唯讀。")
        return "\n".join(L)

    L.append("── 計畫 vs 結果（每筆含快照單）──")
    for pr in res["plan_vs_result"]:
        d = pr["delta_r"]
        sign = "" if d is None else ("超出計畫 +" if d >= 0 else "不如計畫 ")
        L.append(f"  #{pr['id']} {pr['symbol']} [{pr['source']}] "
                 f"預期R={pr['expected_r']} 實現R={pr['realized_r']} "
                 f"({sign}{d if d is not None else '?'}) 出場={pr['exit_reason']}")
    L.append("")
    L.append("── 第③問：缺哪個數據時最容易誤判（gap<0＝缺該數據時結果較差）──")
    mi = res["missing_context_impact"]
    ranked = sorted(mi.items(),
                    key=lambda kv: (kv[1]["gap_absent_minus_present"] is None,
                                    kv[1]["gap_absent_minus_present"] or 0))
    for k, v in ranked:
        L.append(f"  {k:<22} 缺席n={v['n_absent']} 在場n={v['n_present']} "
                 f"缺席平均R={v['mean_r_when_absent']} 在場平均R={v['mean_r_when_present']} "
                 f"gap={v['gap_absent_minus_present']}")
    L.append("")
    if o["n_forward_sample"] < 30:
        L.append(f"⚠ 前向樣本僅 {o['n_forward_sample']} 筆（<30）：以上一律【樣本不足，僅供觀察，"
                 "未達統計顯著】，不得當作勝率/期望值宣稱。")
    return "\n".join(L)


def main(argv: list[str]) -> int:
    rows = load_closed()
    res = analyze(rows)
    if "--json" in argv:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(render_report(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
