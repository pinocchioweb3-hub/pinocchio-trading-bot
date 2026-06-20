"""復盤引擎 step8（task#53）── alloc-aware 自洽性測試（核心安全保證）。

驗證「進場時凍結 tp_alloc → 重算/自洽/回放全程用『該筆當初的分配』」這條一致性，
否則拿預設 champion 回放一筆以覆寫分配記帳的單會誤判竄改（false tamper flag）。

關鍵推理（timeout 腿）：tp/stop 的 leg_r 與分配無關（純價格）；但 timeout 腿出場價未存，
是從帳本 realized_r 減去非 timeout 腿後『反推』的——反推時用的分配必須＝記帳時的分配，
配對才自洽。本測試把這條 invariant 釘死，並證明用預設分配回放覆寫單會 mismatch（故修必要）。

全離線、零 DB、零網路：直接餵 dict。亦覆向後相容（tp_alloc=None → 預設＝今日行為）。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from l3_dispatcher.paper_audit import recompute_r, _resolve_splits, SPLITS
from l3_dispatcher.champion_challenger import (
    _trade_alloc, replay_trade_r, champion_alloc, _self_check, AllocPolicy, R_TOL)
from l3_dispatcher.trade_monitor import _resolve_pt_alloc


# entry=100, stop=90, sl_dist=10, bull → tp1=110→1R, tp2=120→2R, stop=90→-1R
def _mk(legs, realized_r, *, tp_alloc=None, filled=1.0):
    return {"id": 1, "symbol": "BTC", "setup": "intraday", "direction": "bull",
            "entry_price": 100.0, "stop_price": 90.0, "tp1": 110.0, "tp2": 120.0,
            "tp3": 140.0, "entry_at": 0, "exit_at": 10, "legs_hit": legs,
            "exit_reason": legs.split(",")[-1], "realized_r": realized_r,
            "pnl_usd": realized_r * 100, "entry_filled_pct": filled,
            "tp_alloc": tp_alloc}


# ════════════════════════════════════════════════════════════════════════
#  1) recompute_r 用「該筆凍結分配」重現帳本 realized_r（tp/stop 獨立檢查）
# ════════════════════════════════════════════════════════════════════════
def test_recompute_reproduces_override_realized_r():
    # 覆寫 (0.6,0.25,0.15)，hit tp1,tp2,stop：
    #   r = 0.6*1 + 0.25*2 + 0.15*(-1) = 0.95
    t = _mk("tp1,tp2,stop", 0.95, tp_alloc=[0.6, 0.25, 0.15])
    rec = recompute_r(t)
    assert rec["recomputed_r"] is not None
    assert abs(rec["recomputed_r"] - 0.95) <= R_TOL


def test_recompute_default_unchanged_when_no_override():
    # tp_alloc=None → 用預設 (0.5,0.3,0.2)：0.5*1 + 0.3*2 + 0.2*(-1) = 0.9（今日行為）
    t = _mk("tp1,tp2,stop", 0.9, tp_alloc=None)
    rec = recompute_r(t)
    assert abs(rec["recomputed_r"] - 0.9) <= R_TOL


def test_recompute_override_differs_from_default():
    """同一筆腿序，覆寫分配的重算結果應與預設不同（證明 tp_alloc 真的有被吃進去）。"""
    t_ov = _mk("tp1,tp2,stop", 0.95, tp_alloc=[0.6, 0.25, 0.15])
    t_df = _mk("tp1,tp2,stop", 0.95, tp_alloc=None)
    assert recompute_r(t_ov)["recomputed_r"] != recompute_r(t_df)["recomputed_r"]


# ════════════════════════════════════════════════════════════════════════
#  2) _self_check 不誤判覆寫單（timeout 腿——核心 false-flag 防護）
# ════════════════════════════════════════════════════════════════════════
def test_self_check_no_false_flag_on_override_timeout_trade():
    # 覆寫 (0.6,0.25,0.15)，hit tp1 然後 timeout(出場 105→leg_r=0.5)：
    #   r = 0.6*1 + 0.4*0.5 = 0.8（timeout 吃剩餘 0.4）
    t = _mk("tp1,timeout", 0.8, tp_alloc=[0.6, 0.25, 0.15])
    n_chk, n_mis, ids = _self_check([t])
    assert n_chk == 1 and n_mis == 0, f"誤判：{ids}"


def test_replay_with_trade_alloc_reproduces_but_default_would_mismatch():
    """直接證明修的必要性：用該筆分配回放＝realized_r；用預設 champion 回放則對不上。"""
    t = _mk("tp1,timeout", 0.8, tp_alloc=[0.6, 0.25, 0.15])
    # 用「該筆凍結分配」回放 → 重現帳本
    r_own = replay_trade_r(t, _trade_alloc(t))
    assert r_own is not None and abs(r_own - 0.8) <= R_TOL
    # 用「預設 champion」回放 → 顯著對不上（這正是舊 _self_check 會誤判的情形）
    r_def = replay_trade_r(t, champion_alloc())
    assert r_def is not None and abs(r_def - 0.8) > R_TOL


def test_self_check_default_trade_still_passes():
    """向後相容：tp_alloc=None 的單，_self_check 仍以預設 champion 自洽（今日行為不變）。"""
    # 預設 (0.5,0.3,0.2)，hit tp1 然後 timeout(105→0.5)：r = 0.5*1 + 0.5*0.5 = 0.75
    t = _mk("tp1,timeout", 0.75, tp_alloc=None)
    n_chk, n_mis, ids = _self_check([t])
    assert n_chk == 1 and n_mis == 0, f"誤判：{ids}"


# ════════════════════════════════════════════════════════════════════════
#  3) 三個解析器的驗證規則一致（_resolve_splits / _trade_alloc / _resolve_pt_alloc）
# ════════════════════════════════════════════════════════════════════════
def test_resolve_splits_validation():
    assert _resolve_splits({"tp_alloc": None}) == SPLITS
    assert _resolve_splits({"tp_alloc": [0.6, 0.25, 0.15]}) == {"tp1": 0.6, "tp2": 0.25, "tp3": 0.15}
    assert _resolve_splits({"tp_alloc": "[0.6, 0.25, 0.15]"}) == {"tp1": 0.6, "tp2": 0.25, "tp3": 0.15}
    assert _resolve_splits({"tp_alloc": [0.5, 0.3]}) == SPLITS          # 段數錯
    assert _resolve_splits({"tp_alloc": [0.5, 0.3, 0.5]}) == SPLITS     # 總和 1.3
    assert _resolve_splits({"tp_alloc": [-0.1, 0.6, 0.5]}) == SPLITS    # 負值
    assert _resolve_splits({"tp_alloc": "garbage"}) == SPLITS           # 壞字串
    assert _resolve_splits({}) == SPLITS


def test_trade_alloc_validation():
    assert _trade_alloc({"tp_alloc": None}).tp_alloc == champion_alloc().tp_alloc
    assert _trade_alloc({"tp_alloc": [0.6, 0.25, 0.15]}).tp_alloc == (0.6, 0.25, 0.15)
    assert _trade_alloc({"tp_alloc": "[0.6, 0.25, 0.15]"}).tp_alloc == (0.6, 0.25, 0.15)
    assert _trade_alloc({"tp_alloc": [0.5, 0.3, 0.5]}).tp_alloc == champion_alloc().tp_alloc
    assert isinstance(_trade_alloc({"tp_alloc": [0.6, 0.25, 0.15]}), AllocPolicy)


def test_resolve_pt_alloc_validation():
    assert _resolve_pt_alloc({"tp_alloc": None}) is None
    assert _resolve_pt_alloc({"tp_alloc": [0.6, 0.25, 0.15]}) == {"tp1": 0.6, "tp2": 0.25, "tp3": 0.15}
    assert _resolve_pt_alloc({"tp_alloc": "[0.6, 0.25, 0.15]"}) == {"tp1": 0.6, "tp2": 0.25, "tp3": 0.15}
    assert _resolve_pt_alloc({"tp_alloc": [0.5, 0.3]}) is None          # 段數錯
    assert _resolve_pt_alloc({"tp_alloc": [0.5, 0.3, 0.5]}) is None     # 總和 1.3
    assert _resolve_pt_alloc({"tp_alloc": "garbage"}) is None
    assert _resolve_pt_alloc({}) is None
