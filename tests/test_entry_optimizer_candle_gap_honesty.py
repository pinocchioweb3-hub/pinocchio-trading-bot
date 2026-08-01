"""v206：入場優化器不得把「取不到 K 線」折成「這批本來就沒這些標的」。

同物種第 26 次（把讀不出來折成本來就沒有）。這次落點是 entry_policy_optimizer
的 K 線載入迴圈：

    try:    bars = await get_ohlc(...)
    except: bars = []
    if not bars: continue    # 註解寫「無源（如美股/黃金 OKX 無永續）」

於是三件不同的事被壓成同一個靜默 continue：
  (1) 這個 symbol 真的沒永續合約（合法、by design）
  (2) 取 K 線丟例外（網路/資料問題）
  (3) data_loader.get_ohlc 自己把「兩路都失敗」吞成 []（v206 同批已補印痕）

整個 symbol 的訊號因此無聲掉出重放樣本，而**樣本數正是本優化器唯一的瓶頸**
（每輪報告只被讀一個數字：n_aligned 差門檻 30 幾筆）。資料壞掉的那天，報告
與「今天真的沒單」完全同形。

⛔ 斷言順序刻意把「報告是否靜音」排在最前面：那才是舊碼真正的罪證。若把
   res["n_excluded_no_candles"] 這種結構性斷言排前面，舊碼只會吐 KeyError，
   看起來像有抓到、其實是虛設檢定。
"""
import json
import tempfile
from pathlib import Path

import pytest

from l3_dispatcher import entry_policy_optimizer as epo
from l3_dispatcher import entry_policy_store as eps

_TS0 = 1_700_000_000_000


def _row(tid, sym="MU"):
    snap = json.dumps({"direction": "bull", "planned_entry": 95.0,
                       "planned_stop": 90.0, "planned_tp": {"tp1": 110.0},
                       "regime_at_entry": {"oi_price_quadrant": "price_up_oi_up"}})
    return {"id": tid, "symbol": sym, "direction": "bull", "entry_price": 95.0,
            "stop_price": 90.0, "tp1": 110.0, "entry_at": _TS0,
            "exit_reason": "tp1", "plan_snapshot": snap}


async def _empty_ohlc(symbol, tf, days, end_ms=None):
    return []


async def _boom_ohlc(symbol, tf, days, end_ms=None):
    raise RuntimeError("binance 503")


def _run(rows, get_ohlc):
    import asyncio

    from backtest.l2_stat_gates import TrialLedger
    with tempfile.TemporaryDirectory() as td:
        return asyncio.run(epo.run_entry_optimization(
            rows=rows, at_ms=1785600000000,
            ledger=TrialLedger(Path(td) / "ledger.jsonl"),
            active_path=Path(td) / eps.ACTIVE_NAME,
            audit_path=Path(td) / eps.AUDIT_NAME,
            get_ohlc=get_ohlc))


@pytest.mark.parametrize("fetch,mark", [(_empty_ohlc, "成因不明"),
                                        (_boom_ohlc, "確定是資料/網路問題")])
def test_report_is_not_silent_when_candles_are_missing(fetch, mark):
    """⛔ 舊碼在這裡回 None（靜音）＝與『今天真的沒樣本』一模一樣，人看不出資料壞了。"""
    res = _run([_row(i) for i in range(4)], fetch)
    report = epo.render_report(res)
    assert report is not None, "取不到 K 線導致 0 桶時報告靜音＝把資料故障講成沒樣本"
    assert "取不到 K 線" in report
    assert mark in report, "兩種成因不可同格呈現（例外＝確定故障；回空＝分不出來）"


def test_dropped_rows_are_counted_not_just_skipped():
    """掉出樣本的筆數要有數字——否則沒人看得出 n_aligned 是被縮小過的。"""
    res = _run([_row(i) for i in range(4)], _empty_ohlc)
    assert res["n_excluded_no_candles"] == 4
    assert res["candles_diag"]["empty"] == ["MU"]
    assert res["candles_diag"]["fetch_error"] == {}


def test_exception_and_empty_are_kept_apart():
    """例外＝確定是取用失敗；回空＝可能無源也可能被吞。⛔ 不可把回空斷言成『無源』。"""
    res = _run([_row(i) for i in range(3)], _boom_ohlc)
    assert res["candles_diag"]["empty"] == []
    assert "MU" in res["candles_diag"]["fetch_error"]
    assert res["n_excluded_no_candles"] == 3
    line = epo._render_no_candles_line(res)
    assert "無源" not in line.split("成因不明")[0], "例外路徑不得出現『無源』字樣"


def test_healthy_fetch_records_nothing():
    """回歸護欄：K 線正常時不得憑空記掉樣本（誤報比漏報更快讓人不看報告）。"""
    bars = [{"ts": _TS0 + i * 3_600_000, "open": 100.0, "high": 101.0,
             "low": 94.0, "close": 100.0} for i in range(epo._FORWARD_BARS + 80)]

    async def _good(symbol, tf, days, end_ms=None):
        return bars

    res = _run([_row(i) for i in range(3)], _good)
    assert res["n_excluded_no_candles"] == 0
    assert epo._render_no_candles_line(res) == ""
