# -*- coding: utf-8 -*-
"""熔斷口徑漏記必須進監督帳本（v170・監督員 r68）。

健康檔多一個欄位，只是把「只存在於 log」搬成「只存在於一個沒人開的 json」。
要真的有出口，必須進 oversight_ledger 的 blockers——那是使用者唯一會看到的面板。

⛔ 與 live_exec_verdict 的**刻意差異**（本檔的存在理由）：
live_exec_verdict 有新鮮度閘，因為 consecutive_fail_rounds 是「現況量」——健康檔
太舊代表執行器沒在跑，拿昨天的 streak 當今天的阻塞是舊快照陷阱。
但 pnl_unaccounted_total 是**累計的既成事實**：漏掉的損益不會因為執行器停了就補
回來。對它套新鮮度閘＝執行器一停、真實且不可逆的漏記就從帳本上消失，那正是
v162-v167 一路在修的「把已知壓回未知」。所以這裡不擋，改成標注「截至」時間。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from l3_dispatcher.ceo_oversight import (  # noqa: E402
    assess, live_exec_verdict, pnl_gap_verdict,
)

NOW_S = 1_785_000_000.0


def _h(**kw):
    h = {"pnl_unaccounted_total": 2, "updated_at": NOW_S - 60,
         "pnl_unaccounted_recent": [
             {"inst_id": "SOXL-USDT-SWAP", "pos_side": "long",
              "placed_at": NOW_S - 7200, "ts": NOW_S - 120}]}
    h.update(kw)
    return h


def test_gap_becomes_a_verdict_with_an_actionable_text():
    v = pnl_gap_verdict(_h(), now_s=NOW_S)
    assert v and v["count"] == 2
    assert "熔斷" in v["text"], "要講清楚後果落在風險上限上，不是一般觀測噪音"
    assert "SOXL-USDT-SWAP" in v["text"], "要指名 instId，人才查得回那筆金額"


def test_no_gap_means_no_verdict():
    assert pnl_gap_verdict(_h(pnl_unaccounted_total=0), now_s=NOW_S) is None
    assert pnl_gap_verdict({}, now_s=NOW_S) is None
    assert pnl_gap_verdict(None, now_s=NOW_S) is None


def test_a_stale_health_file_still_reports_the_gap():
    """⛔ 本檔最重要的一條：既成事實不因執行器停擺而消失（見檔頭）。"""
    stale = _h(updated_at=NOW_S - 86400)
    v = pnl_gap_verdict(stale, now_s=NOW_S)
    assert v and v["count"] == 2
    assert "截至" in v["text"], "太舊要標注量測時點，但不可整條消失"
    # 對照組：同一份健康檔對 live_exec_verdict 就該被新鮮度閘擋掉
    assert live_exec_verdict({"consecutive_fail_rounds": 99,
                              "updated_at": NOW_S - 86400}, now_s=NOW_S) is None


def test_garbage_values_do_not_crash_or_invent_a_gap():
    for bad in ("x", None, [], {"a": 1}):
        assert pnl_gap_verdict(_h(pnl_unaccounted_total=bad), now_s=NOW_S) is None
    v = pnl_gap_verdict(_h(pnl_unaccounted_recent="not-a-list"), now_s=NOW_S)
    assert v and v["count"] == 2, "明細壞掉只該讓明細從缺，不可害死整條判定"


def _assess(**kw):
    base = dict(now_ms=NOW_S * 1000, commit_age_sec=60, paper_n=359, paper_min=100,
                live_n=0, live_min=30, demo_n=31, demo_live=1, demo_active=True,
                open_decisions=0, pending_outbox=0)
    base.update(kw)
    return assess(**base)


def test_gap_reaches_the_ledger_blockers():
    """只有人能去 OKX 把那筆金額查回來、也只有人能決定要不要在低估的熔斷下繼續跑
    ⇒ 歸 blockers（球在使用者），比照 live_exec 的 user_actionable 分流。"""
    a = _assess(pnl_gap=pnl_gap_verdict(_h(), now_s=NOW_S))
    assert any("熔斷" in b for b in a["blockers"])
    assert a["state"] == "BLOCKED_ON_USER"


def test_no_gap_leaves_the_ledger_untouched():
    a = _assess(pnl_gap=None)
    assert a["blockers"] == []
