# -*- coding: utf-8 -*-
"""v247：入場優化器「0 桶」不再靜音——「還沒熟」不得折成「今天沒單／worker 死了」。

同物種第 68 次。落點：`l3_dispatcher/entry_policy_optimizer.py`
（`load_plans_and_candles` 的兩個 silent continue ＋ `render_report` 的 `return None`
 ＋ 稽核 sink 整輪不寫任何一列）。

**這一次的形狀比前幾次刁：系統沒有壞。** 它跑了、判斷完全正確、而且會自己解——
但整條鏈一個字都沒說，於是「跑了、樣本還沒滿窗」和「worker 死了四天」在
**任何一個 sink 上都同形**。我自己在 2026-08-03 這一輪就先讀成後者，被自己的
量測推翻，這正是本測試存在的理由。

量到的下場（2026-08-03）：
    entry_policy_audit.jsonl   4445 列，07-30 之前每天約 119 列，
                               **2026-07-30 10:02:25 之後整整四天零列**
    auto_tuner_state.json      last_review_date = 2026-08-03（⇒ 它今天有跑）
    逐步量測                    載入 279 筆 → 同代(okx_free_fallback) 48 筆
                               → data_plane_filter 後 16 筆 → **n_plans = 0**
    逐列量測                    16 筆全部 TOO_NEW：需訊號後 133 根 1h K，
                               最成熟一筆（SOL）只有 61 根，age 60.3h
    candles_diag               `{"fetch_error": {}, "empty": [], "n_rows_dropped": 0}`
                               ＝v206 的計數器誠實地說「零筆掉隊」，因為掉隊發生在
                               **它涵蓋不到的那一層**（per-row，不是 per-symbol）。

⛔ 邊界：
  * ⛔ 不動任何訊號數學：_FORWARD_BARS／閾值／L2 門檻一律不碰。樣本不足就是不足，
    **不得為了讓桶長出來而放寬窗**（章程：不為湊樣本改策略）。
  * ⛔ 「真的一筆樣本都沒有」時報告仍回 None（不製造 ⚠️ 雜訊，否則符號貶值）。
  * ⛔ v202（快照讀不出來）／v206（取不到 K 線）兩個既有 carve-out 的文字與優先序不變。
  * ⛔ 成因湊不出來時必須誠實寫 "unknown" 並出聲，**不得**挑一個最像的填上去。

改動前的碼會失敗在哪（非虛設檢定的證明）：
  * candles_diag 沒有 n_rows_forward_window_short／n_rows_signal_too_early 這兩個鍵
  * res 沒有對應的 n_excluded_* 鍵
  * render_report 在這種 0 桶上回 None（靜音）
  * 整輪 0 桶時稽核檔一列都不寫（四天沉默的直接來源）
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from l3_dispatcher import entry_policy_optimizer as epo
from l3_dispatcher import entry_policy_store as eps

_TS0 = 1_780_000_000_000            # 序列起點（ms，對齊 1h）
_STEP = epo._STEP_MS


def _bars(n: int, ts0: int = _TS0) -> list[dict]:
    out = []
    for i in range(n):
        px = 100.0 + (i % 7) * 0.1
        out.append({"ts": ts0 + i * _STEP, "open": px, "high": px + 1.0,
                    "low": px - 1.0, "close": px, "volume": 1000.0})
    return out


def _row(rid: int, entry_at: int, symbol: str = "PULL") -> dict:
    snap = json.dumps({"planned_entry": 100.0, "planned_stop": 95.0,
                       "planned_tp": {"tp1": 110.0}, "direction": "bull",
                       "regime_at_entry": {"oi_price_quadrant": "up_up"}})
    return {"id": rid, "symbol": symbol, "setup": "deepdive", "direction": "bull",
            "entry_price": 100.0, "stop_price": 95.0, "tp1": 110.0,
            "entry_at": int(entry_at), "exit_reason": "tp1", "status": "closed",
            "plan_snapshot": snap, "entry_splits": None}


def _ohlc(bars):
    async def _f(symbol, tf, days, end_ms=None):
        return list(bars)
    return _f


def _load(rows, bars):
    return asyncio.run(epo.load_plans_and_candles(rows, get_ohlc=_ohlc(bars)))


# ═══════════ ① 兩個 silent continue 必須被計數 ═══════════


def test_forward_window_short_is_counted():
    """實測到的那一種：K 線好好的、快照好好的，但訊號後窗不足 ⇒ 舊碼一聲不吭。"""
    bars = _bars(200)
    row = _row(1, bars[190]["ts"])                 # 訊號後只剩 9 根，需 133
    plans, _q, _c, diag = _load([row], bars)
    assert plans == []                              # 略過本身是對的（⛔ 不放寬窗）
    assert diag.get("n_rows_forward_window_short") == 1, \
        "訊號後窗不足者被靜默 continue ⇒ 0 桶那天長得跟『今天真的沒單』一模一樣"
    fw = diag.get("forward_window") or {}
    assert fw.get("need") == epo._FORWARD_BARS
    assert fw.get("max_have") == 9, \
        "要能算出『最成熟一筆還差幾根』，否則說不出還要等多久"


def test_signal_before_series_start_is_counted():
    bars = _bars(200)
    row = _row(2, _TS0 - 10 * _STEP)                # 訊號早於序列起點
    plans, _q, _c, diag = _load([row], bars)
    assert plans == []
    assert diag.get("n_rows_signal_too_early") == 1


def test_counters_stay_zero_when_plans_build():
    """反向側：一切正常時 ⛔ 不得謊報有東西被排除。"""
    bars = _bars(400)
    row = _row(3, bars[100]["ts"])                  # 後面還有 299 根，滿窗
    plans, _q, _c, diag = _load([row], bars)
    assert len(plans) == 1
    assert not diag.get("n_rows_forward_window_short")
    assert not diag.get("n_rows_signal_too_early")


# ═══════════ ② 報告：0 桶的成因必須說得出來 ═══════════


def _render(**over):
    base = {"buckets": [], "n_rows": 16, "n_plans": 0, "n_promoted": 0,
            "n_excluded_unreadable_snapshot": 0, "n_excluded_no_candles": 0,
            "n_excluded_forward_window_short": 0, "n_excluded_signal_too_early": 0,
            "candles_diag": {"fetch_error": {}, "empty": [], "n_rows_dropped": 0},
            "cohort": {"active_generation": "okx_free_fallback",
                       "mix": {"unknown": 231, "okx_free_fallback": 48},
                       "n_in": 279, "n_kept": 16, "n_excluded_other_generation": 263}}
    base.update(over)
    return epo.render_report(base)


def test_report_speaks_when_all_samples_are_too_new():
    rep = _render(n_excluded_forward_window_short=16,
                  candles_diag={"fetch_error": {}, "empty": [], "n_rows_dropped": 0,
                                "n_rows_forward_window_short": 16,
                                "forward_window": {"need": 133, "max_have": 61}})
    assert rep, "全部樣本太新導致 0 桶時報告靜音＝把『還沒熟』講成『今天沒單』"
    assert "133" in rep and "61" in rep, f"要說得出還差多少：{rep}"
    assert "72" in rep, "『還差幾根』要算給人看，否則沒人知道還要等多久"


def test_report_never_calls_it_no_samples():
    """⛔ 紅線③：樣本明明在，不得對外寫成『沒有樣本』。"""
    rep = _render(n_excluded_forward_window_short=16,
                  candles_diag={"fetch_error": {}, "empty": [], "n_rows_dropped": 0,
                                "n_rows_forward_window_short": 16,
                                "forward_window": {"need": 133, "max_have": 61}})
    assert "沒樣本" not in rep.replace("不是『今天沒樣本』", "")


def test_report_stays_silent_when_there_really_are_no_samples():
    """反向側：真的一筆都沒載到 ⇒ 仍安靜（⛔ 不製造 ⚠️ 雜訊）。"""
    assert _render(n_rows=0,
                   cohort={"active_generation": None, "mix": {},
                           "n_in": 0, "n_kept": 0,
                           "n_excluded_other_generation": 0}) is None


def test_report_speaks_when_generation_filter_emptied_it():
    """世代過濾把樣本濾光＝我自己 v144/v178 造出來的成因，也不是『今天沒單』。"""
    rep = _render(n_rows=0,
                  cohort={"active_generation": "okx_free_fallback",
                          "mix": {"unknown": 231, "okx_free_fallback": 48},
                          "n_in": 279, "n_kept": 0, "n_excluded_other_generation": 279})
    assert rep and "279" in rep


def test_report_admits_when_the_cause_is_unknown():
    """⛔ 成因湊不出來時必須誠實說不知道——**不得**挑一個最像的填上。"""
    rep = _render()                                 # 樣本在、四個計數器全 0、仍 0 桶
    assert rep and ("不明" in rep or "未知" in rep), rep


def test_v202_and_v206_carveouts_unchanged():
    assert "快照讀不出來" in _render(n_excluded_unreadable_snapshot=3)
    assert "取不到 K 線" in _render(
        n_excluded_no_candles=4,
        candles_diag={"fetch_error": {}, "empty": ["MU"], "n_rows_dropped": 4})


# ═══════════ ③ 稽核 sink：跑過就要留痕 ═══════════


def _run(rows, bars, td, monkeypatch):
    # active_generation 第一順位讀**進程全域**的現行宇宙源；全庫一起跑時會被別的測試
    # 帶成 'coinglass' 而殘留 ⇒ 本測試的樣本全被判別代。釘成 None 讓它退回讀樣本留痕。
    import l3_dispatcher.universe_provenance as up
    monkeypatch.setattr(up, "get_universe_source", lambda: None)
    return asyncio.run(epo.run_entry_optimization(
        rows=rows, at_ms=1_785_600_000_000, ledger=object(),
        active_path=Path(td) / eps.ACTIVE_NAME,
        audit_path=Path(td) / eps.AUDIT_NAME,
        get_ohlc=_ohlc(bars)))


def _audit_rows(td):
    p = Path(td) / eps.AUDIT_NAME
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_zero_bucket_run_leaves_one_audit_row(monkeypatch):
    """四天零列的直接來源：0 桶 ⇒ _optimize_bucket 一次都不跑 ⇒ 稽核檔完全不動。"""
    bars = _bars(200)
    with tempfile.TemporaryDirectory() as td:
        res = _run([_row(1, bars[190]["ts"])], bars, td, monkeypatch)
        assert res["n_buckets"] == 0
        assert res.get("n_excluded_forward_window_short") == 1
        rows = [r for r in _audit_rows(td) if r.get("action") == "run_no_buckets"]
        assert len(rows) == 1, "跑了 0 桶卻不留痕 ⇒ 與『沒跑』永久同形"
        assert rows[0].get("cause") == "forward_window_short"
        assert rows[0].get("n_rows") == 1


def test_nonzero_bucket_run_adds_no_run_row(monkeypatch):
    """⛔ 反向側：有桶時不得多寫這一列（避免 sink 膨脹、⚠️ 貶值）。"""
    def _fake(plans, quad_by_pid, candles_by_pid, **kw):
        return {"at_ms": kw["at_ms"], "n_buckets": 2, "n_pooled": 1,
                "n_plans": len(plans), "n_promoted": 0, "buckets": [{}, {}]}
    monkeypatch.setattr(epo, "optimize_entry", _fake)
    bars = _bars(400)
    with tempfile.TemporaryDirectory() as td:
        _run([_row(1, bars[100]["ts"])], bars, td, monkeypatch)
        assert [r for r in _audit_rows(td) if r.get("action") == "run_no_buckets"] == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
