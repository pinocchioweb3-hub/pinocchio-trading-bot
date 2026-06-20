"""task#74：trade_monitor 紙上 R「同根雙觸」灌水 bug 的回測對照鎖。

定位：l3_dispatcher/trade_monitor.py::_check_trade 舊版用 max(high)/min(low) 聚合整窗
4 根 5m K 線後「先記 TP、再記 stop」，且無「同一根 SL/TP 皆觸→保守先判 stop」守則。
結果＝同一筆行情，backtest(simulator.py:97「保守：同根先判 stop」)算虧、live paper 卻記成
「TP 部分獲利＋剩餘止損」→ 系統性灌水紙上 R 曲線/勝率（=紅線①實盤解鎖判據的統計基礎）。

治本＝逐根時序走訪（bars 升序），同一根 SL/TP 皆觸→保守先判 stop（不記該根 TP）；
TP 在較早 K 線、stop 在較晚 K 線則合法依序成立。本檔把以下語意鎖死：

  1. 同根雙觸（bull/bear）→ 只記 stop（全平），不得記該根 TP。  ← 直接證明 bug 已修
  2. stop-only K 線 → 全止損。
  3. 全 TP、無 stop → 三段 TP 依序成立，零 stop。
  4. TP 在較早 K 線、stop 在較晚 K 線 → 部分 TP 合法成立＋剩餘止損（證明沒過度修正）。
  5. 跨對照：對同一組 OHLC，trade_monitor 與 simulator 給「相同勝負」（synthesis 驗收準則）。

純離線：_check_trade 與 simulate 皆純函式，零 DB／零網路／零真錢／零訊號數學。

註：本修不含 simulator 的「TP1 後止損移到開倉價(breakeven)」遷移——trade_monitor 紙上帳本
沿用固定 stop（breakeven 只在 Telegram 對人的建議）。故第 5 項跨對照只比「無 partial-then-stop」
的情境（A/B/C）的勝負；breakeven 口徑差另議，不在本修範圍。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from l3_dispatcher import trade_monitor as tm
from backtest.simulator import simulate

# 固定 TP 分批，讓測試不受 botconfig.tp_size_split 影響
SPLIT = {"tp1": 0.5, "tp2": 0.3, "tp3": 0.2}


def _trade(direction, stop, tp1, tp2, tp3, legs_hit=None, size_remaining=1.0):
    return {
        "direction": direction,
        "entry_price": 100.0,
        "stop_price": stop,
        "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "legs_hit": legs_hit or [],
        "size_remaining": size_remaining,
    }


def _bar(ts, hi, lo, cl):
    return {"ts": ts, "high": hi, "low": lo, "close": cl}


def _net_r(events, entry, stop, direction, last_close, opened=1.0):
    """把 _check_trade 的觸發事件換算成淨 R（剩餘未平的以 last_close 視為 timeout）。

    無手續費版，對齊 simulate(taker_fee=0, slippage=0) 以做 apples-to-apples 勝負比較。
    """
    sld = abs(entry - stop)
    rem = opened
    total = 0.0

    def _unit(px):
        return (px - entry) / sld if direction == "bull" else (entry - px) / sld

    for _label, price, size in events:
        total += _unit(price) * size
        rem -= size
    if rem > 1e-9:
        total += _unit(last_close) * rem
    return total


def _sign(x, eps=1e-9):
    return 1 if x > eps else (-1 if x < -eps else 0)


# --- 1. 同根雙觸：bug 的核心。只記 stop，不得記 TP ---------------------------

def test_same_bar_both_touched_bull_books_stop_only():
    t = _trade("bull", stop=95, tp1=105, tp2=110, tp3=115)
    # 一根內 high=106(≥tp1) 且 low=94(≤stop)：盤中先後未知 → 保守先判 stop。
    bars = [_bar(1, 106, 94, 100)]
    events = tm._check_trade(t, bars, SPLIT)
    assert events == [("stop", 95, 1.0)], events
    # 舊聚合版會回 [("tp1",105,0.5),("stop",95,0.5)] = 灌水；新版只記全止損。


def test_same_bar_both_touched_bear_books_stop_only():
    t = _trade("bear", stop=105, tp1=95, tp2=90, tp3=85)
    # bear：一根內 low=94(≤tp1) 且 high=106(≥stop) → 保守先判 stop。
    bars = [_bar(1, 106, 94, 100)]
    events = tm._check_trade(t, bars, SPLIT)
    assert events == [("stop", 105, 1.0)], events


def test_same_bar_all_tp_and_stop_books_stop_only():
    """極端：一根掃過全部 TP 也掃到 stop → 仍保守全止損（不得記任何 TP）。"""
    t = _trade("bull", stop=95, tp1=105, tp2=110, tp3=115)
    bars = [_bar(1, 120, 94, 100)]   # high 過 tp3，low 破 stop
    events = tm._check_trade(t, bars, SPLIT)
    assert events == [("stop", 95, 1.0)], events


# --- 2. stop-only ----------------------------------------------------------

def test_stop_only_books_full_stop():
    t = _trade("bull", stop=95, tp1=105, tp2=110, tp3=115)
    bars = [_bar(1, 99, 94, 96)]    # 未到 tp1，破 stop
    events = tm._check_trade(t, bars, SPLIT)
    assert events == [("stop", 95, 1.0)], events


# --- 3. 全 TP、無 stop ------------------------------------------------------

def test_all_tp_no_stop():
    t = _trade("bull", stop=95, tp1=105, tp2=110, tp3=115)
    bars = [
        _bar(1, 106, 99, 104),   # tp1
        _bar(2, 111, 104, 110),  # tp2
        _bar(3, 116, 110, 115),  # tp3
    ]
    events = tm._check_trade(t, bars, SPLIT)
    assert events == [("tp1", 105, 0.5), ("tp2", 110, 0.3), ("tp3", 115, 0.2)], events


# --- 4. TP 在較早 K 線、stop 在較晚 K 線 → 沒過度修正 -----------------------

def test_tp_then_stop_in_later_bar_partial_then_stop():
    t = _trade("bull", stop=95, tp1=105, tp2=110, tp3=115)
    bars = [
        _bar(1, 106, 99, 104),   # tp1（不破 stop）
        _bar(2, 104, 94, 96),    # 稍後破 stop（high 未到 tp2）
    ]
    events = tm._check_trade(t, bars, SPLIT)
    # tp1 早於 stop 那根 → 合法成立；剩餘 0.5 在較晚 K 線止損。
    assert events == [("tp1", 105, 0.5), ("stop", 95, 0.5)], events


def test_respects_prior_legs_hit_and_size_remaining():
    """跨輪狀態：上輪已記 tp1、剩 0.5 倉位；本窗破 stop → 只對剩餘 0.5 記 stop。"""
    t = _trade("bull", stop=95, tp1=105, tp2=110, tp3=115,
               legs_hit=["tp1"], size_remaining=0.5)
    bars = [_bar(1, 104, 94, 96)]
    events = tm._check_trade(t, bars, SPLIT)
    assert events == [("stop", 95, 0.5)], events


# --- 5. 跨對照 simulator：同一組 OHLC → 相同勝負（synthesis 驗收準則）-------

def _outcome_sign_via_simulator(direction, stop, tps, ohlc, hold=48):
    out = simulate(
        symbol="T", setup_name="x", direction=direction,
        entry_ts=0, entry_price=100.0, stop=stop, tps=tps,
        future_prices=[(b["ts"], b["high"], b["low"], b["close"]) for b in ohlc],
        hold_max_hours=hold, taker_fee=0.0, slippage=0.0,
    )
    return _sign(out.realized_r)


def _outcome_sign_via_monitor(direction, stop, tps, ohlc):
    t = _trade(direction, stop, tps[0], tps[1], tps[2])
    events = tm._check_trade(t, ohlc, SPLIT)
    return _sign(_net_r(events, 100.0, stop, direction, ohlc[-1]["close"]))


def test_cross_agreement_same_bar_both_touched():
    """THE 灌水 案例：兩引擎都必須判『負』（虧）。修前 monitor 會判正/打平＝灌水。"""
    stop, tps = 95.0, (105.0, 110.0, 115.0)
    ohlc = [_bar(1, 106, 94, 100)]
    s_mon = _outcome_sign_via_monitor("bull", stop, tps, ohlc)
    s_sim = _outcome_sign_via_simulator("bull", stop, tps, ohlc)
    assert s_mon == s_sim == -1, (s_mon, s_sim)


def test_cross_agreement_stop_only():
    stop, tps = 95.0, (105.0, 110.0, 115.0)
    ohlc = [_bar(1, 99, 94, 96)]
    assert _outcome_sign_via_monitor("bull", stop, tps, ohlc) \
        == _outcome_sign_via_simulator("bull", stop, tps, ohlc) == -1


def test_cross_agreement_all_tp():
    stop, tps = 95.0, (105.0, 110.0, 115.0)
    ohlc = [
        _bar(1, 106, 99, 104),
        _bar(2, 111, 104, 110),
        _bar(3, 116, 110, 115),
    ]
    assert _outcome_sign_via_monitor("bull", stop, tps, ohlc) \
        == _outcome_sign_via_simulator("bull", stop, tps, ohlc) == 1


def test_cross_agreement_bear_same_bar():
    stop, tps = 105.0, (95.0, 90.0, 85.0)
    ohlc = [_bar(1, 106, 94, 100)]
    s_mon = _outcome_sign_via_monitor("bear", stop, tps, ohlc)
    s_sim = _outcome_sign_via_simulator("bear", stop, tps, ohlc)
    assert s_mon == s_sim == -1, (s_mon, s_sim)
