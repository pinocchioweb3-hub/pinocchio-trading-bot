"""task#10(2d) deepdive 卡「📋 複製 JSON」紙上來源 intent 路徑測試。

修的真實缺口：deepdive 是目前唯一活躍的使用者卡，但其「機器可讀 JSON」在此之前是死的——
    舊路（intent:fire_id）走 get_signal_for_intent，只讀真錢帳本 `trades`（恆空，deepdive
    只寫 paper_trades）。於是 deepdive 卡上既無按鈕、/intent 也產不出 JSON。
    本層新增 paper_trades 來源路：
        paper_journal.get_latest_deepdive_plan → plan.intent_from_deepdive_paper
                                               → intent_format.validate_intent

⛔ 紅線①：deepdive→JSON 永遠只讀模擬盤 paper_trades，絕不碰真錢帳本 trades。
   （本檔每個用到帳本的測試都把 pj.DB_PATH 指向臨時檔，與正式帳完全隔離。）

執行方式：
    pytest tests/test_deepdive_intent.py

驗證重點：
    1. 限價（split_mode）deepdive 紙上單 → get_latest_deepdive_plan 還原 limit 形狀
       （entry_type='limit'、entry_lo/hi=splits 價 min/max、entry=存入中點）。
    2. 市價（非 split）deepdive 紙上單 → 還原 market 形狀（entry_lo/hi=None）。
    3. 無 deepdive 列 → 回 None（呼叫端安全降級）。
    4. symbol/direction 過濾 + 取最新（id DESC LIMIT 1）。
    5. 非 deepdive setup（如 us_breakout）不會被誤撈。
    6. intent_from_deepdive_paper（crypto_perp）產出能通過 validate_intent 的 intent，
       且 symbol_canonical 帶 -USDT、margin_mode='isolated'。
    7. equity_signal → symbol_canonical 不加 -USDT、margin_mode=None。
    8. _intent_asset_class 對照：🇺🇸→equity_signal、🪙/🥇→crypto_perp。
    9. ⛔execution_policy 永遠是 human_gated（紅線①：永不自動下實盤）。
   10. 紅線①回歸：intent 路徑原始碼不觸真錢帳本（trades / record_entry / trade_journal）。
"""
from __future__ import annotations

import inspect
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import l3_dispatcher.paper_journal as pj

# 把 DB 指到臨時檔，與正式紙上帳完全隔離（import 後改 module 全域，_conn 每次讀全域）
_TEST_DB = Path(tempfile.mkdtemp(prefix="deepdive_intent_test_")) / "trade_journal_test.db"
pj.DB_PATH = _TEST_DB


def _fresh():
    """清空『目前 pj.DB_PATH 指向』的測試 DB，確保每個案例獨立。

    ⚠️ 全套件隔離：其他測試模組（test_plan_snapshot / test_integration_l1_l3 /
       test_post_close_cooldown）也在 import 時改寫 pj.DB_PATH，最後 import 者（字母序
       post_close）勝出 → full suite 執行期全域指向它的臨時檔。本檔沿用 test_plan_snapshot
       的『自洽』作法：清檔與讀寫都認同一個全域 pj.DB_PATH——不論全域被指到誰都正確、
       且**不改動全域指標**，故不會汙染後跑模組（post_close 的 _fresh 認自己的常數，若被
       我們改了全域它會誤清）。單獨跑本檔時全域＝本檔 _TEST_DB，行為相同。
       （早期版本用模組常數 _TEST_DB 清檔、卻用全域讀寫 → 不一致，full suite 下誤清別檔、
        讀到別模組殘列，曾讓 get_latest_deepdive_plan 不回 None。）"""
    db = str(pj.DB_PATH)
    for suffix in ("", "-wal", "-shm"):
        p = Path(db + suffix)
        if p.exists():
            p.unlink()
    pj.init_db()


# ── 1. 限價（split_mode）round-trip ────────────────────────────────────────────
def test_get_latest_limit_roundtrip():
    _fresh()
    # bull limit：zone 99~101、entry 中點 100、stop 95、tp 110/120/130
    pid = pj.record_paper_entry(
        "BTC", "deepdive", "bull", 100.0, 95.0, 110.0, 120.0, 130.0,
        zone_lo=99.0, zone_hi=101.0, split_mode=True)
    assert pid > 0
    plan = pj.get_latest_deepdive_plan(symbol="BTC", direction="bull")
    assert plan is not None
    assert plan["actionable"] is True
    assert plan["symbol"] == "BTC"
    assert plan["direction"] == "bull"
    assert plan["entry_type"] == "limit"
    assert plan["entry"] == 100.0
    # splits bull=[zone_hi, zone_lo]=[101,99]；還原 lo=min、hi=max
    assert plan["entry_lo"] == 99.0
    assert plan["entry_hi"] == 101.0
    assert plan["stop"] == 95.0
    assert (plan["tp1"], plan["tp2"], plan["tp3"]) == (110.0, 120.0, 130.0)


# ── 2. 市價（非 split）round-trip ──────────────────────────────────────────────
def test_get_latest_market_roundtrip():
    _fresh()
    pid = pj.record_paper_entry(
        "ETH", "deepdive", "bear", 2000.0, 2100.0, 1900.0, 1800.0, 1700.0)
    assert pid > 0
    plan = pj.get_latest_deepdive_plan(symbol="ETH")
    assert plan is not None
    assert plan["entry_type"] == "market"
    assert plan["entry"] == 2000.0
    assert plan["entry_lo"] is None
    assert plan["entry_hi"] is None
    assert plan["direction"] == "bear"


# ── 3. 無 deepdive 列 → None ───────────────────────────────────────────────────
def test_get_latest_none_when_empty():
    _fresh()
    assert pj.get_latest_deepdive_plan() is None
    assert pj.get_latest_deepdive_plan(symbol="BTC", direction="bull") is None


# ── 4. symbol/direction 過濾 + 取最新 ─────────────────────────────────────────
def test_get_latest_filters_and_recency():
    _fresh()
    pj.record_paper_entry("BTC", "deepdive", "bull", 100.0, 95.0, 110.0, 120.0, 130.0)
    pj.record_paper_entry("BTC", "deepdive", "bear", 100.0, 105.0, 90.0, 80.0, 70.0)
    pj.record_paper_entry("SOL", "deepdive", "bull", 50.0, 48.0, 55.0, 60.0, 65.0)
    # 同幣再來一筆 bull（更新）→ 應撈到這筆（id 最大）
    pj.record_paper_entry("BTC", "deepdive", "bull", 101.0, 96.0, 111.0, 121.0, 131.0)

    p = pj.get_latest_deepdive_plan(symbol="BTC", direction="bull")
    assert p is not None and p["entry"] == 101.0 and p["stop"] == 96.0
    p2 = pj.get_latest_deepdive_plan(symbol="BTC", direction="bear")
    assert p2 is not None and p2["direction"] == "bear" and p2["entry"] == 100.0
    p3 = pj.get_latest_deepdive_plan(symbol="SOL")
    assert p3 is not None and p3["symbol"] == "SOL"
    # 無此方向
    assert pj.get_latest_deepdive_plan(symbol="SOL", direction="bear") is None


# ── 5. 非 deepdive setup 不被誤撈 ─────────────────────────────────────────────
def test_get_latest_ignores_non_deepdive_setup():
    _fresh()
    pj.record_paper_entry("AAPL", "us_breakout", "bull", 200.0, 195.0, 210.0, 220.0, 230.0)
    assert pj.get_latest_deepdive_plan(symbol="AAPL") is None
    assert pj.get_latest_deepdive_plan() is None


# ── 6. crypto_perp intent 通過 validate_intent ────────────────────────────────
def test_intent_crypto_perp_validates():
    _fresh()
    from telegram_bot.intent_format import validate_intent
    from telegram_bot.plan import intent_from_deepdive_paper

    pj.record_paper_entry(
        "BTC", "deepdive", "bull", 100.0, 95.0, 110.0, 120.0, 130.0,
        zone_lo=99.0, zone_hi=101.0, split_mode=True)
    plan = pj.get_latest_deepdive_plan(symbol="BTC", direction="bull")
    intent = intent_from_deepdive_paper(plan, asset_class="crypto_perp")

    assert validate_intent(intent) == []
    assert intent["asset_class"] == "crypto_perp"
    assert intent["symbol_canonical"] == "BTC-USDT"
    assert intent["margin_mode"] == "isolated"
    assert intent["side"] == "long"
    assert intent["entry_zone"]["low"] == 99.0
    assert intent["entry_zone"]["high"] == 101.0
    assert intent["invalidation"]["price"] == 95.0
    # R 倍數由絕對價反推：(110-100)/5=2.0 ...
    rs = [tp["r_multiple"] for tp in intent["take_profits"]]
    assert rs == [2.0, 4.0, 6.0]


# ── 7. equity_signal → 不加 -USDT、margin_mode None ───────────────────────────
def test_intent_equity_signal_shape():
    _fresh()
    from telegram_bot.intent_format import validate_intent
    from telegram_bot.plan import intent_from_deepdive_paper

    pj.record_paper_entry(
        "AAPL", "deepdive", "bull", 200.0, 195.0, 210.0, 220.0, 230.0)
    plan = pj.get_latest_deepdive_plan(symbol="AAPL")
    intent = intent_from_deepdive_paper(plan, asset_class="equity_signal")

    assert validate_intent(intent) == []
    assert intent["asset_class"] == "equity_signal"
    assert intent["symbol_canonical"] == "AAPL"   # 不加 -USDT
    assert intent["margin_mode"] is None


# ── 8. _intent_asset_class 對照 ───────────────────────────────────────────────
def test_intent_asset_class_mapping():
    from telegram_bot.callbacks import _intent_asset_class
    assert _intent_asset_class("AAPL") == "equity_signal"   # 🇺🇸 美股白名單
    assert _intent_asset_class("NVDA") == "equity_signal"   # 🇺🇸
    assert _intent_asset_class("BTC") == "crypto_perp"      # 🪙 加密
    assert _intent_asset_class("XAU") == "crypto_perp"      # 🥇 商品（掃描器為 -USDT 永續）


# ── 9. 紅線①：execution_policy 永遠 human_gated ───────────────────────────────
def test_execution_policy_human_gated():
    _fresh()
    from telegram_bot.plan import intent_from_deepdive_paper
    pj.record_paper_entry("BTC", "deepdive", "bull", 100.0, 95.0, 110.0, 120.0, 130.0)
    plan = pj.get_latest_deepdive_plan(symbol="BTC")
    intent = intent_from_deepdive_paper(plan, asset_class="crypto_perp")
    assert intent["execution_policy"]["mode"] == "human_gated"

    # 顯式傳入非法 execution_policy（auto_live）必被 raise（紅線：永不自動下實盤）
    import pytest
    with pytest.raises(ValueError):
        intent_from_deepdive_paper(plan, asset_class="crypto_perp",
                                   execution_policy="auto_live")


# ── 10. 紅線①回歸：可執行碼不觸真錢帳本 ──────────────────────────────────────
def _code_without_doc(fn) -> str:
    """回傳函式『去掉 docstring 與註解』後的可執行碼（ast.unparse）。
    目的：紅線檢查只看真正會跑的碼，docstring 裡解釋『舊路為何是死的』而提到
    get_signal_for_intent / trades 是合法說明，不該誤判。"""
    import ast
    import textwrap
    src = textwrap.dedent(inspect.getsource(fn))
    node = ast.parse(src).body[0]
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)):
        node.body = node.body[1:]          # 移除 docstring
    return ast.unparse(node)               # ast.unparse 也會去掉所有註解


def test_deepdive_intent_path_never_touches_real_money_ledger():
    """get_latest_deepdive_plan 與 intent_from_deepdive_paper 的可執行碼裡，
    不得出現任何寫/讀真錢帳本 trades 的痕跡（紅線①）。"""
    from telegram_bot import plan as plan_mod
    code_pj = _code_without_doc(pj.get_latest_deepdive_plan)
    code_plan = _code_without_doc(plan_mod.intent_from_deepdive_paper)
    for needle in ("INSERT INTO trades", "record_entry", "trade_journal",
                   "get_signal_for_intent"):
        assert needle not in code_pj, f"get_latest_deepdive_plan 不應含 {needle!r}"
        assert needle not in code_plan, f"intent_from_deepdive_paper 不應含 {needle!r}"
    # get_latest_deepdive_plan 必須只 SELECT paper_trades（不得讀真錢 trades 表）
    assert "paper_trades" in code_pj
    assert "FROM trades" not in code_pj


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
