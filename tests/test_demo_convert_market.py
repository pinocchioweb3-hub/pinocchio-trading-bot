# -*- coding: utf-8 -*-
"""task#14：限價逾時轉市價（D 政策）——理性閘 + 帳本更新 + 市價下單。純模擬盤路徑。"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from l4_execution import demo_trader as dt
from l4_execution import demo_guard


def test_convert_gate_bull():
    assert dt.convert_gate("bull", 100.0, 95.0, 110.0, 102.0) == (True, "ok")
    assert dt.convert_gate("bull", 100.0, 95.0, 110.0, 111.0)[1] == "past_tp1"
    assert dt.convert_gate("bull", 100.0, 95.0, 110.0, 94.0)[1] == "price_below_stop"
    # 價跑太遠：新 sl 距離(55) > 原(5)×1.8 → 放棄追
    assert dt.convert_gate("bull", 100.0, 95.0, 200.0, 150.0)[1] == "sl_distance_blown"
    assert dt.convert_gate("bull", 100.0, 95.0, 110.0, 0.0)[1] == "no_price"


def test_convert_gate_bear():
    assert dt.convert_gate("bear", 100.0, 105.0, 90.0, 98.0) == (True, "ok")
    assert dt.convert_gate("bear", 100.0, 105.0, 90.0, 89.0)[1] == "past_tp1"
    assert dt.convert_gate("bear", 100.0, 105.0, 90.0, 106.0)[1] == "price_above_stop"


def test_convert_to_market_updates_row(monkeypatch, tmp_path):
    from l3_dispatcher import demo_journal as dj
    monkeypatch.setattr(dj, "DB_PATH", str(tmp_path / "tj.db"))
    dj.init_db()
    conn = dj._conn()
    conn.execute(
        "INSERT INTO demo_trades(intent_id,symbol,direction,entry_price,stop_price,"
        "risk_usd,status,entry_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("p1", "BTC", "bull", 100.0, 95.0, 125.0, "pending", 1000, 1000))
    conn.close()
    ok = dj.convert_to_market("p1", entry_price=102.0, stop_price=95.0, tp1=110.0,
                              tp2=120.0, tp3=140.0, leverage=20, notional_usd=5000.0,
                              margin_usd=250.0, contracts=7.0, risk_usd=124.0,
                              entry_order_id="oid9", entry_at_ms=2000, note="converted")
    assert ok
    c = dj._conn()
    row = c.execute("SELECT entry_price,contracts,entry_order_id,entry_at,status,leverage "
                    "FROM demo_trades WHERE intent_id='p1'").fetchone()
    c.close()
    assert row[0] == 102.0 and row[1] == 7.0 and row[2] == "oid9"
    assert row[3] == 2000 and row[4] == "pending" and row[5] == 20
    # 冪等：已成交(open)不被轉換覆寫
    c = dj._conn()
    c.execute("UPDATE demo_trades SET status='open' WHERE intent_id='p1'")
    c.close()
    assert dj.convert_to_market("p1", entry_price=999.0, stop_price=95.0, tp1=1, tp2=2,
                                tp3=3, leverage=1, notional_usd=1, margin_usd=1,
                                contracts=1, risk_usd=1, entry_order_id="x",
                                entry_at_ms=3000, note="n") is False


def test_place_demo_market_entry_uses_market_type(monkeypatch):
    calls = {}

    class FakeEx:
        headers = {"x-simulated-trading": "1"}

        async def set_leverage(self, *a, **k):
            return {}

        async def create_order(self, symbol, type, side, amount, params):
            calls.update(type=type, side=side, amount=amount, symbol=symbol)
            return {"id": "mkt1"}

    async def fake_confirm(ex):
        return True

    monkeypatch.setattr(demo_guard, "confirm_okx_demo", fake_confirm)
    monkeypatch.setattr(dt, "kill_switch_active", lambda: False)
    plan = dt.build_order_plan("BTC", "bull", 100.0, 95.0, risk_usd=125.0,
                               ct_val=0.01, lot_sz=1.0, min_sz=1.0, seq=1)
    assert plan.ok
    res = asyncio.run(dt.place_demo_market_entry(FakeEx(), plan))
    assert res.get("ok") is True
    assert calls["type"] == "market" and calls["side"] == "buy"  # 市價、多單買
