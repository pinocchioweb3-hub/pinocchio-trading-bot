"""🧭 CEO 監督 Session（P0-B / task #9）— 把全局塞回一個視窗。

使用者（發起人）最大的痛點：「我埋在細節裡，忘了整個架構長怎樣、現在在哪、還差什麼。」
這個 Session 不另蓋多進程框架，只是在既有 run_bot.py supervise() 架構加一個角色型
worker，每天把所有自治 Session 的產出彙整成 **一份 CEO 簡報**，分兩段推到系統主題：

    ✅ 一切正常·今日重點   —— 系統健康／績效／風控／驗證進度／策略概況（你掃一眼即可）
    ⚠️ 需發起人決策        —— 等你拍板的事項、待審對外內容、Phase 0 是否達標（只有這段要你動腦）

本模組同時提供 task #9 的另外三塊：
    feature_registry  機器可讀「功能完成度清單」+ 健康探針（對應你怕的「四不像、半成品」）
    promotion_gate    phase0_status()：寫死的達標判斷，AI 只偵測+回報，永不自我宣告（紅線 3）
    （outbox / decision_registry 為獨立模組，本 Session 負責把它們彙整呈現）

純讀。不下任何單、不發任何對外內容。"""
from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3
import time

from botpaths import db_path as _db_path

from . import decision_registry as _dec
from . import outbox as _outbox

_TJ_DB = _db_path("trade_journal.db")

# === Phase 0 解鎖硬門檻（與 VISION.md / PROJECT_CHARTER.md 對齊；寫死，AI 不能改） ===
PHASE0_PAPER_MIN = 100   # 模擬盤累積 ≥100 筆已平倉
PHASE0_LIVE_MIN = 30     # 真實小額 ≥30 筆已平倉且整體期望值為正


# ===========================================================================
# feature_registry —— 機器可讀的功能完成度清單（單一真實來源在程式碼，不會漂移）
# 狀態：shipped 已上線 / partial 部分完成 / blocked 卡在外部 / planned 規劃中
# ===========================================================================
FEATURES = [
    # 自治 Session（6 個 + 本 CEO Session）
    {"key": "supervisor", "name": "健康監督 Session", "kind": "session", "status": "shipped"},
    {"key": "auto_tuner", "name": "調參 Session（紙上帳→參數建議）", "kind": "session", "status": "shipped"},
    {"key": "backtest", "name": "回測 Session（每週歷史回放）", "kind": "session", "status": "shipped"},
    {"key": "auditor", "name": "訊息稽核 Session", "kind": "session", "status": "shipped"},
    {"key": "narrative", "name": "敘事引擎 Session", "kind": "session", "status": "shipped"},
    {"key": "ledger_anchor", "name": "帳本錨定 Session（OpenTimestamps）", "kind": "session", "status": "shipped"},
    {"key": "ceo_session", "name": "CEO 監督 Session（本簡報）", "kind": "session", "status": "shipped"},
    # 風控
    {"key": "risk_gates", "name": "風控閘門（總曝險+日開倉上限，%制）", "kind": "risk", "status": "shipped"},
    {"key": "budget_tiering", "name": "依預算自適應風控分級（本金→槓桿/風險/曝險護欄）", "kind": "risk", "status": "shipped"},
    {"key": "leverage_tier", "name": "你這台是否改吃 tier 槓桿預設（現行明確 15x 不動）", "kind": "risk", "status": "blocked",
     "by": "decision", "note": "等發起人決策；框架已上線且零行為改變，僅你本機是否改設"},
    {"key": "coach_monitor", "name": "教練式持倉提醒（追高/止損/熔斷/手續費侵蝕/降檔）", "kind": "risk", "status": "shipped"},
    {"key": "discipline_kpi", "name": "紀律遵守率 KPI（決斷率+不追高率）", "kind": "risk", "status": "shipped"},
    # 治理 / 對外
    {"key": "decision_registry", "name": "決策佇列（需發起人拍板）", "kind": "gov", "status": "shipped"},
    {"key": "outbox", "name": "對外內容待審佇列（/approve）", "kind": "gov", "status": "partial",
     "note": "模組+/approve 已上線；目前 Threads 草稿走 docs/threads 人工佇列，尚未自動餵入 outbox"},
    {"key": "promotion_gate", "name": "Phase 0 達標偵測（AI 不自我宣告）", "kind": "gov", "status": "shipped"},
    {"key": "rebate_tiers", "name": "返佣誠實分級標籤", "kind": "gov", "status": "shipped"},
    # 驗證真實性
    {"key": "okx_paper", "name": "OKX 模擬盤自動下單（驗證持倉真實性）", "kind": "verify", "status": "blocked",
     "note": "等使用者建 OKX Demo 金鑰"},
    # 呈現 / 信任
    {"key": "dual_audience", "name": "雙受眾呈現層（人話卡+機器JSON）", "kind": "ux", "status": "partial",
     "note": "人話卡(message_format)+機器JSON(intent_format)+白話對照(glossary)已具雛形；新手/專家模式待整合"},
    {"key": "trust_site", "name": "靜態信任網頁（GitHub Pages）", "kind": "ux", "status": "partial",
     "note": "docs/trust-site 骨架+設計+資料檔已就緒；尚未部署 Pages、狀態列待接動態 JSON"},
    {"key": "build_log", "name": "建造日誌連載", "kind": "ux", "status": "blocked",
     "note": "使用者說晚點再看，不發布"},
]

_STATUS_ICON = {"shipped": "✅", "partial": "🟡", "blocked": "⛔", "planned": "⬜"}


def feature_registry_dump() -> dict:
    """機器可讀完成度快照（供未來雙受眾層 / 信任網頁取用）。"""
    counts: dict[str, int] = {}
    for f in FEATURES:
        counts[f["status"]] = counts.get(f["status"], 0) + 1
    return {"generated_at": int(time.time()), "counts": counts, "features": FEATURES}


# ===========================================================================
# 健康探針 —— 偵測「半成品 / 退化 / 卡住」（對應使用者怕的「四不像」）
# 每個探針回 (severity, message)；severity: "ok" | "info" | "warn"
# 探針本身永不拋例外（CEO 簡報必須永遠能產生）
# ===========================================================================
def _probe_db() -> tuple[str, str]:
    try:
        conn = sqlite3.connect(_TJ_DB, timeout=5)
        conn.execute("SELECT 1 FROM trades LIMIT 1")
        conn.close()
        return "ok", "交易帳本 DB 可讀"
    except Exception as e:
        return "warn", f"交易帳本 DB 異常：{type(e).__name__}"


def _probe_queue() -> tuple[str, str]:
    try:
        from .fire_queue import stats as _qs
        s = _qs()
        queued = s.get("queued", 0)
        if queued >= 10:
            return "warn", f"訊號 queue 塞 {queued} 筆未送（dispatcher 慢或 TG 故障）"
        return "ok", f"訊號 queue 暢通（queued={queued}）"
    except Exception as e:
        return "info", f"queue 狀態讀取失敗：{type(e).__name__}"


def _probe_backtest_freshness() -> tuple[str, str]:
    """回測 Session 每週一跑；超過 ~10 天沒新結果 = 可能卡住。"""
    try:
        from backtest.backtest_session import latest_backtest
        from l2_trigger.registry import scheduler_strategies
        strats = scheduler_strategies()
        newest = 0
        for s in strats:
            bt = latest_backtest(getattr(s, "id", s) if not isinstance(s, str) else s)
            if bt and bt.get("run_ts"):
                newest = max(newest, int(bt["run_ts"]))
        if newest == 0:
            return "info", "回測尚無歷史結果（首輪未跑或樣本不足）"
        age_d = (time.time() - newest) / 86400
        if age_d > 10:
            return "warn", f"回測結果已 {age_d:.0f} 天未更新（每週應更新一次）"
        return "ok", f"回測結果新鮮（{age_d:.1f} 天前）"
    except Exception as e:
        return "info", f"回測新鮮度讀取失敗：{type(e).__name__}"


def feature_health() -> list[tuple[str, str]]:
    """跑全套探針，回 [(severity, message)]。"""
    return [_probe_db(), _probe_queue(), _probe_backtest_freshness()]


# ===========================================================================
# promotion_gate —— Phase 0 達標偵測（紅線 3：AI 只偵測+回報，永不自我宣告解鎖）
# ===========================================================================
def _count_closed(table: str) -> tuple[int, float]:
    """某表已平倉筆數 + 平均 R（表不存在/無欄位 → (0, 0.0)）。"""
    try:
        conn = sqlite3.connect(_TJ_DB, timeout=5)
        try:
            row = conn.execute(
                f"SELECT COUNT(*), COALESCE(AVG(realized_r), 0) "
                f"FROM {table} WHERE status='closed'"
            ).fetchone()
            return int(row[0] or 0), round(float(row[1] or 0), 3)
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return 0, 0.0
    except Exception:
        return 0, 0.0


def phase0_status() -> dict:
    """回 Phase 0 解鎖進度（純偵測）。

    硬門檻（寫死）：模擬盤 ≥100 筆 且 真實小額 ≥30 筆且整體期望值為正。
    ready=True 也 **只代表「達標、可由人類考慮宣告」**，本系統永遠不會自動宣告解鎖。
    """
    paper_n, paper_ev = _count_closed("paper_trades")
    live_n, live_ev = _count_closed("trades")
    paper_ok = paper_n >= PHASE0_PAPER_MIN
    live_ok = live_n >= PHASE0_LIVE_MIN and live_ev > 0
    return {
        "ready": paper_ok and live_ok,
        "paper_n": paper_n, "paper_ev_r": paper_ev, "paper_min": PHASE0_PAPER_MIN,
        "paper_ok": paper_ok,
        "live_n": live_n, "live_ev_r": live_ev, "live_min": PHASE0_LIVE_MIN,
        "live_ok": live_ok,
    }


def _bar(n: int, target: int, width: int = 10) -> str:
    filled = min(width, int(width * n / target)) if target else 0
    return "█" * filled + "░" * (width - filled)


# ===========================================================================
# CEO 簡報 —— 兩段式單一視窗
# ===========================================================================
def _section_normal() -> str:
    """✅ 一切正常·今日重點。"""
    from .trade_journal import discipline_stats, get_stats
    from .risk_manager import get_risk_status

    lines = ["✅ <b>一切正常 · 今日重點</b>", "━━━━━━━━━━━━━━━━"]

    # 1) 系統健康（探針）
    health = feature_health()
    warns = [m for sev, m in health if sev == "warn"]
    if warns:
        lines.append("🩺 系統：" + "；".join(warns))
    else:
        lines.append("🩺 系統：全部 worker 由監督器看顧，核心管線正常")

    # 2) 績效快照（真實帳本，R 為主）
    try:
        s7 = get_stats(7)
        s30 = get_stats(30)
        if s7["n_trades_closed"] or s30["n_trades_closed"]:
            lines.append(
                f"📊 績效：近7天 {s7['n_trades_closed']} 筆／勝率 {s7['win_rate_pct']}%"
                f"／期望 {s7['avg_r']:+.2f}R｜"
                f"近30天 {s30['n_trades_closed']} 筆／{s30['win_rate_pct']}%"
                f"／{s30['avg_r']:+.2f}R")
        else:
            lines.append("📊 績效：近期尚無已平倉交易（樣本累積中）")
    except Exception as e:
        lines.append(f"📊 績效：讀取失敗（{type(e).__name__}）")

    # 3) 風控狀態（一行）
    try:
        rs = get_risk_status()
        icon = {"active": "🟢", "paused_daily": "🟡", "halted_weekly": "🔴"}.get(rs["status"], "⚪")
        lines.append(
            f"🛡 風控 {icon}：持倉 {rs['open_trades']}/{rs['max_concurrent']}｜"
            f"曝險 ${rs['total_risk_open_usd']:.0f}/${rs['risk_cap_usd']:.0f}｜"
            f"今日開倉 {rs['opened_today']}/{rs['daily_max_opens']}｜"
            f"今日 {rs['today_pnl_pct']:+.1f}%／週 {rs['week_pnl_pct']:+.1f}%")
    except Exception as e:
        lines.append(f"🛡 風控：讀取失敗（{type(e).__name__}）")

    # 3.5) 紀律遵守率（task #8 ⑥）
    try:
        dsc = discipline_stats(30)
        if dsc["overall_pct"] is not None:
            lines.append(
                f"🎯 紀律：{dsc['overall_pct']}%"
                f"（決斷 {dsc['decisiveness_pct'] if dsc['decisiveness_pct'] is not None else '—'}%／"
                f"不追高 {dsc['no_chase_pct'] if dsc['no_chase_pct'] is not None else '—'}%，近30天）")
        else:
            lines.append(
                f"🎯 紀律：資料累積中（決斷 {dsc['acted']}/{dsc['ghosted']}、"
                f"進場 {dsc['in_zone']}/{dsc['chased']}）")
    except Exception:
        pass

    # 4) 驗證進度（Phase 0）
    p = phase0_status()
    lines.append(
        f"🔬 驗證：模擬盤 {p['paper_n']}/{p['paper_min']} {_bar(p['paper_n'], p['paper_min'])}"
        f"｜真實 {p['live_n']}/{p['live_min']} {_bar(p['live_n'], p['live_min'])}")

    # 5) 功能完成度（一行摘要）
    d = feature_registry_dump()["counts"]
    lines.append(
        f"🧱 功能：已上線 {d.get('shipped', 0)}｜部分 {d.get('partial', 0)}"
        f"｜卡關 {d.get('blocked', 0)}｜規劃 {d.get('planned', 0)}")

    return "\n".join(lines)


def _section_decisions() -> str:
    """⚠️ 需發起人決策。"""
    lines = ["⚠️ <b>需發起人決策</b>", "━━━━━━━━━━━━━━━━"]
    any_item = False

    # 1) 決策佇列
    open_decs = _dec.list_open()
    if open_decs:
        any_item = True
        lines.append(_dec.render_open(open_decs))

    # 2) 待審對外內容（紅線 2）
    n_out = _outbox.count_pending()
    if n_out:
        any_item = True
        lines.append(f"📤 有 <b>{n_out}</b> 則對外內容待你核准 —— 輸入 /approve 查看")

    # 3) Phase 0 達標旗標（promotion_gate；達標也只是「可考慮宣告」）
    p = phase0_status()
    if p["ready"]:
        any_item = True
        lines.append("🎯 <b>Phase 0 硬門檻已達標</b>（模擬+真實樣本與期望值皆過）。\n"
                     "依紅線 3，系統<b>不會自動宣告解鎖</b> —— 是否對外宣告由你拍板。")

    # 4) 卡在使用者身上的功能（blocked 且需使用者「動作」；decision 類已在上面決策佇列呈現，不重複）
    blocked = [f for f in FEATURES
               if f["status"] == "blocked" and f.get("note") and f.get("by") != "decision"]
    if blocked:
        any_item = True
        for f in blocked:
            lines.append(f"⛔ {f['name']}：{f['note']}")

    if not any_item:
        lines.append("（今天沒有需要你拍板的事項，安心放著就好）")
    return "\n".join(lines)


def build_ceo_brief() -> str:
    """產生完整 CEO 簡報（兩段式）。純函式，可離線測試。"""
    now = dt.datetime.now(tz=dt.timezone.utc) + dt.timedelta(hours=8)  # 台北時間
    header = (f"🧭 <b>CEO 每日簡報</b>　{now.strftime('%Y-%m-%d %H:%M')} 台北\n"
              f"<i>由 Claude Code（監督人角色）自動彙整</i>")
    return f"{header}\n\n{_section_normal()}\n\n{_section_decisions()}"


# ===========================================================================
# Worker 主迴圈
# ===========================================================================
async def run_ceo_loop(tg, target_hour_utc: int = 1):
    """每日一次彙整簡報（預設 01:00 UTC = 09:00 台北，接在晨間宏觀之後）。"""
    print("[ceo_session] loop online（每日 CEO 彙整簡報）")
    # 啟動時把已知待決策事項種進佇列（idempotent）
    try:
        _dec.seed_known_decisions()
    except Exception as e:
        print(f"[ceo_session] seed decisions error: {type(e).__name__}: {e}")
    await asyncio.sleep(300)  # 啟動後延後，避開開機洗版
    while True:
        now = dt.datetime.now(tz=dt.timezone.utc)
        nxt = now.replace(hour=target_hour_utc, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += dt.timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            brief = build_ceo_brief()
            if tg is not None:
                await tg.send_message(brief, parse_mode="HTML")
                print("[ceo_session] brief sent")
        except Exception as e:
            print(f"[ceo_session] error: {type(e).__name__}: {e}")


if __name__ == "__main__":
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    import re
    _dec.seed_known_decisions()
    print("=== phase0 ===")
    print(phase0_status())
    print("\n=== feature counts ===")
    print(feature_registry_dump()["counts"])
    print("\n=== CEO BRIEF (plain) ===")
    print(re.sub(r"<[^>]+>", "", build_ceo_brief()))
