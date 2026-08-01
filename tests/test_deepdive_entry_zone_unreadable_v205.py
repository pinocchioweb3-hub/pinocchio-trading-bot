"""v205：deepdive 進場階梯「讀不出來」不得折成「這筆本來就是市價單」。

同物種第 25 次。這條路的下場落在**人手下真錢單**的那一步：deepdive 卡的
「📋 複製 JSON」按鈕與 /intent 後備都走 get_latest_deepdive_plan，使用者是照那份
JSON 到交易所手動下單的人。舊碼 entry_splits 解不開時 `except: entry_type="market"`，
下游 canonical_from_deepdive 再把進場區退化成單點，最後產出的 intent 與一筆正常的
市價計畫**完全同形**——「這是一段限價階梯、區間我還原不出來」的限定語整個消失。

⛔ 對照組（真的沒有 entry_splits＝真的是市價進場、以及正常可讀的階梯）必須維持原行為，
   證明修補沒有反向誤報（把「本來就沒有」標成「壞掉」）。
"""
import json
import sqlite3

import pytest

from l3_dispatcher import paper_journal as pj
from telegram_bot.plan import intent_from_deepdive_paper


def _mk_db(tmp_path, monkeypatch, splits_json):
    """建一個只有一列 deepdive 的 paper_trades，entry_splits 由參數指定。"""
    db = tmp_path / "paper.db"
    monkeypatch.setattr(pj, "_conn", lambda: sqlite3.connect(db))
    monkeypatch.setattr(pj, "init_db", lambda: None)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE paper_trades (id INTEGER PRIMARY KEY, symbol TEXT, "
        "direction TEXT, entry_price REAL, stop_price REAL, tp1 REAL, tp2 REAL, "
        "tp3 REAL, entry_at INTEGER, entry_splits TEXT, setup TEXT)")
    conn.execute(
        "INSERT INTO paper_trades (symbol, direction, entry_price, stop_price, "
        "tp1, tp2, tp3, entry_at, entry_splits, setup) VALUES "
        "('BTC','bull',100.0,90.0,110.0,120.0,130.0,1785600000000,?,'deepdive')",
        (splits_json,))
    conn.commit()
    conn.close()
    return db


# --------------------------------------------------------------------------
# 壞檔：階梯在、但讀不出來 → 不可宣稱 market
# --------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [
    "{not json",          # 根本不是 JSON
    '"just a string"',    # 合法 JSON 但迭代出字元 → AttributeError
    '{"a": 1}',           # 合法 JSON 但是 dict → 迭代出 key 字串
    '[1, 2, 3]',          # list 但元素不是 dict → AttributeError
])
def test_unreadable_splits_never_claims_market(tmp_path, monkeypatch, bad):
    _mk_db(tmp_path, monkeypatch, bad)
    plan = pj.get_latest_deepdive_plan()
    assert plan is not None, "計畫是存在的，不可回 None（那是『沒有這筆』的意思）"
    # ⛔ 行為性斷言排在最前面：舊碼在這裡就要炸，且訊息本身就是罪證
    #    （否則先斷言新欄位會得到 KeyError＝虛設檢定，看不出舊碼到底錯在哪）。
    assert plan["entry_type"] != "market", (
        f"讀不出來被折成市價單：entry_type={plan['entry_type']!r}（bad={bad!r}）")
    assert plan.get("entry_zone_status") == "unreadable"
    assert plan.get("actionable") is False, "區間不可還原的計畫不是可執行計畫"


def test_readable_but_no_usable_price_is_also_unreadable(tmp_path, monkeypatch):
    """階梯解得開、但一個 price 都取不到：區間同樣不可還原，不可當市價單。"""
    _mk_db(tmp_path, monkeypatch, json.dumps([{"price": None}, {"qty": 1}]))
    plan = pj.get_latest_deepdive_plan()
    assert plan["entry_type"] != "market", (
        f"讀不出來被折成市價單：entry_type={plan['entry_type']!r}")
    assert plan.get("entry_zone_status") == "unreadable"
    assert plan.get("actionable") is False


def test_unreadable_plan_refuses_to_become_intent(tmp_path, monkeypatch):
    """第二道防線：就算有呼叫端漏擋，也不得編出可執行 intent。"""
    _mk_db(tmp_path, monkeypatch, "{not json")
    plan = pj.get_latest_deepdive_plan()
    with pytest.raises(ValueError) as ei:
        intent_from_deepdive_paper(plan, asset_class="crypto")
    assert "不等於它是市價單" in str(ei.value)


def test_unreadable_reason_is_surfaced_not_swallowed(tmp_path, monkeypatch):
    """壞檔原因要留得下來（fail-loud），不可靜音。"""
    _mk_db(tmp_path, monkeypatch, "{not json")
    plan = pj.get_latest_deepdive_plan()
    assert plan["entry_zone_error"], "讀失敗原因被吞掉了"


# --------------------------------------------------------------------------
# 對照組：既有正確行為不可被改壞
# --------------------------------------------------------------------------
def test_absent_splits_is_genuinely_market(tmp_path, monkeypatch):
    """entry_splits 本來就空＝真的是市價進場，by-design，必須維持 actionable。"""
    _mk_db(tmp_path, monkeypatch, None)
    plan = pj.get_latest_deepdive_plan()
    assert plan["entry_type"] == "market"
    assert plan["entry_zone_status"] == "ok"
    assert plan["actionable"] is True
    assert plan["entry_lo"] is None and plan["entry_hi"] is None


def test_good_splits_still_reconstructs_limit_zone(tmp_path, monkeypatch):
    """正常可讀的階梯：區間照還原，entry_type 仍是 limit。"""
    _mk_db(tmp_path, monkeypatch,
           json.dumps([{"price": 99.0}, {"price": 101.0}, {"price": 100.0}]))
    plan = pj.get_latest_deepdive_plan()
    assert plan["entry_type"] == "limit"
    assert plan["entry_zone_status"] == "ok"
    assert plan["actionable"] is True
    assert (plan["entry_lo"], plan["entry_hi"]) == (99.0, 101.0)


def test_good_plan_still_builds_intent(tmp_path, monkeypatch):
    """對照組：正常計畫仍編得出 intent（證明沒有把好檔一起擋掉）。"""
    _mk_db(tmp_path, monkeypatch, json.dumps([{"price": 99.0}, {"price": 101.0}]))
    plan = pj.get_latest_deepdive_plan()
    intent = intent_from_deepdive_paper(plan, asset_class="crypto")
    assert intent, "正常計畫被誤擋了"
