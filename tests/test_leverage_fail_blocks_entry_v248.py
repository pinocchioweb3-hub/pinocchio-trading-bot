# -*- coding: utf-8 -*-
"""v248（監督員 r139）：設槓桿失敗 ⇒ 整筆不下。零網路、零 OKX 呼叫。

治的洞（真錢實證，不是推測）：2026-08-03 17:12，SOXL-USDT-SWAP short 這筆真錢單
算出的槓桿剛好等於執行器上限，設槓桿被 OKX 以 59102「Leverage exceeds the maximum
limit」擋下（該合約上限低於執行器上限），ensure_leverage 回 False 並記了 leverage_fail
——但 v155（r45）的「風險帶」只擋 lev < 上限 的情形，於是**三腿真錢單照樣送出去**，
倉開在交易所預設的 3x 上。事後的讀回閘（v171）按設計不擋單，只能在錢已經下去之後
喊 mismatch ⇒ 那一刻沒有任何東西攔得住。

v155 的推理只想過「交易所側比意圖**高**」（清算距離縮進止損內）一種偏離，漏了對稱
的另一半「交易所側比意圖**低**」。本檔把兩半都鎖起來：**只要設槓桿沒成功，就不下單**，
與算出來的 lev 落在哪一段無關。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "atk_consumer"))

import consume_intents as ci  # noqa: E402


def _intent() -> dict:
    """一筆語法完整的進場意圖（欄位齊全，好讓「沒擋住」時真的會走到下單那一步）。"""
    return {
        "inst_id": "SOXL-USDT-SWAP",
        "pos_side": "short",
        "side": "sell",
        "entry": 100.0,
        "stop": 101.0,          # 止損 1% ⇒ leverage_for_trade 算出的值會頂到上限
        "tp1": 98.0,
        "cl_ord_id": "atktest0000000000",
    }


@pytest.fixture
def no_okx(monkeypatch):
    """任何 OKX 呼叫都是「真錢單送出去了」的代理——測試裡一律視為失敗。"""
    calls = []

    def _boom(args, *a, **kw):
        calls.append(args)
        raise AssertionError(f"⛔ 設槓桿失敗後仍呼叫了 OKX：{args}")

    monkeypatch.setattr(ci, "_okx", _boom)
    return calls


def test_leverage_fail_at_cap_blocks_entry_the_soxl_20260803_case(monkeypatch, no_okx):
    """算出的 lev 頂到上限（＝v155 判定為「白擋」而放行的那一段）也必須擋。"""
    monkeypatch.setattr(ci, "ensure_leverage", lambda *a, **kw: False)
    it = _intent()
    assert ci.leverage_for_trade(it["entry"], it["stop"]) == ci.LEVERAGE, \
        "前提壞了：這筆的槓桿應該剛好頂到上限，否則測不到 v155 放行的那一段"
    assert ci.place(it, sz=5.14, dry=False, spec=None) is False
    assert no_okx == []


def test_leverage_fail_below_cap_still_blocks_entry(monkeypatch, no_okx):
    """v155 原本就擋的那一段（lev < 上限）不可因本次修改而鬆掉。"""
    monkeypatch.setattr(ci, "ensure_leverage", lambda *a, **kw: False)
    it = _intent()
    it["stop"] = 120.0          # 止損 20% ⇒ 算出的槓桿遠低於上限
    assert ci.leverage_for_trade(it["entry"], it["stop"]) < ci.LEVERAGE
    assert ci.place(it, sz=5.14, dry=False, spec=None) is False
    assert no_okx == []


def test_leverage_ok_does_not_block(monkeypatch):
    """反向側：設槓桿成功時不可被本閘擋掉——否則「修好」會是靠全面停擺換來的。"""
    monkeypatch.setattr(ci, "ensure_leverage", lambda *a, **kw: True)
    seen = []

    def _fake_okx(args, *a, **kw):
        seen.append(args)
        if args[:2] == ["swap", "get"]:
            return 1, "51603 order does not exist"
        return 0, '{"sCode": "0"}'

    monkeypatch.setattr(ci, "_okx", _fake_okx)
    assert ci.place(_intent(), sz=5.14, dry=False, spec=None) is True
    assert any(a[:2] == ["swap", "place"] for a in seen), "沒走到真正的下單呼叫＝這個反向側是虛設的"
