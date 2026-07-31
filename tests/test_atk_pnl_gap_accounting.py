# -*- coding: utf-8 -*-
"""已了結但「損益查不到」的部位，不可無聲離開熔斷口徑（v170・監督員 r68）。

背景（2026-07-31 讀碼發現，尚未在線上發生過＝這是**還沒被咬到**的洞）：
manage_positions 對帳時，若某筆倉在交易所上已消失（TP/SL/手動平掉），會呼叫
`_realized_pnl_since()` 把已實現損益記進 `day_pnl`——那是日 60U／週 150U 熔斷
**唯一**的輸入。但查詢失敗時舊碼做的是：

    print("🏁 … 已了結（損益查詢失敗，不計入熔斷口徑）")
    ps["open"].pop(iid, None)          # ← 紀錄就此消失

於是這筆真錢的已實現損益永遠不會進 day_pnl，而且**不可逆**：紀錄一 pop，就再也
沒有任何東西知道要去查它。後果不是「少一行 log」，是**熔斷低估已實現虧損**——
該停手的日子可能照常接新單。唯一的痕跡是 log 裡一行中文。

與 v164（讀失敗→當成沒有故障史）、v166（寫失敗→只寫進 log）、v167（用旗標代理
「活著」）、v169（過期丟棄只在 log）同一物種，這是第六次：**要用來下判斷的量，
只以 log 文字存在，等於沒有存在。**前五次都在觀測層，這一次直接落在風險上限上。

⛔ 關鍵不變式（本檔的存在理由）：
  ① 查詢失敗要先**有限重試**（每輪都是獨立行程，重試計數必須寫回部位帳才活得過
     下一輪）——一次連線抖動不該換來一筆永久漏記；
  ② 真的放棄時，必須留下**數字**（健康檔累計＋明細）與一個**故障類別**，
     不可只 print；
  ③ 漏記不分「斷流期／健康期」——不像過期丟棄，它在任何情況下都是同一個洞。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "atk_consumer"))

import consume_intents as ci  # noqa: E402


def _gap(inst_id="SOXL-USDT-SWAP", side="long", iid="i-1"):
    return {"intent_id": iid, "inst_id": inst_id, "pos_side": side,
            "symbol": "SOXL", "placed_at": 500.0, "retries": 5}


# ── 健康帳（純函式） ────────────────────────────────────────────────
def test_pnl_gap_is_counted_in_health_not_only_printed():
    """最低要求：漏記一筆就要有一個數字長大，而且跨輪累加。"""
    h = ci.update_health({}, {}, 1000.0, oks=1, pnl_gaps=[_gap()])
    assert int(h.get("pnl_unaccounted_total", 0)) == 1
    h2 = ci.update_health(h, {}, 1060.0, oks=1,
                          pnl_gaps=[_gap(iid="i-2"), _gap(iid="i-3")])
    assert int(h2.get("pnl_unaccounted_total", 0)) == 3, "跨輪必須累加，不可每輪重數"


def test_pnl_gap_is_recorded_before_the_idle_round_early_return():
    """比照 v169 的教訓：記帳必須在空轉輪的提早 return **之前**。

    漏記本身不必然發生在空轉輪，但只要有一次落在那條路徑上就永久記不到，
    而這種東西只會在事後才發現。位置錯了，這條會紅。"""
    h = ci.update_health({"consecutive_fail_rounds": 3}, {}, 1000.0, oks=0,
                         pnl_gaps=[_gap()])
    assert int(h.get("idle_rounds", 0)) == 1, "前提：這確實是一個空轉輪"
    assert int(h.get("pnl_unaccounted_total", 0)) == 1


def test_pnl_gap_detail_keeps_enough_to_look_it_up_on_okx():
    """明細要足以讓人去 OKX 把那筆金額查回來——否則「知道漏了」等於沒用。"""
    h = ci.update_health({}, {}, 1000.0, oks=1, pnl_gaps=[_gap()])
    rec = (h.get("pnl_unaccounted_recent") or [])[-1]
    assert rec["inst_id"] == "SOXL-USDT-SWAP"
    assert rec["pos_side"] == "long"
    assert float(rec["placed_at"]) == 500.0, "要有開倉時間才查得到成交區間"
    assert float(rec["ts"]) == 1000.0


def test_pnl_gap_recent_list_is_bounded_and_keeps_the_newest():
    """明細要有上限——健康檔每輪整份重寫，無界成長會養到寫不進去，
    那會反過來打死 v166 才修好的告警計數（同一個檔）。"""
    h: dict = {}
    for i in range(ci.PNL_GAP_RECENT_MAX + 5):
        h = ci.update_health(h, {}, 1000.0 + i, oks=1,
                             pnl_gaps=[_gap(inst_id=f"S{i}-USDT-SWAP")])
    recent = h["pnl_unaccounted_recent"]
    assert len(recent) == ci.PNL_GAP_RECENT_MAX
    assert recent[-1]["inst_id"] == f"S{ci.PNL_GAP_RECENT_MAX + 4}-USDT-SWAP", "留最新的"
    assert int(h["pnl_unaccounted_total"]) == ci.PNL_GAP_RECENT_MAX + 5, \
        "明細有上限，但總數不可被上限截斷"


def test_pnl_gap_counters_survive_a_recovery_round():
    """⛔ 恢復輪不可清空——漏記是既成事實，不會因為連線好了就補回來。"""
    h = {"consecutive_fail_rounds": 9, "last_alert_ts": 900.0,
         "last_fail_class": "auth_ip_whitelist",
         "pnl_unaccounted_total": 2, "pnl_unaccounted_recent": [_gap()]}
    out = ci.update_health(h, {}, 2000.0, oks=5)
    assert out.get("recovered_from"), "前提：這確實是恢復輪"
    assert int(out["pnl_unaccounted_total"]) == 2
    assert len(out["pnl_unaccounted_recent"]) == 1


def test_no_gap_means_no_noise():
    """沒漏記就不該憑空生出欄位（避免帳本每輪顯示一排 0）。"""
    h = ci.update_health({}, {}, 1000.0, oks=1, pnl_gaps=[])
    assert not h.get("pnl_unaccounted_recent")
    assert int(h.get("pnl_unaccounted_total", 0)) == 0


def test_pnl_unaccounted_outranks_transport_classes_as_the_round_representative():
    """同輪多類故障時，代表類別不可被下游的 query_fail 蓋掉：漏記是不可逆的
    風險上限失真，和「這輪送不出去」不是同一個量級，處置方式也完全不同。"""
    assert "pnl_unaccounted" in ci._CLASS_PRIORITY
    assert ci.worst_class({"query_fail", "pnl_unaccounted"}) == "pnl_unaccounted"
    assert ci.worst_class({"pnl_unaccounted", "pos_state_unreadable"}) == "pnl_unaccounted"
    assert "pnl_unaccounted" in ci._CLASS_HINT, "告警要講得出人該做什麼"


# ── manage_positions（函式級：純函式對了不代表對帳迴圈有把資料交上去） ──
def _arm(tmp_path, monkeypatch, *, fills_ok, open_rec):
    """佈一個「本地帳有一筆倉、交易所上已消失」的對帳輪。

    fills_ok=False ⇒ swap fills 查詢失敗（＝_realized_pnl_since 回 None）。
    回 (讀回的部位帳, 本輪 gap 清單, 本輪故障清單)。
    """
    pos = tmp_path / "pos.json"
    pos.write_text(json.dumps({"open": {"i-1": open_rec}, "day_pnl": {}}),
                   encoding="utf-8")
    monkeypatch.setattr(ci, "POS_STATE", pos)
    ci._ROUND_FAILS.clear()
    ci._ROUND_PNL_GAPS.clear()

    def fake_okx(args, timeout=30):
        if args[:2] == ["account", "positions"]:
            return 0, json.dumps([])          # 交易所上沒有任何倉＝那筆已了結
        if args[:2] == ["swap", "fills"]:
            if not fills_ok:
                return 1, "HTTP 500"
            return 0, json.dumps([{"ts": 600_000, "fillPnl": "-3.5", "fee": "-0.5"}])
        return 0, "[]"

    monkeypatch.setattr(ci, "_okx", fake_okx)
    ci.manage_positions(dry=False)
    return (json.loads(pos.read_text(encoding="utf-8")),
            list(ci._ROUND_PNL_GAPS), dict(ci._ROUND_FAILS))


def _rec(placed_at=500.0, **extra):
    r = {"inst_id": "SOXL-USDT-SWAP", "pos_side": "long", "symbol": "SOXL",
         "contracts": 1.0, "placed_at": placed_at}
    r.update(extra)
    return r


def test_first_pnl_query_failure_retries_instead_of_dropping_the_record(tmp_path, monkeypatch):
    """⛔ 一次抖動不可換來一筆永久漏記：紀錄要留著、重試計數要**寫回檔案**
    （每輪是獨立行程，只存在記憶體的計數活不過這一分鐘）。"""
    ps, gaps, fails = _arm(tmp_path, monkeypatch, fills_ok=False, open_rec=_rec())
    assert "i-1" in ps["open"], "第一次查失敗就 pop＝把可重試的事做成不可逆"
    assert int(ps["open"]["i-1"].get("pnl_retry", 0)) == 1, "重試計數必須落地"
    assert gaps == [], "還在重試中，不算漏記"
    assert "pnl_unaccounted" not in fails


def test_pnl_gap_is_reported_only_after_retries_are_exhausted(tmp_path, monkeypatch):
    """重試用盡才放棄——放棄時必須同時留下 ①數字 ②故障類別，不可只 print。"""
    ps, gaps, fails = _arm(tmp_path, monkeypatch, fills_ok=False,
                           open_rec=_rec(pnl_retry=ci.PNL_RETRY_MAX - 1))
    assert "i-1" not in ps["open"], "重試用盡後才移出（否則那筆倉會卡住同幣同向新單）"
    assert len(gaps) == 1, "放棄時必須把漏記交給收尾層"
    assert gaps[0]["inst_id"] == "SOXL-USDT-SWAP"
    assert float(gaps[0]["placed_at"]) == 500.0
    assert "pnl_unaccounted" in fails, "必須有故障類別，否則永遠到不了告警與帳本"


def test_a_successful_retry_accounts_the_pnl_normally(tmp_path, monkeypatch):
    """反向護欄：重試成功就照常記進 day_pnl，且不可留下任何漏記痕跡。"""
    ps, gaps, fails = _arm(tmp_path, monkeypatch, fills_ok=True,
                           open_rec=_rec(pnl_retry=ci.PNL_RETRY_MAX - 1))
    assert "i-1" not in ps["open"]
    assert gaps == []
    assert "pnl_unaccounted" not in fails
    assert round(sum(ps["day_pnl"].values()), 6) == -4.0, "fillPnl+fee 要進熔斷口徑"


def test_timed_out_position_is_untouched_by_the_retry_bookkeeping(tmp_path, monkeypatch):
    """護欄：重試欄位只掛在「已了結但查不到損益」這條路上，
    不可污染逾時強平那條（那筆倉在交易所上還在）。"""
    pos = tmp_path / "pos.json"
    pos.write_text(json.dumps({"open": {"i-1": _rec(placed_at=0.0)},
                               "day_pnl": {}}), encoding="utf-8")
    monkeypatch.setattr(ci, "POS_STATE", pos)
    ci._ROUND_PNL_GAPS.clear()
    calls: list = []

    def fake_okx(args, timeout=30):
        calls.append(args[:2])
        if args[:2] == ["account", "positions"]:
            return 0, json.dumps([{"instId": "SOXL-USDT-SWAP", "posSide": "long",
                                   "pos": "1"}])
        return 0, "{}"

    monkeypatch.setattr(ci, "_okx", fake_okx)
    ci.manage_positions(dry=False)
    ps = json.loads(pos.read_text(encoding="utf-8"))
    assert "i-1" in ps["open"], "倉還在交易所上，不可被對帳分支移走"
    assert "pnl_retry" not in ps["open"]["i-1"]
    assert list(ci._ROUND_PNL_GAPS) == []


# ── 迴圈級 ────────────────────────────────────────────────────────
def test_loop_hands_the_pnl_gap_to_finish_round(tmp_path, monkeypatch):
    """主迴圈必須把本輪漏記交給收尾層，否則健康帳再對也沒人餵它。"""
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    (tmp_path / "pos.json").write_text(json.dumps({"open": {}, "day_pnl": {}}),
                                       encoding="utf-8")
    seen: dict = {}
    monkeypatch.setattr(ci, "OUTBOX", outbox)
    monkeypatch.setattr(ci, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(ci, "POS_STATE", tmp_path / "pos.json")
    monkeypatch.setattr(ci, "verify_demo_profile", lambda: True)

    def fake_manage(dry):
        ci._ROUND_PNL_GAPS.append(_gap())
        return []

    monkeypatch.setattr(ci, "manage_positions", fake_manage)
    monkeypatch.setattr(ci, "finish_round", lambda *a, **k: (seen.update(k), {})[1])
    monkeypatch.setattr(sys, "argv", ["consume_intents.py", "--once"])
    assert ci.main() == 0
    assert len(seen.get("pnl_gaps") or []) == 1, "漏記必須上報收尾層"


def test_round_gap_list_is_cleared_between_rounds(tmp_path, monkeypatch):
    """每輪開頭要清空，否則同一筆漏記會被重複計到天荒地老。"""
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    (tmp_path / "pos.json").write_text(json.dumps({"open": {}, "day_pnl": {}}),
                                       encoding="utf-8")
    seen: dict = {}
    ci._ROUND_PNL_GAPS.append(_gap("STALE-USDT-SWAP"))
    monkeypatch.setattr(ci, "OUTBOX", outbox)
    monkeypatch.setattr(ci, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(ci, "POS_STATE", tmp_path / "pos.json")
    monkeypatch.setattr(ci, "verify_demo_profile", lambda: True)
    monkeypatch.setattr(ci, "manage_positions", lambda dry: [])
    monkeypatch.setattr(ci, "finish_round", lambda *a, **k: (seen.update(k), {})[1])
    monkeypatch.setattr(sys, "argv", ["consume_intents.py", "--once"])
    assert ci.main() == 0
    assert not (seen.get("pnl_gaps") or []), "上一輪的殘值不可帶進這一輪"


def test_time_is_not_used_to_decide_retry_exhaustion():
    """設計護欄：用『輪數』而非牆鐘判定放棄——每輪是獨立行程，
    牆鐘門檻會在排程漏跑時把重試窗白白吃掉。"""
    assert isinstance(ci.PNL_RETRY_MAX, int) and ci.PNL_RETRY_MAX >= 3
