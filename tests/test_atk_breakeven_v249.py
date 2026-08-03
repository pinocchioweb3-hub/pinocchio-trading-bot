# -*- coding: utf-8 -*-
"""v249：TP1 成交後把剩餘腿的止損搬到保本。零網路、零 OKX 呼叫。

治的洞（2026-08-03 使用者親眼在 OKX App 上看到的那個）：
    place() 的每一腿都是**獨立的一張市價單 + 自己的附掛 OCO**，而三腿的 OCO 全都帶
    同一個 `--slTriggerPx = intent["stop"]`（見 _leg_args）。於是 TP1 成交只平掉腿1，
    腿2/腿3 的止損**原封不動**留在原始止損上——已經到手的浮盈可以整段吐回去。
    實測：MU-USDT-SWAP short 吃完 TP1+TP2、剩 0.23 張，掛單的 slTriggerPx 仍是 838.39，
    而實際成交均價是 815.83。整個系統沒有任何一段程式碼會去搬它。

⛔ 誠實揭露（紅線③）：自家 1366 訊號的止損管理 A/B（task#13）判定「TP1 後保本」
   在**加密突破訊號**上淨 R 期望 −0.027R、配對 PSR P=0% 顯著劣於不搬。使用者已知
   此結論仍指示落地。本測試鎖的是「搬得正確、搬不動時出得了聲」，
   ⛔ **不是**在主張保本能提高期望值。

改動前的碼會失敗在哪（非虛設檢定的證明）：
  * ci 沒有 breakeven_stop_px / stop_is_at_least_breakeven / pending_stop_legs /
    move_stops_to_breakeven / maybe_breakeven 這幾個名字（AttributeError）
  * update_health 不吃 be_gaps；健康檔長不出 breakeven_unmoved_*
  * manage_positions 走完一輪後，部位紀錄上不會有 be 欄位
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "atk_consumer"))

import consume_intents as ci  # noqa: E402

# 2026-08-03 真錢實測值（read-only 查回，非杜撰）
MU_AVG, MU_SL, MU_TICK = 815.8269444444444445, 838.39, 0.01
SOXL_AVG, SOXL_SL, SOXL_TICK = 115.1050389105058367, 120.33, 0.01


# ═══════════ ① 保本價：基準必須是**實際成交均價** ═══════════

def test_breakeven_uses_actual_fill_not_planned_entry():
    """MU 空單：計畫進場 822.88、實際成交 815.83。拿計畫價當保本＝每單位認賠 7.06。"""
    be = ci.breakeven_stop_px(MU_AVG, MU_SL, "short", MU_TICK)
    assert be is not None
    assert be < MU_AVG, f"空單保本止損必須低於成交均價才是獲利側，得 {be}"
    planned_entry = 822.88
    assert be < planned_entry, "拿計畫進場價當『保本』會把止損擺在虧損側"
    # 0.1R 緩衝：R = |815.827 − 838.39| = 22.563 → 均價 − 2.256 ≈ 813.57
    assert 813.5 <= be <= 813.6, be


def test_breakeven_long_side_is_mirrored():
    be = ci.breakeven_stop_px(100.0, 95.0, "long", 0.01)
    assert be is not None and 100.0 < be <= 100.5, be


def test_breakeven_is_never_exactly_the_average_price():
    """⛔ 剛好停在均價：扣完進出雙邊手續費仍是淨虧，且貼著均價＝雜訊磁鐵。"""
    for avg, sl, side in ((MU_AVG, MU_SL, "short"), (100.0, 95.0, "long")):
        be = ci.breakeven_stop_px(avg, sl, side, 0.01)
        assert be != pytest.approx(avg), (avg, side, be)


def test_breakeven_is_always_strictly_better_than_the_original_stop():
    """保本永遠不該讓風險變大——這是這個功能唯一不可協商的不變式。"""
    for avg, sl, side, tick in ((MU_AVG, MU_SL, "short", MU_TICK),
                                (SOXL_AVG, SOXL_SL, "short", SOXL_TICK),
                                (100.0, 95.0, "long", 0.01)):
        be = ci.breakeven_stop_px(avg, sl, side, tick)
        assert be is not None
        assert (be > sl) if side == "long" else (be < sl), (side, be, sl)


def test_breakeven_rounds_to_tick():
    be = ci.breakeven_stop_px(SOXL_AVG, SOXL_SL, "short", SOXL_TICK)
    assert be is not None
    assert abs(round(be / SOXL_TICK) - be / SOXL_TICK) < 1e-6, be


def test_breakeven_returns_none_when_inputs_unusable():
    """⛔ 算不出來就回 None——不得退回猜一個值（那個值會變成真錢的止損）。"""
    assert ci.breakeven_stop_px(None, MU_SL, "short", MU_TICK) is None
    assert ci.breakeven_stop_px(MU_AVG, None, "short", MU_TICK) is None
    assert ci.breakeven_stop_px("", "", "short", MU_TICK) is None
    assert ci.breakeven_stop_px(100.0, 100.0, "long", 0.01) is None
    # 原始止損擺在獲利側＝形狀不對，不猜、不動
    assert ci.breakeven_stop_px(100.0, 105.0, "long", 0.01) is None


# ═══════════ ② 已達保本的判定：未知 ≠ 已經到了 ═══════════

def test_stop_already_at_breakeven_is_tristate():
    assert ci.stop_is_at_least_breakeven(813.0, 813.57, "short") is True
    assert ci.stop_is_at_least_breakeven(838.39, 813.57, "short") is False
    assert ci.stop_is_at_least_breakeven(100.6, 100.5, "long") is True
    assert ci.stop_is_at_least_breakeven(95.0, 100.5, "long") is False
    for bad in (None, "", "abc"):
        assert ci.stop_is_at_least_breakeven(bad, 813.57, "short") is None, bad


# ═══════════ ③ 掛單挑選：查不到 ≠ 確認沒有 ═══════════

def _algo_row(**over):
    row = {"algoId": "3799902930107846656", "instId": "MU-USDT-SWAP",
           "posSide": "short", "state": "live", "ordType": "oco", "sz": "0.23",
           "slTriggerPx": "838.39", "tpTriggerPx": "791.85"}
    row.update(over)
    return row


def test_pending_legs_none_means_unknown_not_empty():
    """⛔ 這兩者在風險上是相反的：一個是下輪重試，一個是『這倉現在沒有止損』。"""
    assert ci.pending_stop_legs(None, "short") is None
    assert ci.pending_stop_legs([], "short") == []
    assert ci.pending_stop_legs(["not-a-dict"], "short") is None


def test_pending_legs_filters_side_and_state():
    rows = [_algo_row(), _algo_row(posSide="long", algoId="x"),
            _algo_row(state="canceled", algoId="y"),
            _algo_row(slTriggerPx="", algoId="z")]
    got = ci.pending_stop_legs(rows, "short")
    assert [r["algoId"] for r in got] == ["3799902930107846656"]


# ═══════════ ④ 搬移：逐腿 amend，失敗要出得了聲 ═══════════

def _fake_okx(monkeypatch, algo_rows, amend_ok=True, query_code=0):
    calls = []

    def _f(args, timeout=None):
        calls.append(list(args))
        if args[:3] == ["swap", "algo", "orders"]:
            if query_code != 0:
                return 1, "Error: HTTP 500"
            return 0, json.dumps(algo_rows)
        if args[:3] == ["swap", "algo", "amend"]:
            return (0, '{"sCode": "0"}') if amend_ok else (1, 'Error: 51279 rejected')
        raise AssertionError(f"未預期的 OKX 呼叫：{args}")

    monkeypatch.setattr(ci, "_okx", _f)
    return calls


def _rec(**over):
    r = {"inst_id": "MU-USDT-SWAP", "pos_side": "short", "symbol": "MU",
         "contracts": 0.72, "stop": MU_SL, "tickSz": MU_TICK,
         "placed_at": time.time()}
    r.update(over)
    return r


def test_move_amends_every_remaining_leg(monkeypatch):
    rows = [_algo_row(), _algo_row(algoId="B", sz="0.21", tpTriggerPx="799.60")]
    calls = _fake_okx(monkeypatch, rows)
    state, info = ci.move_stops_to_breakeven(_rec(), MU_AVG, dry=False)
    assert state == "done", info
    assert info["moved"] == 2
    amends = [c for c in calls if c[:3] == ["swap", "algo", "amend"]]
    assert len(amends) == 2
    for c in amends:
        assert "--newSlTriggerPx" in c
        assert float(c[c.index("--newSlTriggerPx") + 1]) < MU_AVG


def test_move_skips_legs_already_at_breakeven(monkeypatch):
    """冪等：每分鐘一輪、每輪一個獨立行程，重跑不得把止損愈搬愈貼身。"""
    rows = [_algo_row(slTriggerPx="813.00")]
    calls = _fake_okx(monkeypatch, rows)
    state, info = ci.move_stops_to_breakeven(_rec(), MU_AVG, dry=False)
    assert state == "done" and info["moved"] == 0 and info["already"] == 1
    assert not [c for c in calls if c[:3] == ["swap", "algo", "amend"]]


def test_move_reports_unknown_when_query_fails(monkeypatch):
    _fake_okx(monkeypatch, [], query_code=1)
    state, info = ci.move_stops_to_breakeven(_rec(), MU_AVG, dry=False)
    assert state == "unknown" and info["reason"] == "algo_query_failed"


def test_move_reports_no_pending_separately_from_unknown(monkeypatch):
    """查得到、確認一張都沒有＝這倉此刻沒有交易所端止損，比搬不動更嚴重。"""
    _fake_okx(monkeypatch, [])
    state, info = ci.move_stops_to_breakeven(_rec(), MU_AVG, dry=False)
    assert state == "no_pending" and info["reason"] == "no_pending_algo"


def test_move_reports_amend_rejection(monkeypatch):
    _fake_okx(monkeypatch, [_algo_row()], amend_ok=False)
    state, info = ci.move_stops_to_breakeven(_rec(), MU_AVG, dry=False)
    assert state == "amend_failed" and info["failed"]


def test_move_uses_recorded_stop_not_the_live_one_to_avoid_ratchet(monkeypatch):
    """紀錄裡有原始止損時就用它——否則搬過一次後掛單上的值已是保本價，
    再算一次會得到更貼身的止損（棘輪），每輪收緊一次直到被掃掉。"""
    _fake_okx(monkeypatch, [_algo_row(slTriggerPx="813.57")])
    _s, info = ci.move_stops_to_breakeven(_rec(), MU_AVG, dry=False)
    first = ci.breakeven_stop_px(MU_AVG, MU_SL, "short", MU_TICK)
    assert info["px"] == pytest.approx(first)


def test_legacy_record_without_ticksz_asks_the_exchange(monkeypatch):
    """線上唯一那筆真錢倉（MU）就是這個形狀：v249 之前下的單，部位檔裡
    stop=None、tickSz=None。沒有 tickSz 就送出未對齊的價格 ⇒ 交易所每輪退件、
    保本永遠搬不成。⛔ 這不是『量不到』——離一次 instruments 查詢只有一步。"""
    def _f(args, timeout=None):
        if args[:2] == ["market", "instruments"]:
            return 0, json.dumps([{"ctVal": "1", "lotSz": "0.01", "minSz": "0.01",
                                   "tickSz": "0.01", "lever": "50"}])
        if args[:3] == ["swap", "algo", "orders"]:
            return 0, json.dumps([_algo_row()])
        if args[:3] == ["swap", "algo", "amend"]:
            px = args[args.index("--newSlTriggerPx") + 1]
            # 0.01 的整數倍才對齊；813.571 這種會被 OKX 退件
            assert abs(round(float(px) / 0.01) - float(px) / 0.01) < 1e-6, px
            return 0, '{"sCode": "0"}'
        raise AssertionError(f"未預期：{args}")

    monkeypatch.setattr(ci, "_okx", _f)
    rec = _rec(stop=None, tickSz=None)
    state, info = ci.move_stops_to_breakeven(rec, MU_AVG, dry=False)
    assert state == "done" and info["moved"] == 1
    assert rec["tickSz"] == 0.01, "問到了就補記，下輪不必再問"
    # 舊紀錄沒有 stop ⇒ 退回讀掛單上的值（還沒搬過，所以那就是原始止損）
    assert info["px"] == pytest.approx(
        ci.breakeven_stop_px(MU_AVG, MU_SL, "short", MU_TICK))


def test_ticksz_lookup_failure_still_attempts_the_move(monkeypatch):
    """問不到就退回原樣送，讓交易所當權威。未對齊只會被拒、不會改到錯的價位；
    為了一次查詢失敗就放著倉不保護，代價比較大。"""
    def _f(args, timeout=None):
        if args[:2] == ["market", "instruments"]:
            return 1, "Error: HTTP 500"
        if args[:3] == ["swap", "algo", "orders"]:
            return 0, json.dumps([_algo_row()])
        if args[:3] == ["swap", "algo", "amend"]:
            return 0, '{"sCode": "0"}'
        raise AssertionError(f"未預期：{args}")

    monkeypatch.setattr(ci, "_okx", _f)
    state, info = ci.move_stops_to_breakeven(_rec(stop=None, tickSz=None),
                                             MU_AVG, dry=False)
    assert state == "done" and info["moved"] == 1


def test_ticksz_is_not_asked_for_when_already_recorded(monkeypatch):
    def _f(args, timeout=None):
        if args[:2] == ["market", "instruments"]:
            raise AssertionError("已經記了 tickSz 還去問＝每輪多一次無謂呼叫")
        if args[:3] == ["swap", "algo", "orders"]:
            return 0, json.dumps([_algo_row()])
        return 0, '{"sCode": "0"}'

    monkeypatch.setattr(ci, "_okx", _f)
    assert ci.move_stops_to_breakeven(_rec(), MU_AVG, dry=False)[0] == "done"


def test_dry_run_never_calls_amend(monkeypatch):
    calls = _fake_okx(monkeypatch, [_algo_row()])
    state, _ = ci.move_stops_to_breakeven(_rec(), MU_AVG, dry=True)
    assert state == "done"
    assert not [c for c in calls if c[:3] == ["swap", "algo", "amend"]]


# ═══════════ ⑤ 觸發時機 ═══════════

def test_no_move_before_any_tp_leg_fills(monkeypatch):
    def _boom(args, timeout=None):
        raise AssertionError("一腿都沒成交就去改止損＝提前保本，砍掉的是還沒發生的行情")
    monkeypatch.setattr(ci, "_okx", _boom)
    rec = _rec()
    ci._ROUND_BE_GAPS.clear()
    ci.maybe_breakeven(rec, 0.72, MU_AVG, dry=False, iid_key="i1")
    assert "be" not in rec


def test_moves_once_tp1_filled(monkeypatch):
    _fake_okx(monkeypatch, [_algo_row()])
    rec = _rec()
    ci._ROUND_BE_GAPS.clear()
    ci.maybe_breakeven(rec, 0.23, MU_AVG, dry=False, iid_key="i1")
    assert rec["be"]["state"] == "done"
    assert rec["be"]["px"] < MU_AVG
    assert ci._ROUND_BE_GAPS == []


def test_done_is_sticky_so_later_rounds_do_not_recompute(monkeypatch):
    def _boom(args, timeout=None):
        raise AssertionError("已保本的倉不該每輪再去算一次（防棘輪＋防噪音）")
    monkeypatch.setattr(ci, "_okx", _boom)
    rec = _rec(be={"state": "done", "px": 813.57})
    ci.maybe_breakeven(rec, 0.23, MU_AVG, dry=False, iid_key="i1")


def test_unknown_placed_size_is_not_folded_into_no_tp_yet(monkeypatch):
    """⛔ 同物種第 N 次：下單張數沒記錄 ⇒ 判不出有沒有吃到 TP1。
    默認『沒吃到』會讓保本永遠不觸發而且完全無聲。"""
    def _boom(args, timeout=None):
        raise AssertionError("張數未知時不該真的去改單")
    monkeypatch.setattr(ci, "_okx", _boom)
    rec = _rec()
    rec.pop("contracts")
    ci._ROUND_BE_GAPS.clear()
    ci.maybe_breakeven(rec, 0.23, MU_AVG, dry=False, iid_key="i1")
    assert rec["be"]["reason"] == "placed_size_unknown"
    assert [g["reason"] for g in ci._ROUND_BE_GAPS] == ["placed_size_unknown"]


def test_failure_lands_in_round_gaps(monkeypatch):
    _fake_okx(monkeypatch, [], query_code=1)
    ci._ROUND_BE_GAPS.clear()
    rec = _rec()
    ci.maybe_breakeven(rec, 0.23, MU_AVG, dry=False, iid_key="i1")
    assert rec["be"]["state"] == "unknown"
    assert ci._ROUND_BE_GAPS[0]["reason"] == "algo_query_failed"
    assert ci._ROUND_BE_GAPS[0]["inst_id"] == "MU-USDT-SWAP"


def test_disabled_switch_is_honoured(monkeypatch):
    monkeypatch.setattr(ci, "BE_ENABLED", False)
    def _boom(args, timeout=None):
        raise AssertionError("關掉了還去改單")
    monkeypatch.setattr(ci, "_okx", _boom)
    rec = _rec()
    ci.maybe_breakeven(rec, 0.23, MU_AVG, dry=False, iid_key="i1")
    assert "be" not in rec


# ═══════════ ⑥ 健康帳：缺口要有數字，且⛔不得污染連續故障輪 ═══════════

def test_be_gap_lands_in_health():
    h = ci.update_health({}, {}, 1000.0, oks=1, be_gaps=[
        {"inst_id": "MU-USDT-SWAP", "pos_side": "short", "symbol": "MU",
         "reason": "algo_query_failed", "tries": 2, "want_px": 813.57}])
    assert h["breakeven_unmoved_total"] == 1
    assert h["breakeven_unmoved_recent"][0]["reason"] == "algo_query_failed"
    assert h["breakeven_unmoved_recent"][0]["want_px"] == 813.57


def test_be_gap_is_recorded_even_on_an_idle_round():
    """搬不動最典型的成因就是查詢失敗，而那種輪往往一次成功呼叫都沒有（oks==0）。
    記在空轉輪的提早 return 之後，唯一要記的情境就一筆都記不到（v169/v170 教訓）。"""
    h = ci.update_health({}, {}, 1000.0, oks=0, be_gaps=[{"inst_id": "X",
                                                         "reason": "algo_unreadable"}])
    assert h["breakeven_unmoved_total"] == 1
    assert h["idle_rounds"] == 1


def test_be_gap_does_not_touch_consecutive_fail_rounds():
    """⛔ v171 lev_mismatch 的坑：部位狀態每輪記成輪級故障 ⇒ consecutive_fail_rounds
    永不歸零、last_ok_ts 凍住、蓋掉其他類別，帳本把『照跑』誤報成『管線停擺』。"""
    h = ci.update_health({"consecutive_fail_rounds": 0}, {}, 1000.0, oks=1,
                         be_gaps=[{"inst_id": "X", "reason": "algo_query_failed"}])
    assert h["consecutive_fail_rounds"] == 0
    assert h.get("last_ok_ts") == 1000.0


def test_no_gap_means_no_field():
    h = ci.update_health({}, {}, 1000.0, oks=1)
    assert "breakeven_unmoved_total" not in h


# ═══════════ ⑦ 迴圈級：manage_positions 真的會叫到它 ═══════════

def test_manage_positions_moves_stop_after_partial_fill(monkeypatch, tmp_path):
    """⛔ 只測純函式不算數（v154 方法論）：這個洞的本體是『整條迴圈裡沒有任何一段
    程式碼會去搬止損』，不是某個函式算錯。"""
    pos = tmp_path / "pos.json"
    pos.write_text(json.dumps({"open": {"i1": _rec()}, "day_pnl": {}}),
                   encoding="utf-8")
    monkeypatch.setattr(ci, "POS_STATE", pos)
    amends = []

    def _f(args, timeout=None):
        if args[:2] == ["account", "positions"]:
            return 0, json.dumps([{"instId": "MU-USDT-SWAP", "posSide": "short",
                                   "pos": "0.23", "avgPx": str(MU_AVG),
                                   "lever": "20"}])
        if args[:3] == ["swap", "algo", "orders"]:
            return 0, json.dumps([_algo_row()])
        if args[:3] == ["swap", "algo", "amend"]:
            amends.append(list(args))
            return 0, '{"sCode": "0"}'
        raise AssertionError(f"未預期：{args}")

    monkeypatch.setattr(ci, "_okx", _f)
    ci._ROUND_BE_GAPS.clear()
    assert ci.manage_positions(dry=False) == []
    assert len(amends) == 1, "TP1 已成交、剩餘腿仍掛原始止損 ⇒ 整輪走完卻沒人去搬它"
    saved = json.loads(pos.read_text(encoding="utf-8"))
    assert saved["open"]["i1"]["be"]["state"] == "done"
    assert saved["open"]["i1"]["be"]["px"] < MU_AVG


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
