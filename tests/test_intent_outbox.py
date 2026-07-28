# -*- coding: utf-8 -*-
"""trade-intent v1.1 outbox（v133）：確定性ID/政策分流/缺料拒出/過期窗。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from l4_execution.intent_outbox import build_intent


def _row(**kw):
    base = dict(id=1, symbol="NVDA", setup="us_breakout", direction="bull",
                entry_price=190.0, stop_price=185.0, tp1=198.0, tp2=205.0,
                tp3=None, entry_at=1785000000000)
    base.update(kw)
    return base


def test_intent_deterministic_and_shape():
    a = build_intent(_row())
    b = build_intent(_row())
    assert a["intent_id"] == b["intent_id"]           # 同訊號永遠同 ID（冪等錨）
    assert a["inst_id"] == "NVDA-USDT-SWAP"
    assert a["side"] == "buy" and a["pos_side"] == "long"
    assert a["cl_ord_id"].startswith("atk") and len(a["cl_ord_id"]) <= 24
    assert a["tp3"] is None                            # 缺 tp3 誠實 None 不猜
    assert a["expires_at"] == a["created_at"] + int(6.0 * 3600_000)


def test_policy_split_us_vs_crypto():
    """美股（已過統計閘）=demo_only 可自動執行；加密=human_gated 只列印（紅線秩序）。"""
    us = build_intent(_row())
    cr = build_intent(_row(setup="deepdive", symbol="SOL"))
    assert us["execution_policy"] == "demo_only" and us["engine"] == "us"
    assert cr["execution_policy"] == "human_gated" and cr["engine"] == "crypto"
    assert cr["entry_type"] == "limit" and us["entry_type"] == "market"


def test_missing_critical_fields_refused():
    assert build_intent(_row(stop_price=None)) is None
    assert build_intent(_row(tp1=None)) is None
    assert build_intent(_row(direction="sideways")) is None
