# -*- coding: utf-8 -*-
"""監督員 r64：模擬盤「活著」不再拿閘①旗標當事實（「用代理值當事實」同物種第三次治本）。

背景（實測，非推測）：demo_guard 閘擋住後，run_demo_operator_cycle 直接 return，
既有部位的監控/回讀同輪一併停；而 ceo_oversight 的 demo_active 只讀 is_active()＝
閘①的環境旗標，於是 oversight_ledger 連續 50+ 小時寫著 demo_active=true、demo_live=1，
而模擬盤實際上零新單、在場那筆 50 小時未對帳。使用者與每一輪監督員讀到的都是假的。

治法＝量測而非代理：
  1. run_demo_operator_cycle 每輪把「這一輪到底跑了沒、為何沒跑」寫進 demo_operator_state。
  2. ceo_oversight 讀那個實測值判 demo_active，且**未知一律不算在跑**（fail-closed，
     承接 v162-v166「未知 ≠ 確認沒有」的同一紀律，方向相反但精神一致）。

⛔ 本輪明確不做、且後輪也不該做的事：把 _monitor 移到 demo_guard 閘之前／或在閘擋時
   走「唯讀對帳」路徑。理由已在程式面確認：demo_guard.make_demo_exchange() 自己就會呼叫
   ensure_demo_env()，而目前閘失敗的原因正是「偵測到實盤金鑰已設定」——要讓對帳在閘擋時
   仍跑，唯一途徑是放寬 ensure_demo_env 的實盤金鑰檢查，那就是弱化 demo_guard（永久禁止）。
   ⇒ 對帳中斷不是可繞過的程式 bug，是 ①B（實盤金鑰與模擬盤鏡像共存與否）這個**使用者決策**
   的必然後果；程式端該做的只有「別再謊報它活著」。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from l3_dispatcher.ceo_oversight import demo_activity_verdict, next_step


# ---------------------------------------------------------------------------
# 純函式層：demo_activity_verdict
# ---------------------------------------------------------------------------
def test_flag_on_but_gate_blocked_is_not_active():
    """核心案例：旗標開著、但實測每輪都被 demo_guard 擋 → 不可報 active。"""
    v = demo_activity_verdict(True, 1000.0, "skipped:demo_guard:偵測到實盤交易金鑰已設定",
                              now_s=1030.0)
    assert v["active"] is False
    assert "demo_guard" in v["reason"]


def test_flag_on_and_cycle_ran_is_active():
    v = demo_activity_verdict(True, 1000.0, "ran", now_s=1030.0)
    assert v["active"] is True


def test_unknown_cycle_state_is_not_active():
    """未知 ≠ 確認在跑。讀不到／沒有紀錄一律 fail-closed，且理由要說「未知」不說「停了」。"""
    v = demo_activity_verdict(True, None, None, now_s=1030.0)
    assert v["active"] is False
    assert v["reason"] == "unknown"


def test_stale_cycle_state_is_not_active():
    """輪次紀錄過舊＝worker 本身沒在跑（舊快照陷阱），不可拿昨天的 ran 當今天的活著。"""
    v = demo_activity_verdict(True, 1000.0, "ran", now_s=1000.0 + 99999)
    assert v["active"] is False
    assert v["reason"] == "stale"


def test_flag_off_is_not_active():
    v = demo_activity_verdict(False, 1000.0, "ran", now_s=1030.0)
    assert v["active"] is False
    assert v["reason"] == "flag_off"


# ---------------------------------------------------------------------------
# next_step：閘擋住時不可叫使用者「去啟用它」（它已經啟用了，是被擋）
# ---------------------------------------------------------------------------
def test_next_step_blocked_does_not_tell_user_to_enable():
    s = next_step(paper_n=355, paper_min=100, live_n=0, live_min=30, demo_n=31,
                  demo_active=False,
                  demo_stall_reason="skipped:demo_guard:偵測到實盤交易金鑰已設定")
    assert "啟用 OKX 模擬盤操盤手" not in s
    assert "demo_guard" in s or "擋" in s


def test_next_step_genuinely_off_still_says_enable():
    """旗標真的沒開時，原本那句建議必須完好保留（不可被本次改動洗掉）。"""
    s = next_step(paper_n=38, paper_min=100, live_n=0, live_min=30, demo_n=0,
                  demo_active=False, demo_stall_reason="flag_off")
    assert "啟用 OKX 模擬盤操盤手" in s


# ---------------------------------------------------------------------------
# 迴圈層：run_demo_operator_cycle 真的有把每輪結果寫下來（非只純函式測）
# ---------------------------------------------------------------------------
def _run_cycle_with_stubbed_state(monkeypatch_env: dict):
    """跑一輪真的 run_demo_operator_cycle，把 state 讀寫導到記憶體，不碰正式 DB。"""
    from l3_dispatcher import demo_journal as dj
    from l3_dispatcher import demo_operator

    store: dict[str, str] = {}
    orig = (dj.init_db, dj.get_state, dj.set_state)
    dj.init_db = lambda: None
    dj.get_state = lambda k, default=None: store.get(k, default)
    dj.set_state = lambda k, v: store.__setitem__(k, str(v))
    saved_env = {k: os.environ.get(k) for k in monkeypatch_env}
    os.environ.update({k: v for k, v in monkeypatch_env.items()})
    try:
        asyncio.run(demo_operator.run_demo_operator_cycle())
    finally:
        dj.init_db, dj.get_state, dj.set_state = orig
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return store


def test_cycle_records_outcome_when_gate_blocks():
    """實盤金鑰在場 → demo_guard 擋 → 該輪必須留下可被 ceo_oversight 讀到的實測紀錄。

    ⛔ 這一輪仍然不可以下任何單——本測只斷言「有沒有誠實記帳」，不改變閘的行為。
    """
    store = _run_cycle_with_stubbed_state({
        "DEMO_OPERATOR_ACTIVE": "1",
        "OKX_TRADE_API_KEY": "dummy-real-key-present",   # 迫使 demo_guard 擋下
    })
    assert store.get("last_cycle_ts"), "每輪都必須寫下輪次時戳，否則 oversight 只能猜"
    outcome = store.get("last_cycle_outcome") or ""
    assert "demo_guard" in outcome, f"擋單原因要落地成可讀理由，實得：{outcome!r}"


def test_cycle_records_outcome_when_flag_off():
    store = _run_cycle_with_stubbed_state({"DEMO_OPERATOR_ACTIVE": "0"})
    assert store.get("last_cycle_ts")
    assert "inactive" in (store.get("last_cycle_outcome") or "")
