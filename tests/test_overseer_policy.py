"""task #51：監督員治理策略層測試（純單元、零 IO、零網路）。

最關鍵的一條：CI 證明「呼叫任一下單類工具必被框架層拒絕」——這是紅線①在
監督員 Layer 2 的程式化護欄。其餘覆蓋 PDP 新鮮度、FrozenScores 不可變、
死 schema、四門 AND。
"""
import ast
import os

import pytest

from l3_dispatcher import overseer_policy as op
from l3_dispatcher.overseer_policy import (
    DEFAULT_POLICY,
    FrozenScores,
    PDPDecision,
    ToolDenied,
    ToolPolicy,
    evaluate_freshness,
    ledger_freshness,
    reflect_and_validate,
    validate_dispatch,
)


# ===========================================================================
# 🔴 關鍵 CI：okx-trade-mcp 任一下單/動帳工具必被框架層拒絕（紅線①）
# ===========================================================================
# 代表性的下單 / 改單 / 撤單 / 平倉 / 轉帳 / 槓桿 / 理財申贖工具（接真錢 server）。
OKX_EXECUTION_TOOLS = [
    "mcp__okx-trade-mcp__futures_place_order",
    "mcp__okx-trade-mcp__futures_place_algo_order",
    "mcp__okx-trade-mcp__futures_place_move_stop_order",
    "mcp__okx-trade-mcp__futures_close_position",
    "mcp__okx-trade-mcp__futures_cancel_order",
    "mcp__okx-trade-mcp__futures_amend_order",
    "mcp__okx-trade-mcp__futures_batch_orders",
    "mcp__okx-trade-mcp__futures_set_leverage",
    "mcp__okx-trade-mcp__swap_place_order",
    "mcp__okx-trade-mcp__swap_close_position",
    "mcp__okx-trade-mcp__swap_set_leverage",
    "mcp__okx-trade-mcp__swap_cancel_order",
    "mcp__okx-trade-mcp__spot_place_order",
    "mcp__okx-trade-mcp__spot_cancel_order",
    "mcp__okx-trade-mcp__account_transfer",
    "mcp__okx-trade-mcp__account_set_position_mode",
    "mcp__okx-trade-mcp__grid_create_order",
    "mcp__okx-trade-mcp__dca_create_order",
    "mcp__okx-trade-mcp__option_place_order",
    "mcp__okx-trade-mcp__earn_savings_purchase",
    "mcp__okx-trade-mcp__onchain_earn_purchase",
]


@pytest.mark.parametrize("tool", OKX_EXECUTION_TOOLS)
def test_okx_execution_tool_denied_decide(tool):
    assert DEFAULT_POLICY.decide(tool).allow is False


@pytest.mark.parametrize("tool", OKX_EXECUTION_TOOLS)
def test_okx_execution_tool_raises_on_check(tool):
    with pytest.raises(ToolDenied):
        DEFAULT_POLICY.check(tool)


# 連 okx 的純讀工具也整 server deny（不靠工具名判斷，靠 server 黑名單）。
OKX_READ_TOOLS = [
    "mcp__okx-trade-mcp__market_get_candles",
    "mcp__okx-trade-mcp__market_get_ticker",
    "mcp__okx-trade-mcp__account_get_balance",
    "mcp__okx-trade-mcp__account_get_positions",
    "mcp__okx-trade-mcp__system_get_capabilities",
]


@pytest.mark.parametrize("tool", OKX_READ_TOOLS)
def test_okx_whole_server_denied_even_read(tool):
    d = DEFAULT_POLICY.decide(tool)
    assert d.allow is False
    assert "okx-trade-mcp" in d.reason


def test_okx_denied_by_server_not_verb():
    """okx 讀取工具是「因 server 黑名單」被拒，而非「因動詞」——順序保證。"""
    d = DEFAULT_POLICY.decide("mcp__okx-trade-mcp__account_get_balance")
    assert "整體預設 deny" in d.reason


# ===========================================================================
# 縱深防禦：任何 server 的副作用動詞都被擋（防止改名/搬家繞過）
# ===========================================================================
@pytest.mark.parametrize("tool", [
    "mcp__some-other-mcp__spot_place_order",
    "mcp__random__swap_close_position",
    "mcp__x__account_transfer",
    "mcp__y__futures_set_leverage",
    "mcp__265305a9__create_file",
    "mcp__265305a9__copy_file",
    "mcp__fs__delete_file",
])
def test_execution_verb_denied_on_any_server(tool):
    assert DEFAULT_POLICY.decide(tool).allow is False


# ===========================================================================
# allowlist：純讀工具放行
# ===========================================================================
@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob", "LS"])
def test_builtin_read_tools_allowed(tool):
    assert DEFAULT_POLICY.decide(tool).allow is True


@pytest.mark.parametrize("tool", [
    "mcp__8762a8e2__get_candlestick",
    "mcp__8762a8e2__get_ticker",
    "mcp__8762a8e2__get_book",
    "mcp__8762a8e2__get_index_price",
    "mcp__market__market_get_candles",
    "mcp__265305a9__read_file_content",
    "mcp__265305a9__search_files",
    "mcp__265305a9__list_recent_files",
    "mcp__265305a9__get_file_metadata",
])
def test_readonly_market_and_fs_allowed(tool):
    assert DEFAULT_POLICY.decide(tool).allow is True


@pytest.mark.parametrize("tool", [
    "mcp__x__do_something",
    "mcp__x__list_tabs",
    "mcp__x__navigate",
    "Write",
    "Edit",
    "",
    "   ",
])
def test_unknown_tools_default_deny(tool):
    assert DEFAULT_POLICY.decide(tool).allow is False


def test_check_returns_decision_when_allowed():
    d = DEFAULT_POLICY.check("Read")
    assert d.allow is True


def test_custom_deny_servers():
    pol = ToolPolicy(deny_servers=frozenset({"foo-mcp"}))
    assert pol.decide("mcp__foo-mcp__get_ticker").allow is False
    # okx 不在這個自訂黑名單時，其純讀 market 工具會被「動詞」以外規則處理——
    # 但 place_order 仍被動詞門擋住。
    assert pol.decide("mcp__okx-trade-mcp__futures_place_order").allow is False


# ===========================================================================
# PDP 新鮮度前置門
# ===========================================================================
NOW = 1_000_000_000.0
NOW_MS = int(NOW * 1000)


def test_pdp_fresh_allows():
    d = evaluate_freshness(NOW_MS - 60_000, max_age_sec=5400, now_epoch_sec=NOW)
    assert d.allow is True
    assert d.checks["age_sec"] == pytest.approx(60.0, abs=0.001)


def test_pdp_stale_denies():
    d = evaluate_freshness(NOW_MS - 3 * 3600 * 1000, max_age_sec=5400, now_epoch_sec=NOW)
    assert d.allow is False
    assert any("過期" in r for r in d.reasons)


def test_pdp_future_timestamp_denies():
    d = evaluate_freshness(NOW_MS + 600_000, max_age_sec=5400, now_epoch_sec=NOW)
    assert d.allow is False
    assert any("未來" in r for r in d.reasons)


def test_pdp_small_future_skew_tolerated():
    # 30s 未來在 60s 容忍內 → 仍 allow
    d = evaluate_freshness(NOW_MS + 30_000, max_age_sec=5400, now_epoch_sec=NOW)
    assert d.allow is True


def test_pdp_missing_timestamp_denies():
    assert evaluate_freshness(None, max_age_sec=5400, now_epoch_sec=NOW).allow is False
    assert evaluate_freshness("nope", max_age_sec=5400, now_epoch_sec=NOW).allow is False


def test_pdp_uses_live_clock_when_now_not_passed():
    # 不傳 now_epoch_sec → 用 time.time()。剛剛的時戳必新鮮。
    import time as _t
    d = evaluate_freshness(int(_t.time() * 1000) - 1000, max_age_sec=5400)
    assert d.allow is True


def test_ledger_freshness_prefers_ms():
    d = ledger_freshness({"generated_at_ms": NOW_MS - 30_000, "generated_at": NOW - 9999},
                         max_age_sec=5400, now_epoch_sec=NOW)
    assert d.allow is True
    assert d.checks["age_sec"] == pytest.approx(30.0, abs=0.001)


def test_ledger_freshness_falls_back_to_seconds():
    d = ledger_freshness({"generated_at": NOW - 30}, max_age_sec=5400, now_epoch_sec=NOW)
    assert d.allow is True


def test_ledger_freshness_empty_denies():
    assert ledger_freshness({}, max_age_sec=5400, now_epoch_sec=NOW).allow is False


def test_build_pdp_block_shape():
    blk = op.build_pdp_block(NOW_MS, max_age_sec=5400)
    assert blk["policy_max_age_sec"] == 5400
    assert blk["generated_at_ms"] == NOW_MS
    assert blk["fresh_at_write"] is True


# ===========================================================================
# FrozenScores：不可變 + digest
# ===========================================================================
def test_frozenscores_readable():
    fs = FrozenScores({"strength": 72, "regime": "risk_off"})
    assert fs["strength"] == 72
    assert set(fs) == {"strength", "regime"}
    assert len(fs) == 2


def test_frozenscores_immutable():
    fs = FrozenScores({"a": 1})
    with pytest.raises(TypeError):
        fs["a"] = 2  # type: ignore[index]


def test_frozenscores_digest_order_independent():
    assert FrozenScores({"a": 1, "b": 2}).digest == FrozenScores({"b": 2, "a": 1}).digest


def test_frozenscores_digest_changes_with_content():
    assert FrozenScores({"a": 1}).digest != FrozenScores({"a": 2}).digest


def test_frozenscores_verify():
    fs = FrozenScores({"x": 1})
    assert fs.verify(fs.digest) is True
    assert fs.verify("0" * 64) is False


def test_frozenscores_snapshot_decoupled_from_source():
    src = {"a": 1}
    fs = FrozenScores(src)
    src["a"] = 999  # 改外部來源
    assert fs["a"] == 1  # 內部快照不受影響


# ===========================================================================
# 死 schema
# ===========================================================================
GOOD = {"action": "no_op", "rationale": "資料不足先觀望",
        "cited_scores": [], "tools_requested": []}


def test_schema_good_passes():
    ok, errs = validate_dispatch(GOOD)
    assert ok and errs == []


def test_schema_unknown_field_rejected():
    ok, errs = validate_dispatch({**GOOD, "evil": 1})
    assert not ok and any("未知" in e for e in errs)


def test_schema_bad_action_rejected():
    ok, _ = validate_dispatch({**GOOD, "action": "wire_money"})
    assert not ok


def test_schema_missing_required_rejected():
    ok, _ = validate_dispatch({"action": "no_op"})
    assert not ok


def test_schema_open_session_requires_kind():
    ok, _ = validate_dispatch({"action": "open_session", "rationale": "x",
                               "cited_scores": [], "tools_requested": []})
    assert not ok
    ok2, _ = validate_dispatch({"action": "open_session", "session_kind": "review",
                                "rationale": "x", "cited_scores": [], "tools_requested": []})
    assert ok2


def test_schema_continue_task_requires_task_id():
    ok, _ = validate_dispatch({"action": "continue_task", "rationale": "x",
                               "cited_scores": [], "tools_requested": []})
    assert not ok


def test_schema_non_dict_rejected():
    ok, _ = validate_dispatch(["not", "a", "dict"])
    assert not ok


def test_schema_bad_field_types_rejected():
    ok, _ = validate_dispatch({"action": "no_op", "rationale": "",
                               "cited_scores": "notalist", "tools_requested": [1, 2]})
    assert not ok


# ===========================================================================
# reflect_and_validate（四門 AND）
# ===========================================================================
FS = FrozenScores({"strength": 72, "regime": "risk_off"})
PDP_OK = evaluate_freshness(NOW_MS - 60_000, max_age_sec=5400, now_epoch_sec=NOW)
PDP_STALE = evaluate_freshness(NOW_MS - 9_999_999, max_age_sec=5400, now_epoch_sec=NOW)

DISPATCH_OK = {"action": "continue_task", "task_id": "51",
               "rationale": "strength 高，推進下一步",
               "cited_scores": ["strength"],
               "tools_requested": ["Read", "mcp__8762a8e2__get_ticker"]}


def test_reflect_all_gates_pass():
    v = reflect_and_validate(DISPATCH_OK, frozen_scores=FS, pdp=PDP_OK)
    assert v.accepted is True


def test_reflect_stale_ledger_rejected():
    v = reflect_and_validate(DISPATCH_OK, frozen_scores=FS, pdp=PDP_STALE)
    assert v.accepted is False
    assert any("新鮮度" in r for r in v.reasons)


def test_reflect_fabricated_score_rejected():
    d = {**DISPATCH_OK, "cited_scores": ["ghost_score"]}
    v = reflect_and_validate(d, frozen_scores=FS, pdp=PDP_OK)
    assert v.accepted is False
    assert any("不存在" in r for r in v.reasons)


def test_reflect_order_tool_request_rejected():
    d = {**DISPATCH_OK, "tools_requested": ["mcp__okx-trade-mcp__futures_place_order"]}
    v = reflect_and_validate(d, frozen_scores=FS, pdp=PDP_OK)
    assert v.accepted is False
    assert any("工具被拒" in r for r in v.reasons)


def test_reflect_bad_schema_rejected():
    d = {"action": "wire_money", "rationale": "x", "cited_scores": [], "tools_requested": []}
    v = reflect_and_validate(d, frozen_scores=FS, pdp=PDP_OK)
    assert v.accepted is False


# ===========================================================================
# 守門：本模組必須零 IO / 零網路 / 零下單依賴（AST 靜態檢查）
# ===========================================================================
def test_no_network_or_daemon_imports():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "l3_dispatcher", "overseer_policy.py")
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    forbidden = {"aiohttp", "requests", "httpx", "websocket", "websockets",
                 "telegram", "ccxt", "okx", "asyncio", "socket", "urllib"}
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                found.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.add(node.module.split(".")[0])
    leaked = forbidden & found
    assert not leaked, f"overseer_policy 不應 import 網路/daemon 模組，發現：{leaked}"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
