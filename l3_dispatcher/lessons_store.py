"""復盤引擎 step7（task#52）── 教訓庫 lessons.jsonl（regime 可檢索純讀提示）。

定位：把已平倉『模擬盤』單蒸餾成「可按 regime / quadrant 檢索的教訓卡」，給 deepdive
/ 操盤手當「歷史類比」純讀提示。**DB（paper_trades）才是真相源**；本檔只是 derived view，
可隨時 rebuild（rebuild_lessons_file 整檔重寫）。零下單數學、零訊號變更、純讀 DB + 寫
一個 derived 檔（data_dir/lessons.jsonl）。

learning invariant（學習不變式 — 紅線③「不臆造」的延伸）：
  **僥倖單（lucky）不得進正向學習集。** 一筆「賺錢」單只有在
    (1) realized_r > 0，且
    (2) 它是『照計畫的機制』賺的：exit_class == 'plan_worked'（吃到 TP），且
    (3) 不是逆 HTF 大勢硬凹的：htf_aligned 不為 False
  三者皆成立，才 counts_as_positive_evidence=True（正向證據）。其餘賺錢單仍**記錄**、
  但標 lucky=True 並**排除**於正向集——避免「賭對一把」被學成「策略有效」。
  賠錢單一律記為負向教訓（negative_evidence=True；永遠是合法的負證據）。
  htf_aligned 未知（None）時不視為違規（不懲罰未知），但也不會讓 lucky 成立。

每筆 quadrant 來自 plan_snapshot.regime_at_entry.oi_price_quadrant（OI×價四象限）。
舊單（#47 之前無 plan_snapshot）→ quadrant='unknown'、htf_aligned=None、expected_r=None，
仍收錄但多半落不進正向集（無法證明照計畫贏）。

用法：
    set PYTHONIOENCODING=utf-8
    python -X utf8 -m l3_dispatcher.lessons_store --selftest   # 離線自測（無 DB/網路）
    python -X utf8 -m l3_dispatcher.lessons_store --rebuild    # 從 DB 整檔重建 lessons.jsonl
    python -X utf8 -m l3_dispatcher.lessons_store --show       # 印 quadrant 彙總（含誠實橫幅）
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

# 允許以檔案直接執行時找得到專案根
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from backtest.review_attribution import _classify_exit  # 出場劇本分類（plan_worked 等）
from botpaths import data_dir, db_path

SCHEMA_VER = 1
_R_EPS = 1e-6
MIN_BUCKET_HONEST = 30   # 與 L2 MIN_BUCKET_N 同口徑：彙總低於此一律標「樣本不足」


def lessons_path() -> Path:
    return data_dir() / "lessons.jsonl"


# ════════════════════════════════════════════════════════════════════════
#  learning invariant（純函式，可獨立測試）
# ════════════════════════════════════════════════════════════════════════
def apply_learning_invariant(realized_r: float, exit_class: str,
                             htf_aligned) -> tuple[str, bool, bool, bool]:
    """回 (outcome, lucky, counts_as_positive_evidence, negative_evidence)。

    outcome: 'win'(R>0) / 'loss'(R<0) / 'scratch'(≈0)。
    """
    if realized_r > _R_EPS:
        outcome = "win"
    elif realized_r < -_R_EPS:
        outcome = "loss"
    else:
        outcome = "scratch"

    lucky = False
    positive = False
    negative = (outcome == "loss")

    if outcome == "win":
        won_by_plan = (exit_class == "plan_worked")
        counter_trend = (htf_aligned is False)   # 只有『明確逆勢』才算；None 不算
        if won_by_plan and not counter_trend:
            positive = True
        else:
            lucky = True                          # 賺到但非照計畫機制 → 僥倖，排除於正向集
    return outcome, lucky, positive, negative


# ════════════════════════════════════════════════════════════════════════
#  讀 DB → 蒸餾教訓卡
# ════════════════════════════════════════════════════════════════════════
def _ro_conn() -> sqlite3.Connection:
    p = db_path("trade_journal.db")
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    except Exception:
        conn = sqlite3.connect(str(p))
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _load_rows(days: int = 365) -> list[dict]:
    """讀已平倉模擬單（排除 entry_expired 掛單作廢）。唯讀。"""
    conn = _ro_conn()
    try:
        import time
        cutoff = int(time.time() * 1000) - days * 86400 * 1000
        cur = conn.execute(
            "SELECT id, symbol, setup, direction, realized_r, exit_reason, "
            "       entry_at, exit_at, regime, plan_snapshot "
            "FROM paper_trades "
            "WHERE status='closed' AND IFNULL(exit_reason,'') != 'entry_expired' "
            "AND entry_at >= ? ORDER BY exit_at ASC",
            (cutoff,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def _parse_snap(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def distill(row: dict) -> dict:
    """把一筆 DB 列蒸餾成教訓卡（套 learning invariant）。"""
    snap = _parse_snap(row.get("plan_snapshot"))
    regime = (snap or {}).get("regime_at_entry") or {}
    ctx = (snap or {}).get("context_at_entry") or {}

    quadrant = regime.get("oi_price_quadrant") or "unknown"
    funding_state = regime.get("funding_state")
    cvd_state = regime.get("cvd_state")
    vol_trend = regime.get("vol_trend")
    htf_aligned = ctx.get("htf_aligned")  # True / False / None
    expected_r = (snap or {}).get("expected_r")
    missing = (snap or {}).get("missing_context_keys") or []

    realized_r = float(row.get("realized_r") or 0.0)
    exit_class = _classify_exit(row.get("exit_reason"))
    outcome, lucky, positive, negative = apply_learning_invariant(
        realized_r, exit_class, htf_aligned)

    delta_r = (round(realized_r - float(expected_r), 4)
               if expected_r is not None else None)

    return {
        "schema_ver": SCHEMA_VER,
        "paper_id": row.get("id"),
        "symbol": row.get("symbol"),
        "setup": row.get("setup"),
        "direction": row.get("direction"),
        "quadrant": quadrant,
        "funding_state": funding_state,
        "cvd_state": cvd_state,
        "vol_trend": vol_trend,
        "htf_aligned": htf_aligned,
        "realized_r": round(realized_r, 4),
        "expected_r": expected_r,
        "delta_r": delta_r,
        "exit_reason": row.get("exit_reason"),
        "exit_class": exit_class,
        "outcome": outcome,
        "lucky": lucky,
        "counts_as_positive_evidence": positive,
        "negative_evidence": negative,
        "missing_context_keys": missing,
        "exit_at_ms": row.get("exit_at"),
        "has_snapshot": snap is not None,
        "source": "paper_journal",
    }


def build_lessons(days: int = 365) -> list[dict]:
    """從 DB 蒸餾全部教訓卡（不寫檔）。"""
    return [distill(r) for r in _load_rows(days)]


def rebuild_lessons_file(path: Path | None = None, days: int = 365) -> dict:
    """從 DB 整檔重建 lessons.jsonl（DB 為真相源；derived 檔可隨時重寫）。
    回 {n, n_positive, n_lucky, n_loss, path}。"""
    p = path or lessons_path()
    lessons = build_lessons(days)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        for ln in lessons:
            f.write(json.dumps(ln, ensure_ascii=False) + "\n")
    return {
        "n": len(lessons),
        "n_positive": sum(1 for x in lessons if x["counts_as_positive_evidence"]),
        "n_lucky": sum(1 for x in lessons if x["lucky"]),
        "n_loss": sum(1 for x in lessons if x["outcome"] == "loss"),
        "path": str(p),
    }


# ════════════════════════════════════════════════════════════════════════
#  純讀檢索
# ════════════════════════════════════════════════════════════════════════
def _read_file(path: Path | None = None) -> list[dict]:
    p = path or lessons_path()
    if not p.exists():
        return []
    out = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    return out


def query_lessons(*, quadrant: str | None = None, setup: str | None = None,
                  direction: str | None = None, positive_only: bool = False,
                  path: Path | None = None) -> list[dict]:
    """regime 可檢索純讀提示。任一條件 None＝不篩。positive_only＝只回正向學習集。"""
    out = []
    for ls in _read_file(path):
        if quadrant is not None and ls.get("quadrant") != quadrant:
            continue
        if setup is not None and ls.get("setup") != setup:
            continue
        if direction is not None and ls.get("direction") != direction:
            continue
        if positive_only and not ls.get("counts_as_positive_evidence"):
            continue
        out.append(ls)
    return out


def learning_set(path: Path | None = None) -> list[dict]:
    """正向學習集（已套 invariant：僥倖單已被排除）。"""
    return [ls for ls in _read_file(path) if ls.get("counts_as_positive_evidence")]


def summarize_by_quadrant(path: Path | None = None) -> dict:
    """按 quadrant 彙總（含誠實樣本不足標記）。純描述、不下參數指令。"""
    rows = _read_file(path)
    buckets: dict[str, dict] = {}
    for ls in rows:
        q = ls.get("quadrant") or "unknown"
        b = buckets.setdefault(q, {"n": 0, "n_positive": 0, "n_lucky": 0,
                                   "n_loss": 0, "_rs": []})
        b["n"] += 1
        b["n_positive"] += 1 if ls.get("counts_as_positive_evidence") else 0
        b["n_lucky"] += 1 if ls.get("lucky") else 0
        b["n_loss"] += 1 if ls.get("outcome") == "loss" else 0
        b["_rs"].append(float(ls.get("realized_r") or 0.0))
    for q, b in buckets.items():
        rs = b.pop("_rs")
        b["avg_r"] = round(sum(rs) / len(rs), 4) if rs else None
        b["sample_sufficient"] = b["n"] >= MIN_BUCKET_HONEST
        b["honesty"] = ("樣本足（≥%d）" % MIN_BUCKET_HONEST if b["sample_sufficient"]
                        else "⚠️ 樣本不足（<%d），僅供觀察、未達統計顯著" % MIN_BUCKET_HONEST)
    return {"total": len(rows), "by_quadrant": buckets}


def render_summary(path: Path | None = None) -> str:
    s = summarize_by_quadrant(path)
    L = ["═" * 62,
         "教訓庫彙總（lessons.jsonl｜復盤引擎 step7）",
         "═" * 62,
         "⚠️ 全為『模擬盤』樣本；僥倖單已排除於正向集（learning invariant）。",
         f"   總筆數 = {s['total']}", ""]
    if not s["by_quadrant"]:
        L.append("（尚無教訓卡，先 --rebuild）")
    for q, b in sorted(s["by_quadrant"].items(), key=lambda kv: -kv[1]["n"]):
        L.append(f"  [{q}] n={b['n']}｜正向={b['n_positive']}｜僥倖={b['n_lucky']}"
                 f"｜賠={b['n_loss']}｜avg_r={b['avg_r']}")
        L.append(f"        {b['honesty']}")
    L.append("═" * 62)
    return "\n".join(L)


# ════════════════════════════════════════════════════════════════════════
#  離線自測（純函式 + 暫存檔，無需 DB/網路）
# ════════════════════════════════════════════════════════════════════════
def _selftest() -> bool:
    ok_all = True

    def chk(name, cond):
        nonlocal ok_all
        print(f"  {'✅' if cond else '❌'} {name}")
        ok_all = ok_all and cond

    # — learning invariant 四象限 —
    o, l, p, n = apply_learning_invariant(1.5, "plan_worked", True)
    chk("照計畫贏+順勢 → 正向、非僥倖", o == "win" and p and not l)
    o, l, p, n = apply_learning_invariant(1.5, "plan_worked", None)
    chk("照計畫贏+HTF未知 → 仍正向（不懲罰未知）", p and not l)
    o, l, p, n = apply_learning_invariant(1.5, "plan_worked", False)
    chk("照計畫贏但逆勢 → 僥倖、排除正向", l and not p)
    o, l, p, n = apply_learning_invariant(0.8, "inconclusive", True)
    chk("逾時飄出去賺到 → 僥倖、排除正向", o == "win" and l and not p)
    o, l, p, n = apply_learning_invariant(-1.0, "stop_as_planned", True)
    chk("止損賠 → 負證據、非正向非僥倖", o == "loss" and n and not p and not l)
    o, l, p, n = apply_learning_invariant(0.0, "inconclusive", True)
    chk("打平 → scratch、皆非", o == "scratch" and not p and not l and not n)

    # — distill：合成 row（含 plan_snapshot）—
    snap = {"regime_at_entry": {"oi_price_quadrant": "price_up_oi_up",
                                "funding_state": "hot", "cvd_state": "up",
                                "vol_trend": "expanding"},
            "context_at_entry": {"htf_aligned": True},
            "expected_r": 1.0, "missing_context_keys": ["whale_net"]}
    row = {"id": 1, "symbol": "BTC", "setup": "intraday", "direction": "bull",
           "realized_r": 1.8, "exit_reason": "tp2", "entry_at": 0, "exit_at": 5,
           "regime": None, "plan_snapshot": json.dumps(snap)}
    d = distill(row)
    chk("distill quadrant 正確", d["quadrant"] == "price_up_oi_up")
    chk("distill 正向證據", d["counts_as_positive_evidence"] and not d["lucky"])
    chk("distill delta_r=realized-expected", d["delta_r"] == round(1.8 - 1.0, 4))
    chk("distill 帶 missing_context_keys", d["missing_context_keys"] == ["whale_net"])

    # 舊單無 snapshot → quadrant unknown、htf None → 賺也進不了正向（無法證明照計畫）
    row_old = {"id": 2, "symbol": "ETH", "setup": "intraday", "direction": "bull",
               "realized_r": 1.2, "exit_reason": "tp1", "entry_at": 0, "exit_at": 5,
               "regime": None, "plan_snapshot": None}
    d_old = distill(row_old)
    # 註：tp1 → plan_worked，htf None 不算逆勢 → 仍會是正向。確認 quadrant=unknown。
    chk("舊單 quadrant=unknown", d_old["quadrant"] == "unknown")
    chk("舊單 has_snapshot=False", d_old["has_snapshot"] is False)

    # — 檔案往返：rebuild → query → learning_set → summarize —
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "lessons.jsonl"
        lessons = [
            distill(dict(row, id=10)),                       # 正向 price_up_oi_up
            distill(dict(row, id=11, realized_r=0.9, exit_reason="timeout")),  # 僥倖
            distill(dict(row, id=12, realized_r=-1.0, exit_reason="stop")),    # 賠
        ]
        with open(p, "w", encoding="utf-8") as f:
            for ln in lessons:
                f.write(json.dumps(ln, ensure_ascii=False) + "\n")
        q = query_lessons(quadrant="price_up_oi_up", path=p)
        chk("query 按 quadrant 取回 3 筆", len(q) == 3)
        ls = learning_set(p)
        chk("learning_set 只含正向 1 筆（僥倖+賠被排除）", len(ls) == 1)
        summ = summarize_by_quadrant(p)
        b = summ["by_quadrant"]["price_up_oi_up"]
        chk("彙總 n=3 / 正向=1 / 僥倖=1 / 賠=1",
            b["n"] == 3 and b["n_positive"] == 1 and b["n_lucky"] == 1 and b["n_loss"] == 1)
        chk("小樣本標『樣本不足』", b["sample_sufficient"] is False)

    print("  自測通過：learning invariant + distill + 檔案往返 + 彙總誠實橫幅 ✅"
          if ok_all else "  ❌ 有失敗項")
    return ok_all


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if _selftest() else 1)
    if "--rebuild" in sys.argv:
        res = rebuild_lessons_file()
        print(f"[lessons_store] rebuilt {res['n']} 筆 → {res['path']}")
        print(f"  正向={res['n_positive']}｜僥倖={res['n_lucky']}｜賠={res['n_loss']}")
        sys.exit(0)
    if "--show" in sys.argv:
        print(render_summary())
        sys.exit(0)
    print(__doc__)
    print("用法：--selftest | --rebuild | --show")
