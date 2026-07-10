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
from .plan_snapshot_health import capture_health

# daemon 啟動時刻代理（本模組在 daemon 開機時被 import）。capture_health 用它把
# 「前一個 daemon 化身（可能跑過期碼）」寫的歷史列排除，只判當前碼的 snapshot 產出。
_DAEMON_START_TS = time.time()


@dataclass
class SupervisorState:
    """蒐集 scheduler / dispatcher 的最新狀態。"""
    last_scan_ts: float | None = None
    last_scan_summary: dict | None = None
    last_dispatch_ts: float | None = None
    consecutive_dispatch_failures: int = 0
    source_fail_streak: int = 0   # v108：連續 source 不健康次數（區分暫時限流 vs 持續故障）
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
    # 口徑治本(v108)：區分『限流(429,暫時性+免費源fallback+下輪自癒)』vs『真失聯』。
    #   限流是冷啟動爆量逐檔打 CoinGlass 的已知降級(task#64/#68)——單次blip不該推[嚴重]嚇人。
    #   暫時限流→info(只記log不推)；持續限流(≥3連續≈15分)→warn[警告]；真連不上(非429)→alert[嚴重]。
    try:
        health = await source.health()
        if health.get("ok"):
            state.source_fail_streak = 0
        else:
            state.source_fail_streak += 1
            _d = f"{health.get('details', '')} {health.get('code', '')}".lower()
            _rate_limited = ("too many requests" in _d or "429" in _d
                             or ("rate" in _d and "limit" in _d))
            if _rate_limited:
                _persistent = state.source_fail_streak >= 3
                print(f"[supervisor] {source.name} 限流(429) streak="
                      f"{state.source_fail_streak}（暫時性、免費源接手中）"
                      + ("→持續，升 warn" if _persistent else "→info 不推"))
                results.append(HealthCheck(
                    kind="source_rate_limited",
                    severity="warn" if _persistent else "info",
                    message=(f"資料來源 {source.name} 持續限流(429) {state.source_fail_streak} "
                             "次——免費源接手中，建議降低呼叫頻率(task#68)"
                             if _persistent else
                             f"資料來源 {source.name} 暫時限流(429)——免費源接手、下輪自癒（暫時性、無需處理）"),
                    detail=health,
                ))
            else:
                results.append(HealthCheck(
                    kind="source_down",
                    severity="alert",
                    message=f"資料來源 {source.name} 無回應",
                    detail=health,
                ))
    except Exception as e:
        state.source_fail_streak += 1
        results.append(HealthCheck(
            kind="source_exception",
            severity="alert",
            message=f"資料來源檢查異常：{type(e).__name__}",
            detail={"error": str(e)},
        ))

    # === 1.5 LLM 合成健康（v115，治本 2026-07-04 憑證 401 靜默斷流 34h 事故）===
    # synthesizer 每次合成結果寫 synth_health.json；連續失敗 ≥3（≈跨多輪 deepdive/pulse）
    # ＝訊號生成事實上停擺 → alert 推播（30 分節流沿用）。401 類錯誤附上 /login 指引。
    try:
        import json as _json
        from botpaths import data_dir as _dd
        _sh = _json.loads((_dd() / "synth_health.json").read_text(encoding="utf-8"))
        _cf = int(_sh.get("consecutive_failures", 0))
        if _cf >= 3:
            _le = str(_sh.get("last_error", ""))
            _hint = ("——多為 claude CLI 登入失效：請開終端機執行 claude /login 重新授權"
                     if ("401" in _le or "auth" in _le.lower()) else "")
            results.append(HealthCheck(
                kind="llm_synth_down",
                severity="alert",
                message=(f"LLM 合成已連續失敗 {_cf} 次＝訊號生成停擺"
                         f"（最後錯誤：{_le[:120]}）{_hint}"),
                detail=_sh,
            ))
    except Exception:  # noqa: BLE001 — 無檔案/壞檔＝尚無資料，不告警
        pass

    # === 1.6 各 setup 開單速率突變（v123）===
    # 教訓 2026-07-06：intraday 突破引擎沉睡數月後在強趨勢中甦醒（3 天 62 筆、同幣疊 10 筆），
    # 是使用者看持倉畫面先發現、不是監控——監督面從此觀測「每個訊號源的開單速率」：
    # 近 24h ≥10 筆且 >4× 前 7 日日均 → warn（沉睡甦醒/迴圈異常/市場劇變皆值得人先知道）。
    try:
        if now - state.last_alert_per_kind.get("setup_rate_surge", 0) >= 6 * 3600:
            import sqlite3 as _sq
            from botpaths import db_path as _dbp
            _conn = _sq.connect(f"file:{_dbp('trade_journal.db')}?mode=ro", uri=True)
            try:
                _now_ms = now * 1000
                _r24 = dict(_conn.execute(
                    "SELECT setup, COUNT(*) FROM paper_trades WHERE entry_at > ? "
                    "GROUP BY setup", (_now_ms - 86400_000,)).fetchall())
                _r7d = dict(_conn.execute(
                    "SELECT setup, COUNT(*) FROM paper_trades WHERE entry_at BETWEEN ? AND ? "
                    "GROUP BY setup", (_now_ms - 8 * 86400_000, _now_ms - 86400_000)).fetchall())
            finally:
                _conn.close()
            for _s, _n24 in _r24.items():
                _base = _r7d.get(_s, 0) / 7.0
                if _n24 >= 10 and _n24 > 4 * max(_base, 1.0):
                    results.append(HealthCheck(
                        kind="setup_rate_surge",
                        severity="warn",
                        message=(f"訊號源 {_s} 開單速率突變：24h 開 {_n24} 筆 vs 前 7 日均 "
                                 f"{_base:.1f} 筆/日——沉睡引擎甦醒或市場劇變，"
                                 f"建議檢視疊倉/樣本純度"),
                        detail={"setup": _s, "n24": _n24, "daily_base": round(_base, 2)},
                    ))
                    break     # 一次一則足矣（配合 6h 自我節流）
    except Exception:  # noqa: BLE001 — 觀測性檢查失敗不致命
        pass

    # === 1.6 系統資源健康（v128，使用者要求「定時排查電腦狀態」）===
    # 背景：兩次「當機」驗屍無藍屏/無硬體錯誤，但 RAM 曾壓到 <11%（Chrome+多開
    # Claude+WSL）——記憶體耗盡型凍機不留事件紀錄，正是無聲當機的頭號嫌疑。
    # RAM 可用 <1.5GB→warn、<0.8GB→alert；C 槽 <10GB→warn、<3GB→alert。
    # 自帶 3h 節流（資源吃緊常持續數小時，30 分一響太吵）。零依賴（ctypes+shutil）。
    try:
        import ctypes
        import shutil as _sh

        class _MEMSTAT(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        _ms = _MEMSTAT(); _ms.dwLength = ctypes.sizeof(_MEMSTAT)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(_ms))
        _avail_gb = _ms.ullAvailPhys / 1024 ** 3
        _free_gb = _sh.disk_usage("C:\\").free / 1024 ** 3
        _sys_checks = []
        if _avail_gb < 0.8:
            _sys_checks.append(("sys_memory_low", "alert",
                                f"記憶體僅剩 {_avail_gb:.1f}GB＝凍機高風險（無聲當機主嫌）"
                                "——請關閉閒置的瀏覽器分頁/多餘 Claude 視窗"))
        elif _avail_gb < 1.5:
            _sys_checks.append(("sys_memory_low", "warn",
                                f"記憶體吃緊（剩 {_avail_gb:.1f}GB）——建議關閉閒置應用，"
                                "避免整機凍住（交易機器人本身僅佔 ~300MB）"))
        if _free_gb < 3:
            _sys_checks.append(("sys_disk_low", "alert", f"C 槽僅剩 {_free_gb:.1f}GB"))
        elif _free_gb < 10:
            _sys_checks.append(("sys_disk_low", "warn", f"C 槽偏低（剩 {_free_gb:.1f}GB）"))
        for _kind, _sev, _msg in _sys_checks:
            if now - state.last_alert_per_kind.get(_kind, 0) < 10800:   # 3h 自我節流
                continue
            results.append(HealthCheck(kind=_kind, severity=_sev, message=_msg,
                                       detail={"ram_avail_gb": round(_avail_gb, 2),
                                               "disk_free_gb": round(_free_gb, 1)}))
    except Exception:  # noqa: BLE001 — 非 Windows/取值失敗＝不檢查，不告警
        pass

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

    # === 4. 資料品質：核心行情欄位多源中斷才告警 ===
    # 口徑修正（v34）：只在「核心 8 欄」stale 時告警 —— 這些欄位 CoinGlass + Binance
    # 雙源皆可補，連補值都失敗才代表真實資料源故障。進階衍生欄（cvd/清算/structure
    # 等）為 CoinGlass 免費層獨有來源，偶發 stale 屬可接受降級，且下游決策對 stale
    # 免疫（engine 把 STALE→HOLD/不計票），故不再告警，避免每 ~30 分誤報誣指主流幣、
    # 淹沒真正的 source_down / scheduler_stalled 告警。
    if state.last_scan_summary:
        snapshots = state.last_scan_summary.get("snapshots", [])
        if snapshots:
            avg_core_stale = sum(
                s.get("core_stale_count", 0) for s in snapshots
            ) / len(snapshots)
            if avg_core_stale >= 2:
                hit_syms = [
                    s["symbol"] for s in snapshots
                    if s.get("core_stale_count", 0) >= 2
                ]
                results.append(HealthCheck(
                    kind="data_quality_low",
                    severity="warn",
                    message=(
                        f"核心行情多源中斷：平均 {avg_core_stale:.1f} 個核心欄位"
                        f"連 Binance 補值都失敗"
                        + (f"（影響 {', '.join(hit_syms[:5])}）" if hit_syms else "")
                    ),
                    detail={"avg_core_stale": round(avg_core_stale, 1),
                            "symbols": hit_syms},
                ))

    # === 5. plan_snapshot 捕捉退化（唯讀，例外安全；治本「重啟載過期碼→靜默退化」）===
    # 只在近窗 deepdive 樣本夠且退化（NULL/殘留簽名）才告警；誠實盤整 None 不算退化。
    try:
        ch = capture_health(since_ts=_DAEMON_START_TS)
        if ch.get("verdict") == "degraded":
            c = ch.get("counts", {})
            results.append(HealthCheck(
                kind="plan_snapshot_degraded",
                severity="warn",
                message=(
                    f"進場計畫捕捉退化：近 {ch.get('window_hours')}h {ch.get('sample')} 筆 deepdive 中，"
                    f"NULL {c.get('null',0)}+解析錯 {c.get('parse_err',0)} 筆"
                    f"（{ch.get('null_rate'):.0%}）、過期碼殘留 {c.get('stale_leak',0)} 筆"
                    f"（{ch.get('stale_leak_rate'):.0%}）→ 復盤優化器將拿不到象限標籤。"
                    f"疑似 daemon 跑到過期工作樹碼，請重啟。"
                ),
                detail=ch,
            ))
    except Exception as e:
        print(f"[supervisor] capture_health check skipped: {type(e).__name__}: {e}")

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
