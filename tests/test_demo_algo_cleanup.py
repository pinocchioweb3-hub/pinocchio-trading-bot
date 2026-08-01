"""v210｜幽靈委託清理：「查不到／取消失敗」不得折成「確認沒有殘留掛單」。

同物種第 30 次（未知 → 折成確認）。落點是 demo 側**下單路徑**上僅剩的一處：
倉平掉之後清 attachAlgoOrds 殘留 TP/SL 的那一步。舊碼 `任何失敗回 0`，而 0 同時
也是「確認沒有殘留」的答案；呼叫端只在 `if n_cxl:` 為真時記帳 ⇒ 查詢失敗、回應
形狀認不得、取消被交易所打回，三件事全部無聲。

⛔ 為什麼這一處比它的風險級別更要緊：那是**一次性**路徑。intent 一旦
apply_demo_close 就不再回訪，清不掉的委託沒有下一輪重試 ⇒ 幽靈 TP/SL 永久殘留
在交易所端（而它是會成交的活單）。

另一半是反向的謊報：舊碼取消後無條件 `return len(body)`，交易所逐筆打回也照樣
記成「取消了 N 筆」。本檔 test_partial_cancel_* 直接量這一項，且**不需要**新參數
就能在舊碼上失敗（斷言形狀 = `2 == 1`，非 TypeError 這種虛設檢定）。
"""
import asyncio

import pytest

from l4_execution import demo_guard, demo_trader as dt
from l3_dispatcher import demo_operator as do


@pytest.fixture(autouse=True)
def _pass_demo_guard(monkeypatch):
    async def fake_confirm(ex):
        return True
    monkeypatch.setattr(demo_guard, "confirm_okx_demo", fake_confirm)


def _pending_two_op():
    return {"data": [
        {"algoId": "a1", "instId": "OP-USDT-SWAP"},
        {"algoId": "a2", "instId": "INJ-USDT-SWAP"},
        {"algoId": "a3", "instId": "OP-USDT-SWAP"},
    ]}


# ─────────────── 行為性斷言：舊碼上失敗，且失敗形狀就是「謊報／無聲」本身 ───────────────

def test_partial_cancel_not_counted_as_full_success():
    """交易所逐筆打回一筆 → 只能記 1 筆取消成功（舊碼回 2＝謊報）。"""
    class FakeEx:
        async def private_get_trade_orders_algo_pending(self, params):
            return _pending_two_op()

        async def private_post_trade_cancel_algos(self, body):
            return {"code": "1", "data": [
                {"algoId": "a1", "sCode": "0", "sMsg": ""},
                {"algoId": "a3", "sCode": "51400", "sMsg": "cancel failed"},
            ]}

    n = asyncio.run(dt.cancel_algos_for_symbol(FakeEx(), "OP"))
    assert n == 1


def test_unreadable_cancel_result_is_not_counted_as_success():
    """取消回應形狀認不得 → 不准宣稱取消成功（舊碼回 2）。"""
    class FakeEx:
        async def private_get_trade_orders_algo_pending(self, params):
            return _pending_two_op()

        async def private_post_trade_cancel_algos(self, body):
            return None          # ccxt 換版／端點改形狀

    n = asyncio.run(dt.cancel_algos_for_symbol(FakeEx(), "OP"))
    assert n == 0


def test_cleanup_query_failure_is_not_silent(monkeypatch, capsys):
    """呼叫端層級（真正的行為面）：查不到殘留掛單時，這一輪必須出聲。

    舊碼失敗形狀＝捕獲輸出裡一個字都沒有＝無聲本身。
    """
    calls = {"cancel_attempted": 0}

    class FakeEx:
        async def private_get_trade_orders_algo_pending(self, params):
            calls["cancel_attempted"] += 1
            raise RuntimeError("okx 50011 rate limit")

        async def private_post_trade_cancel_algos(self, body):   # pragma: no cover
            raise AssertionError("查詢就失敗了，不該送出取消")

    summary = _run_monitor_close_path(monkeypatch, FakeEx())
    printed = capsys.readouterr().out
    assert calls["cancel_attempted"] == 1
    assert "幽靈委託清理未確認" in printed
    assert summary.get("algos_cleanup_unresolved") == 1
    assert summary.get("algos_cancelled") in (None, 0)


def test_cleanup_partial_is_flagged_at_caller(monkeypatch, capsys):
    """逐筆打回也算「未確認」——記下真的取消掉的那筆，同時讓殘留的那筆出聲。"""
    class FakeEx:
        async def private_get_trade_orders_algo_pending(self, params):
            return _pending_two_op()

        async def private_post_trade_cancel_algos(self, body):
            return {"code": "1", "data": [
                {"algoId": "a1", "sCode": "0", "sMsg": ""},
                {"algoId": "a3", "sCode": "51400", "sMsg": "cancel failed"},
            ]}

    summary = _run_monitor_close_path(monkeypatch, FakeEx())
    printed = capsys.readouterr().out
    assert "幽靈委託清理未確認" in printed
    assert summary.get("algos_cleanup_unresolved") == 1
    assert summary.get("algos_cancelled") == 1


# ─────────────── 三態：確認沒有 / 未知 / 確認清乾淨 ───────────────

def test_confirmed_none_is_confirmed_not_unknown():
    class FakeEx:
        async def private_get_trade_orders_algo_pending(self, params):
            return {"data": []}

        async def private_post_trade_cancel_algos(self, body):   # pragma: no cover
            raise AssertionError("沒有掛單就不該送出取消")

    rec = {}
    n = asyncio.run(dt.cancel_algos_for_symbol(FakeEx(), "OP", out=rec))
    assert n == 0
    assert rec["outcome"] == "confirmed_none"
    assert rec["unresolved"] is False


def test_unreadable_rows_is_unknown_not_confirmed_none():
    class FakeEx:
        async def private_get_trade_orders_algo_pending(self, params):
            return {"result": "ok"}      # 認不得的信封

        async def private_post_trade_cancel_algos(self, body):   # pragma: no cover
            raise AssertionError("讀不出來就不該送出取消")

    rec = {}
    n = asyncio.run(dt.cancel_algos_for_symbol(FakeEx(), "OP", out=rec))
    assert n == 0
    assert rec["outcome"] == "rows_unreadable"
    assert rec["unresolved"] is True


def test_query_exception_is_unknown():
    class FakeEx:
        async def private_get_trade_orders_algo_pending(self, params):
            raise TimeoutError("read timeout")

        async def private_post_trade_cancel_algos(self, body):   # pragma: no cover
            raise AssertionError("查詢失敗就不該送出取消")

    rec = {}
    n = asyncio.run(dt.cancel_algos_for_symbol(FakeEx(), "OP", out=rec))
    assert n == 0
    assert rec["outcome"] == "query_failed"
    assert rec["unresolved"] is True
    assert "TimeoutError" in rec["detail"]


def test_cancel_exception_keeps_attempted_count():
    class FakeEx:
        async def private_get_trade_orders_algo_pending(self, params):
            return _pending_two_op()

        async def private_post_trade_cancel_algos(self, body):
            raise RuntimeError("connection reset")

    rec = {}
    n = asyncio.run(dt.cancel_algos_for_symbol(FakeEx(), "OP", out=rec))
    assert n == 0
    assert rec["outcome"] == "cancel_failed"
    assert rec["attempted"] == 2
    assert rec["unresolved"] is True


def test_full_success_still_reports_two(monkeypatch):
    """反向側（舊碼上就綠）：正常路徑不得因本次修補而改變或誤報。"""
    cancelled = {}

    class FakeEx:
        async def private_get_trade_orders_algo_pending(self, params):
            return _pending_two_op()

        async def private_post_trade_cancel_algos(self, body):
            cancelled["body"] = body
            return {"code": "0"}

    rec = {}
    n = asyncio.run(dt.cancel_algos_for_symbol(FakeEx(), "OP", out=rec))
    assert n == 2
    assert rec["outcome"] == "cancelled"
    assert rec["unresolved"] is False
    assert {c["algoId"] for c in cancelled["body"]} == {"a1", "a3"}
    assert all(c["instId"] == "OP-USDT-SWAP" for c in cancelled["body"])


def test_bare_list_response_shape_is_readable():
    """反向側：裸清單（部分包裝層會直接回 list）仍算讀得出來。"""
    class FakeEx:
        async def private_get_trade_orders_algo_pending(self, params):
            return [{"algoId": "a1", "instId": "OP-USDT-SWAP"}]

        async def private_post_trade_cancel_algos(self, body):
            return {"code": "0"}

    rec = {}
    n = asyncio.run(dt.cancel_algos_for_symbol(FakeEx(), "OP", out=rec))
    assert n == 1
    assert rec["outcome"] == "cancelled"


# ─────────────── 純函式 ───────────────

def test_parse_algo_rows_三態():
    assert dt.parse_algo_rows({"data": []}) == []          # 確認沒有
    assert dt.parse_algo_rows([]) == []                    # 裸清單、確認沒有
    assert dt.parse_algo_rows({"data": [{"algoId": "a"}]}) == [{"algoId": "a"}]
    assert dt.parse_algo_rows({"result": []}) is None      # 未知
    assert dt.parse_algo_rows("data") is None
    assert dt.parse_algo_rows(None) is None
    assert dt.parse_algo_rows({"data": {"algoId": "a"}}) is None


def test_parse_cancel_result_semantics():
    # 頂層 code=="0" ＝ OKX 語意的「全部成功」
    assert dt.parse_cancel_result({"code": "0"}, 3) == 3
    # code=="1" ＝ 有失敗，逐筆看 sCode
    assert dt.parse_cancel_result(
        {"code": "1", "data": [{"sCode": "0"}, {"sCode": "51400"}]}, 2) == 1
    assert dt.parse_cancel_result(
        {"code": "1", "data": [{"sCode": "51400"}]}, 1) == 0
    # 認不得的形狀一律未知，⛔ 不准當成功
    assert dt.parse_cancel_result(None, 2) is None
    assert dt.parse_cancel_result({"code": "1"}, 2) is None
    assert dt.parse_cancel_result({"code": "1", "data": []}, 2) is None
    assert dt.parse_cancel_result({"code": "1", "data": [{"algoId": "a"}]}, 1) is None
    assert dt.parse_cancel_result("ok", 1) is None


# ─────────────── 驅動 _monitor 的平倉清理分支 ───────────────

def _run_monitor_close_path(monkeypatch, fake_ex):
    """讓 _monitor 走到「倉已平 → 清殘留算單」那一格，回傳該輪 summary。

    只替換 journal 與 OKX 真相這兩層；**清理那一步用真碼**，否則就量不到本檔要量的東西。
    """
    from l3_dispatcher import demo_journal as dj

    trade = {"intent_id": "i-1", "symbol": "OP", "pos_side": "long",
             "status": "open", "entry_at": 1, "filled_at": 2}

    async def fake_positions(ex):
        return []                     # OKX 上已無此倉 ⇒ 走平倉結算路徑

    async def fake_closed_pnl(ex, symbol, pos_side, since_ms=None):
        return {"found": True, "pnl_usd": 1.0}

    monkeypatch.setattr(dt, "fetch_okx_positions", fake_positions)
    monkeypatch.setattr(dt, "fetch_okx_closed_pnl", fake_closed_pnl)
    monkeypatch.setattr(dj, "get_live_demo_trades", lambda: [trade])
    monkeypatch.setattr(dj, "get_state", lambda k, d="": "")
    monkeypatch.setattr(dj, "set_state", lambda k, v: None)
    monkeypatch.setattr(dj, "apply_demo_close", lambda *a, **k: None)
    monkeypatch.setattr(dj, "touch_synced", lambda iid: None)
    return asyncio.run(do._monitor(fake_ex, now_ms=1_000_000))
