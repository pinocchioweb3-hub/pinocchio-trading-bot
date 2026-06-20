"""task#71 暖機（startup-burst 飢餓治本）：run_macro_confluence_loop 的延遲/短重試控制流。

根因：daemon 開機時 daily macro / 全市場掃描 / 各 worker 首輪幾乎同時打 CoinGlass →
429 → macro_confluence 首輪嚴重缺料 → present_mass < _MIN_PRESENT_MASS → 分數 floor-
bound（被地板綁定、低品質），且要乾等一整個 interval（1h）才有下一輪。

治本＝①啟動延遲拉長（可由 MACRO_CONFLUENCE_WARMUP_S 覆寫）避開尖峰②首輪若 floor-
bound 改短間隔重試（不乾等一小時），拿到一輪「非 floor-bound」健康分數或用盡重試額度
後回正常 hourly 節奏；之後（數小時後）若再 floor-bound 屬真實缺口、不再短重試。

純觀測：不改任何分數數學、不回填、floor-bound 行照常落盤帶 score_method provenance。

本檔以「asyncio shim」攔截 await asyncio.sleep（只攔 macro_confluence 模組看到的
asyncio.sleep，不碰測試自身 asyncio.run 用的真 asyncio），記錄睡眠秒數並在第 N 次睡眠
丟 _StopLoop 終止無窮迴圈，再斷言睡眠序列＝控制流是否如設計。全離線、零網路、零真錢。
"""
import asyncio
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import l3_dispatcher.macro_confluence as mc


class _StopLoop(Exception):
    """哨兵：在第 N 次 sleep 拋出以終止 run_macro_confluence_loop 的 while True。"""


class _AsyncioShim:
    """只提供 sleep 的 asyncio 替身（loop 內僅用到 asyncio.sleep）。"""
    def __init__(self, sleep_fn):
        self.sleep = sleep_fn


def _drive_loop(monkeypatch, cycle_results, *, max_sleeps, env=None,
                interval_seconds=3600):
    """驅動 run_macro_confluence_loop，回傳記錄到的 sleep 秒數序列。

    cycle_results：依序餵給假 _run_cycle 的 summary（present_mass 決定 floor-bound）；
    用盡後回健康 summary。max_sleeps：第幾次 sleep 拋 _StopLoop 終止。
    """
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)

    sleeps: list[float] = []
    results = list(cycle_results)
    appended: list[dict] = []

    async def fake_run_cycle(source=None):
        return results.pop(0) if results else {
            "present_mass": 1.0, "macro_confluence_score": 0,
            "bias": "neutral", "n_present": 9, "history_inserted": 0}

    async def fake_sleep(secs):
        sleeps.append(secs)
        if len(sleeps) >= max_sleeps:
            raise _StopLoop

    monkeypatch.setattr(mc, "_run_cycle", fake_run_cycle)
    monkeypatch.setattr(mc, "_append_jsonl", lambda s: appended.append(s))
    monkeypatch.setattr(mc, "asyncio", _AsyncioShim(fake_sleep))

    with pytest.raises(_StopLoop):
        asyncio.run(mc.run_macro_confluence_loop(
            source=None, interval_seconds=interval_seconds))
    return sleeps, appended


_FLOOR = {"present_mass": 0.10, "macro_confluence_score": 5, "bias": "neutral",
          "n_present": 1, "history_inserted": 0}      # < _MIN_PRESENT_MASS(0.25)
_HEALTHY = {"present_mass": 0.80, "macro_confluence_score": 12, "bias": "risk_on",
            "n_present": 9, "history_inserted": 1}


def test_warmup_delay_read_from_env(monkeypatch):
    # 啟動延遲（第一個 sleep）取自環境變數，取代舊固定 90s
    sleeps, _ = _drive_loop(
        monkeypatch, [_HEALTHY], max_sleeps=2,
        env={"MACRO_CONFLUENCE_WARMUP_S": "123"})
    assert sleeps[0] == 123


def test_healthy_first_cycle_uses_full_interval_no_short_retry(monkeypatch):
    # 首輪即健康 → 直接回正常 interval，全程不出現短重試間隔
    sleeps, _ = _drive_loop(
        monkeypatch, [_HEALTHY], max_sleeps=2,
        env={"MACRO_CONFLUENCE_WARMUP_S": "0",
             "MACRO_CONFLUENCE_WARMUP_RETRY_S": "300"}, interval_seconds=3600)
    assert sleeps == [0, 3600]


def test_floor_bound_then_healthy_short_retries_then_settles(monkeypatch):
    # 首輪 floor-bound → 短重試（300s），下一輪健康 → 回正常 interval（3600s）
    sleeps, _ = _drive_loop(
        monkeypatch, [_FLOOR, _HEALTHY], max_sleeps=3,
        env={"MACRO_CONFLUENCE_WARMUP_S": "0",
             "MACRO_CONFLUENCE_WARMUP_RETRY_S": "300",
             "MACRO_CONFLUENCE_WARMUP_MAX_RETRIES": "3"}, interval_seconds=3600)
    assert sleeps == [0, 300, 3600]


def test_retry_budget_exhausted_falls_back_to_full_interval(monkeypatch):
    # 持續 floor-bound：短重試額度（2）用盡後即回正常 interval，不再無限短重試
    sleeps, _ = _drive_loop(
        monkeypatch, [_FLOOR, _FLOOR, _FLOOR, _FLOOR], max_sleeps=4,
        env={"MACRO_CONFLUENCE_WARMUP_S": "0",
             "MACRO_CONFLUENCE_WARMUP_RETRY_S": "300",
             "MACRO_CONFLUENCE_WARMUP_MAX_RETRIES": "2"}, interval_seconds=3600)
    # warmup(0) + 2 次短重試(300) + 回正常(3600)
    assert sleeps == [0, 300, 300, 3600]
    assert sleeps.count(300) == 2          # 嚴格守住重試額度上限


def test_short_retry_floor_is_60s(monkeypatch):
    # 即使環境把短重試設極小，仍夾到 >=60s（不 busy-loop）
    sleeps, _ = _drive_loop(
        monkeypatch, [_FLOOR, _HEALTHY], max_sleeps=2,
        env={"MACRO_CONFLUENCE_WARMUP_S": "0",
             "MACRO_CONFLUENCE_WARMUP_RETRY_S": "1"}, interval_seconds=3600)
    assert sleeps == [0, 60]


def test_bad_env_falls_back_to_defaults(monkeypatch):
    # 壞環境值（非整數）→ 回預設，不拋例外
    sleeps, _ = _drive_loop(
        monkeypatch, [_HEALTHY], max_sleeps=2,
        env={"MACRO_CONFLUENCE_WARMUP_S": "not-an-int"})
    assert sleeps[0] == mc._WARMUP_DELAY_S_DEFAULT
