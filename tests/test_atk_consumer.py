# -*- coding: utf-8 -*-
"""ATK 消費腳本純函式測試（v139 倉位管理迴圈）。零網路、零 OKX 呼叫。"""
import json
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


# ── v154（監督員 r44）修A：同幣同向已在場閘 ─────────────────────────────
def test_dup_gate_pure_blocks_same_side_allows_opposite():
    open_map = {"i-1": {"inst_id": "BTC-USDT-SWAP", "pos_side": "long"}}
    assert ci.dup_open_same_side(open_map, "BTC-USDT-SWAP", "long")
    assert not ci.dup_open_same_side(open_map, "BTC-USDT-SWAP", "short")  # hedge 反向合法
    assert not ci.dup_open_same_side(open_map, "ETH-USDT-SWAP", "long")
    assert not ci.dup_open_same_side({}, "BTC-USDT-SWAP", "long")
    assert not ci.dup_open_same_side(None, "BTC-USDT-SWAP", "long")


def _arm_loop(tmp_path, monkeypatch, open_map, pos_side, place_ok=True):
    """把主迴圈接到臨時目錄上跑一輪 --once（零網路、零下單）。回 (下單清單, done)。"""
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    intent = {"intent_id": "i-new", "symbol": "BTC", "inst_id": "BTC-USDT-SWAP",
              "pos_side": pos_side, "entry": 100.0, "stop": 95.0,
              "execution_policy": "demo_only",
              "expires_at": (time.time() + 3600) * 1000}
    (outbox / "a.json").write_text(json.dumps(intent), encoding="utf-8")
    (tmp_path / "pos.json").write_text(
        json.dumps({"open": open_map, "day_pnl": {}}), encoding="utf-8")
    placed: list = []
    monkeypatch.setattr(ci, "OUTBOX", outbox)
    monkeypatch.setattr(ci, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(ci, "POS_STATE", tmp_path / "pos.json")
    monkeypatch.setattr(ci, "verify_demo_profile", lambda: True)
    monkeypatch.setattr(ci, "manage_positions", lambda dry: None)
    monkeypatch.setattr(ci, "finish_round", lambda *a, **k: {})
    monkeypatch.setattr(ci, "contracts_for", lambda *a, **k: 1.0)
    # place_ok=False 模擬「部分成腿」：交易所側可能已有一腿成交，但整筆回報失敗
    monkeypatch.setattr(ci, "place",
                        lambda intent, sz, dry, spec=None:
                        (placed.append(intent["intent_id"]), place_ok)[1])
    monkeypatch.setattr(sys, "argv", ["consume_intents.py", "--once"])
    assert ci.main() == 0
    done = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["done"]
    return placed, done


def test_loop_blocks_new_order_when_same_symbol_same_side_already_open(tmp_path, monkeypatch):
    held = {"i-old": {"inst_id": "BTC-USDT-SWAP", "pos_side": "long",
                      "contracts": 1.0, "placed_at": time.time() - 600}}
    placed, done = _arm_loop(tmp_path, monkeypatch, held, "long")
    assert placed == []                    # 併倉風險 → 不下單
    assert "i-new" not in done             # 不記 done：倉平掉且未過期就自然接上


def test_loop_still_places_opposite_side_on_same_symbol(tmp_path, monkeypatch):
    held = {"i-old": {"inst_id": "BTC-USDT-SWAP", "pos_side": "long",
                      "contracts": 1.0, "placed_at": time.time() - 600}}
    placed, done = _arm_loop(tmp_path, monkeypatch, held, "short")
    assert placed == ["i-new"]             # hedge 雙向合法，別擋過頭
    assert "i-new" in done


# ── v154（監督員 r44）修B：熔斷記帳吃成交日、且不被 14 天修剪吃掉 ────────
def _arm_manage(tmp_path, monkeypatch, fill_ts_s, placed_at_s):
    """在場倉在交易所已消失 → 走對帳記帳路徑。回 day_pnl。"""
    (tmp_path / "pos.json").write_text(json.dumps({
        "open": {"i-1": {"inst_id": "BTC-USDT-SWAP", "pos_side": "long",
                         "symbol": "BTC", "contracts": 1.0,
                         "placed_at": placed_at_s}},
        "day_pnl": {}}), encoding="utf-8")
    monkeypatch.setattr(ci, "POS_STATE", tmp_path / "pos.json")

    def fake_okx(args, timeout=30):
        if args[:2] == ["account", "positions"]:
            return 0, "[]"                              # 交易所側已無倉＝已了結
        if args[:2] == ["swap", "fills"]:
            return 0, json.dumps([{"ts": str(int(fill_ts_s * 1000)),
                                   "fillPnl": "-5.0", "fee": "-0.5"}])
        return 1, "unexpected"
    monkeypatch.setattr(ci, "_okx", fake_okx)
    ci.manage_positions(dry=False)
    return json.loads((tmp_path / "pos.json").read_text(encoding="utf-8"))["day_pnl"]


def test_realized_pnl_recorded_on_fill_day_not_reconcile_day(tmp_path, monkeypatch):
    now = time.time()
    fill_ts = now - 26.5 * 3600            # 斷流 26.5h 後才對帳（本次 401 的真實跨度）
    day_pnl = _arm_manage(tmp_path, monkeypatch, fill_ts, now - 30 * 3600)
    assert day_pnl == {ci._day_key(fill_ts): -5.5}
    if ci._day_key(fill_ts) != ci._day_key(now):
        assert ci._day_key(now) not in day_pnl   # 不得算到對帳當天頭上


def test_old_fill_is_clamped_into_retention_window_not_silently_pruned(tmp_path, monkeypatch):
    now = time.time()
    fill_ts = now - 15 * 86400             # 比 14 天保留窗更舊
    day_pnl = _arm_manage(tmp_path, monkeypatch, fill_ts, now - 16 * 86400)
    edge = ci._day_key(now - 14 * 86400)
    assert day_pnl == {edge: -5.5}         # 夾到窗邊界：仍計入熔斷口徑，不無聲蒸發
    assert sum(day_pnl.values()) == -5.5


# ── v155（監督員 r45）修C：設槓桿失敗只在「風險帶」擋單，並記可辨識類別 ────
# 風險帶＝算出的 lev < LEVERAGE。lev == LEVERAGE 時交易所舊值不可能更高⇒白擋，
# 維持照下（見 docs/org/2026-07-31-真錢路徑設槓桿失敗-唯一不擋單也不記帳的呼叫.md §五）。
_LEV_401 = ("Error: HTTP 401 from OKX: Your IP 203.0.113.7 is not included in "
            "your API key's 00000000-0000-4000-8000-000000000000 IP whitelist.")


def _arm_place(monkeypatch, entry, stop, max_lev, lev_err=_LEV_401):
    """跑一次 place()，只讓 swap leverage 失敗，其餘呼叫成功。回 (place回傳, 已送出的腿)。"""
    ci._LEV_SET.clear()
    ci._ROUND_FAILS.clear()
    monkeypatch.setattr(ci, "LEVERAGE", max_lev)
    legs: list = []

    def fake_okx(args, timeout=30):
        if args[:2] == ["swap", "leverage"]:
            ci._note_fail(ci.classify_failure(1, lev_err), lev_err)   # 比照 _okx 真實副作用
            return 1, lev_err
        legs.append(args)
        return 0, '{"sCode": "0", "ordId": "1"}'

    monkeypatch.setattr(ci, "_okx", fake_okx)
    monkeypatch.setattr(ci, "_order_exists", lambda inst_id, cl: False)
    intent = {"inst_id": "BTC-USDT-SWAP", "pos_side": "long", "side": "buy",
              "entry": entry, "stop": stop, "tp1": entry * 1.1,
              "cl_ord_id": "c1"}
    return ci.place(intent, 1.0, dry=False), legs


def test_leverage_fail_blocks_order_when_computed_lev_below_cap(monkeypatch):
    # 止損 10%、上限 20x → 應設 7x。設失敗＝交易所可能仍卡在更高的舊值，
    # 清算會先於止損（v84 不變式破了）→ 必須 fail-closed、零腿送出。
    ok, legs = _arm_place(monkeypatch, entry=100.0, stop=90.0, max_lev=20)
    assert ci.leverage_for_trade(100.0, 90.0, 20) == 7 < 20   # 確認確實落在風險帶
    assert ok is False                     # 本輪不下，下輪重試
    assert legs == []                      # 一腿都不准送出去
    assert "leverage_fail" in ci._ROUND_FAILS   # 且類別要分得出是哪一支呼叫死的


def test_leverage_fail_still_places_when_computed_lev_equals_cap(monkeypatch):
    # 止損 2%、上限 20x → 算出來就是上限 20x。舊值不可能比上限更高⇒擋單純屬白擋，
    # 維持現行「只警告、照下」，但故障仍要進帳（安靜≠健康）。
    ok, legs = _arm_place(monkeypatch, entry=100.0, stop=98.0, max_lev=20)
    assert ci.leverage_for_trade(100.0, 98.0, 20) == 20       # 確認不在風險帶
    assert ok is True
    assert len(legs) == 1                  # 照樣送出
    assert "leverage_fail" in ci._ROUND_FAILS


def test_leverage_fail_is_distinguishable_from_downstream_query_fail():
    # 傳輸層類別（401）由 _okx 記，本身分不出是設槓桿還是查單死的（r45 探針實證）。
    # leverage_fail 要排在 query_fail 之前：查單失敗是下游症狀，設槓桿失敗是根因。
    assert "leverage_fail" in ci._CLASS_PRIORITY
    assert (ci._CLASS_PRIORITY.index("leverage_fail")
            < ci._CLASS_PRIORITY.index("query_fail"))
    assert ci.worst_class({"leverage_fail", "query_fail"}) == "leverage_fail"
    assert "leverage_fail" in ci._CLASS_HINT       # 告警要講得出怎麼處置


# ── r46（監督員）斷流期倉位保護：孤兒部位的成因與偵測 ──────────────────
# 規格：docs/2026-07-31-斷流期倉位保護-規格.md
# 底線事實：進場每一腿都附掛交易所端 SL（--slTriggerPx），所以斷流不會讓在場倉
# 「裸奔」——止損照樣由 OKX 執行。真正會掉的是本地帳這一側：逾時平倉、了結對帳、
# 以及餵給日/週熔斷的 day_pnl。以下三支先把成因與盲點釘成回歸鎖（偵測函式已就位，
# 尚未接入迴圈；接入會改動交易語義，須在有人值守的輪次做）。

def test_partial_leg_failure_sends_a_real_leg_but_reports_failure(monkeypatch):
    """孤兒的成因（上半）：腿1 真的送出去了（交易所已有倉＋附掛 SL），
    但腿2 查單失敗 ⇒ place() 回 False。"""
    ci._LEV_SET.clear(); ci._ROUND_FAILS.clear()
    legs: list = []

    def fake_okx(args, timeout=30):
        if args[:2] == ["swap", "leverage"]:
            return 0, '{"code":"0"}'
        legs.append(args)
        return 0, '{"sCode": "0", "ordId": "1"}'

    monkeypatch.setattr(ci, "_okx", fake_okx)
    # 腿1 查得到「不存在」→ 照下；腿2 查詢失敗（斷流）→ 不下、整筆回 False
    seen: list = []
    monkeypatch.setattr(ci, "_order_exists",
                        lambda inst_id, cl: seen.append(cl) or (False if len(seen) == 1 else None))
    intent = {"inst_id": "BTC-USDT-SWAP", "pos_side": "long", "side": "buy",
              "entry": 100.0, "stop": 98.0, "tp1": 105.0, "tp2": 110.0,
              "cl_ord_id": "c1"}
    ok = ci.place(intent, 10.0, dry=False,
                  spec={"lotSz": 1.0, "minSz": 1.0, "ctVal": 1.0})
    assert ok is False                       # 對外報失敗
    assert len(legs) == 1                    # 但交易所側已經真的有一腿成交了
    assert any("--slTriggerPx" in a for a in legs[0]), \
        "進場腿必須附掛交易所端止損——斷流保護的底線就靠這個"
    assert "query_fail" in ci._ROUND_FAILS


def test_loop_writes_no_ledger_record_when_place_reports_failure(tmp_path, monkeypatch):
    """孤兒的成因（下半）：place() 回 False ⇒ 本地帳一個字都不寫。
    配上「腿1 其實已成交」＝交易所有倉、帳本空白。"""
    placed, done = _arm_loop(tmp_path, monkeypatch, {}, "long", place_ok=False)
    assert placed == ["i-new"]               # 確實走進了下單路徑（非被閘擋掉）
    ps = json.loads((tmp_path / "pos.json").read_text(encoding="utf-8"))
    assert ps["open"] == {}                  # 帳本空白
    assert "i-new" not in done               # 未記 done：會重試到 expires_at 為止


def test_manage_positions_is_structurally_blind_to_orphans(tmp_path, monkeypatch):
    """盲點特徵化：本地帳空 ⇒ manage_positions 連交易所都不問就返回。
    ⚠️ 偵測接入迴圈後，本支的預期會改成「有問到並回報孤兒」——屆時一起改。"""
    (tmp_path / "pos.json").write_text(
        json.dumps({"open": {}, "day_pnl": {}}), encoding="utf-8")
    monkeypatch.setattr(ci, "POS_STATE", tmp_path / "pos.json")
    calls: list = []
    monkeypatch.setattr(ci, "_okx", lambda args, timeout=30: calls.append(args) or (0, "[]"))
    ci.manage_positions(dry=False)
    assert calls == [], "本地帳空就直接返回——交易所側從未被枚舉過"


def test_orphan_detector_flags_exchange_only_positions():
    """偵測函式本體：交易所有、帳本沒有 → 抓出來。"""
    ledger = {"i-1": {"inst_id": "BTC-USDT-SWAP", "pos_side": "long"}}
    ex = [{"instId": "BTC-USDT-SWAP", "posSide": "long", "pos": "3"},    # 帳本有 → 不算
          {"instId": "ETH-USDT-SWAP", "posSide": "short", "pos": "5"}]   # 帳本沒有 → 孤兒
    assert ci.orphan_positions(ex, ledger) == [("ETH-USDT-SWAP", "short", 5.0)]


def test_orphan_detector_distinguishes_side_and_ignores_closed():
    """同幣反向是不同部位（hedge）；pos=0 是已平，不是孤兒。"""
    ledger = {"i-1": {"inst_id": "BTC-USDT-SWAP", "pos_side": "long"}}
    ex = [{"instId": "BTC-USDT-SWAP", "posSide": "short", "pos": "2"},
          {"instId": "SOL-USDT-SWAP", "posSide": "long", "pos": "0"}]
    assert ci.orphan_positions(ex, ledger) == [("BTC-USDT-SWAP", "short", 2.0)]


def test_orphan_detector_never_raises_on_malformed_input():
    """將來要跑在交易路徑上：畸形資料只能略過，不可丟例外炸掉整輪。"""
    ex = [None, "x", {}, {"instId": "BTC-USDT-SWAP", "posSide": "long", "pos": "abc"},
          {"posSide": "long", "pos": "1"}]
    assert ci.orphan_positions(ex, None) == []
    assert ci.orphan_positions(None, None) == []
    assert ci.orphan_positions([], {"i": {"inst_id": "A", "pos_side": "long"}}) == []


def test_leverage_for_trade_risk_band_boundaries():
    # 回歸鎖：擋單規則整條建立在這條純函式上，邊界動了規則就失準。
    assert ci.leverage_for_trade(100.0, 96.6, 20) == 20       # 止損 3.4% → 上限
    # ⚠️ 實測校正：止損「剛好 3.5%」算出的是 19 不是 20——int() 截斷（70/3.5 在
    # 浮點下是 19.999…）。方向是保守側（槓桿更低＝保證金更多），不改；但這代表
    # 3.5% 這一格仍落在會擋單的風險帶內，r41 文件寫的「≤3.5% ⇒ 20x」要往下修一格。
    assert ci.leverage_for_trade(100.0, 96.5, 20) == 19
    assert ci.leverage_for_trade(100.0, 90.0, 20) == 7        # 止損 10%
    assert ci.leverage_for_trade(100.0, 70.0, 20) == 3        # 止損 30% → 下限 3
    assert ci.leverage_for_trade(0.0, 0.0, 20) == 5           # 缺值 → min(上限,5)
