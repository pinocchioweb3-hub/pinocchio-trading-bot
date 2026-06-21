# -*- coding: utf-8 -*-
"""task#7：CEO 深度綜合『系統自評』跨 session 瓶頸歸因（純函式 + 整合）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from l3_dispatcher.ceo_session import _synthesize_bottleneck, build_ceo_brief


def test_too_few_samples_is_honest_not_fabricated():
    out = _synthesize_bottleneck(3, 100, 0, 30, 0, 0)
    assert "尚無足夠基礎" in out and "不臆測" in out


def test_sample_supply_is_bottleneck_not_strategy():
    out = _synthesize_bottleneck(69, 100, 0, 30, 1, 0)
    assert "樣本供給不足" in out and "非策略失效" in out and "L2" in out


def test_high_demo_reject_rate_surfaced():
    out = _synthesize_bottleneck(69, 100, 0, 30, 1, 34)  # 34/35 拒
    assert "拒單率偏高" in out


def test_low_demo_reject_no_warning():
    out = _synthesize_bottleneck(69, 100, 0, 30, 10, 1)
    assert "拒單率偏高" not in out


def test_samples_met_changes_attribution():
    out = _synthesize_bottleneck(120, 100, 35, 30, 35, 0)
    assert "樣本達標" in out


def test_build_brief_runs_and_includes_synthesis():
    brief = build_ceo_brief()
    assert isinstance(brief, str) and "系統自評" in brief and "跨 session 綜合" in brief
