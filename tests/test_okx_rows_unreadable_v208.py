# -*- coding: utf-8 -*-
"""交易所回應「讀不出任何一筆」不可折成「交易所上確認沒有」（v208・監督員 r103）。

同物種第 28 次。前 27 次修的都是**本地檔**的讀取端（intent、決策快照、部位帳、
健康檔、已處理清單、已確認手動倉…），這一次落在**交易所回應**的解析端——也是
真錢路徑上最後一處還在做「未知→折成確認」的地方。

舊碼兩處同形（consume_intents.py）：

    plist = json.loads(out)
    plist = plist if isinstance(plist, list) else plist.get("data", [])   # ← 這裡

`.get("data", [])` 對「認得的空清單」與「根本不認得的形狀」給同一個答案：空清單。
於是只要 CLI 換版、換包裝鍵、或回一個 {"code":..,"msg":..} 的錯誤信封，就會得到：

  ① account positions 這一路 → 空清單＝「交易所上確認沒有任何倉」。後果有兩層，
     兩層都落在真錢上：
       (a) 孤兒偵測回 []＝**確認沒有孤兒** ⇒ 目前唯一那條真錢阻塞會被無聲解除，
           擋同幣同向新單的閘一起消失（正是 v162/r53 花一整輪堵回來的那個洞，
           只是這次的入口不是「查詢失敗」而是「查詢成功但看不懂」）；
       (b) 本地帳上每一筆在場倉都會被判定為「交易所上已消失＝已了結」⇒ 整批走
           結算路徑、被移出部位帳。一次看不懂的回應，換來一次全倉假平倉。
  ② swap fills 這一路 → 空清單＝總和 0.0 ⇒ `_realized_pnl_since` 回 (0.0, None)，
     一個**看起來成功**的答案。那筆真錢的已實現損益就以 0.00 記進 day_pnl，而
     day_pnl 是日/週熔斷唯一的輸入 ⇒ 熔斷低估虧損。更糟的是它繞過 v170（r68）
     才補上的重試：重試只在回 None（查詢失敗）時啟動，回 (0.0, None) 不會。

⛔ 關鍵不變式（本檔的存在理由）：
  ① 認得的形狀（裸清單／{"data": [...]}）行為一字不變——包含 {"data": []} 必須
     仍是「**確認**沒有」，⛔ 不可為了保險把它一起打成未知，那會讓正常的空倉輪
     每輪都擋單、變成慢性假警報（v162 三態的整個重點就在這條線上）。
  ② 認不得的形狀一律回 None＝未知，走既有的「未知就不對帳、不平倉、不接新單」
     分支，並留下一個故障類別（不可只 print）。
  ③ 安全預設只能往嚴格的那一邊倒：未知時寧可少做，絕不多平一筆倉、絕不放行
     一張被擋的新單。
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "atk_consumer"))

import consume_intents as ci  # noqa: E402

CLS = "exchange_rows_unreadable"

# 認不得的形狀：錯誤信封／換了包裝鍵／data 不是清單／整份不是物件
UNREADABLE = (
    {"code": "0", "msg": "ok"},                 # 有回應、就是沒有 data 鍵
    {"code": "51001", "msg": "instrument not found"},
    {"result": [{"instId": "BTC-USDT-SWAP"}]},  # 換了包裝鍵
    {"data": None},                             # data 是 null
    {"data": {"instId": "BTC-USDT-SWAP"}},      # data 是物件不是清單
    "just a string",
    123,
)


def _arm(tmp_path, monkeypatch, positions_payload, open_recs=None, fills_payload=None):
    """跑一輪 manage_positions。positions_payload 直接當 CLI stdout 的 JSON 內容。"""
    (tmp_path / "pos.json").write_text(
        json.dumps({"open": open_recs or {}, "day_pnl": {}}), encoding="utf-8")
    monkeypatch.setattr(ci, "POS_STATE", tmp_path / "pos.json")
    monkeypatch.setattr(ci, "ACKED_POS", tmp_path / "no-ack.json")

    def fake_okx(args, timeout=30):
        if args[:2] == ["account", "positions"]:
            return 0, json.dumps(positions_payload)
        if args[:2] == ["swap", "fills"]:
            return 0, json.dumps(fills_payload if fills_payload is not None else [])
        return 1, "unexpected"

    monkeypatch.setattr(ci, "_okx", fake_okx)
    ci._ROUND_FAILS.clear()
    res = ci.manage_positions(dry=False)
    saved = json.loads((tmp_path / "pos.json").read_text(encoding="utf-8"))
    return res, saved, dict(ci._ROUND_FAILS)


def _open_rec():
    return {"i-1": {"inst_id": "BTC-USDT-SWAP", "pos_side": "long", "symbol": "BTC",
                    "contracts": 1.0, "lev": 5, "placed_at": time.time() - 600}}


# ── 純函式：三態解析 ────────────────────────────────────────────────
@pytest.mark.parametrize("raw", UNREADABLE)
def test_unreadable_shapes_parse_to_unknown_not_empty(raw):
    assert ci.parse_okx_rows(raw) is None, f"{raw!r} 認不得就必須是未知，不可是空清單"


def test_recognised_shapes_parse_to_their_rows():
    rows = [{"instId": "BTC-USDT-SWAP"}]
    assert ci.parse_okx_rows(rows) == rows              # 裸清單
    assert ci.parse_okx_rows({"data": rows}) == rows    # 標準信封


def test_recognised_empty_stays_a_confirmed_empty_list():
    """⛔ 這條是本次修補的邊界線：確認沒有 ≠ 未知。"""
    assert ci.parse_okx_rows([]) == []
    assert ci.parse_okx_rows({"data": []}) == []


# ── 迴圈級①：孤兒閘（唯一那條真錢阻塞的解除機制） ──────────────────
@pytest.mark.parametrize("raw", UNREADABLE)
def test_unreadable_positions_never_read_as_confirmed_no_orphans(raw, tmp_path, monkeypatch):
    """回 [] 代表「確認沒有孤兒」⇒ 擋單閘消失。認不得就必須回 None。"""
    res, _, _ = _arm(tmp_path, monkeypatch, raw)
    assert res is None, "看不懂的回應被折成了「交易所上確認沒有倉」"


def test_unreadable_positions_are_accounted_as_a_failure_class(tmp_path, monkeypatch):
    """只 print 不記帳＝沒有存在（同物種前 27 次的共同結論）。"""
    _, _, fails = _arm(tmp_path, monkeypatch, {"code": "0", "msg": "ok"})
    assert CLS in fails, "無聲：這一輪什麼故障都沒記到"
    assert fails[CLS].strip(), "故障樣本不可是空字串"


def test_unreadable_positions_never_settle_open_positions(tmp_path, monkeypatch):
    """最重的一層：一次看不懂的回應，不可換來一次全倉假平倉。"""
    _, saved, _ = _arm(tmp_path, monkeypatch, {"code": "0", "msg": "ok"},
                       open_recs=_open_rec())
    assert "i-1" in saved["open"], "在場倉被當成『交易所上已消失』移出了部位帳"
    assert not saved["day_pnl"], "還把一筆憑空的損益記進了熔斷口徑"


def test_null_data_with_open_positions_fails_closed_not_crashes(tmp_path, monkeypatch):
    """{"data": null} 在舊碼會逃出 try 直接 TypeError（非受控中止）。"""
    res, saved, _ = _arm(tmp_path, monkeypatch, {"data": None}, open_recs=_open_rec())
    assert res is None
    assert "i-1" in saved["open"]


# ── 迴圈級②：認得的形狀行為一字不變（守住「不誤報」） ────────────
def test_bare_list_still_detects_the_orphan(tmp_path, monkeypatch):
    res, _, fails = _arm(tmp_path, monkeypatch,
                         [{"instId": "WLFI-USDT-SWAP", "posSide": "long", "pos": "11618"}])
    assert res == [("WLFI-USDT-SWAP", "long")]
    assert "orphan_position" in fails
    assert CLS not in fails


def test_data_envelope_still_detects_the_orphan(tmp_path, monkeypatch):
    res, _, fails = _arm(tmp_path, monkeypatch,
                         {"code": "0", "data": [{"instId": "WLFI-USDT-SWAP",
                                                 "posSide": "long", "pos": "11618"}]})
    assert res == [("WLFI-USDT-SWAP", "long")]
    assert CLS not in fails


def test_confirmed_empty_stays_confirmed_empty(tmp_path, monkeypatch):
    """空倉輪必須仍回 []（確認乾淨）且不記故障——否則每輪擋單＝慢性假警報。"""
    for payload in ([], {"code": "0", "data": []}):
        res, _, fails = _arm(tmp_path, monkeypatch, payload)
        assert res == [], f"{payload!r} 是確認沒有，不可被打成未知"
        assert CLS not in fails


# ── 迴圈級③：已實現損益（日/週熔斷唯一的輸入） ────────────────────
@pytest.mark.parametrize("raw", UNREADABLE)
def test_unreadable_fills_never_read_as_zero_realized_pnl(raw, tmp_path, monkeypatch):
    """回 (0.0, None) 是「看起來成功」的答案：記 0 進熔斷口徑，還繞過 v170 重試。"""
    monkeypatch.setattr(ci, "_okx", lambda args, timeout=30: (0, json.dumps(raw)))
    assert ci._realized_pnl_since("BTC-USDT-SWAP", 0.0) is None


def test_readable_fills_still_sum_normally(monkeypatch):
    fills = [{"ts": "2000000", "fillPnl": "3", "fee": "-1"},
             {"ts": "1000", "fillPnl": "99", "fee": "0"}]      # 早於 since，不計
    monkeypatch.setattr(ci, "_okx", lambda args, timeout=30: (0, json.dumps(fills)))
    total, last_ts = ci._realized_pnl_since("BTC-USDT-SWAP", 100.0)
    assert total == pytest.approx(2.0)
    assert last_ts == pytest.approx(2000.0)
    monkeypatch.setattr(ci, "_okx", lambda args, timeout=30: (0, json.dumps({"data": fills})))
    assert ci._realized_pnl_since("BTC-USDT-SWAP", 100.0)[0] == pytest.approx(2.0)


def test_confirmed_no_fills_still_means_zero_not_unknown(monkeypatch):
    """認得的空清單＝確認沒有成交 ⇒ 維持 (0.0, None)，⛔ 不可一起打成未知。"""
    for payload in ([], {"code": "0", "data": []}):
        monkeypatch.setattr(ci, "_okx", lambda args, timeout=30, p=payload: (0, json.dumps(p)))
        res = ci._realized_pnl_since("BTC-USDT-SWAP", 100.0)
        assert res is not None and res[0] == pytest.approx(0.0)
