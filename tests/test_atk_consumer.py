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


def test_breaker_daily_ignores_yesterday_but_weekly_catches_it():
    now = time.time()
    yesterday = ci._day_key(now - 86400)
    # 昨日大虧不觸發「日」熔斷，但 ≤−750 觸發「週」熔斷
    assert ci.breaker_tripped({yesterday: -9999.0}, now, stop_usd=300.0)
    # 週窗內小虧合計未達 −750 → 不觸發
    spread = {ci._day_key(now - d * 86400): -100.0 for d in range(7)}
    assert not ci.breaker_tripped(spread, now, stop_usd=300.0, week_stop_usd=750.0)
    # 週窗內合計 −770 → 觸發
    spread2 = {ci._day_key(now - d * 86400): -110.0 for d in range(7)}
    assert ci.breaker_tripped(spread2, now, stop_usd=300.0, week_stop_usd=750.0)
    # 8 天前的舊虧不入週窗
    old = {ci._day_key(now - 8 * 86400): -9999.0}
    assert not ci.breaker_tripped(old, now, stop_usd=300.0, week_stop_usd=750.0)


def test_split_tp_levels_three_legs_40_30_30():
    # sz=10, lot=0.01: 40/30/30 → 4.0 / 3.0 / 3.0（尾腿吃餘數）
    legs = ci.split_tp_levels(10.0, 0.01, 0.01, [100.0, 110.0, 120.0])
    assert [l for _, l in legs] == [4.0, 3.0, 3.0]
    assert [p for p, _ in legs] == [100.0, 110.0, 120.0]


def test_split_tp_levels_two_legs_50_50_and_remainder():
    legs = ci.split_tp_levels(0.03, 0.01, 0.01, [100.0, 110.0])
    assert [l for _, l in legs] == [0.01, 0.02]          # floor 後尾腿吃餘數
    assert sum(l for _, l in legs) == 0.03


def test_split_tp_levels_single_and_tiny():
    assert ci.split_tp_levels(5.0, 0.01, 0.01, [100.0]) == [(100.0, 5.0)]
    # 總量太小分不動 → 單腿 100%
    legs = ci.split_tp_levels(0.01, 0.01, 0.01, [100.0, 110.0, 120.0])
    assert sum(l for _, l in legs) == 0.01


def test_split_tp_levels_conservation():
    # 任意組合下腿張數合計恆等於 sz（不多平不漏平）
    for sz in (0.02, 0.05, 1.23, 15.4, 100.0):
        legs = ci.split_tp_levels(sz, 0.01, 0.01, [1.0, 2.0, 3.0])
        assert abs(sum(l for _, l in legs) - sz) < 1e-9, sz


def test_profile_hardcoded_demo():
    # 紅線①防退化：原檔的 PROFILE 永遠是 demo，真盤=使用者自建副本
    assert ci.PROFILE == "demo"


# ── v143：連續 fail-closed 告警（2026-07-30 401 靜默斷流的治本） ──────────
_FAKE_401 = ("Error: HTTP 401 from OKX: Your IP 203.0.113.7 is not included "
             "in your API key's 00000000-0000-4000-8000-000000000000 whitelist")


def test_classify_ip_whitelist_vs_plain_auth():
    # 401＋"not included in" → 要人去補白名單，與一般認證失敗必須分流
    assert ci.classify_failure(1, _FAKE_401) == "auth_ip_whitelist"
    assert ci.classify_failure(1, "HTTP 401: Invalid Sign") == "auth"


def test_classify_benign_order_not_found_is_not_a_failure():
    # 查無此單＝冪等查詢的正常答案；誤記成故障會讓告警天天誤鳴
    assert ci.classify_failure(1, '{"code":"51603","msg":"order does not exist"}') is None
    assert ci.classify_failure(1, "Order doesn't exist") is None


def test_classify_transport_classes():
    assert ci.classify_failure(127, "okx CLI 未安裝") == "cli_missing"
    assert ci.classify_failure(124, "okx CLI timeout") == "timeout"
    assert ci.classify_failure(1, '{"code":"50011","msg":"Too Many Requests"}') == "rate_limit"
    assert ci.classify_failure(1, "some weird breakage") == "other"


def test_redact_keeps_ip_but_masks_key_id():
    out = ci.redact_secrets(_FAKE_401)
    assert "00000000-0000-4000-8000-000000000000" not in out
    assert "203.0.113.7" in out          # IP 是使用者補白名單唯一有用的資訊，不遮


def test_alert_only_after_threshold_consecutive_rounds():
    now, h = 1_000_000.0, {}
    for i in range(ci.FAIL_ALERT_AFTER - 1):
        h = ci.update_health(h, {"auth_ip_whitelist": _FAKE_401}, now + i * 60)
        assert not ci.should_alert(h, now + i * 60), i    # 單輪抖動不吵
    h = ci.update_health(h, {"auth_ip_whitelist": _FAKE_401}, now + 300)
    assert ci.should_alert(h, now + 300)                  # 達門檻→告警


def test_single_bad_round_then_clean_never_alerts():
    now = 1_000_000.0
    h = ci.update_health({}, {"timeout": "x"}, now)
    h = ci.update_health(h, {}, now + 60, oks=1)
    assert h["consecutive_fail_rounds"] == 0
    assert not ci.should_alert(h, now + 60)
    assert not h.get("recovered_from")      # 沒告警過就不該發恢復通知


def test_cooldown_then_repeat_and_class_change_bypasses_cooldown():
    now = 1_000_000.0
    h = {}
    for i in range(ci.FAIL_ALERT_AFTER):
        h = ci.update_health(h, {"auth_ip_whitelist": _FAKE_401}, now + i * 60)
    h["last_alert_ts"], h["last_alert_class"] = now + 120, "auth_ip_whitelist"
    # 冷卻內同類不重複吵
    assert not ci.should_alert(h, now + 180)
    # 超過重複間隔→再提醒（故障還在，不可轉安靜）
    assert ci.should_alert(h, now + 120 + ci.FAIL_ALERT_REPEAT_SEC)
    # 換了故障類別→立刻再報，不被舊冷卻蓋掉
    h2 = ci.update_health(h, {"cli_missing": "gone"}, now + 200)
    assert ci.should_alert(h2, now + 200)


def test_recovery_notice_after_alerted_streak():
    now = 1_000_000.0
    h = {}
    for i in range(ci.FAIL_ALERT_AFTER):
        h = ci.update_health(h, {"auth": "bad key"}, now + i * 60)
    h["last_alert_ts"], h["last_alert_class"] = now + 120, "auth"
    h = ci.update_health(h, {}, now + 600, oks=1)
    assert h["recovered_from"]["fail_rounds"] == ci.FAIL_ALERT_AFTER
    assert not h.get("last_alert_ts")       # 冷卻重置：下次故障立刻能再報


def test_idle_round_never_counts_as_recovery():
    """v151 假痊癒治本：零呼叫的空轉輪不得歸零、不得送恢復通知。

    重現 2026-07-31：401 斷流中，佇列裡的 intent 剛好全部過期→那一輪一次呼叫
    都沒發生→fails 是空的→舊版判乾淨輪→送「✅已恢復」→下一輪立刻又 401。"""
    now = 1_000_000.0
    h = {}
    for i in range(ci.FAIL_ALERT_AFTER):
        h = ci.update_health(h, {"auth_ip_whitelist": _FAKE_401}, now + i * 60)
    h["last_alert_ts"], h["last_alert_class"] = now + 120, "auth_ip_whitelist"
    streak = h["consecutive_fail_rounds"]
    # 空轉輪（無故障、無成功呼叫）：判定完全維持原狀
    h = ci.update_health(h, {}, now + 600, oks=0)
    assert h["consecutive_fail_rounds"] == streak   # 不歸零
    assert not h.get("recovered_from")              # 不假報恢復
    assert h["last_alert_ts"] == now + 120          # 冷卻不被空轉輪洗掉
    assert h["idle_rounds"] == 1 and h["last_idle_ts"] == now + 600
    # 之後真的通了才算恢復
    h = ci.update_health(h, {}, now + 660, oks=1)
    assert h["consecutive_fail_rounds"] == 0
    assert h["recovered_from"]["fail_rounds"] == streak


def test_oks_defaults_to_zero_so_callers_must_prove_reachability():
    # fail-closed 方向：呼叫端沒證明「通了」就不算通，寧可晚報恢復不可假報
    h = ci.update_health({}, {"auth": "x"}, 1.0)
    assert ci.update_health(h, {}, 2.0)["consecutive_fail_rounds"] == 1


def test_benign_response_counts_as_reachable_not_idle(monkeypatch):
    # 「查無此單」是良性回應：不算故障，但確實通到 OKX＝要算成功呼叫
    class _R:
        returncode, stdout, stderr = 1, "51603 order does not exist", ""
    monkeypatch.setattr(ci, "_OKX_BIN", "okx")
    monkeypatch.setattr(ci.subprocess, "run", lambda *a, **k: _R())
    ci._ROUND_FAILS.clear()
    ci._ROUND_OKS["ok"] = 0
    ci._okx(["order", "get"])
    assert ci._ROUND_FAILS == {} and ci._ROUND_OKS["ok"] == 1


def test_worst_class_priority_and_counts_per_round():
    # 同輪多類取最嚴重者當代表（401 白名單優先於它造成的下游查單失敗）
    assert ci.worst_class({"query_fail", "auth_ip_whitelist"}) == "auth_ip_whitelist"
    h = ci.update_health({}, {"auth_ip_whitelist": "a", "query_fail": "b"}, 1.0)
    assert h["last_fail_class"] == "auth_ip_whitelist"
    assert h["class_counts"] == {"auth_ip_whitelist": 1, "query_fail": 1}


def test_alert_text_is_actionable_and_makes_no_perf_claim():
    now = 1_000_000.0
    h = {}
    for i in range(ci.FAIL_ALERT_AFTER):
        h = ci.update_health(h, {"auth_ip_whitelist": ci.redact_secrets(_FAKE_401)}, now + i * 60)
    txt = ci.alert_text(h, now + 300)
    assert "203.0.113.7" in txt and "白名單" in txt        # 看了就知道要做什麼
    assert "00000000-0000-4000-8000-000000000000" not in txt
    # 紅線③：告警只講連線狀態，不得夾帶勝率/報酬宣稱
    for banned in ("勝率", "報酬", "年化", "獲利"):
        assert banned not in txt


def test_health_layer_never_raises_into_trading_path(monkeypatch):
    # 告警層壞掉也絕不能弄掛執行器（它的職責只是讓失敗有出口）
    monkeypatch.setattr(ci, "_load_health", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ci.finish_round({"auth": "x"}, 1.0, dry=True) == {}


def test_note_fail_records_first_sample_and_redacts():
    ci._ROUND_FAILS.clear()
    ci._note_fail(ci.classify_failure(1, _FAKE_401), _FAKE_401)
    ci._note_fail("auth_ip_whitelist", "second sample")     # 同類只留第一個
    assert list(ci._ROUND_FAILS) == ["auth_ip_whitelist"]
    assert "<key-id-redacted>" in ci._ROUND_FAILS["auth_ip_whitelist"]
    ci._note_fail(None, "benign")                            # 良性不入帳
    assert list(ci._ROUND_FAILS) == ["auth_ip_whitelist"]
    ci._ROUND_FAILS.clear()
