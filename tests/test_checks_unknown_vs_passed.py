# -*- coding: utf-8 -*-
"""cross-check 卡：「這項量不到」不得折成「這項通過」。

同物種（未知 → 折成正常）在**訊號評分卡**上的實例。舊行為：

    Check 3 funding / Check 4 清算 / Check 5 ETF / Check 6 情緒，
    只要對應資料是 None 或帶 error，整段 `if` 就跳過——**卡上連一列都不會出現**。

於是讀卡的人（和 CEO 報告）看到的是七項檢查裡只列了三項，而且結論那行會印

    reason = "all checks passed"      ← 全部檢查通過
    confidence = 100                  ← 滿分

實際上四項裡有幾項根本沒跑過。這句「all checks passed」是會送進 Telegram 卡片、
再進 enqueue payload 的對外措辭，不是內部 debug 字串。

⛔ 本輪**只治可見性，不動計分**：
    - 每一列「量不到」都是 delta=0，score 一分不動、pass_ 判準不動。
    - 「未知是否該像 Check 2 的 BTC 閘那樣保守扣分」是**進場濾網數學**的改動，
      按規矩要走回測閘（PSR/DSR），不在本輪射程內。這裡先讓它可見。

⛔ 另一條邊界：「不適用」不是「量不到」。ETF 流向本來就只算 BTC/ETH
    （scheduler._gather_check_context 對其他標的直接給 None ＝ by-design），
    若把它也標成未知，每張山寨幣卡都會多一列假警訊——那會讓 ❓ 這個符號貶值，
    等於用另一種方式製造失明。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from l2_trigger.types import TriggerDecision, TriggerAction, SignalState
from l3_dispatcher.checks import cross_check_fire
from tests import fixtures as F
from tests.fixtures import _replace


# --- 齊全的外部資料：這組全給，才是真正的「all checks passed」 ---
FULL_SENTIMENT = {"fear_greed_now": 55, "ahr999_now": 0.9}
FULL_LIQ = {"items": [{"symbol": "SUI", "imbalance": 0.0}]}
FULL_ETF = {"cumulative_7d_flow_usd": 0}


def _fire_bull(snap) -> TriggerDecision:
    return TriggerDecision(
        action=TriggerAction.FIRE,
        direction=SignalState.BULL,
        setup_name="test_setup",
        confirmed=(),
        composite_score=0.0,
        snapshot=snap,
        reason="test",
    )


def _run(snap, **ctx):
    return asyncio.run(cross_check_fire(_fire_bull(snap), **ctx))


def _row(res, name: str) -> dict | None:
    return next((c for c in res.checks if c["name"] == name), None)


def _unknown_rows(res) -> list[dict]:
    return [c for c in res.checks if c.get("unknown")]


def _sui():
    """SUI：非 BTC/ETH，且 btc_gate 給 True 讓 Check 2 不搶戲。"""
    return _replace(F.sui_intraday_fire_bull(), btc_gate_open=True)


def _btc():
    return _replace(F.sui_intraday_fire_bull(), symbol="BTC", btc_gate_open=True)


def _decision_dict() -> dict:
    """render_fire_message 要的是 JSON 化後的 decision（欄位形狀與 dataclass 不同）。"""
    return {
        "direction": "bull",
        "setup_name": "intraday",
        "composite_score": 2.31,
        "confirmed": [
            {"name": "oi_trajectory", "state": "bull", "evidence": {}},
        ],
        "snapshot": {
            "symbol": "SUI", "price": 4.05, "atr_pct_7d": 4.2,
            "strength_score": 78, "ts": 1_750_000_000_000,
        },
    }


# ── ① 量不到的項目必須在卡上留下一列 ──────────────────────────────────


def test_missing_funding_leaves_a_row():
    res = _run(_replace(_sui(), funding=None),
               sentiment=FULL_SENTIMENT, liq_scan=FULL_LIQ)
    row = _row(res, "funding_check")
    assert row is not None, "funding 讀不到 → 整列從卡上消失，讀卡的人以為沒問題"
    assert row.get("unknown") is True
    assert row.get("delta", 0) == 0, "本輪不動計分"


def test_missing_sentiment_leaves_a_row():
    res = _run(_sui(), sentiment=None, liq_scan=FULL_LIQ)
    row = _row(res, "sentiment_check")
    assert row is not None, "F&G/AHR999 是全系統唯二量化擋單接點，讀不到不能無聲"
    assert row.get("unknown") is True
    assert row.get("delta", 0) == 0


def test_errored_sentiment_leaves_a_row():
    """帶 error 的 dict 與 None 是同一件事：沒有可用讀數。"""
    res = _run(_sui(), sentiment={"error": "upstream 500"}, liq_scan=FULL_LIQ)
    row = _row(res, "sentiment_check")
    assert row is not None and row.get("unknown") is True


def test_sentiment_present_but_fields_empty_leaves_a_row():
    """⛔ 最陰的一種：連線成功、dict 回來了、但欄位是 None。"""
    res = _run(_sui(), sentiment={"fear_greed_now": None, "ahr999_now": None},
               liq_scan=FULL_LIQ)
    row = _row(res, "sentiment_check")
    assert row is not None and row.get("unknown") is True, (
        "「有回應」被當成「有讀數」——這正是 r33 那次假恢復的同一個誤讀")


def test_missing_liq_scan_leaves_a_row():
    res = _run(_sui(), sentiment=FULL_SENTIMENT, liq_scan=None)
    row = _row(res, "liquidation_alignment")
    assert row is not None and row.get("unknown") is True


def test_missing_etf_for_btc_leaves_a_row():
    res = _run(_btc(), sentiment=FULL_SENTIMENT, liq_scan=FULL_LIQ, etf_flows=None)
    row = _row(res, "etf_alignment")
    assert row is not None and row.get("unknown") is True, (
        "BTC 的 ETF 流向讀不到，是真的量不到（不是不適用）")


# ── ② ⛔「不適用」不是「量不到」 ──────────────────────────────────────


def test_etf_not_applicable_on_altcoin_is_not_unknown():
    """SUI 沒有 ETF 這回事；標成未知＝每張山寨幣卡都多一列假警訊。"""
    res = _run(_sui(), sentiment=FULL_SENTIMENT, liq_scan=FULL_LIQ, etf_flows=None)
    row = _row(res, "etf_alignment")
    assert row is None or not row.get("unknown"), (
        "把 by-design 的不適用講成量不到，會讓 ❓ 這個符號貶值")


def test_symbol_absent_from_liq_top20_is_not_unknown():
    """清算榜前 20 名沒有它＝量到了、答案是「量小」，本來就有明確一列。"""
    res = _run(_sui(), sentiment=FULL_SENTIMENT,
               liq_scan={"items": [{"symbol": "BTC", "imbalance": 0.5}]})
    row = _row(res, "liquidation_alignment")
    assert row is not None and not row.get("unknown")


# ── ③ 結論措辭：有東西沒量到就不准說「全部通過」 ─────────────────────


def test_reason_does_not_claim_all_passed_when_something_unknown():
    """最尖的一條：這句話會進 Telegram 卡片與 enqueue payload。"""
    res = _run(_replace(_sui(), funding=None), sentiment=None, liq_scan=None)
    assert _unknown_rows(res), "前提就不成立：這組情境本來就該有未知列"
    assert "all checks passed" not in res.reason, (
        f"四項沒跑過還印『全部檢查通過』：{res.reason!r}")
    assert "未能量測" in res.reason or "無法確認" in res.reason, (
        f"結論那行沒讓人看見有東西沒量到：{res.reason!r}")


def test_reason_still_says_all_passed_when_everything_measured():
    """對照組：資料齊全且無扣分時，措辭不變（確認上面不是靠癱瘓 reason 過關）。"""
    res = _run(_sui(), sentiment=FULL_SENTIMENT, liq_scan=FULL_LIQ)
    assert not _unknown_rows(res), f"這組不該有未知列：{res.checks!r}"
    assert res.reason == "all checks passed"


# ── ④ ⛔ 本輪不得動到計分（否則就變成需要回測閘的進場濾網改動）─────────


def test_unknown_rows_never_change_the_score():
    """所有未知列 delta 恆 0，且 confidence 仍等於 100＋各列 delta。"""
    res = _run(_replace(_sui(), funding=None), sentiment=None, liq_scan=None)
    for row in _unknown_rows(res):
        assert row.get("delta", 0) == 0, f"未知列動了計分：{row!r}"
    expected = max(0, min(100, 100 + sum(c.get("delta", 0) for c in res.checks)))
    assert res.confidence == expected


def test_unknown_rows_do_not_flip_pass():
    """全部外部資料掛掉時，pass_ 仍由既有計分決定，不因新增列而改變。"""
    res = _run(_replace(_sui(), funding=None), sentiment=None, liq_scan=None)
    assert res.pass_ is True, "本輪只治可見性；讓未知擋單是回測閘後才准做的事"


# ── ⑤ 渲染：❓ 不得被畫成 ✅ ───────────────────────────────────────────


def test_renderer_does_not_paint_unknown_as_a_green_tick():
    """`pass=True, delta=0` 在舊渲染邏輯下會拿到 ✅——「✅ 資料讀不到」是荒謬的。"""
    from telegram_bot.message_format import render_fire_with_checks

    res = _run(_replace(_sui(), funding=None), sentiment=None, liq_scan=None)
    text, _buttons = render_fire_with_checks(_decision_dict(), res)

    # 逐行比對（note 經過 HTML escape，`&` 會變 `&amp;`，不可拿原字串直接 find）
    unknown_lines = [ln for ln in text.splitlines() if "讀不到" in ln]
    assert len(unknown_lines) == len(_unknown_rows(res)), (
        f"未知列沒被完整渲染出來：{unknown_lines!r}")
    for ln in unknown_lines:
        assert "❓" in ln, f"未知列沒有 ❓：{ln!r}"
        assert "✅" not in ln, f"未知列被畫成綠勾＝把「量不到」畫成「沒問題」：{ln!r}"


def test_renderer_qualifies_the_confidence_headline():
    """100/100「高信心」但四項沒量到 ⇒ 標題必須帶限定語。"""
    from telegram_bot.message_format import render_fire_with_checks

    res = _run(_replace(_sui(), funding=None), sentiment=None, liq_scan=None)
    text, _ = render_fire_with_checks(_decision_dict(), res)

    head = next(ln for ln in text.splitlines() if "Cross-Check" in ln)
    assert "未能量測" in head or "未量測" in head, (
        f"信心分數旁沒有涵蓋率限定語，讀的人會把 100 讀成七項全過：{head!r}")


def test_renderer_headline_clean_when_everything_measured():
    """對照組：資料齊全時標題不得多出限定語（否則限定語會失去訊息量）。"""
    from telegram_bot.message_format import render_fire_with_checks

    res = _run(_sui(), sentiment=FULL_SENTIMENT, liq_scan=FULL_LIQ)
    text, _ = render_fire_with_checks(_decision_dict(), res)

    head = next(ln for ln in text.splitlines() if "Cross-Check" in ln)
    assert "未能量測" not in head and "未量測" not in head


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
