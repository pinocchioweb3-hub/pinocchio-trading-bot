# -*- coding: utf-8 -*-
"""v254：成交均價把單筆實際風險推開，不得只存在於交易所的保證金欄位裡。

張數是用**計畫進場價**換算的（風險預算 ÷ |計畫進場價 − 止損|），但這筆單真正押了
多少錢，是由**成交均價**到止損的距離決定的。往不利方向滑＝實際 1R 超出預算，而本地
帳上的 risk_usd 仍是下單那一刻寫死的值 ⇒ 超額曝險本地一本帳都查不到。

v252 已經量到同一個數字的**另一半**（送出前被名義值夾層砍小）。只做一半＝仍然講不出
「這筆單的 1R 到底是多少」。實測 SNDK（2026-08-03）：計畫進場 ~1193、成交 1180.39、
止損 1228.38 ⇒ 實際 1R 比 20U 預算高約 20%。

⛔ 這一版純觀測：不改倉、不補單、不動任何風險參數（真錢部位大小只有使用者能定）。
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ci = _load("_ci_v254", "tools/atk_consumer/consume_intents.py")

# SNDK 形狀（數字取整成可手算的版本）：0.5 個標的單位 × 47.99 價差 = 23.995U
SZ, CT_VAL = 50.0, 0.01
AVG_PX, STOP, BOOKED = 1180.39, 1228.38, 20.0


# ── 純函式：三態 ───────────────────────────────────────────────────────────
def test_fill_risk_uses_avg_px_not_planned_entry():
    state, info = ci.fill_risk_verdict(SZ, CT_VAL, AVG_PX, STOP, BOOKED)
    assert state == "ok"
    assert info["risk_fill"] == pytest.approx(23.995, abs=1e-3)
    assert info["drift_pct"] == pytest.approx(0.19975, abs=1e-4)


@pytest.mark.parametrize("kwargs, why", [
    (dict(ct_val=None), "ct_val"),
    (dict(avg_px=None), "avg_px"),
    (dict(avg_px="0"), "avg_px"),          # 交易所回 0＝沒有均價，不是「均價是 0」
    (dict(contracts=None), "contracts"),
    (dict(stop=None), "stop"),
    (dict(risk_booked=None), "risk_booked"),
])
def test_any_missing_input_is_unknown_never_folded_into_no_drift(kwargs, why):
    """⛔ 算不出來 ≠ 沒有偏移。任一項讀不出來一律 unknown 並講出缺哪一項。"""
    args = dict(contracts=SZ, ct_val=CT_VAL, avg_px=AVG_PX, stop=STOP,
                risk_booked=BOOKED)
    args.update(kwargs)
    state, info = ci.fill_risk_verdict(**args)
    assert state == "unknown"
    assert why in info["reason"]


def test_fill_price_sitting_on_the_stop_is_unknown_not_zero_risk():
    state, info = ci.fill_risk_verdict(SZ, CT_VAL, STOP, STOP, BOOKED)
    assert state == "unknown"
    assert info["reason"] == "zero_stop_distance"


# ── 迴圈級：就地改 rec ＋ 進本輪帳 ────────────────────────────────────────
def _rec(**over):
    rec = {"inst_id": "SNDK-USDT-SWAP", "pos_side": "short", "symbol": "SNDK",
           "contracts": SZ, "ct_val": CT_VAL, "stop": STOP, "risk_usd": BOOKED,
           "entry_planned": 1193.0}
    rec.update(over)
    return rec


def test_drift_beyond_threshold_lands_in_round_ledger_and_record():
    ci._ROUND_RISK_DRIFTS.clear()
    rec = _rec()
    ci.maybe_risk_drift(rec, AVG_PX, "intent-1")
    assert rec["risk_fill"]["risk_usd"] == pytest.approx(23.995, abs=1e-3)
    assert len(ci._ROUND_RISK_DRIFTS) == 1
    row = ci._ROUND_RISK_DRIFTS[0]
    assert row["state"] == "ok" and row["risk_booked"] == BOOKED
    assert row["avg_px"] == AVG_PX and row["entry_planned"] == 1193.0


def test_measured_once_not_every_round():
    """成交均價定案後不會再變（本執行器從不加倉）——每輪重算＝重複刷屏＋重複記帳。"""
    ci._ROUND_RISK_DRIFTS.clear()
    rec = _rec()
    ci.maybe_risk_drift(rec, AVG_PX, "intent-1")
    ci.maybe_risk_drift(rec, AVG_PX, "intent-1")
    assert len(ci._ROUND_RISK_DRIFTS) == 1


def test_small_slippage_is_recorded_but_not_alerted():
    """小滑價是正常的：數字仍要落在紀錄上（不然「有沒有量過」也分不出來），但不進帳。"""
    ci._ROUND_RISK_DRIFTS.clear()
    rec = _rec()
    near = STOP - (BOOKED / (SZ * CT_VAL)) * 1.02      # 剛好只超出預算 2%
    ci.maybe_risk_drift(rec, near, "intent-1")
    assert rec["risk_fill"]["state"] == "ok"
    assert rec["risk_fill"]["drift_pct"] == pytest.approx(0.02, abs=1e-6)
    assert ci._ROUND_RISK_DRIFTS == []


def test_unknown_ct_val_retries_before_giving_up_then_speaks(monkeypatch):
    """舊紀錄沒有 ct_val：先回頭查規格（有限次），真的查不到才定案成 unknown 並出聲。"""
    ci._ROUND_RISK_DRIFTS.clear()
    monkeypatch.setattr(ci, "fetch_inst_spec", lambda *a, **k: None)
    rec = _rec(ct_val=None)
    for _ in range(ci.CTVAL_RETRY_MAX - 1):
        ci.maybe_risk_drift(rec, AVG_PX, "intent-1")
        assert "risk_fill" not in rec        # 還在重試窗內＝不提早定案
    ci.maybe_risk_drift(rec, AVG_PX, "intent-1")   # 第 CTVAL_RETRY_MAX 次＝最後一次
    assert rec["ct_val_retry"] == ci.CTVAL_RETRY_MAX
    assert rec["risk_fill"]["state"] == "unknown"
    assert len(ci._ROUND_RISK_DRIFTS) == 1
    assert ci._ROUND_RISK_DRIFTS[0]["state"] == "unknown"


def test_record_missing_other_inputs_finalises_now_and_asks_nothing(monkeypatch):
    """v254 之前下的舊倉沒有 risk_usd：那是永遠補不回來的，不是「這輪讀不到」。

    ⛔ 兩個都要成立：(a) 不為了 ctVal 多打一次規格查詢——補到它也算不出來；
    (b) 當輪就定案並出聲。若照「缺 ct_val 就等下輪重試」處理，答案在第一輪就已經
    確定是 unknown，卻要拖滿 CTVAL_RETRY_MAX 輪、路上還白打 5 次查詢才講得出來。
    （實測過舊行為：ct_val_retry 會前進、第 5 輪仍會定案，所以是「延後＋白費呼叫」，
    ⛔ 不是永久沉默——別把這條寫成比實際更嚴重。）"""
    ci._ROUND_RISK_DRIFTS.clear()
    calls = {"n": 0}

    def _spec(*_a, **_k):
        calls["n"] += 1
        return None                          # 查詢失敗＝舊碼會判「下輪再試」

    monkeypatch.setattr(ci, "fetch_inst_spec", _spec)
    rec = _rec(ct_val=None, risk_usd=None)
    ci.maybe_risk_drift(rec, AVG_PX, "intent-1")
    assert calls["n"] == 0, "答案已經不可能算得出來，還去問交易所＝每輪一次白費呼叫"
    assert rec["risk_fill"]["state"] == "unknown"
    assert "risk_booked" in rec["risk_fill"]["reason"]
    assert len(ci._ROUND_RISK_DRIFTS) == 1


def test_ct_val_backfilled_into_record_so_later_rounds_cost_nothing(monkeypatch):
    calls = {"n": 0}

    def _spec(*_a, **_k):
        calls["n"] += 1
        return {"ctVal": CT_VAL, "lotSz": 0.1, "minSz": 0.1,
                "tickSz": 0.01, "maxLever": 20.0}

    monkeypatch.setattr(ci, "fetch_inst_spec", _spec)
    rec = _rec(ct_val=None)
    assert ci._rec_ct_val(rec) == CT_VAL
    assert rec["ct_val"] == CT_VAL
    assert ci._rec_ct_val(rec) == CT_VAL
    assert calls["n"] == 1                   # 第二次零額外呼叫


# ── 健康帳：⛔ 必須在「空轉輪提早 return」之前 ───────────────────────────
def test_accounting_survives_an_idle_round():
    """v169/v170/v249/v252 四次都栽在這條路徑：偏移是在管理在場部位時算出來的，
    那一輪完全可能沒有任何新單、也沒有成功呼叫（oks==0）＝空轉輪分支。"""
    drifts = [{"inst_id": "SNDK-USDT-SWAP", "state": "ok", "drift_pct": 0.2,
               "risk_booked": 20.0, "risk_fill": 24.0}]
    h = ci.update_health({}, {}, 1_800_000_000.0, oks=0, risk_drifts=drifts)
    assert h["risk_drift_total"] == 1
    assert h["risk_drift_over_total"] == 1
    assert h["idle_rounds"] == 1             # 確認這輪真的是空轉輪（非虛設檢定）


def test_unknown_rows_counted_separately():
    drifts = [{"inst_id": "X", "state": "unknown", "reason": "missing:ct_val"}]
    h = ci.update_health({}, {}, 1_800_000_000.0, oks=1, risk_drifts=drifts)
    assert h["risk_drift_unknown_total"] == 1
    assert h["risk_drift_over_total"] == 0


def test_no_drift_no_fields():
    """沒事不生欄位＝帳本不長出一排 0（比照 v249/v252）。"""
    h = ci.update_health({}, {}, 1_800_000_000.0, oks=1, risk_drifts=[])
    assert "risk_drift_total" not in h


# ── 帳本側：只有人能裁決，且是現況條件不是既成事實 ───────────────────────
def test_ledger_surfaces_recent_sizing_gap_as_a_user_blocker():
    from l3_dispatcher.ceo_oversight import risk_sizing_verdict
    now = 1_800_000_000.0
    h = {"risk_capped_total": 3, "risk_capped_last_ts": now - 600,
         "risk_capped_recent": [{"inst_id": "INTC-USDT-SWAP",
                                 "risk_intended": 20.0, "risk_effective": 9.3}]}
    v = risk_sizing_verdict(h, now_s=now)
    assert v and v["capped"] == 3
    assert "9.3" in v["text"] and "只有你能定" in v["text"]


def test_ledger_text_has_no_float_tails():
    """這串會原樣進帳本給人看：實測線上第一筆就長成 5.937165000000033U。"""
    from l3_dispatcher.ceo_oversight import risk_sizing_verdict
    now = 1_800_000_000.0
    h = {"risk_capped_total": 1, "risk_capped_last_ts": now - 60,
         "risk_capped_recent": [{"inst_id": "QQQ-USDT-SWAP",
                                 "risk_intended": 20.0,
                                 "risk_effective": 5.937165000000033}]}
    text = risk_sizing_verdict(h, now_s=now)["text"]
    assert "5.94U" in text, text
    assert "5.937165" not in text


def test_ledger_gap_expires_because_it_is_a_condition_not_a_fact():
    """⚠️ 與 pnl_gap 的刻意差異：漏記的損益不可逆（不套窗），夾層／滑價是現況條件——
    參數一改或單純沒有新單它就不再發生。拿累計數當永久阻塞，等於使用者裁決完了帳本
    還永遠掛著一條他已經決定過的事（r71 org_coverage 踩過的坑）。"""
    from l3_dispatcher.ceo_oversight import risk_sizing_verdict
    now = 1_800_000_000.0
    h = {"risk_capped_total": 3, "risk_capped_last_ts": now - 48 * 3600}
    assert risk_sizing_verdict(h, now_s=now) is None


def test_ledger_reports_unknown_rows_too():
    from l3_dispatcher.ceo_oversight import risk_sizing_verdict
    now = 1_800_000_000.0
    h = {"risk_drift_unknown_total": 2, "risk_drift_last_ts": now - 60}
    v = risk_sizing_verdict(h, now_s=now)
    assert v and "不等於沒有偏移" in v["text"]


def test_assess_puts_it_on_the_user_side():
    from l3_dispatcher.ceo_oversight import assess
    out = assess(now_ms=1_800_000_000_000, commit_age_sec=60,
                 paper_n=0, paper_min=30, live_n=0, live_min=30,
                 demo_n=0, demo_live=False, demo_active=True,
                 open_decisions=0, pending_outbox=0,
                 risk_sizing={"capped": 1, "drift_over": 0, "unknown": 0,
                              "text": "實際單筆風險與風險預算不一致"})
    assert any("風險預算" in b for b in out["blockers"])
    assert out["state"] == "BLOCKED_ON_USER"
    assert out["risk_sizing"] is not None
