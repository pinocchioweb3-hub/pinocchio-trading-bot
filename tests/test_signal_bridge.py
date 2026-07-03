# -*- coding: utf-8 -*-
"""pap 訊號橋（v112）：紅線硬擋 + Alert 2.0 映射 + 去重掃描。"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from l4_execution.signal_bridge import (
    build_alert, map_action, pap_url_ok, post_alert,
)


def test_pap_url_guard_only_accepts_okx_pap():
    """紅線①：只放行 OKX 模擬盤(pap)端點；實盤 /algo/ 與任何其他網址一律擋。"""
    assert pap_url_ok("https://www.okx.com/pap/algo/signal/trigger")
    assert not pap_url_ok("https://www.okx.com/algo/signal/trigger")      # 實盤 → 擋
    assert not pap_url_ok("https://evil.example.com/pap/xxx")             # 假 host → 擋
    assert not pap_url_ok("http://www.okx.com/pap/algo/signal/trigger")   # 非 https → 擋
    assert not pap_url_ok("") and not pap_url_ok(None)


def test_post_alert_refuses_non_pap_without_network():
    """非 pap 網址在發出任何網路請求之前就被擋（回 blocked，零外呼）。"""
    ok, note = asyncio.run(post_alert("https://www.okx.com/algo/signal/trigger",
                                      {"action": "ENTER_LONG"}))
    assert ok is False and "non-pap" in note


def test_action_mapping_alert2():
    assert map_action("bull", "entry") == "ENTER_LONG"
    assert map_action("bull", "exit") == "EXIT_LONG"
    assert map_action("bear", "entry") == "ENTER_SHORT"
    assert map_action("bear", "exit") == "EXIT_SHORT"
    assert map_action("sideways", "entry") is None      # 未知不猜


def test_build_alert_shape_and_missing():
    a = build_alert("SOL", "bear", "entry", "tok123")
    assert a["action"] == "ENTER_SHORT" and a["instrument"] == "SOL-USDT-SWAP"
    assert a["signalToken"] == "tok123" and "timestamp" in a
    assert build_alert("", "bull", "entry", "t") is None       # 缺 symbol
    assert build_alert("BTC", "bull", "entry", "") is None     # 缺 token
