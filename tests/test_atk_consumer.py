# -*- coding: utf-8 -*-
"""ATK 消費腳本純函式測試（v139 倉位管理迴圈）。零網路、零 OKX 呼叫。"""
import ast
import json
import re
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


def _arm_loop(tmp_path, monkeypatch, open_map, pos_side, place_ok=True, orphans=(),
              scan_failed=False, ledger_raw=None, day_pnl=None, state_raw=None):
    """把主迴圈接到臨時目錄上跑一輪 --once（零網路、零下單）。回 (下單清單, done)。

    ledger_raw＝直接寫進本地帳檔的原文（給 v163 的壞檔情境用；None＝正常 JSON）。
    state_raw＝直接寫進已處理清單檔的原文（給 v165 的壞檔情境用；None＝不預先建檔）。
    done 讀不回來時回 None——v165 起「清單未知」的那一輪本來就不該寫回那個檔。"""
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    intent = {"intent_id": "i-new", "symbol": "BTC", "inst_id": "BTC-USDT-SWAP",
              "pos_side": pos_side, "entry": 100.0, "stop": 95.0,
              "execution_policy": "demo_only",
              "expires_at": (time.time() + 3600) * 1000}
    (outbox / "a.json").write_text(json.dumps(intent), encoding="utf-8")
    (tmp_path / "pos.json").write_text(
        ledger_raw if ledger_raw is not None
        else json.dumps({"open": open_map, "day_pnl": day_pnl or {}}),
        encoding="utf-8")
    if state_raw is not None:
        (tmp_path / "state.json").write_text(state_raw, encoding="utf-8")
    placed: list = []
    monkeypatch.setattr(ci, "OUTBOX", outbox)
    monkeypatch.setattr(ci, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(ci, "POS_STATE", tmp_path / "pos.json")
    monkeypatch.setattr(ci, "verify_demo_profile", lambda: True)
    # v162（r53）：manage_positions 的回傳有三態——list=查得到（可能空＝確認乾淨）、
    #   None=這輪根本查不到（未知）。⛔ 不可再用 `list(orphans) or None` 把「空」
    #   和「未知」混成同一個值：那正是 r47 只做半套的破口（見下方 scan_failed 測）。
    monkeypatch.setattr(ci, "manage_positions",
                        lambda dry: None if scan_failed else list(orphans))
    monkeypatch.setattr(ci, "finish_round", lambda *a, **k: {})
    monkeypatch.setattr(ci, "contracts_for", lambda *a, **k: 1.0)
    # place_ok=False 模擬「部分成腿」：交易所側可能已有一腿成交，但整筆回報失敗
    monkeypatch.setattr(ci, "place",
                        lambda intent, sz, dry, spec=None:
                        (placed.append(intent["intent_id"]), place_ok)[1])
    monkeypatch.setattr(sys, "argv", ["consume_intents.py", "--once"])
    assert ci.main() == 0
    try:
        done = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["done"]
    except Exception:  # noqa: BLE001
        done = None                        # v165：清單未知的那輪不該有寫回
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


# ── v248（監督員 r139）：設槓桿失敗 ⇒ 無條件擋單，並記可辨識類別 ──────────
# ⚠️ 這裡原本鎖的是 v155（r45）修C 的「風險帶」規則（只擋 lev < LEVERAGE，lev 等於
# 上限時視為白擋、照下）。2026-08-03 17:12 真錢實證推翻了它：SOXL-USDT-SWAP short
# 算出的 lev 剛好是上限，設槓桿被 OKX 回 59102（該合約上限低於本執行器上限），
# 三腿真錢單照樣送出，倉開在交易所預設 3x 上。v155 的推理只涵蓋「交易所側比意圖
# 高」，漏了對稱的「比意圖低」。詳見 tests/test_leverage_fail_blocks_entry_v248.py
# 與 docs/2026-08-03-v248-設槓桿失敗仍下真錢單-風險帶推理漏了另一半.md。
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


def test_leverage_fail_blocks_order_when_computed_lev_equals_cap(monkeypatch):
    # 止損 2%、上限 20x → 算出來就是上限 20x，也就是 v155 判定「白擋」而放行的那一段。
    # SOXL 那筆證明交易所側可以**低於**意圖（59102／預設 3x），一樣破壞下單前提 ⇒ 必須擋。
    ok, legs = _arm_place(monkeypatch, entry=100.0, stop=98.0, max_lev=20)
    assert ci.leverage_for_trade(100.0, 98.0, 20) == 20       # 確認就是 v155 放行的那一段
    assert ok is False                     # 本輪不下，下輪重試
    assert legs == []                      # 一腿都不准送出去
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


def test_manage_positions_enumerates_exchange_even_when_ledger_empty(tmp_path, monkeypatch):
    """v159（r47）盲點已翻面：本地帳空**也要**枚舉交易所側，否則孤兒永遠看不見。
    （本支原本是盲點特徵化「calls == []」，接入後預期反過來。）"""
    (tmp_path / "pos.json").write_text(
        json.dumps({"open": {}, "day_pnl": {}}), encoding="utf-8")
    monkeypatch.setattr(ci, "POS_STATE", tmp_path / "pos.json")
    calls: list = []
    monkeypatch.setattr(ci, "_okx", lambda args, timeout=30: calls.append(args) or (0, "[]"))
    assert ci.manage_positions(dry=False) == []
    assert calls == [["account", "positions"]], "本地帳空也要問交易所一次"


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


# ── v159（監督員 r47）孤兒偵測接入主迴圈：記帳＋擋單，但⛔不平倉⛔不收編 ────
# 規格 §4.2。方向只有一個：保守側。偵測到孤兒 → 記健康帳（既有連續輪告警自然接手）
# ＋擋同幣同向新單；⛔ 不自動平倉（不確定時動真錢部位是把不確定變成損失）、
# ⛔ 不自動收編進本地帳（沒有 placed_at／進場價／R 值，收編＝製造假帳）。

def _arm_orphan_round(tmp_path, monkeypatch, ex_positions, open_map=None,
                      query_ok=True):
    """跑一輪 manage_positions，交易所側回 ex_positions。回 (回傳值, 送出的呼叫)。"""
    (tmp_path / "pos.json").write_text(
        json.dumps({"open": open_map or {}, "day_pnl": {}}), encoding="utf-8")
    monkeypatch.setattr(ci, "POS_STATE", tmp_path / "pos.json")
    ci._ROUND_FAILS.clear()
    calls: list = []

    def fake_okx(args, timeout=30):
        calls.append(args)
        if args[:2] == ["account", "positions"]:
            if not query_ok:
                return 1, "Error: HTTP 401 from OKX"      # 斷流輪
            return 0, json.dumps(ex_positions)
        return 0, "[]"

    monkeypatch.setattr(ci, "_okx", fake_okx)
    return ci.manage_positions(dry=False), calls


def test_orphan_is_recorded_into_health_with_identifiers(tmp_path, monkeypatch):
    """偵測到孤兒 → 記健康帳 orphan_position，且樣本要講得出是哪一筆倉
    （instId／posSide／張數），告警才有可操作性。"""
    ex = [{"instId": "ETH-USDT-SWAP", "posSide": "short", "pos": "5"}]
    got, _ = _arm_orphan_round(tmp_path, monkeypatch, ex)
    assert got == [("ETH-USDT-SWAP", "short")]
    assert "orphan_position" in ci._ROUND_FAILS
    sample = ci._ROUND_FAILS["orphan_position"]
    assert "ETH-USDT-SWAP" in sample and "short" in sample and "5" in sample
    ci._ROUND_FAILS.clear()


def test_orphan_is_never_auto_closed_nor_adopted_into_ledger(tmp_path, monkeypatch):
    """⛔ 兩條紅線：不得送出平倉、不得把孤兒寫進本地帳（會製造假的 placed_at／R）。"""
    ex = [{"instId": "ETH-USDT-SWAP", "posSide": "short", "pos": "5"}]
    _, calls = _arm_orphan_round(tmp_path, monkeypatch, ex)
    assert calls == [["account", "positions"]], "只准查，不准動"
    assert not any(a[:2] == ["swap", "close"] for a in calls)
    ps = json.loads((tmp_path / "pos.json").read_text(encoding="utf-8"))
    assert ps["open"] == {}, "孤兒不得被收編進本地帳"
    ci._ROUND_FAILS.clear()


def test_orphan_query_failure_is_not_treated_as_no_orphan(tmp_path, monkeypatch):
    """fail-closed 方向：查不到 ≠ 沒有孤兒。不得記成『本輪乾淨無孤兒』，
    也不得因查不到就動既有的本地帳（那是誤判平倉）。
    v162（r53）：回傳值由 [] 改為 None——[] 在下游等同「確認乾淨」（見下一支）。"""
    held = {"i-old": {"inst_id": "BTC-USDT-SWAP", "pos_side": "long",
                      "symbol": "BTC", "contracts": 1.0,
                      "placed_at": time.time() - 600}}
    got, calls = _arm_orphan_round(tmp_path, monkeypatch, [], open_map=held,
                                   query_ok=False)
    assert got is None                                 # 不宣稱有孤兒、也不宣稱沒有
    assert "orphan_position" not in ci._ROUND_FAILS    # 也不宣稱沒有（不記乾淨）
    assert calls == [["account", "positions"]]         # 查失敗就停手，不再往下動
    ps = json.loads((tmp_path / "pos.json").read_text(encoding="utf-8"))
    assert ps["open"] == held                          # 既有倉一個字都不准改
    ci._ROUND_FAILS.clear()


# ── v162（監督員 r53）把 r47 的 fail-closed 從「函式內」推到「迴圈」──────────
# r47 已寫下原則「查不到 ≠ 沒有孤兒」，但只做到不記健康帳；回傳仍是 []，而主迴圈
# 讀到 [] 就等於「本輪確認沒有孤兒」→ 擋單閘在查詢失敗的那些輪整個消失。
# 危害場景：OKX 各端點分開限流／偶發 timeout ⇒ account positions 查失敗、下單端點
# 仍通 ⇒ 同幣同向新單照送、與那筆脫帳的孤兒在交易所側併倉 ⇒ 曝險無聲翻倍。
# 而孤兒正是「查單失敗」生出來的 ⇒ 最可能有孤兒的輪，恰好就是閘最可能瞎掉的輪。

def test_manage_positions_reports_unknown_distinctly_from_confirmed_clean(
        tmp_path, monkeypatch):
    """型別要能表達『不知道』：查失敗回 None、查得到且真的沒有才回 []。"""
    got_fail, _ = _arm_orphan_round(tmp_path, monkeypatch, [], query_ok=False)
    got_clean, _ = _arm_orphan_round(tmp_path, monkeypatch, [], query_ok=True)
    assert got_fail is None, "查不到必須回 None，不可與『確認乾淨』同型"
    assert got_clean == [], "查得到且真的沒孤兒才回空清單"
    ci._ROUND_FAILS.clear()


def test_loop_skips_new_orders_when_orphan_scan_could_not_run(tmp_path, monkeypatch):
    """迴圈級（本輪主修）：孤兒掃描沒跑成 ⇒ 本輪一律不接新單、且不記 done。
    寧可少下一輪（intent 到期前每輪都會再試），也不可在看不見交易所部位時開新倉。"""
    placed, done = _arm_loop(tmp_path, monkeypatch, {}, "long", scan_failed=True)
    assert placed == []
    assert "i-new" not in done


def test_loop_still_places_when_orphan_scan_ran_and_found_nothing(tmp_path, monkeypatch):
    """別擋過頭（三）：掃描有跑成、結果是空 ⇒ 正常下單。"""
    placed, done = _arm_loop(tmp_path, monkeypatch, {}, "long")
    assert placed == ["i-new"]
    assert "i-new" in done


def test_loop_blocks_new_order_when_orphan_same_symbol_same_side(tmp_path, monkeypatch):
    """迴圈級：孤兒在場 ⇒ 同幣同向新單本輪不接、且不記 done（人工處理完可自然接上）。"""
    placed, done = _arm_loop(tmp_path, monkeypatch, {}, "long",
                             orphans=[("BTC-USDT-SWAP", "long")])
    assert placed == []
    assert "i-new" not in done


def test_loop_still_places_when_orphan_is_opposite_side(tmp_path, monkeypatch):
    """別擋過頭（一）：hedge 反向合法。"""
    placed, _ = _arm_loop(tmp_path, monkeypatch, {}, "long",
                          orphans=[("BTC-USDT-SWAP", "short")])
    assert placed == ["i-new"]


def test_loop_still_places_when_orphan_is_another_symbol(tmp_path, monkeypatch):
    """別擋過頭（二）：別的幣的孤兒與本單無關。"""
    placed, _ = _arm_loop(tmp_path, monkeypatch, {}, "long",
                          orphans=[("ETH-USDT-SWAP", "long")])
    assert placed == ["i-new"]


def test_orphan_class_outranks_transport_classes_and_has_hint():
    """孤兒只可能在『查詢成功』的輪被記到（⇒ 不可能洗掉斷流主因），
    而它代表真錢部位脫帳 ⇒ 同輪多類故障時它要當代表。"""
    assert ci._CLASS_PRIORITY.index("orphan_position") == 0
    assert ci.worst_class({"orphan_position", "query_fail"}) == "orphan_position"
    assert "orphan_position" in ci._CLASS_HINT


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


# ── v160（監督員 r48）日誌洩密：印給人看的那條路徑從沒過遮蔽 ──────────────
# 實證：斷流期間 atk_live.log 累積 1436 行含未遮蔽 API key-id（單一 key、全部
# 來自「設槓桿失敗」那行），遮蔽標記 0 行——redact_secrets 只掛在寫健康檔的
# _note_fail 上。日誌本身在資料目錄不在 repo，但只要有人把片段貼進 issue／
# docs／commit 訊息就外洩，而日誌是長期保存的＝明文永久落檔。

def test_leverage_fail_log_line_masks_key_id_but_keeps_ip(monkeypatch, capsys):
    """印出去的那一行要遮 key-id、留 IP（IP 是使用者補白名單唯一有用的資訊）。"""
    ci._LEV_SET.clear(); ci._ROUND_FAILS.clear()
    monkeypatch.setattr(ci, "_okx", lambda args, timeout=30: (1, _LEV_401))
    assert ci.ensure_leverage("BTC-USDT-SWAP", "long", dry=False, lev=7) is False
    printed = capsys.readouterr().out
    assert "00000000-0000-4000-8000-000000000000" not in printed
    assert "<key-id-redacted>" in printed
    assert "203.0.113.7" in printed


def test_redaction_happens_before_truncation_not_after(monkeypatch, capsys):
    """順序回歸鎖：先截斷再遮蔽會把 UUID 切一半、正則失配 ⇒ 漏出半截 key-id。
    構造一則「UUID 剛好跨過 120 字元截斷點」的錯誤訊息來釘住順序。"""
    uuid = "00000000-0000-4000-8000-000000000000"
    err = "x" * 110 + " " + uuid + " tail"          # UUID 起於第 111 字元
    ci._LEV_SET.clear(); ci._ROUND_FAILS.clear()
    monkeypatch.setattr(ci, "_okx", lambda args, timeout=30: (1, err))
    ci.ensure_leverage("BTC-USDT-SWAP", "long", dry=False, lev=7)
    printed = capsys.readouterr().out
    # 半截也算洩：key-id 前 8 碼足以指認是哪一把金鑰
    assert uuid[:8] not in printed
    # 標記本身被 120 字元截斷成 "<key-id-r" 是無害的，不必完整；
    # 有它就證明「替換發生在截斷之前」——順序反了這裡會是原始 UUID 前半段。
    assert "<key-id-" in printed


# ── v163（監督員 r54）本地部位帳：「讀失敗」不可再等於「確認空帳」 ────────────
# 與 r53 修的孤兒閘同一物種，但下游更致命：同一個空值同時餵三個地方——
#   ①breaker_tripped(day_pnl={}) ⇒ 日/週熔斷整個消失；
#   ②dup_open_same_side(open={}) ⇒ 同幣同向擋單閘瞎掉；
#   ③下單成功後拿這本空帳寫回檔案 ⇒ 既有倉與 14 天熔斷損益被永久抹掉。
# 觸發條件不需要網路：舊 _save_positions 是非原子寫（先截斷再寫），行程在中途被殺
# 就留下半截 JSON，之後每一輪都讀壞。

_CORRUPT = '{"open": {"i-old": {"inst_id": "BTC-USDT-SWAP", "pos_'   # 被截斷的半截檔


def test_ledger_read_failure_is_unknown_but_missing_file_is_empty(tmp_path, monkeypatch):
    """型別要能表達『不知道』：壞檔回 None、檔案不存在（首跑）才是合法空帳。"""
    ci._ROUND_FAILS.clear()
    monkeypatch.setattr(ci, "POS_STATE", tmp_path / "pos.json")
    assert ci._load_positions() == {"open": {}, "day_pnl": {}}   # 首跑：還沒有倉
    (tmp_path / "pos.json").write_text(_CORRUPT, encoding="utf-8")
    assert ci._load_positions() is None, "讀壞了不可與『確認沒有倉』同型"
    assert "pos_state_unreadable" in ci._ROUND_FAILS, "無聲失敗＝沒有出口"
    (tmp_path / "pos.json").write_text('["not", "a", "dict"]', encoding="utf-8")
    assert ci._load_positions() is None, "結構不對也是未知，不可當空帳"
    ci._ROUND_FAILS.clear()


def test_loop_never_places_when_ledger_is_unreadable(tmp_path, monkeypatch):
    """迴圈級（本輪主修）：帳本讀不到 ⇒ 熔斷口徑與同幣同向閘皆無資料 ⇒ 本輪不接新單，
    且不記 done（帳本修好、intent 未過期就自然接上）。"""
    ci._ROUND_FAILS.clear()
    placed, done = _arm_loop(tmp_path, monkeypatch, {}, "long", ledger_raw=_CORRUPT)
    assert placed == [], "看不見在場倉與今日損益時開新倉＝風險上限暫時不存在"
    assert "i-new" not in done
    ci._ROUND_FAILS.clear()


def test_corrupt_ledger_is_never_overwritten_by_an_empty_one(tmp_path, monkeypatch):
    """⛔ 最不可逆的一條：壞檔絕不可被『空帳＋這筆新倉』覆蓋掉——
    那會把既有倉與近 14 天熔斷損益一起清零，且無聲。"""
    ci._ROUND_FAILS.clear()
    _arm_loop(tmp_path, monkeypatch, {}, "long", ledger_raw=_CORRUPT)
    assert (tmp_path / "pos.json").read_text(encoding="utf-8") == _CORRUPT
    ci._ROUND_FAILS.clear()


def test_ledger_write_is_atomic_so_a_failed_write_keeps_the_old_file(tmp_path, monkeypatch):
    """寫入失敗要留下**完整的舊檔**（而不是半截新檔），並回 False 讓呼叫端出聲。"""
    ci._ROUND_FAILS.clear()
    good = json.dumps({"open": {}, "day_pnl": {"2026-07-31": -1.0}})
    (tmp_path / "pos.json").write_text(good, encoding="utf-8")
    monkeypatch.setattr(ci, "POS_STATE", tmp_path / "pos.json")

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(ci.os, "replace", boom)
    assert ci._save_positions({"open": {"x": 1}, "day_pnl": {}}) is False
    assert (tmp_path / "pos.json").read_text(encoding="utf-8") == good
    assert "pos_state_write_fail" in ci._ROUND_FAILS
    ci._ROUND_FAILS.clear()


def test_ledger_write_success_returns_true_and_round_trips(tmp_path, monkeypatch):
    """別擋過頭：正常寫入回 True、讀得回來。"""
    ci._ROUND_FAILS.clear()
    monkeypatch.setattr(ci, "POS_STATE", tmp_path / "pos.json")
    assert ci._save_positions({"open": {}, "day_pnl": {"2026-07-31": -1.0}}) is True
    assert ci._load_positions()["day_pnl"] == {"2026-07-31": -1.0}
    assert not ci._ROUND_FAILS


def test_ledger_write_failure_after_a_placed_order_is_loud(tmp_path, monkeypatch, capsys):
    """單已送出但沒記進帳本＝下一輪的孤兒。這件事必須出聲，不可無聲吞掉。"""
    ci._ROUND_FAILS.clear()
    monkeypatch.setattr(ci, "_save_positions", lambda ps: False)
    placed, _ = _arm_loop(tmp_path, monkeypatch, {}, "long")
    assert placed == ["i-new"]
    assert "🚨" in capsys.readouterr().out
    ci._ROUND_FAILS.clear()


def test_pos_state_classes_outrank_transport_classes_and_have_hints():
    """本地帳壞掉不是『這輪送不出去』而是『風險上限暫時不存在』，且與網路無關、
    修法完全不同（要人去看那個檔案）⇒ 同輪多類故障時它要蓋過傳輸類當代表。"""
    for cls in ("pos_state_unreadable", "pos_state_write_fail"):
        assert cls in ci._CLASS_PRIORITY and cls in ci._CLASS_HINT
        assert (ci._CLASS_PRIORITY.index(cls)
                < ci._CLASS_PRIORITY.index("auth_ip_whitelist"))
    assert ci.worst_class({"pos_state_unreadable", "query_fail"}) == "pos_state_unreadable"


def test_no_print_echoes_raw_okx_body_unredacted():
    """結構鎖：模組內任何 print() 只要把 OKX 回傳文字（out／body）塞進輸出，
    就必須先過 redact_secrets——擋的是「以後又加一條沒遮蔽的 print」這種漂移。"""
    src = Path(ci.__file__).read_text(encoding="utf-8")
    bad = []
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == "print"):
            continue
        seg = ast.get_source_segment(src, node) or ""
        if (re.search(r"\{\s*(out|body)(?![A-Za-z0-9_])", seg)
                and "redact_secrets" not in seg):
            bad.append(seg[:100])
    assert not bad, f"未遮蔽就回顯 OKX 原文的 print：{bad}"


# ── v164（r55）健康檔「讀失敗 ≠ 沒有故障史」 ──────────────────────────
def _arm_health(tmp_path, monkeypatch, primary=None, backup=None):
    """把健康檔導到 tmp；primary/backup 給 str 就原樣寫入（可寫壞檔）。"""
    h = tmp_path / "health.json"
    monkeypatch.setattr(ci, "HEALTH", h)
    if primary is not None:
        h.write_text(primary, encoding="utf-8")
    if backup is not None:
        h.with_name(h.name + ".bak").write_text(backup, encoding="utf-8")
    return h


def test_corrupt_health_does_not_reset_streak_into_alert_blindness(tmp_path, monkeypatch):
    """半截 JSON（非原子寫被中斷）讓 _load_health 讀失敗時，舊版回 {} ⇒ 連續故障
    每輪歸零 ⇒ 永遠到不了門檻 ⇒ 告警層自己無聲死掉（正是 v143 要治的物種）。
    讀不到＝**未知**，不可當成「沒有故障史」。"""
    _arm_health(tmp_path, monkeypatch, primary='{"consecutive_fail_rounds": 4')
    h = ci.finish_round({"auth_ip_whitelist": "401"}, 1000.0, dry=True)
    assert h.get("consecutive_fail_rounds", 0) >= ci.FAIL_ALERT_AFTER
    assert h.get("last_alert_ts") == 1000.0, "健康檔壞掉時故障輪必須仍能告警"


def test_corrupt_health_falls_back_to_backup_and_keeps_counting(tmp_path, monkeypatch):
    """主檔壞掉但備份好的 ⇒ 從備份續算，而不是從零重數（否則 401 這種長斷流
    每次檔案抖動就把『已持續多久』重置，使用者看到的時數會被低報）。"""
    _arm_health(tmp_path, monkeypatch, primary="{oops",
                backup=json.dumps({"consecutive_fail_rounds": 9,
                                   "first_fail_ts": 100.0,
                                   "last_alert_ts": 200.0,
                                   "last_alert_class": "auth_ip_whitelist",
                                   "last_fail_class": "auth_ip_whitelist"}))
    h = ci.finish_round({"auth_ip_whitelist": "401"}, 1000.0, dry=True)
    assert h["consecutive_fail_rounds"] == 10
    assert h["first_fail_ts"] == 100.0, "備份裡的起始時間要留著＝持續時長不被低報"


def test_health_save_is_atomic_and_mirrors_a_backup(tmp_path, monkeypatch):
    """先截斷再寫的非原子寫是壞檔來源本身 ⇒ 改暫存檔＋os.replace，並留一份
    last-known-good 備份（兩次分開的原子寫，被砍時最多壞掉其中一個）。"""
    h = _arm_health(tmp_path, monkeypatch)
    assert ci._save_health({"consecutive_fail_rounds": 2}) is True
    bak = h.with_name(h.name + ".bak")
    assert json.loads(h.read_text(encoding="utf-8"))["consecutive_fail_rounds"] == 2
    assert json.loads(bak.read_text(encoding="utf-8"))["consecutive_fail_rounds"] == 2
    assert not h.with_name(h.name + ".tmp").exists(), "暫存檔不可留下"


def test_missing_health_file_is_still_a_legitimate_first_run(tmp_path, monkeypatch):
    """首跑（檔案不存在）是真的沒有故障史 ⇒ 不可被當成『未知』而誤報。"""
    _arm_health(tmp_path, monkeypatch)
    h = ci.finish_round({}, 1000.0, dry=True, oks=1)
    assert h.get("consecutive_fail_rounds", 0) == 0
    assert not h.get("last_alert_ts")


def test_corrupt_health_never_fabricates_a_recovery_notice(tmp_path, monkeypatch):
    """讀不到故障史時，乾淨輪不可以反過來變成『✅已恢復』——
    r33 假痊癒的同一個坑（沒有證據時只能維持原判，不能報好消息）。"""
    _arm_health(tmp_path, monkeypatch, primary="}}not json{{")
    h = ci.finish_round({}, 1000.0, dry=True, oks=1)
    assert not h.get("recovered_from")


# ── v165（監督員 r56）：已處理清單的「未知 vs 確認沒處理過」 ──────────────
_STATE_CORRUPT = '{"done": ["0965ba0c26c12842", "0ee01a27b41'   # 被截斷的半截檔


def test_done_state_read_failure_is_unknown_but_missing_file_is_empty(tmp_path, monkeypatch):
    """型別要能表達『不知道』：壞檔回 None、檔案不存在（首跑）才是合法空清單。"""
    ci._ROUND_FAILS.clear()
    monkeypatch.setattr(ci, "STATE", tmp_path / "state.json")
    assert ci._load_state() == {"done": []}                  # 首跑：真的還沒處理過
    assert not ci._ROUND_FAILS, "首跑不是故障，不可誤報"
    (tmp_path / "state.json").write_text(_STATE_CORRUPT, encoding="utf-8")
    assert ci._load_state() is None, "讀壞了不可與『確認一筆都沒處理過』同型"
    assert "done_state_unreadable" in ci._ROUND_FAILS, "無聲失敗＝沒有出口"
    (tmp_path / "state.json").write_text('{"done": "not-a-list"}', encoding="utf-8")
    assert ci._load_state() is None, "結構不對也是未知，不可當空清單"
    ci._ROUND_FAILS.clear()


def test_loop_never_places_when_done_state_is_unreadable(tmp_path, monkeypatch):
    """迴圈級（本輪主修）：分不出哪些 intent 下過單 ⇒ 冪等第一道鎖失效 ⇒ 本輪不接新單。

    舊碼把讀失敗壓成 {"done": []}，未過期的舊 intent 會整批重跑。"""
    ci._ROUND_FAILS.clear()
    placed, done = _arm_loop(tmp_path, monkeypatch, {}, "long",
                             state_raw=_STATE_CORRUPT)
    assert placed == [], "分不出下過沒下過還照下＝重跑舊 intent"
    assert "done_state_unreadable" in ci._ROUND_FAILS
    ci._ROUND_FAILS.clear()


def test_corrupt_done_state_is_never_overwritten_by_an_empty_one(tmp_path, monkeypatch):
    """⛔ 唯一不可逆的一條：壞檔絕不可被一本乾淨的空清單覆蓋掉——
    那會把最多 500 筆已處理紀錄無聲清零（比照 v163 對部位帳的處置）。"""
    ci._ROUND_FAILS.clear()
    _arm_loop(tmp_path, monkeypatch, {}, "long", state_raw=_STATE_CORRUPT)
    assert (tmp_path / "state.json").read_text(encoding="utf-8") == _STATE_CORRUPT
    ci._ROUND_FAILS.clear()


def test_already_done_intent_is_not_reprocessed_into_a_phantom_position(tmp_path, monkeypatch):
    """迴圈級·這才是「重跑舊 intent」真正的傷口：clOrdId 冪等擋得住重複成交，
    擋不住「每腿都已存在 ⇒ place() 回 True ⇒ 把早已平掉的倉又寫進本地帳」。
    正常（清單讀得到）時該 intent 必須被 done 濾掉、連 place 都不該進去。"""
    ci._ROUND_FAILS.clear()
    good = json.dumps({"done": ["i-new"]})
    placed, done = _arm_loop(tmp_path, monkeypatch, {}, "long", state_raw=good)
    assert placed == [], "已處理過的 intent 不可再進下單路徑"
    assert done == ["i-new"], "清單本身要原樣留著"
    ci._ROUND_FAILS.clear()


def test_done_state_write_is_atomic_so_a_failed_write_keeps_the_old_file(tmp_path, monkeypatch):
    """寫入失敗要留下**完整的舊檔**（而不是半截新檔），並回 False 讓呼叫端出聲。
    半截新檔正是下一輪 _load_state 讀壞的成因＝舊碼自己製造再自己誤讀。"""
    ci._ROUND_FAILS.clear()
    good = json.dumps({"done": ["aaa", "bbb"]})
    (tmp_path / "state.json").write_text(good, encoding="utf-8")
    monkeypatch.setattr(ci, "STATE", tmp_path / "state.json")

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(ci.os, "replace", boom)
    assert ci._save_state({"done": ["ccc"]}) is False
    assert (tmp_path / "state.json").read_text(encoding="utf-8") == good
    assert "done_state_write_fail" in ci._ROUND_FAILS, "無聲吞掉＝沒有出口"
    ci._ROUND_FAILS.clear()


def test_done_state_write_success_returns_true_and_round_trips(tmp_path, monkeypatch):
    """別擋過頭：正常寫入回 True、讀得回來、不留暫存檔。"""
    ci._ROUND_FAILS.clear()
    monkeypatch.setattr(ci, "STATE", tmp_path / "state.json")
    assert ci._save_state({"done": ["aaa"]}) is True
    assert ci._load_state()["done"] == ["aaa"]
    assert not (tmp_path / "state.json.tmp").exists(), "暫存檔不可留下"
    assert not ci._ROUND_FAILS


# ── v166（監督員 r57）：健康檔「寫不進去」＝告警層記不住事情 ──────────────
def _arm_unwritable_health(tmp_path, monkeypatch):
    """健康檔導到一個不存在的資料夾底下（寫必失敗、讀是合法首跑），
    節流痕跡也導進 tmp（不污染真實暫存目錄）。回收到的告警文字清單。"""
    monkeypatch.setattr(ci, "HEALTH", tmp_path / "nodir" / "health.json")
    monkeypatch.setattr(ci, "DEGRADED_MARKER", tmp_path / "marker.ts", raising=False)
    sent: list[str] = []
    monkeypatch.setattr(ci, "send_alert",
                        lambda text, dry=False: (sent.append(text), ("dry", None))[1])
    return sent


def test_frozen_counter_never_silences_the_alert_layer(tmp_path, monkeypatch):
    """本輪主修：健康檔寫不進去 ⇒ 每輪都從舊值重數、**永遠**到不了 FAIL_ALERT_AFTER
    ⇒ 一場真實斷流可以整場零通知（v164 治讀、這裡治寫，同一物種）。
    舊碼只 print 一行到 log ⇒ 連續 5 輪 401 一封通知都沒有。"""
    sent = _arm_unwritable_health(tmp_path, monkeypatch)
    for i in range(5):
        ci.finish_round({"auth_ip_whitelist": "401 ... not included in ..."},
                        1000.0 + i * 60, dry=True)
    assert sent, "計數凍結時完全不出聲＝告警層自己無聲死掉"
    assert "健康檔寫不進去" in sent[0]
    assert "不可信" in sent[0], "數字已不可信必須講清楚，否則使用者會照著讀"


def test_degraded_notice_fires_even_on_a_clean_round(tmp_path, monkeypatch):
    """不可以只在『這輪剛好有故障』時才講——記事本已經壞了本身就是事故，
    等下一次故障才講，等到的時候已經沒有能力講了。"""
    sent = _arm_unwritable_health(tmp_path, monkeypatch)
    h = ci.finish_round({}, 1000.0, dry=True, oks=1)
    assert h.get("health_write_failed") is True
    assert h.get("degraded_alert_ts") == 1000.0
    assert len(sent) == 1


def test_degraded_notice_is_throttled_but_not_muted(tmp_path, monkeypatch):
    """節流：一小時內只講一次（否則每分鐘一封＝使用者靜音＝又變回無聲）；
    超過重複間隔就要再講一次（故障還在，不能只講開頭那一封）。"""
    sent = _arm_unwritable_health(tmp_path, monkeypatch)
    ci.finish_round({}, 1000.0, dry=True, oks=1)
    ci.finish_round({}, 1060.0, dry=True, oks=1)
    ci.finish_round({}, 1120.0, dry=True, oks=1)
    assert len(sent) == 1, "冷卻內不可重複轟炸"
    ci.finish_round({}, 1000.0 + ci.FAIL_ALERT_REPEAT_SEC + 1, dry=True, oks=1)
    assert len(sent) == 2, "超過重複間隔仍要再提醒＝故障沒好就不能停口"


def test_unreadable_throttle_marker_defaults_to_speaking_up(tmp_path, monkeypatch):
    """⛔ 節流痕跡讀不到／寫不進去＝『不知道剛剛講過沒有』，
    只能推 True（講）。推 False 就是拿沒有證據當作已經講過——同一個坑再踩一次。"""
    sent = _arm_unwritable_health(tmp_path, monkeypatch)
    monkeypatch.setattr(ci, "DEGRADED_MARKER", tmp_path / "nodir" / "marker.ts",
                        raising=False)
    assert ci.degraded_alert_due(1000.0) is True
    assert ci.degraded_alert_due(1001.0) is True, "痕跡寫不進去時不可自行閉嘴"
    ci.finish_round({}, 1000.0, dry=True, oks=1)
    assert len(sent) == 1


def test_degraded_notice_never_fires_while_health_writes_fine(tmp_path, monkeypatch):
    """反向護欄：正常情況（寫得進去）永不出現降級通知，也不留旗標——
    fail-closed 不等於可以亂吵。"""
    _arm_health(tmp_path, monkeypatch)
    monkeypatch.setattr(ci, "DEGRADED_MARKER", tmp_path / "marker.ts", raising=False)
    sent: list[str] = []
    monkeypatch.setattr(ci, "send_alert",
                        lambda text, dry=False: (sent.append(text), ("dry", None))[1])
    h = ci.finish_round({}, 1000.0, dry=True, oks=1)
    assert not h.get("health_write_failed")
    assert not h.get("degraded_alert_ts")
    assert sent == []
    assert not (tmp_path / "marker.ts").exists(), "沒事就不該留節流痕跡"


def test_degraded_alert_text_redacts_key_ids_and_claims_no_performance(tmp_path):
    """告警文字鐵則：不得洩漏 key-id（公開 repo／截圖風險），
    也不得出現任何績效字樣（紅線③）。"""
    # 佔位 UUID 一律 00000000 開頭（tools/secret_leak_scan.py 的約定，⛔勿改成真實樣式）
    fake_key = "00000000-1111-4000-8000-000000000001"
    txt = ci.degraded_alert_text({"profile": "live", "consecutive_fail_rounds": 7},
                                 1000.0, f"OSError: key {fake_key}")
    assert fake_key not in txt and "<key-id-redacted>" in txt
    assert not any(w in txt for w in ("勝率", "報酬", "年化", "獲利"))
    assert "交易路徑與風險閘不受影響" in txt
