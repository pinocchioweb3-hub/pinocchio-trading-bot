# -*- coding: utf-8 -*-
"""ATK 消費腳本純函式測試（v139 倉位管理迴圈）。零網路、零 OKX 呼叫。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "atk_consumer"))

import consume_intents as ci  # noqa: E402


def test_timed_out_boundary():
    now = time.time()
    assert not ci.timed_out(now - 23.9 * 3600, now, limit_h=24.0)
    assert ci.timed_out(now - 24.1 * 3600, now, limit_h=24.0)


def test_breaker_trips_on_daily_loss():
    now = time.time()
    dk = ci._day_key(now)
    assert ci.breaker_tripped({dk: -300.0}, now, stop_usd=300.0)
    assert ci.breaker_tripped({dk: -450.5}, now, stop_usd=300.0)


def test_breaker_holds_on_small_loss_or_profit():
    now = time.time()
    dk = ci._day_key(now)
    assert not ci.breaker_tripped({dk: -299.9}, now, stop_usd=300.0)
    assert not ci.breaker_tripped({dk: +500.0}, now, stop_usd=300.0)
    assert not ci.breaker_tripped({}, now, stop_usd=300.0)


def test_breaker_ignores_yesterday():
    now = time.time()
    yesterday = ci._day_key(now - 86400)
    assert not ci.breaker_tripped({yesterday: -9999.0}, now, stop_usd=300.0)


def test_profile_hardcoded_demo():
    # 紅線①防退化：原檔的 PROFILE 永遠是 demo，真盤=使用者自建副本
    assert ci.PROFILE == "demo"
