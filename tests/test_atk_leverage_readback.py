# -*- coding: utf-8 -*-
"""v171（監督員 r69）：交易所側槓桿讀回。零網路、零 OKX 呼叫。

治的洞：ensure_leverage 只看「設定呼叫的 exit code」就回 True，從來沒有讀回過
交易所實際的槓桿值——而 v99 那個 bug 的形狀正是「設定呼叫成功回應、交易所卻
靜默沿用預設 3x」。⇒ 「這倉的槓桿是對的」這件事一直只以代理值存在。
同時 r65 的首筆真錢驗收清單第一項就是「交易所側讀回的槓桿值」，而目前任何檔案、
任何 log 行都產不出這個數字：倉一平掉，證據就永遠沒了。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "atk_consumer"))

import consume_intents as ci  # noqa: E402


# ── 純函式：三態判定（比照 v162-v166 的紀律，未知永遠不可壓成「確認」）─────
def test_verdict_match_when_exchange_echoes_intended():
    assert ci.leverage_verdict(10, "10") == "match"
    assert ci.leverage_verdict(10, 10.0) == "match"


def test_verdict_mismatch_is_the_v99_shape_silent_default_3x():
    # v99：hedge 模式沒帶 posSide → OKX 回 200 但實際留在預設 3x
    assert ci.leverage_verdict(10, "3") == "mismatch"


def test_verdict_unknown_when_exchange_value_unusable():
    for raw in (None, "", "  ", "abc", "0", 0):
        assert ci.leverage_verdict(10, raw) == "unknown", raw


def test_verdict_unknown_when_intended_missing_so_legacy_records_never_false_alarm():
    # v171 之前開的倉沒有 lev 欄位 ⇒ 只能說「不知道」，⛔ 不可報 mismatch
    assert ci.leverage_verdict(None, "3") == "unknown"
    assert ci.leverage_verdict(0, "3") == "unknown"


def test_verdict_never_raises_on_garbage():
    for a, b in (({}, []), ([], {}), (object(), object()), ("x", "y")):
        assert ci.leverage_verdict(a, b) in ("match", "mismatch", "unknown")


# ── 迴圈級：真的跑 manage_positions ──────────────────────────────────────
def _arm(tmp_path, monkeypatch, rec_extra, lever, pos_sz="1"):
    """在場倉（交易所側仍有倉）跑一輪對帳。回 (部位檔內容, 本輪故障帳)。"""
    rec = {"inst_id": "BTC-USDT-SWAP", "pos_side": "long", "symbol": "BTC",
           "contracts": 1.0, "placed_at": time.time() - 600}
    rec.update(rec_extra)
    (tmp_path / "pos.json").write_text(
        json.dumps({"open": {"i-1": rec}, "day_pnl": {}}), encoding="utf-8")
    monkeypatch.setattr(ci, "POS_STATE", tmp_path / "pos.json")
    pos = {"instId": "BTC-USDT-SWAP", "posSide": "long", "pos": pos_sz}
    if lever is not None:
        pos["lever"] = lever

    def fake_okx(args, timeout=30):
        if args[:2] == ["account", "positions"]:
            return 0, json.dumps([pos])
        return 1, "unexpected"
    monkeypatch.setattr(ci, "_okx", fake_okx)
    ci._ROUND_FAILS.clear()
    ci.manage_positions(dry=False)
    saved = json.loads((tmp_path / "pos.json").read_text(encoding="utf-8"))
    return saved, dict(ci._ROUND_FAILS)


def test_exchange_leverage_is_recorded_into_the_position_ledger(tmp_path, monkeypatch):
    saved, fails = _arm(tmp_path, monkeypatch, {"lev": 10}, "10")
    rec = saved["open"]["i-1"]
    assert rec["lev_exchange"] == 10.0          # ← 首筆真錢驗收清單第一項的證據
    assert rec["lev_verdict"] == "match"
    assert rec.get("lev_checked_ts")
    assert "lev_mismatch" not in fails


def test_confirmed_mismatch_is_loud_and_accounted(tmp_path, monkeypatch):
    saved, fails = _arm(tmp_path, monkeypatch, {"lev": 10}, "3")
    assert saved["open"]["i-1"]["lev_verdict"] == "mismatch"
    assert "lev_mismatch" in fails              # 進健康帳＝既有連續輪告警機制接手
    assert "10" in fails["lev_mismatch"] and "3" in fails["lev_mismatch"]


def test_legacy_record_without_intended_lev_never_raises_a_false_alarm(tmp_path, monkeypatch):
    saved, fails = _arm(tmp_path, monkeypatch, {}, "3")
    assert saved["open"]["i-1"]["lev_verdict"] == "unknown"
    assert "lev_mismatch" not in fails


def test_missing_lever_field_is_unknown_not_match(tmp_path, monkeypatch):
    saved, fails = _arm(tmp_path, monkeypatch, {"lev": 10}, None)
    assert saved["open"]["i-1"]["lev_verdict"] == "unknown"
    assert "lev_mismatch" not in fails


def test_mismatch_never_closes_or_drops_the_position(tmp_path, monkeypatch):
    """⛔ 只記錄與告警，不自動平倉、不移出帳本（比照 orphan_position 的處置）。"""
    saved, _ = _arm(tmp_path, monkeypatch, {"lev": 10}, "3")
    assert "i-1" in saved["open"]


def test_closed_position_path_is_untouched_by_the_readback(tmp_path, monkeypatch):
    """在場才讀回；已了結的倉照走原本的損益記帳路徑（不可被新程式碼擋掉）。"""
    (tmp_path / "pos.json").write_text(json.dumps({
        "open": {"i-1": {"inst_id": "BTC-USDT-SWAP", "pos_side": "long",
                         "symbol": "BTC", "contracts": 1.0, "lev": 10,
                         "placed_at": time.time() - 600}},
        "day_pnl": {}}), encoding="utf-8")
    monkeypatch.setattr(ci, "POS_STATE", tmp_path / "pos.json")

    def fake_okx(args, timeout=30):
        if args[:2] == ["account", "positions"]:
            return 0, "[]"                       # 交易所側已無倉＝已了結
        if args[:2] == ["swap", "fills"]:
            return 0, json.dumps([{"ts": str(int(time.time() * 1000)),
                                   "fillPnl": "-5.0", "fee": "-0.5"}])
        return 1, "unexpected"
    monkeypatch.setattr(ci, "_okx", fake_okx)
    ci._ROUND_FAILS.clear()
    ci.manage_positions(dry=False)
    saved = json.loads((tmp_path / "pos.json").read_text(encoding="utf-8"))
    assert saved["open"] == {}                   # 已了結 → 移出
    assert saved["day_pnl"]                      # 損益有記到


# ── 下單時要把「打算用的槓桿」留下來，否則讀回沒有比較基準 ────────────────
def test_placement_records_intended_leverage(tmp_path, monkeypatch):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    intent = {"intent_id": "i-new", "symbol": "BTC", "inst_id": "BTC-USDT-SWAP",
              "pos_side": "long", "entry": 100.0, "stop": 95.0,
              "execution_policy": "demo_only",
              "expires_at": (time.time() + 3600) * 1000}
    (outbox / "a.json").write_text(json.dumps(intent), encoding="utf-8")
    (tmp_path / "pos.json").write_text(json.dumps({"open": {}, "day_pnl": {}}),
                                       encoding="utf-8")
    monkeypatch.setattr(ci, "OUTBOX", outbox)
    monkeypatch.setattr(ci, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(ci, "POS_STATE", tmp_path / "pos.json")
    monkeypatch.setattr(ci, "verify_demo_profile", lambda: True)
    monkeypatch.setattr(ci, "manage_positions", lambda dry: [])
    monkeypatch.setattr(ci, "finish_round", lambda *a, **k: {})
    monkeypatch.setattr(ci, "contracts_for", lambda *a, **k: 1.0)
    monkeypatch.setattr(ci, "place", lambda intent, sz, dry, spec=None: True)
    monkeypatch.setattr(sys, "argv", ["consume_intents.py", "--once"])
    assert ci.main() == 0
    saved = json.loads((tmp_path / "pos.json").read_text(encoding="utf-8"))
    assert saved["open"]["i-new"]["lev"] == ci.leverage_for_trade(100.0, 95.0)


# ── 回歸鎖 ──────────────────────────────────────────────────────────────
def test_class_priority_head_is_unchanged_by_the_new_class():
    """⛔ r68 明令 pnl_unaccounted 不得被擠出原位；新類別只能插在其後。"""
    assert ci._CLASS_PRIORITY[0] == "orphan_position"
    assert ci._CLASS_PRIORITY[1] == "pnl_unaccounted"
    assert "lev_mismatch" in ci._CLASS_PRIORITY


def test_new_class_has_an_actionable_hint():
    hint = ci._CLASS_HINT.get("lev_mismatch") or ""
    assert len(hint) > 30
    assert "%" not in hint and "勝率" not in hint       # 紅線③：不得夾績效宣稱
