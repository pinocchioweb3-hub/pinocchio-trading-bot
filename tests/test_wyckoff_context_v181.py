# -*- coding: utf-8 -*-
"""v181: Wyckoff 吸籌/派發脈絡方向測試。

方法論鐵則：本測試必須先在舊碼（wyckoff.py L67-68 反轉覆蓋）上失敗，
修復（刪除該覆蓋）後才通過——防虛設檢定。
場景=2026-08-01 XRP 週線活案例的合成版：前置趨勢遠高於箱體（深跌進箱），
教科書上是低位「吸籌」脈絡，舊碼卻蓋成「派發」。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from market_intel_mcp.wyckoff import classify_wyckoff  # noqa: E402


def _c(px: float) -> dict:
    return {"open": px, "high": px * 1.01, "low": px * 0.99,
            "close": px, "volume": 1000.0, "ts": 0}


def _falling_into_box() -> list[dict]:
    """前 30 根在 2.5-2.8 高位，後 40 根跌進 1.0-1.1 窄箱（無 Spring/UTAD 事件窗）。"""
    prior = [_c(2.8 - i * 0.01) for i in range(30)]
    box = [_c(1.05 + (0.02 if i % 2 else -0.02)) for i in range(40)]
    return prior + box


def test_deep_fall_into_box_is_accumulation_context():
    out = classify_wyckoff(_falling_into_box())
    # 深跌後的低位箱體＝吸籌脈絡（或至少絕不可標「派發」）
    assert "派發" not in (out.get("context") or out.get("narrative") or str(out)), \
        f"深跌進箱被誤標派發: {out}"


def _rising_into_box() -> list[dict]:
    """前 30 根在 0.3-0.4 低位，後 40 根漲進 1.0-1.1 窄箱＝高位派發脈絡。"""
    prior = [_c(0.3 + i * 0.004) for i in range(30)]
    box = [_c(1.05 + (0.02 if i % 2 else -0.02)) for i in range(40)]
    return prior + box


def test_rise_into_box_is_distribution_context():
    out = classify_wyckoff(_rising_into_box())
    assert "吸籌" not in (out.get("context") or out.get("narrative") or str(out)), \
        f"漲進箱被誤標吸籌: {out}"
