# -*- coding: utf-8 -*-
"""v179 WLFI 追蹤純函式測試。零網路。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from l3_dispatcher import wlfi_watch as w  # noqa: E402

_T = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_BINANCE = "0xf977814e90da44bfa03b6295a0616a897441acec"


def _mklog(frm: str, to: str, amount_wei: int) -> dict:
    return {"topics": [_T,
                       "0x" + "0" * 24 + frm[2:],
                       "0x" + "0" * 24 + to[2:]],
            "data": hex(amount_wei), "transactionHash": "0xabc"}


def test_decode_transfer_roundtrip():
    log = _mklog("0x" + "1" * 40, _BINANCE, 5_000_000 * 10**18)
    tr = w.decode_transfer(log)
    assert tr is not None
    assert tr["amount"] == 5_000_000
    assert tr["to"] == _BINANCE


def test_decode_rejects_garbage():
    assert w.decode_transfer({}) is None
    assert w.decode_transfer({"topics": ["0xdead"], "data": "0x0"}) is None


def test_classify_flow_directions():
    to_ex = {"from": "0x" + "1" * 40, "to": _BINANCE}
    from_ex = {"from": _BINANCE, "to": "0x" + "1" * 40}
    wallet = {"from": "0x" + "1" * 40, "to": "0x" + "2" * 40}
    assert "賣壓" in w.classify_flow(to_ex)
    assert "囤積" in w.classify_flow(from_ex)
    assert "移轉" in w.classify_flow(wallet)


def test_label_known_and_unknown():
    assert w.label_of(_BINANCE) == "幣安冷錢包"
    lab = w.label_of("0x" + "a" * 40)
    assert "…" in lab and len(lab) <= 16


def test_whale_card_display_only_disclaimer():
    tr = {"from": _BINANCE, "to": "0x" + "1" * 40, "amount": 9_000_000,
          "tx": "0xabc"}
    card = w.render_whale_card(tr, 0.055)
    assert "非訊號" in card
    assert "$495K" in card or "0.50M" in card or "0.49M" in card
