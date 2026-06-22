# -*- coding: utf-8 -*-
"""task#11 治本回歸：fetch_okx_closed_pnl 不得把 since_ms 傳進 OKX positions-history
（會回 0 列→已平倉 demo 倉永卡 await_pnl/零筆 tp）。改 since=None + 本地 uTime scope。"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from l4_execution import demo_trader as dt


class _FakeEx:
    """記錄 fetch_positions_history 收到的 since，並回傳預設的 history rows。"""
    def __init__(self, rows):
        self._rows = rows
        self.since_seen = "UNSET"
        self.limit_seen = None
        self.calls = 0

    async def fetch_positions_history(self, symbols=None, since=None, limit=None, params=None):
        self.since_seen = since
        self.limit_seen = limit
        self.calls += 1
        return self._rows


def _row(pos_side, pnl, utime):
    return {"info": {"posSide": pos_side, "realizedPnl": str(pnl), "uTime": str(utime)}}


def _run(coro):
    return asyncio.run(coro)


def test_since_not_passed_to_api():
    # 鐵則：無論呼叫端給什麼 since_ms，傳給 OKX 的 since 必須是 None（否則 OKX 回 0 列）。
    ex = _FakeEx([_row("long", 99.4, 2000)])
    res = _run(dt.fetch_okx_closed_pnl(ex, "OP", "long", since_ms=1500))
    assert ex.since_seen is None, f"since 不得外傳 OKX，實得 {ex.since_seen!r}"
    assert res["found"] is True and abs(res["pnl_usd"] - 99.4) < 1e-9


def test_local_utime_scope_excludes_older_closure():
    # 同標的有舊平倉(uTime=1000<since)與本倉平倉(uTime=2000≥since) → 只認本倉。
    ex = _FakeEx([_row("long", -50.0, 1000), _row("long", 99.4, 2000)])
    res = _run(dt.fetch_okx_closed_pnl(ex, "OP", "long", since_ms=1500))
    assert res["found"] is True and abs(res["pnl_usd"] - 99.4) < 1e-9
    # 全部都早於 since → 視為本倉尚未回填，found=False（保守，不誤配舊倉）
    ex2 = _FakeEx([_row("long", -50.0, 1000)])
    res2 = _run(dt.fetch_okx_closed_pnl(ex2, "OP", "long", since_ms=1500))
    assert res2["found"] is False


def test_picks_most_recent_when_multiple_valid():
    ex = _FakeEx([_row("long", 10.0, 2000), _row("long", 20.0, 3000)])
    res = _run(dt.fetch_okx_closed_pnl(ex, "X", "long", since_ms=1500))
    assert abs(res["pnl_usd"] - 20.0) < 1e-9 and res["u_time"] == 3000


def test_posside_filter():
    ex = _FakeEx([_row("short", 77.0, 2000), _row("long", 99.4, 2100)])
    res = _run(dt.fetch_okx_closed_pnl(ex, "OP", "long", since_ms=1500))
    assert abs(res["pnl_usd"] - 99.4) < 1e-9   # 只取 long


def test_no_since_arg_still_works():
    # since_ms=None（呼叫端沒給）→ 不做本地 scope，取最近一筆。
    ex = _FakeEx([_row("long", 5.0, 2000)])
    res = _run(dt.fetch_okx_closed_pnl(ex, "X", "long"))
    assert ex.since_seen is None and res["found"] is True


def test_empty_history_found_false():
    ex = _FakeEx([])
    res = _run(dt.fetch_okx_closed_pnl(ex, "X", "long", since_ms=1500))
    assert res["found"] is False
