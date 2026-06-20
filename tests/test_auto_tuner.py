"""調參 Session（auto_tuner）測試 ── 純描述報告 + 出場劇本分布聚合。

定位（task#57）：auto_tuner 是復盤引擎『消費端』的每日純描述報告器（v56 task#52 後
已移除所有祈使建議——動參數的唯一合法路徑是 champion/challenger 過 L2 四關）。本檔在
飛輪點火前先把它的三條核心邏輯鎖住，避免日後改動悄悄破壞誠實性：

  • analyze_setup — 出場劇本分布計數（tp_full/stops/timeouts/tp1_only）與期望值；
    且 entry_expired（掛單未成交）與未平倉單必須排除於樣本（紅線③：非真實交易不污染統計）。
  • describe       — 純描述偏態（不下任何祈使指令），各分支文案正確、樣本不足誠實標示。
  • build_report   — 有資料才出報告、無資料回 None（不無中生有）。

全離線：analyze_setup/build_report 走 monkeypatch 的暫存 sqlite DB；describe 用合成 dict；
_backtest_anchor / _lessons_block（會讀真實 data_dir）一律 monkeypatch 掉以保 hermetic。
"""
import sqlite3
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from l3_dispatcher import auto_tuner as at


# ───────────────────────────────────────────────────────────────────────────
# 暫存 DB helper：建最小 paper_trades（只含 analyze_setup 用到的欄）
# ───────────────────────────────────────────────────────────────────────────
def _make_db(path: Path, rows: list[dict]) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE paper_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "setup TEXT, status TEXT, exit_reason TEXT, entry_at INTEGER, "
        "realized_r REAL, legs_hit TEXT, pnl_usd REAL)")
    for r in rows:
        conn.execute(
            "INSERT INTO paper_trades (setup, status, exit_reason, entry_at, "
            "realized_r, legs_hit, pnl_usd) VALUES (?,?,?,?,?,?,?)",
            (r["setup"], r["status"], r.get("exit_reason"), r["entry_at"],
             r.get("realized_r"), r.get("legs_hit", ""), r.get("pnl_usd", 0.0)))
    conn.commit()
    conn.close()


def _now_ms() -> int:
    return int(time.time() * 1000)


# ═══════════════════════════════════════════════════════════════════════════
#  analyze_setup：出場劇本分布 + 排除 entry_expired / 未平倉 / 過期 / 別的 setup
# ═══════════════════════════════════════════════════════════════════════════
def test_analyze_setup_counts_and_exclusions(tmp_path, monkeypatch):
    now = _now_ms()
    rows = [
        # 4 筆 deepdive、closed、最近、含四種出場劇本 → 進統計
        {"setup": "deepdive", "status": "closed", "exit_reason": "tp3",
         "entry_at": now, "realized_r": 2.0, "legs_hit": "tp1,tp2,tp3", "pnl_usd": 200},
        {"setup": "deepdive", "status": "closed", "exit_reason": "stop_loss",
         "entry_at": now, "realized_r": -1.0, "legs_hit": "stop", "pnl_usd": -100},
        {"setup": "deepdive", "status": "closed", "exit_reason": "timeout",
         "entry_at": now, "realized_r": -0.2, "legs_hit": "", "pnl_usd": -20},
        {"setup": "deepdive", "status": "closed", "exit_reason": "tp1",
         "entry_at": now, "realized_r": 0.5, "legs_hit": "tp1", "pnl_usd": 50},
        # 排除①：entry_expired（掛單未成交＝非真實交易，紅線③）
        {"setup": "deepdive", "status": "closed", "exit_reason": "entry_expired",
         "entry_at": now, "realized_r": 0.0, "legs_hit": "", "pnl_usd": 0},
        # 排除②：未平倉
        {"setup": "deepdive", "status": "open", "exit_reason": None,
         "entry_at": now, "realized_r": None, "legs_hit": "", "pnl_usd": 0},
        # 排除③：超出 days 視窗（100 天前）
        {"setup": "deepdive", "status": "closed", "exit_reason": "tp3",
         "entry_at": now - 100 * 86400 * 1000, "realized_r": 9.0,
         "legs_hit": "tp1,tp2,tp3", "pnl_usd": 900},
        # 排除④：別的 setup
        {"setup": "intraday", "status": "closed", "exit_reason": "stop_loss",
         "entry_at": now, "realized_r": -1.0, "legs_hit": "stop", "pnl_usd": -100},
    ]
    db = tmp_path / "trade_journal.db"
    _make_db(db, rows)
    monkeypatch.setattr(at, "DB_PATH", str(db))

    a = at.analyze_setup("deepdive", days=60)
    assert a["setup"] == "deepdive"
    assert a["n"] == 4                       # 4 真實成交（排除 entry_expired/open/過期/別 setup）
    assert a["win_rate"] == 50.0             # 2 勝 / 4
    assert a["expectancy_r"] == 0.325        # (2.0 -1.0 -0.2 +0.5)/4
    assert a["tp_full"] == 1                 # legs_hit 含三 tp
    assert a["stops"] == 1                   # exit_reason 含 stop 且 r<=0
    assert a["timeouts"] == 1                # exit_reason 含 timeout
    assert a["tp1_only"] == 1                # 只到 tp1（tp 計數 <3）


def test_analyze_setup_empty_returns_zero(tmp_path, monkeypatch):
    db = tmp_path / "trade_journal.db"
    _make_db(db, [])
    monkeypatch.setattr(at, "DB_PATH", str(db))
    a = at.analyze_setup("deepdive", days=60)
    assert a == {"setup": "deepdive", "n": 0}


# ═══════════════════════════════════════════════════════════════════════════
#  describe：純描述各分支（n≥MIN_SAMPLE 走主分支，避開 _backtest_anchor 外部相依）
# ═══════════════════════════════════════════════════════════════════════════
def test_describe_timeout_heavy():
    a = {"setup": "x", "n": 20, "win_rate": 40, "expectancy_r": 0.0,
         "tp_full": 0, "stops": 0, "timeouts": 8, "tp1_only": 0}
    notes = at.describe(a)
    assert any("逾時" in s for s in notes)


def test_describe_stop_heavy_negative():
    a = {"setup": "x", "n": 20, "win_rate": 30, "expectancy_r": -0.5,
         "tp_full": 0, "stops": 12, "timeouts": 0, "tp1_only": 0}
    notes = at.describe(a)
    assert any("止損" in s for s in notes)


def test_describe_tp1_only_skew():
    a = {"setup": "x", "n": 20, "win_rate": 40, "expectancy_r": 0.1,
         "tp_full": 1, "stops": 0, "timeouts": 0, "tp1_only": 10}
    notes = at.describe(a)
    assert any("TP1" in s for s in notes)


def test_describe_positive_edge_is_descriptive_not_imperative():
    a = {"setup": "x", "n": 30, "win_rate": 55, "expectancy_r": 0.4,
         "tp_full": 10, "stops": 0, "timeouts": 0, "tp1_only": 0}
    notes = at.describe(a)
    assert any("期望值正" in s for s in notes)
    # v56 task#52：純描述、不下祈使指令——不得出現「建議」字眼
    assert not any("建議" in s for s in notes)


def test_describe_no_skew_fallback():
    a = {"setup": "x", "n": 30, "win_rate": 40, "expectancy_r": 0.0,
         "tp_full": 0, "stops": 0, "timeouts": 0, "tp1_only": 0}
    notes = at.describe(a)
    assert any("無明顯偏態" in s for s in notes)


def test_describe_small_sample_is_honest(monkeypatch):
    # 樣本不足分支：誠實標「樣本不足」；_backtest_anchor 打掉以保 hermetic
    monkeypatch.setattr(at, "_backtest_anchor", lambda setup: None)
    notes = at.describe({"setup": "x", "n": 5})
    assert "樣本不足" in notes[0]
    assert len(notes) == 1


# ═══════════════════════════════════════════════════════════════════════════
#  build_report：有資料才出報告、無資料回 None
# ═══════════════════════════════════════════════════════════════════════════
def test_build_report_with_data(tmp_path, monkeypatch):
    now = _now_ms()
    rows = [
        {"setup": "deepdive", "status": "closed", "exit_reason": "tp3",
         "entry_at": now, "realized_r": 2.0, "legs_hit": "tp1,tp2,tp3", "pnl_usd": 200},
        {"setup": "deepdive", "status": "closed", "exit_reason": "stop_loss",
         "entry_at": now, "realized_r": -1.0, "legs_hit": "stop", "pnl_usd": -100},
    ]
    db = tmp_path / "trade_journal.db"
    _make_db(db, rows)
    monkeypatch.setattr(at, "DB_PATH", str(db))
    monkeypatch.setattr(at, "_lessons_block", lambda: None)       # 不讀真實 lessons.jsonl
    monkeypatch.setattr(at, "_backtest_anchor", lambda setup: None)  # 不碰回測 Session
    rep = at.build_report(days=60)
    assert rep is not None
    assert "調參 Session 報告" in rep
    assert "deepdive" in rep
    # 純描述橫幅：明確指向 L2 才是改參數的合法路徑
    assert "L2" in rep


def test_build_report_none_when_empty(tmp_path, monkeypatch):
    db = tmp_path / "trade_journal.db"
    _make_db(db, [])
    monkeypatch.setattr(at, "DB_PATH", str(db))
    monkeypatch.setattr(at, "_lessons_block", lambda: None)
    rep = at.build_report(days=60)
    assert rep is None


# ───────────────────────────────────────────────────────────────────────────
# 直跑入口
# ───────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        try:
            # 直跑時無 pytest fixture；略過需 tmp_path/monkeypatch 者
            import inspect
            params = inspect.signature(fn).parameters
            if params:
                print(f"… 略過（需 fixture）：{fn.__name__}")
                continue
            fn()
            print(f"✅ {fn.__name__}")
            passed += 1
        except Exception:
            print(f"❌ {fn.__name__}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed（直跑僅涵蓋無 fixture 測試；完整請走 pytest）")
