# -*- coding: utf-8 -*-
"""過期丟棄的 intent 必須是「可讀的數字」，不能只是一行 log（v169・監督員 r67）。

背景（2026-07-31 實測）：401 IP 白名單斷流 18 小時、連續 1071 輪 fail-closed。
期間有 4 筆 intent（SOXL / SNDK×2 / MU）在 `expires_at` 到期後被直接丟棄——
程式只做了 `print("⏭ … 已過期——跳過")` 然後 `done.add(iid)`。於是：

  * 健康檔沒有任何欄位知道「這場斷流吃掉了幾筆訊號」；
  * 監督帳本、Telegram 一個字都沒提；
  * 唯一的證據是 837KB log 裡 grep 得到的 4 行純文字。

這與 v164（讀失敗→當成沒有故障史）、v166（寫失敗→只寫進 log）、v167（用旗標
代理「活著」）是同一物種：**要用來下判斷的量，只以 log 文字存在，等於沒有存在。**
斷流的代價因此永遠問不出來，而「代價多少」正是使用者決定要不要動手修的依據。

⛔ 關鍵不變式（本檔的存在理由）：過期丟棄**恰好發生在空轉輪**——該輪沒有故障
（一次呼叫都沒發出）也沒有成功（oks==0），正是 update_health 會提早 return 的
那條路徑。記帳若放在提早 return 之後，斷流期的丟棄將一筆都記不到＝白修。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "atk_consumer"))

import consume_intents as ci  # noqa: E402


def _exp(iid="i-1", symbol="SOXL", side="long", ts=1000.0):
    return {"intent_id": iid, "symbol": symbol, "side": side, "expires_at": ts}


def test_expired_drop_is_counted_in_health_not_only_printed():
    """最低要求：丟掉一筆就要有一個數字長大。"""
    h = ci.update_health({}, {}, 1000.0, oks=1, expired=[_exp()])
    assert int(h.get("expired_dropped_total", 0)) == 1
    h2 = ci.update_health(h, {}, 1060.0, oks=1, expired=[_exp("i-2"), _exp("i-3")])
    assert int(h2.get("expired_dropped_total", 0)) == 3, "跨輪必須累加，不可每輪重數"


def test_expiry_during_an_outage_is_attributed_to_the_fault_on_an_idle_round():
    """⛔ 本檔最重要的一條：斷流期的丟棄發生在**空轉輪**（無故障、無成功呼叫）。

    update_health 對空轉輪會提早 return（v151 的假痊癒治本）。記帳必須在那個
    return **之前**，否則斷流吃掉的訊號一筆都記不到——而那正是唯一要記的情境。"""
    base = {"consecutive_fail_rounds": 1071, "last_fail_class": "auth_ip_whitelist"}
    h = ci.update_health(base, {}, 1000.0, oks=0, expired=[_exp("i-a", "SOXL")])
    assert int(h.get("idle_rounds", 0)) == 1, "前提：這確實是一個空轉輪"
    assert int(h.get("expired_dropped_total", 0)) == 1
    assert int(h.get("expired_dropped_during_fault", 0)) == 1, \
        "斷流期丟棄必須可歸因，否則問不出『這場故障吃掉幾筆』"
    recent = h.get("expired_dropped_recent") or []
    assert recent and recent[-1]["symbol"] == "SOXL"
    assert recent[-1].get("fault_class") == "auth_ip_whitelist", \
        "要記下當時是哪一類故障，否則事後分不清是哪一場斷流吃的"


def test_expiry_on_a_healthy_round_is_not_blamed_on_a_fault():
    """反向護欄：連續故障為 0 時的自然過期是正常老化，不可算進斷流代價，
    否則這個數字會被日常噪音灌水、失去『這場故障的代價』的意義。"""
    h = ci.update_health({"consecutive_fail_rounds": 0}, {}, 1000.0, oks=3,
                         expired=[_exp("i-b", "MU")])
    assert int(h.get("expired_dropped_total", 0)) == 1
    assert int(h.get("expired_dropped_during_fault", 0)) == 0
    assert not (h.get("expired_dropped_recent") or []), "健康輪不進斷流明細"


def test_expiry_in_the_same_round_as_a_fresh_fault_still_counts_as_during_fault():
    """故障第一輪（先前 streak=0、本輪有 fails）也算斷流期——
    不然每場故障的第一筆丟棄都會被漏記。"""
    h = ci.update_health({}, {"auth_ip_whitelist": "401"}, 1000.0, oks=0,
                         expired=[_exp("i-c", "SNDK")])
    assert int(h.get("expired_dropped_during_fault", 0)) == 1


def test_expired_recent_list_is_bounded_and_keeps_the_newest():
    """明細要有上限——健康檔每輪重寫，無界成長會把檔案養大到寫入失敗，
    那會反過來打死 v166 才修好的告警計數（同一個檔）。"""
    h = {"consecutive_fail_rounds": 5}
    for i in range(ci.EXPIRED_RECENT_MAX + 10):
        h = ci.update_health(h, {"auth_ip_whitelist": "401"}, 1000.0 + i, oks=0,
                             expired=[_exp(f"i-{i}", f"S{i}")])
    recent = h["expired_dropped_recent"]
    assert len(recent) == ci.EXPIRED_RECENT_MAX
    assert recent[-1]["symbol"] == f"S{ci.EXPIRED_RECENT_MAX + 9}", "留最新的"
    assert int(h["expired_dropped_during_fault"]) == ci.EXPIRED_RECENT_MAX + 10, \
        "明細有上限，但總數不可被上限截斷"


def test_expiry_counters_survive_recovery():
    """⛔ 恢復那一輪不可清空代價帳——事後要回答『那場斷流吃掉幾筆』，
    問的時間點正好是恢復之後。"""
    h = {"consecutive_fail_rounds": 1071, "last_alert_ts": 900.0,
         "last_fail_class": "auth_ip_whitelist",
         "expired_dropped_total": 4, "expired_dropped_during_fault": 4,
         "expired_dropped_recent": [_exp("i-old", "MU")]}
    out = ci.update_health(h, {}, 2000.0, oks=5)          # 真恢復輪
    assert out.get("recovered_from"), "前提：這確實是恢復輪"
    assert int(out["expired_dropped_total"]) == 4
    assert int(out["expired_dropped_during_fault"]) == 4
    assert len(out["expired_dropped_recent"]) == 1


def test_no_expiry_means_no_noise():
    """沒有丟棄就不該憑空生出欄位／數字（避免帳本每輪都顯示 0 筆代價的雜訊）。"""
    h = ci.update_health({}, {}, 1000.0, oks=1, expired=[])
    assert not h.get("expired_dropped_recent")
    assert int(h.get("expired_dropped_during_fault", 0)) == 0


# ── 迴圈級（純函式對了不代表主迴圈有把資料交上去） ──────────────────
def _arm_expired_loop(tmp_path, monkeypatch, expires_at_ms):
    """跑一輪 --once，intent 的到期時間由呼叫端決定。回 finish_round 收到的 kwargs。"""
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    intent = {"intent_id": "i-exp", "symbol": "SOXL", "inst_id": "SOXL-USDT-SWAP",
              "pos_side": "long", "entry": 100.0, "stop": 95.0,
              "execution_policy": "demo_only", "expires_at": expires_at_ms}
    (outbox / "a.json").write_text(json.dumps(intent), encoding="utf-8")
    (tmp_path / "pos.json").write_text(json.dumps({"open": {}, "day_pnl": {}}),
                                       encoding="utf-8")
    seen: dict = {}
    placed: list = []
    monkeypatch.setattr(ci, "OUTBOX", outbox)
    monkeypatch.setattr(ci, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(ci, "POS_STATE", tmp_path / "pos.json")
    monkeypatch.setattr(ci, "verify_demo_profile", lambda: True)
    monkeypatch.setattr(ci, "manage_positions", lambda dry: [])
    monkeypatch.setattr(ci, "contracts_for", lambda *a, **k: 1.0)
    monkeypatch.setattr(ci, "place",
                        lambda intent, sz, dry, spec=None:
                        (placed.append(intent["intent_id"]), True)[1])
    monkeypatch.setattr(ci, "finish_round",
                        lambda *a, **k: (seen.update(k), {})[1])
    monkeypatch.setattr(sys, "argv", ["consume_intents.py", "--once"])
    assert ci.main() == 0
    return seen, placed


def test_loop_hands_the_expired_drop_to_finish_round(tmp_path, monkeypatch):
    """迴圈級：過期分支必須把丟棄的 intent 交給收尾層，否則純函式再對也沒人餵它。"""
    seen, placed = _arm_expired_loop(tmp_path, monkeypatch, (time.time() - 60) * 1000)
    assert placed == [], "前提：過期的 intent 不可下單"
    dropped = seen.get("expired") or []
    assert len(dropped) == 1, "過期丟棄必須上報收尾層（目前只 print 就地消失）"
    assert dropped[0]["intent_id"] == "i-exp"
    assert dropped[0]["symbol"] == "SOXL"


def test_loop_reports_nothing_when_the_intent_is_still_valid(tmp_path, monkeypatch):
    """反向護欄：沒過期就不可被記成丟棄（否則代價數字全是假的）。"""
    seen, placed = _arm_expired_loop(tmp_path, monkeypatch, (time.time() + 3600) * 1000)
    assert placed == ["i-exp"], "前提：未過期的 intent 照常送出"
    assert not (seen.get("expired") or [])
