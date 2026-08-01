# -*- coding: utf-8 -*-
"""v189: 已確認手動倉機制純函式測試。零網路。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "atk_consumer"))

import consume_intents as ci  # noqa: E402


def test_partition_separates_acked_from_unacked():
    orphans = [("WLFI-USDT-SWAP", "long", 11618.0),
               ("ETH-USDT-SWAP", "short", 2.0)]
    acked = {("WLFI-USDT-SWAP", "long")}
    un, ack = ci.partition_orphans(orphans, acked)
    assert ack == [("WLFI-USDT-SWAP", "long", 11618.0)]
    assert un == [("ETH-USDT-SWAP", "short", 2.0)]


def test_partition_empty_ack_keeps_all_unacked():
    orphans = [("WLFI-USDT-SWAP", "long", 1.0)]
    un, ack = ci.partition_orphans(orphans, set())
    assert un == orphans and ack == []


def test_load_acked_missing_file_is_empty_set(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "ACKED_POS", tmp_path / "nope.json")
    assert ci.load_acked_keys() == set()


def test_load_acked_reads_entries(tmp_path, monkeypatch):
    p = tmp_path / "ack.json"
    p.write_text('[{"inst_id":"WLFI-USDT-SWAP","pos_side":"long","note":"user manual"}]',
                 encoding="utf-8")
    monkeypatch.setattr(ci, "ACKED_POS", p)
    assert ci.load_acked_keys() == {("WLFI-USDT-SWAP", "long")}


def test_load_acked_corrupt_file_fails_safe(tmp_path, monkeypatch):
    p = tmp_path / "bad.json"
    p.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(ci, "ACKED_POS", p)
    assert ci.load_acked_keys() == set()   # 讀壞=全部當孤兒（安全預設）
