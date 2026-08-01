# -*- coding: utf-8 -*-
"""r85/v191：真錢健康檔「存在但讀不出來」不可折成「沒故障」（同物種第 11 次）。

盤點怎麼找到的
--------------
r83 交辦「換檔案族群：l3_dispatcher 的部位/帳本讀取點，先唯讀盤點」。用 AST 掃全
目錄「except 區塊直接 return 空容器」的寫法，22 處候選逐一判讀，只有這一處落在
**真錢**路徑上：

    def _read_live_exec_health() -> dict:
        try:  ... json.load ...
        except Exception:
            return {}          # ← 檔不存在、與檔壞掉，折成同一個答案

而 `live_exec_verdict` / `live_stall_verdict` / `pnl_gap_verdict` 三個判定的第一行
都是 `if not health: return None`。於是健康檔一旦壞掉（半截檔、權限、編碼、被寫成
list），帳本上真錢那一欄會是**整片空白**——與「一切正常」長得一模一樣，而真相是
「我們根本不知道消費器在不在跑」。這正是 v162–v167、v188–v190 一路在修的同一物種。

⛔ 反方向的護欄（本檔一併釘住）
-------------------------------
檔案**不存在**仍必須回 {}／None——那是「未知」（可能根本還沒部署過執行器），憑空
報停擺會變成慢性假警報。修的是「檔在、讀不出來」，不是「沒有檔」。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from l3_dispatcher import ceo_oversight as co  # noqa: E402

NOW = 1_785_000_000.0


def _read_with(tmp_path, name, content):
    p = tmp_path / name
    if content is not None:
        p.write_text(content, encoding="utf-8")
    old = co.LIVE_HEALTH_PATH
    co.LIVE_HEALTH_PATH = p
    try:
        return co._read_live_exec_health()
    finally:
        co.LIVE_HEALTH_PATH = old


def test_missing_file_still_unknown(tmp_path):
    """反向護欄：檔案不存在＝未知，回空 dict、不帶 _read_error。"""
    got = _read_with(tmp_path, "nope.json", None)
    assert got == {}
    assert not got.get("_read_error")


def test_corrupt_json_is_a_fault_not_silence(tmp_path):
    """半截檔／壞 JSON：必須留下 _read_error，不可折成 {}。"""
    got = _read_with(tmp_path, "h.json", '{"consecutive_fail_rounds": 12')
    assert got.get("_read_error"), "壞檔被折成「沒故障」——同物種第 11 次未修"


def test_json_but_not_a_dict(tmp_path):
    """合法 JSON 但不是 dict（例如被寫成 list）：舊碼會把 list 原封不動傳下去，
    下游 .get 直接爆，再被 build_snapshot 的 except 吞成三個 None。"""
    got = _read_with(tmp_path, "h.json", "[1, 2, 3]")
    assert isinstance(got, dict)
    assert got.get("_read_error")


def test_stall_verdict_names_the_real_reason():
    """讀不出來要走**專屬**分支，不可借用「沒有可信更新時間戳」那句。"""
    d = co.live_stall_verdict({"_read_error": "JSONDecodeError"}, now_s=NOW)
    assert d is not None
    assert d["last_cls"] == "health_unreadable"
    assert "讀不出來" in d["text"]


def test_stall_verdict_read_error_goes_to_system_faults():
    """歸屬：檔壞掉不是使用者能處理的事 ⇒ user_actionable=False ⇒ system_faults。"""
    d = co.live_stall_verdict({"_read_error": "PermissionError"}, now_s=NOW)
    assert d["user_actionable"] is False
    snap = co.assess(
        now_ms=NOW * 1000, commit_age_sec=60, paper_n=360, paper_min=100,
        live_n=0, live_min=30, demo_n=31, demo_live=1, demo_active=True,
        open_decisions=0, pending_outbox=0, last_nudge_ms=0,
        live_stall=d,
    )
    assert any("讀不出來" in s for s in snap["system_faults"])
    assert not any("讀不出來" in b for b in snap["blockers"])


def test_missing_file_never_reports_stall():
    """反向護欄第二道：空 dict 一路到判定仍是 None。"""
    assert co.live_stall_verdict({}, now_s=NOW) is None
    assert co.live_exec_verdict({}, now_s=NOW) is None
