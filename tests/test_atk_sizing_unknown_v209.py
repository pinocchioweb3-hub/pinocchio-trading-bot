"""v209（監督員 r104）：張數換算失敗的「未知」不再被折成「這筆單本來就不該下」。

舊碼（consume_intents.py:1542-1546）：

    sz = contracts_for(...)
    if sz is None:
        print("❌ 張數換算失敗——跳過（不猜）")
        done.add(iid)          # ← 永久丟棄，下輪不再重試
        continue

而 contracts_for 回 None 的來源有兩種完全不同的東西：

  ①「這輪讀不出合約規格」——okx CLI 沒回 0（401／限流／逾時），或回了 0 但輸出
     解不開／形狀認不得。這是**未知**，下一輪可能就好了。
  ②「這筆單本身不成立」——止損距離為零、算出來小於最小張數、名義值夾層後仍小於
     最小張數，或交易所確認沒有這個 instId。再讀一百次都是同一個答案。

舊碼把兩者一律 `done.add(iid)`＝**永久**丟棄，而且緊接在下一行的註解寫的正好相反
（「只在成功時記已處理：失敗留給下輪重試」）。後果：一筆使用者管線打算下的真錢訊號
在第一次讀取失敗時就消失，連 expires_at 的重試窗都用不到，也算不進「過期丟棄」的
統計（那個計數只數活到過期的 intent）。

反向側同樣要守住：②那些情況若改成每輪重試，會變成每分鐘一次的慢性假警報
（v208 對 `{"data": []}` 立下的同一條邊界線）。
"""
import json
import sys
import time

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from tools.atk_consumer import consume_intents as ci  # noqa: E402


def _arm(tmp_path, monkeypatch, instruments_reply, ct_val=1.0, lot=1.0, min_sz=1.0):
    """跑一輪 --once（零網路、零下單）。instruments_reply=(exit_code, stdout)。

    回 (placed, done, fails)：done 為落地的已處理清單，fails 為本輪故障類別。
    ⛔ 這裡**不**替換 contracts_for——本測要驗的正是它與呼叫端的接縫。
    """
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    intent = {"intent_id": "i-new", "symbol": "BTC", "inst_id": "BTC-USDT-SWAP",
              "pos_side": "long", "entry": 100.0, "stop": 95.0,
              "execution_policy": "demo_only",
              "expires_at": (time.time() + 3600) * 1000}
    (outbox / "a.json").write_text(json.dumps(intent), encoding="utf-8")
    (tmp_path / "pos.json").write_text(json.dumps({"open": {}, "day_pnl": {}}),
                                       encoding="utf-8")
    placed: list = []
    monkeypatch.setattr(ci, "OUTBOX", outbox)
    monkeypatch.setattr(ci, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(ci, "POS_STATE", tmp_path / "pos.json")
    monkeypatch.setattr(ci, "verify_demo_profile", lambda: True)
    monkeypatch.setattr(ci, "manage_positions", lambda dry: [])
    monkeypatch.setattr(ci, "finish_round", lambda *a, **k: {})
    monkeypatch.setattr(ci, "place",
                        lambda intent, sz, dry, spec=None:
                        (placed.append((intent["intent_id"], sz)), True)[1])

    def fake_okx(args, timeout=30):
        if args[:2] == ["market", "instruments"]:
            return instruments_reply
        return 0, "[]"
    monkeypatch.setattr(ci, "_okx", fake_okx)
    ci._ROUND_FAILS.clear()
    monkeypatch.setattr(sys, "argv", ["consume_intents.py", "--once"])
    assert ci.main() == 0
    try:
        done = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["done"]
    except Exception:  # noqa: BLE001
        done = None
    return placed, done, dict(ci._ROUND_FAILS)


_GOOD_SPEC = json.dumps([{"ctVal": "1.0", "lotSz": "1", "minSz": "1"}])


# ── 正向側：未知不得被永久丟棄（舊碼在此全紅）─────────────────────────────
def test_unreadable_spec_shape_does_not_permanently_drop_intent(tmp_path, monkeypatch):
    """CLI 回 0 但形狀認不得（錯誤信封／換版換了包裝鍵）＝這輪讀不出來。"""
    reply = (0, json.dumps({"code": "50011", "msg": "Too Many Requests"}))
    placed, done, _fails = _arm(tmp_path, monkeypatch, reply)
    assert placed == []                                   # 讀不出規格當然不下單
    assert "i-new" not in (done or []), (
        "規格這輪讀不出來，卻把 intent 記成已處理＝永久丟棄一筆真錢訊號")


def test_unreadable_spec_shape_is_recorded_as_a_fault(tmp_path, monkeypatch):
    """exit code 是 0 ⇒ _okx 那層一個字都不會記；不在這裡出聲就是全程無聲。"""
    reply = (0, json.dumps({"code": "50011", "msg": "Too Many Requests"}))
    _placed, _done, fails = _arm(tmp_path, monkeypatch, reply)
    assert "instrument_spec_unreadable" in fails, (
        "回應解得開卻認不得，健康帳上完全沒有痕跡＝無聲失敗")


def test_broken_json_spec_does_not_permanently_drop_intent(tmp_path, monkeypatch):
    placed, done, fails = _arm(tmp_path, monkeypatch, (0, "{半截"))
    assert placed == []
    assert "i-new" not in (done or [])
    assert "instrument_spec_unreadable" in fails


def test_cli_failure_does_not_permanently_drop_intent(tmp_path, monkeypatch):
    """CLI 沒回 0（401／逾時／限流）——_okx 已記了連線類故障，但 intent 照樣被丟。"""
    reply = (1, "50110: request IP is not in the whitelist")
    placed, done, _fails = _arm(tmp_path, monkeypatch, reply)
    assert placed == []
    assert "i-new" not in (done or []), (
        "連線失敗是最典型的暫時性故障，丟掉 intent 等於補白名單也救不回這筆")


# ── 反向側：終局失敗仍須永久跳過（舊碼在此本來就綠，改後不得變紅）───────
def test_below_min_size_is_still_permanently_skipped(tmp_path, monkeypatch):
    """算出來不足最小張數＝這筆單本身不成立，重試一百次也一樣。"""
    spec = json.dumps([{"ctVal": "1000", "lotSz": "1", "minSz": "1"}])
    placed, done, fails = _arm(tmp_path, monkeypatch, (0, spec))
    assert placed == []
    assert "i-new" in (done or []), "終局失敗改成重試＝每分鐘一次的慢性假警報"
    assert "instrument_spec_unreadable" not in fails


def test_exchange_confirms_no_such_instrument_is_still_permanently_skipped(
        tmp_path, monkeypatch):
    """{"data": []} ＝交易所**確認**沒有這個 instId（v208 立下的邊界線）。"""
    placed, done, fails = _arm(tmp_path, monkeypatch, (0, json.dumps({"data": []})))
    assert placed == []
    assert "i-new" in (done or [])
    assert "instrument_spec_unreadable" not in fails


def test_normal_spec_still_sizes_and_places(tmp_path, monkeypatch):
    """對照組：規格讀得到就照常下單（別把正常路徑改壞）。"""
    placed, done, fails = _arm(tmp_path, monkeypatch, (0, _GOOD_SPEC))
    assert placed == [("i-new", 20.0)]          # 100U 風險 ÷ 5 距離 ÷ ctVal 1
    assert "i-new" in (done or [])
    assert fails == {}


def test_bare_list_spec_still_accepted(tmp_path, monkeypatch):
    """CLI v1.4.2 實測頂層是裸清單——這個形狀是認得的，不可被打成未知。"""
    placed, _done, fails = _arm(tmp_path, monkeypatch, (0, _GOOD_SPEC))
    assert placed and "instrument_spec_unreadable" not in fails


# ── 純函式層 ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("reason", ["bad_distance", "below_min_size",
                                    "below_min_after_cap", "spec_not_found"])
def test_terminal_reasons_are_not_retryable(reason):
    assert ci.sizing_retryable(reason) is False


@pytest.mark.parametrize("reason", ["spec_cli_failed", "spec_unreadable", None,
                                    "something_new_nobody_classified_yet"])
def test_unknown_reasons_are_retryable(reason):
    """⛔ 未分類（含 None）必須落在重試側：新增失敗來源時的安全預設是「別丟掉」。"""
    assert ci.sizing_retryable(reason) is True


def test_contracts_for_reports_reason_on_terminal_failure(monkeypatch):
    out: dict = {}
    monkeypatch.setattr(ci, "_okx",
                        lambda args, timeout=30: (0, _GOOD_SPEC))
    assert ci.contracts_for("BTC-USDT-SWAP", 100.0, 100.0, {}, out=out) is None
    assert out["reason"] == "bad_distance"


def test_contracts_for_reports_reason_on_unreadable(monkeypatch):
    out: dict = {}
    monkeypatch.setattr(ci, "_okx", lambda args, timeout=30: (0, "{半截"))
    assert ci.contracts_for("BTC-USDT-SWAP", 100.0, 95.0, {}, out=out) is None
    assert out["reason"] == "spec_unreadable"


def test_new_class_is_ranked_after_connectivity_classes():
    """連線類故障若同輪也發生，那才是使用者該先動的主因，⛔ 不可被這一類擠掉。"""
    p = ci._CLASS_PRIORITY
    assert "instrument_spec_unreadable" in p
    for earlier in ("auth_ip_whitelist", "auth", "rate_limit", "timeout",
                    "query_fail", "orphan_position"):
        assert p.index(earlier) < p.index("instrument_spec_unreadable")
    assert "instrument_spec_unreadable" in ci._CLASS_HINT
