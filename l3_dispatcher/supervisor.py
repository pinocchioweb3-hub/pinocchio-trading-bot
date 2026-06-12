"""Supervisor Worker：機器人健康監督 + 自我檢查。

每 N 秒檢查：
    1. Source 是否還活著（mi_health）
    2. fire_queue 是否塞車（queued > N 太久 = dispatcher 卡住）
    3. 最近掃描的 stale 比例（資料品質下降）
    4. 最近 FIRE 失敗率（Telegram API 問題）
    5. 距上次成功掃描時間（scheduler 卡住）

異常 → 推 Telegram 警報（節流：30 分內同類異常不重複推）
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time
from dataclasses import dataclass, field

from .fire_queue import stats as queue_stats


@dataclass
class SupervisorState:
    """蒐集 scheduler / dispatcher 的最新狀態。"""
    last_scan_ts: float | None = None
    last_scan_summary: dict | None = None
    last_dispatch_ts: float | None = None
    consecutive_dispatch_failures: int = 0
    last_alert_per_kind: dict[str, float] = field(default_factory=dict)
    cooldown_seconds: int = 1800  # 30 分內同類警報節流


@dataclass
class HealthCheck:
    kind: str
    severity: str  # "info" | "warn" | "alert"
    message: str
    detail: dict


def _alert(state: SupervisorState, kind: str) -> bool:
    """同類警報 30 分內只推一次"""
    now = time.time()
    last = state.last_alert_per_kind.get(kind, 0)
    if now - last < state.cooldown_seconds:
        return False
    state.last_alert_per_kind[kind] = now
    return True


async def run_health_checks(state: SupervisorState, source) -> list[HealthCheck]:
    """跑全套健康檢查，回有警告/警報的項目。"""
    results: list[HealthCheck] = []
    now = time.time()

    # === 1. Source 活著嗎 ===
    try:
        health = await source.health()
        if not health.get("ok"):
            results.append(HealthCheck(
                kind="source_down",
                severity="alert",
                message=f"資料來源 {source.name} 無回應",
                detail=health,
            ))
    except Exception as e:
        results.append(HealthCheck(
            kind="source_exception",
            severity="alert",
            message=f"資料來源檢查異常：{type(e).__name__}",
            detail={"error": str(e)},
        ))

    # === 2. Queue 塞車檢查 ===
    qs = queue_stats()
    queued = qs.get("queued", 0)
    failed = qs.get("failed", 0)
    if queued >= 10:
        results.append(HealthCheck(
            kind="queue_jammed",
            severity="warn",
            message=f"訊號 queue 塞 {queued} 筆未送（dispatcher 慢或 Telegram 故障）",
            detail=qs,
        ))
    if failed >= 5:
        results.append(HealthCheck(
            kind="dispatch_failures",
            severity="warn",
            message=f"近期 {failed} 筆訊號送失敗",
            detail=qs,
        ))

    # === 3. Scheduler 卡住嗎（距上次掃 > 預期間隔 × 3）===
    if state.last_scan_ts is not None:
        elapsed = now - state.last_scan_ts
        if elapsed > 1800:  # 30 分鐘還沒掃 = 卡住
            results.append(HealthCheck(
                kind="scheduler_stalled",
                severity="alert",
                message=f"Scheduler 已 {int(elapsed/60)} 分鐘未完成掃描",
                detail={"last_scan_ago_sec": int(elapsed)},
            ))

    # === 4. 資料品質：上次掃 stale_count 大宗 ===
    if state.last_scan_summary:
        snapshots = state.last_scan_summary.get("snapshots", [])
        if snapshots:
            avg_stale = sum(s.get("stale_count", 0) for s in snapshots) / len(snapshots)
            if avg_stale >= 3:
                stale_syms = [s["symbol"] for s in snapshots if s.get("stale_count", 0) >= 3]
                results.append(HealthCheck(
                    kind="data_quality_low",
                    severity="warn",
                    message=f"上輪掃描平均 {avg_stale:.1f} 個欄位 stale，影響：{', '.join(stale_syms[:5])}",
                    detail={"avg_stale": round(avg_stale, 1), "symbols": stale_syms},
                ))

    return results


async def run_supervisor_loop(
    tg, source, state: SupervisorState, interval_seconds: int = 300,
):
    """Worker 主迴圈：每 N 秒跑健康檢查，異常推 Telegram。"""
    from telegram_bot.message_format import render_health_alert

    # 啟動延後一個間隔
    await asyncio.sleep(min(interval_seconds, 60))

    while True:
        try:
            checks = await run_health_checks(state, source)
            actionable = [c for c in checks if c.severity in ("warn", "alert")]
            for chk in actionable:
                if _alert(state, chk.kind):
                    await tg.send_message(render_health_alert(chk), parse_mode="HTML")
                    print(f"[supervisor] alert: {chk.kind} - {chk.message}")
            if not actionable:
                print(f"[supervisor] all good (queue={queue_stats()})")
        except Exception as e:
            print(f"[supervisor] error: {type(e).__name__}: {e}")
        await asyncio.sleep(interval_seconds)
