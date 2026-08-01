# -*- coding: utf-8 -*-
"""v207（監督員 r102）：已確認手動倉檔「讀不出來」不再折成「使用者本來就沒確認過」。

同物種第 27 次。這次的落點特別要緊：atk_acknowledged_positions.json 正是**目前唯一**
那條真錢阻塞（WLFI 孤兒倉）的解除機制——使用者在聊天室親口確認、CEO 寫這個檔，
孤兒告警才會停。舊碼把三件事壓成同一個空集合：
  ①檔案不存在（正常：還沒確認過）
  ②檔案在、但解不開／頂層不是清單（使用者確認了，卻被無聲丟掉）
  ③檔案在、是清單、但每一筆的鍵名不對（連例外都不丟，最無聲的一種）
安全預設（讀壞＝全部當孤兒）本身是對的、不動；缺的是**出聲**。

零網路、零真錢。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "atk_consumer"))

import consume_intents as ci  # noqa: E402


def _fresh(monkeypatch, tmp_path, name, text=None):
    p = tmp_path / name
    if text is not None:
        p.write_text(text, encoding="utf-8")
    monkeypatch.setattr(ci, "ACKED_POS", p)
    ci._ROUND_FAILS.clear()
    return p


# ── 這三個是本次真正新增的行為：檔在、卻沒讀出任何一筆＝必須出聲 ──────────

def test_wrong_shape_object_is_not_silent(tmp_path, monkeypatch):
    """頂層包成物件（最像人手寫的一種寫法）——json 解得開、不丟例外 ⇒ 舊碼完全無聲。"""
    _fresh(monkeypatch, tmp_path, "ack.json",
           '{"positions":[{"inst_id":"WLFI-USDT-SWAP","pos_side":"long"}]}')
    assert ci.load_acked_keys() == set()          # 安全預設不變（仍全部當孤兒）
    assert "acked_state_unreadable" in ci._ROUND_FAILS


def test_wrong_key_names_are_not_silent(tmp_path, monkeypatch):
    """清單是對的、鍵名寫錯（symbol/side）——同樣不丟例外，舊碼一樣無聲。"""
    _fresh(monkeypatch, tmp_path, "ack.json",
           '[{"symbol":"WLFI-USDT-SWAP","side":"long"}]')
    assert ci.load_acked_keys() == set()
    assert "acked_state_unreadable" in ci._ROUND_FAILS


def test_corrupt_json_is_not_silent(tmp_path, monkeypatch):
    """半截 JSON：安全預設維持，但不可再無聲。"""
    _fresh(monkeypatch, tmp_path, "ack.json", "{broken")
    assert ci.load_acked_keys() == set()
    assert "acked_state_unreadable" in ci._ROUND_FAILS


def test_partial_entries_keep_good_and_still_speak(tmp_path, monkeypatch):
    """一好一壞：好的照收（別因為一筆壞掉就把使用者的確認全丟掉），壞的要出聲。"""
    _fresh(monkeypatch, tmp_path, "ack.json",
           '[{"inst_id":"WLFI-USDT-SWAP","pos_side":"long"},{"symbol":"X"}]')
    assert ci.load_acked_keys() == {("WLFI-USDT-SWAP", "long")}
    assert "acked_state_unreadable" in ci._ROUND_FAILS


# ── 反向：正常情形絕不可誤報（否則這個新故障類別會變成永久噪音）─────────

def test_missing_file_stays_silent(tmp_path, monkeypatch):
    """檔案不存在＝使用者還沒確認過任何倉，是常態，永遠不可報故障。"""
    _fresh(monkeypatch, tmp_path, "nope.json")
    assert ci.load_acked_keys() == set()
    assert ci._ROUND_FAILS == {}


def test_empty_list_stays_silent(tmp_path, monkeypatch):
    """空清單＝『確認過、目前沒有任何一筆』，也是合法狀態。"""
    _fresh(monkeypatch, tmp_path, "ack.json", "[]")
    assert ci.load_acked_keys() == set()
    assert ci._ROUND_FAILS == {}


def test_valid_entries_stay_silent(tmp_path, monkeypatch):
    _fresh(monkeypatch, tmp_path, "ack.json",
           '[{"inst_id":"WLFI-USDT-SWAP","pos_side":"long","note":"user manual"}]')
    assert ci.load_acked_keys() == {("WLFI-USDT-SWAP", "long")}
    assert ci._ROUND_FAILS == {}


# ── 代表類別排序：這個新類別永遠不可蓋掉真正的擋點 ────────────────────

def test_never_outranks_orphan_position():
    """同輪若同時有孤兒倉與壞掉的 ack 檔，代表類別必須仍是孤兒倉——
    使用者看到的處置建議不可從「去 OKX 看那個真錢倉」被換成「去修一個檔」。"""
    assert ci.worst_class({"orphan_position": "a",
                           "acked_state_unreadable": "b"}) == "orphan_position"


def test_never_outranks_connectivity_classes():
    """與 r86（intent_unreadable）同理：純本地失敗不可把斷流主因擠掉。"""
    for net in ("auth_ip_whitelist", "auth", "rate_limit", "timeout", "query_fail"):
        assert ci.worst_class({net: "a", "acked_state_unreadable": "b"}) == net


def test_class_has_actionable_hint():
    """新故障類別必須有處置建議，否則使用者只會看到一個代號。"""
    assert ci._CLASS_HINT.get("acked_state_unreadable")
