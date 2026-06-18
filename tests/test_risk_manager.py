"""Risk Manager 風控閘門測試（v48 新增）。

風控是「正期望值的最後一道安全網」——熔斷、總曝險、併發、同向重複、相關 family 上限。
過去這層零單元覆蓋，任何一道閘門在改版中被弄反/弄丟都不會被測出來，等於拿真倉位試錯。
本檔逐一驗證 should_block 的每道閘門「該擋的擋、該放的放」，並鎖定優先序（週熔斷 > 日熔斷 > … ）。

設計：所有外部狀態（trade_journal 的 PnL / 持倉 / 今日開倉、econ_calendar 的靜默期）
都以 monkeypatch 隔離 → 純判定邏輯測試，不碰真資料庫、不依賴真實時間。

執行（任一）：
    pytest tests/test_risk_manager.py
    python tests/test_risk_manager.py
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# --- 在 import risk_manager 前先把 econ_calendar 的 lazy import 目標換成可控 stub ---
# should_block 內部 `from news_feed.econ_calendar import in_blackout` 會走 sys.modules，
# 預設回 (False, "")=非靜默期；個別測試可改 _econ.in_blackout 模擬靜默期/故障。
_econ = types.ModuleType("news_feed.econ_calendar")
_econ.in_blackout = lambda: (False, "")
_pkg = sys.modules.get("news_feed") or types.ModuleType("news_feed")
if not hasattr(_pkg, "__path__"):
    _pkg.__path__ = []  # 讓它被當成 package，dotted import 才解析得到
sys.modules["news_feed"] = _pkg
sys.modules["news_feed.econ_calendar"] = _econ

import l3_dispatcher.risk_manager as rm


# === 測試夾具 =================================================================
def _cfg(**over):
    """固定、可預測的 RiskConfig（不吃環境/botconfig 預設，避免測試隨設定漂移）。"""
    base = dict(
        account_balance_usd=10_000.0,
        max_risk_per_trade_usd=50.0,
        max_concurrent_trades=3,
        max_per_family=2,
        total_risk_cap_pct=6.0,     # → 上限 $600
        daily_max_opens=3,
        daily_dd_limit_pct=-3.0,
        weekly_dd_limit_pct=-7.0,
    )
    base.update(over)
    return rm.RiskConfig(**base)


def _decision(symbol="ETH", direction="bull"):
    return {"snapshot": {"symbol": symbol}, "direction": direction, "setup_name": "intraday"}


def _open(symbol, direction="bull", *, id=1, risk_usd=50.0):
    return {"symbol": symbol, "direction": direction, "id": id, "risk_usd": risk_usd}


def _setup(*, week_pct=0.0, today_pct=0.0, opens=None, opened_today=0, blackout=None):
    """把 risk_manager 依賴的所有外部狀態換成可控值（monkeypatch 模組層名稱）。"""
    opens = list(opens or [])
    rm.get_week_pnl = lambda bal: {"pnl_pct_of_account": week_pct, "total_pnl_usd": 0.0}
    rm.get_today_pnl = lambda bal: {
        "pnl_pct_of_account": today_pct, "total_pnl_usd": 0.0,
        "n_trades_closed": 0, "n_wins": 0, "n_losses": 0,
    }
    rm.get_open_trades = lambda: list(opens)
    rm.count_opens_today = lambda: opened_today
    _econ.in_blackout = (lambda: blackout) if blackout is not None else (lambda: (False, ""))


# === 通過案例 ================================================================
def test_pass_through_all_clear():
    _setup()
    blocked, reason, _ = rm.should_block(_decision("AAA"), _cfg())
    assert blocked is False
    assert reason == "ok"


# === 熔斷層（最高優先）=======================================================
def test_weekly_dd_breach():
    _setup(week_pct=-7.5)
    blocked, reason, _ = rm.should_block(_decision(), _cfg())
    assert blocked is True
    assert reason == "weekly_dd_breach"


def test_weekly_priority_over_daily():
    """週與日同時破線 → 先報週熔斷（優先序鎖定，避免日內反覆）。"""
    _setup(week_pct=-8.0, today_pct=-5.0)
    blocked, reason, _ = rm.should_block(_decision(), _cfg())
    assert blocked is True
    assert reason == "weekly_dd_breach"


def test_daily_dd_breach():
    _setup(today_pct=-3.5)
    blocked, reason, _ = rm.should_block(_decision(), _cfg())
    assert blocked is True
    assert reason == "daily_dd_breach"


def test_daily_dd_boundary_exact_limit_blocks():
    """恰好等於 -3.0%（<=）也要熔斷（邊界值，常見差一錯）。"""
    _setup(today_pct=-3.0)
    blocked, reason, _ = rm.should_block(_decision(), _cfg())
    assert blocked is True
    assert reason == "daily_dd_breach"


# === 經濟數據靜默期 ==========================================================
def test_econ_blackout_blocks():
    _setup(blackout=(True, "FOMC 利率決議"))
    blocked, reason, details = rm.should_block(_decision(), _cfg())
    assert blocked is True
    assert reason == "econ_blackout"
    assert "FOMC" in details["msg"]


def test_econ_calendar_failure_does_not_block_pipeline():
    """經濟日曆故障（拋例外）絕不能擋住交易管線 → 應放行繼續後續檢查。

    且故障必須「留痕」可觀測（稽核 #2 治本：不可靜默吞例外）：
    details 須標記 econ_blackout_checked=False + 例外型別。
    """
    _setup()
    def _boom():
        raise RuntimeError("econ feed down")
    _econ.in_blackout = _boom
    blocked, reason, details = rm.should_block(_decision("AAA"), _cfg())
    assert blocked is False
    assert reason == "ok"
    # fail-open 仍成立，但要留痕：
    assert details["econ_blackout_checked"] is False
    assert details["econ_blackout_error"] == "RuntimeError"


def test_econ_calendar_ok_marks_checked():
    """經濟日曆正常運作時，details 標記 econ_blackout_checked=True（可觀測已查過）。"""
    _setup()  # in_blackout → (False, "")
    blocked, reason, details = rm.should_block(_decision("AAA"), _cfg())
    assert blocked is False
    assert reason == "ok"
    assert details["econ_blackout_checked"] is True
    assert "econ_blackout_error" not in details


# === 部位層 ==================================================================
def test_max_concurrent_exceeded():
    # 3 筆非相關、非同向重複的持倉，上限 3 → 擋（每筆 risk 50，總 150 < 上限 600，故非總曝險先擋）
    opens = [_open("AAA", id=1), _open("BBB", id=2), _open("CCC", id=3)]
    _setup(opens=opens)
    blocked, reason, _ = rm.should_block(_decision("DDD"), _cfg())
    assert blocked is True
    assert reason == "max_concurrent_exceeded"


def test_total_risk_cap_exceeded():
    # 放寬併發到 10，讓「總曝險 $ 上限」先觸發：兩筆各 $300 → open 600 + 本筆 50 = 650 > 600
    opens = [_open("AAA", id=1, risk_usd=300.0), _open("BBB", id=2, risk_usd=300.0)]
    _setup(opens=opens)
    blocked, reason, details = rm.should_block(_decision("DDD"), _cfg(max_concurrent_trades=10))
    assert blocked is True
    assert reason == "total_risk_cap_exceeded"
    assert details["risk_cap_usd"] == 600.0


def test_daily_max_opens_reached():
    # 無持倉（避開併發/總曝險），但今日已開 3 次達上限 → 擋（防情緒連續開倉）
    _setup(opened_today=3)
    blocked, reason, _ = rm.should_block(_decision("AAA"), _cfg())
    assert blocked is True
    assert reason == "daily_max_opens_reached"


def test_duplicate_symbol_direction():
    # 已有 ETH/bull 持倉，又想開 ETH/bull → 擋（同向重複先於 family 檢查）
    opens = [_open("ETH", "bull", id=7)]
    _setup(opens=opens)
    blocked, reason, details = rm.should_block(_decision("ETH", "bull"), _cfg())
    assert blocked is True
    assert reason == "duplicate_symbol_direction"
    assert details["existing_trade_id"] == 7


def test_duplicate_opposite_direction_allowed_here():
    """同幣『反向』不算 duplicate（會走到後面的閘門；此處應放行到 ok）。"""
    # 已有 ETH/bull，想開 ETH/bear：非同向重複；ETH 在 btc_family，但只有 1 筆同 family < 2 → 放行
    opens = [_open("ETH", "bull", id=7)]
    _setup(opens=opens)
    blocked, reason, _ = rm.should_block(_decision("ETH", "bear"), _cfg())
    assert blocked is False
    assert reason == "ok"


def test_family_max_exceeded():
    # btc_family=(BTC,ETH,SOL)：已有 BTC/bull + SOL/bull（2 筆），再開 ETH/bull → 同 family 滿 → 擋
    opens = [_open("BTC", "bull", id=1), _open("SOL", "bull", id=2)]
    _setup(opens=opens)
    blocked, reason, details = rm.should_block(_decision("ETH", "bull"), _cfg())
    assert blocked is True
    assert reason == "family_max_exceeded"
    assert details["family"] == "btc_family"


def test_non_family_symbol_not_limited_by_family():
    """不屬任何 family 的幣不受 family 上限影響（family=None 分支）。"""
    opens = [_open("AAA", "bull", id=1), _open("BBB", "bull", id=2)]
    _setup(opens=opens)
    blocked, reason, details = rm.should_block(_decision("CCC", "bull"), _cfg())
    assert blocked is False
    assert reason == "ok"
    assert details["family"] is None


# --- 直接執行（無 pytest 也能跑）---
if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
