"""#54 治本驗證：標的不在 OKX 永續宇宙時 _place_one 的行為（v56）。

兩條路徑：
  1) fetch_okx_contract_spec 丟 BadSymbol/BadRequest → 寫一筆 status='rejected'、
     exit_reason='reject:not_on_okx'，回 {placed:False, reason:'not_on_okx'}（治本：可歸因、不漏記、高水位可前進）。
  2) 暫時性錯誤（網路/超時，類名非 BadSymbol/BadRequest）→ 往上拋，**不**誤記 rejected（避免有效訊號被永久標廢）。
  3) BadSymbol 但該 intent 已存在 → 冪等：不重複寫。

執行：pytest tests/test_demo_operator_noinstr.py  或  python tests/test_demo_operator_noinstr.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from l3_dispatcher import demo_operator as dop
from l3_dispatcher import demo_journal as dj
from l4_execution import demo_trader as dt


# 類名須精確為 "BadSymbol"——生產碼以 type(e).__name__ 白名單精確比對（ccxt.BadSymbol）。
class BadSymbol(Exception):
    pass


class _NetworkError(Exception):
    pass


_SIGNAL = {
    "id": 777, "fire_id": "f777", "symbol": "FOOBAR", "direction": "long",
    "entry_price": 100.0, "stop_price": 95.0, "tp1": 110, "tp2": 120, "tp3": 130,
    "setup": "deepdive", "regime": "bull_pullback",
}


def _patch(monkeypatch_target_raises, *, intent_exists_returns=False):
    """暫存原函式、注入假件，回傳 (calls, restore)。"""
    orig_fetch = dt.fetch_okx_contract_spec
    orig_exists = dj.intent_exists
    orig_record = dj.record_demo_entry
    calls = {"record": [], "exists_q": []}

    async def fake_fetch(ex, symbol):
        raise monkeypatch_target_raises(f"unlisted:{symbol}")

    def fake_exists(intent_id):
        calls["exists_q"].append(intent_id)
        return intent_exists_returns

    def fake_record(**kw):
        calls["record"].append(kw)
        return 1

    dt.fetch_okx_contract_spec = fake_fetch
    dj.intent_exists = fake_exists
    dj.record_demo_entry = fake_record

    def restore():
        dt.fetch_okx_contract_spec = orig_fetch
        dj.intent_exists = orig_exists
        dj.record_demo_entry = orig_record

    return calls, restore


def test_badsymbol_records_rejected():
    calls, restore = _patch(BadSymbol)
    try:
        res = asyncio.run(dop._place_one(None, dict(_SIGNAL), avail_usd=1000.0))
    finally:
        restore()
    assert res == {"placed": False, "reason": "not_on_okx"}, res
    assert len(calls["record"]) == 1, "BadSymbol 應寫恰好一筆 rejected"
    rec = calls["record"][0]
    assert rec["status"] == "rejected"
    assert rec["exit_reason"] == "reject:not_on_okx"
    assert rec["intent_id"] == "noinstr:f777"
    assert rec["paper_id"] == 777 and rec["symbol"] == "FOOBAR"
    assert rec["leverage"] == 0 and rec["notional_usd"] == 0.0 and rec["contracts"] == 0.0


def test_transient_error_reraises_no_record():
    calls, restore = _patch(_NetworkError)
    raised = False
    try:
        try:
            asyncio.run(dop._place_one(None, dict(_SIGNAL), avail_usd=1000.0))
        except _NetworkError:
            raised = True
    finally:
        restore()
    assert raised, "暫時性錯誤應往上拋"
    assert calls["record"] == [], "暫時性錯誤不得誤寫 rejected"


def test_badsymbol_idempotent_when_already_recorded():
    calls, restore = _patch(BadSymbol, intent_exists_returns=True)
    try:
        res = asyncio.run(dop._place_one(None, dict(_SIGNAL), avail_usd=1000.0))
    finally:
        restore()
    assert res == {"placed": False, "reason": "not_on_okx"}, res
    assert calls["record"] == [], "intent 已存在時不得重複寫"
    assert calls["exists_q"] == ["noinstr:f777"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
