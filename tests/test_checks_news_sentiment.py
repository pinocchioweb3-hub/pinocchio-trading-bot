"""checks.cross_check_fire：Check 7「新聞敘事偏向」的 delta=0 影子鐵則（task#66 Q2 Phase 1）。

設計總綱（紅隊定案）：新聞**永不給方向票、永不單獨擋單**。Phase 1 把消息面敘事作為
『純觀測列』釘進 FIRE 一致性卡，讓人/CEO 看見「系統有在看新聞」，但 **delta 恆 0**——
結構上不可能改 confidence / pass_ / 方向 / downgraded。閘②（離線回測 PSR/DSR/CAAR
顯著）通過前，新聞對任何下單數學影響嚴格為零。

本檔鎖死這條鐵則，防止日後有人把 delta 從 0 偷改成非 0：
  * news_sentiment check 永遠存在、pass=True、delta=0。
  * 無論敘事偏多/偏空/與本單同向或反向，confidence 一律不變（最關鍵的回歸護欄）。
  * 敘事讀取失敗 → 中性觀測列，仍 delta=0、不拖垮計分。
  * delta=0+pass → 不污染 reason 字串。

全離線：monkeypatch news_score._active_narratives_safe 灌假敘事；零網路、零真錢、零訊號數學。
執行：pytest tests/test_checks_news_sentiment.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import news_feed.news_score as ns
from l2_trigger.types import TriggerDecision, TriggerAction, SignalState
from l3_dispatcher.checks import cross_check_fire
from tests import fixtures as F
from tests.fixtures import _replace

# assets 含 "BTC"/"RISK_ASSETS" → narrative_lean_for 視為全市場相關 → SUI 也命中（市場級鏡射）
_BULL_NARS = [{"slug": "x", "impact": "bullish", "assets": "BTC", "event_count": 5}]
_BEAR_NARS = [{"slug": "y", "impact": "bearish", "assets": "RISK_ASSETS", "event_count": 9}]


def _fire(direction, snap) -> TriggerDecision:
    return TriggerDecision(
        action=TriggerAction.FIRE, direction=direction, setup_name="t",
        confirmed=(), composite_score=0.0, snapshot=snap, reason="t")


def _news_check(res) -> dict:
    return next(c for c in res.checks if c["name"] == "news_sentiment")


def test_news_check_always_present_and_delta_zero(monkeypatch):
    monkeypatch.setattr(ns, "_active_narratives_safe", lambda: _BULL_NARS)
    snap = _replace(F.sui_intraday_fire_bull(), btc_gate_open=True)
    res = asyncio.run(cross_check_fire(_fire(SignalState.BULL, snap)))
    chk = _news_check(res)
    assert chk["pass"] is True
    assert chk["delta"] == 0


def test_news_never_moves_confidence(monkeypatch):
    """鐵則核心：敘事偏多/偏空/同向/反向，confidence 必須完全一致（delta=0）。"""
    snap = _replace(F.sui_intraday_fire_bull(), btc_gate_open=True)
    monkeypatch.setattr(ns, "_active_narratives_safe", lambda: [])
    base = asyncio.run(cross_check_fire(_fire(SignalState.BULL, snap))).confidence
    monkeypatch.setattr(ns, "_active_narratives_safe", lambda: _BEAR_NARS)   # BULL 單遇反向敘事
    adverse = asyncio.run(cross_check_fire(_fire(SignalState.BULL, snap))).confidence
    monkeypatch.setattr(ns, "_active_narratives_safe", lambda: _BULL_NARS)   # BULL 單遇同向敘事
    favor = asyncio.run(cross_check_fire(_fire(SignalState.BULL, snap))).confidence
    assert base == adverse == favor, \
        f"delta=0 鐵則破裂：新聞改了 confidence（base={base} adverse={adverse} favor={favor}）"


def test_news_read_failure_is_neutral_observation(monkeypatch):
    def _boom():
        raise RuntimeError("narrative.db down")
    monkeypatch.setattr(ns, "_active_narratives_safe", _boom)
    snap = _replace(F.sui_intraday_fire_bull(), btc_gate_open=True)
    res = asyncio.run(cross_check_fire(_fire(SignalState.BULL, snap)))
    chk = _news_check(res)
    assert chk["pass"] is True and chk["delta"] == 0   # 讀取失敗也不影響計分


def test_news_not_in_reason_string(monkeypatch):
    # delta=0 + pass=True → 結論的 reason 串不該收錄這列（不污染擋單理由）
    monkeypatch.setattr(ns, "_active_narratives_safe", lambda: _BEAR_NARS)
    snap = _replace(F.sui_intraday_fire_bull(), btc_gate_open=True)
    res = asyncio.run(cross_check_fire(_fire(SignalState.BULL, snap)))
    assert "📰" not in res.reason


def test_news_relation_label_direction_aware(monkeypatch):
    # 同向敘事 → note 標「同向」；反向 → 標「反向」（純文字，仍 delta=0）
    snap = _replace(F.sui_intraday_fire_bull(), btc_gate_open=True)
    monkeypatch.setattr(ns, "_active_narratives_safe", lambda: _BULL_NARS)
    note_bull = _news_check(asyncio.run(cross_check_fire(_fire(SignalState.BULL, snap))))["note"]
    note_bear = _news_check(asyncio.run(cross_check_fire(_fire(SignalState.BEAR, snap))))["note"]
    assert "同向" in note_bull
    assert "反向" in note_bear


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
