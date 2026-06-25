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
import json
import sqlite3
import time

from botpaths import data_dir, db_path as _db_path

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
    {"key": "leverage_tier", "name": "槓桿/風險分級（決策已定：維持明確 15x／$100）", "kind": "risk", "status": "shipped",
     "note": "v50 使用者授權 CEO 決定→維持現狀；出廠 tier 預設仍保守(5x/1%)保護自架者；生效值已於日報透明化"},
    {"key": "coach_monitor", "name": "教練式持倉提醒（追高/止損/熔斷/手續費侵蝕/降檔）", "kind": "risk", "status": "shipped"},
    {"key": "discipline_kpi", "name": "紀律遵守率 KPI（決斷率+不追高率）", "kind": "risk", "status": "shipped"},
    # 治理 / 對外
    {"key": "decision_registry", "name": "決策佇列（需發起人拍板）", "kind": "gov", "status": "shipped"},
    {"key": "outbox", "name": "對外內容待審佇列（/approve）", "kind": "gov", "status": "partial",
     "note": "模組+/approve 已上線；目前 Threads 草稿走 docs/threads 人工佇列，尚未自動餵入 outbox"},
    {"key": "promotion_gate", "name": "Phase 0 達標偵測（AI 不自我宣告）", "kind": "gov", "status": "shipped"},
    {"key": "rebate_tiers", "name": "返佣誠實分級標籤", "kind": "gov", "status": "shipped"},
    # 驗證真實性
    {"key": "okx_paper", "name": "OKX 模擬盤自動下單（驗證持倉真實性）", "kind": "verify", "status": "partial",
     "note": "程式庫+操盤手已建妥並離線測試（demo_trader 44/44、demo_guard 全綠、demo_operator 23/23）；"
             "預設雙鑰待命（DEMO_OPERATOR_ACTIVE 關），待單次 --cycle-once 監督試跑後再常駐"},
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
    """某表「真實已平倉」筆數 + 平均 R（表不存在/無欄位 → (0, 0.0)）。

    排除 exit_reason='entry_expired'：那是限價單掛了但價格沒走到、從未成交的單，
    realized_r 一律 0，**不是一筆真實交易**。若把它算進 Phase 0 解鎖門檻會兩頭失真——
    既高估筆數（含它 35 / 不含它才 24），又把 0R 拉低真實期望值（0.377 / 真實 0.549）。
    與 paper_audit.load_closed() 的排除規則一致；紅線③：解鎖進度不可灌水。
    """
    try:
        conn = sqlite3.connect(_TJ_DB, timeout=5)
        try:
            row = conn.execute(
                f"SELECT COUNT(*), COALESCE(AVG(realized_r), 0) "
                f"FROM {table} WHERE status='closed' "
                f"AND IFNULL(exit_reason,'') != 'entry_expired'"
            ).fetchone()
            return int(row[0] or 0), round(float(row[1] or 0), 3)
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return 0, 0.0
    except Exception:
        return 0, 0.0


def _paper_edge_tstat(table: str = "paper_trades") -> float | None:
    """紙上『真實已平倉』realized_r 的單樣本 t 值（檢定 EV 是否顯著異於 0）。

    t = mean / (sd/√n)。n<2 或 sd=0 → None（無法檢定，誠實不報）。與 _count_closed 同
    口徑（排除 entry_expired）。供 CEO 自評誠實區分『樣本不足』與『樣本足但 edge 未顯著
    (t<2)』——治本 _synthesize_bottleneck 把『真錢 0/30 人工閘恆成立』誤報成『樣本供給不足』。
    註：此為名目 t，未做 n_eff 叢聚校正（叢聚會讓真 t 更低），故為樂觀上界；t<2 即未證實。"""
    try:
        conn = sqlite3.connect(_TJ_DB, timeout=5)
        try:
            rows = conn.execute(
                f"SELECT realized_r FROM {table} WHERE status='closed' "
                f"AND IFNULL(exit_reason,'') != 'entry_expired' "
                f"AND realized_r IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return None
    rs = [float(r[0]) for r in rows if r[0] is not None]
    n = len(rs)
    if n < 2:
        return None
    mean = sum(rs) / n
    var = sum((x - mean) ** 2 for x in rs) / (n - 1)
    sd = var ** 0.5
    if sd <= 0:
        return None
    return mean / (sd / (n ** 0.5))


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

    # 1.5) 連續性（v50 / task #22）—— daemon 心跳新鮮度 + 過去 24h 離線缺口回顧。
    #      把「引擎到底有沒有不間斷地跑」攤在日報，使用者不必翻 log。
    try:
        from . import liveness
        last = liveness.read_last()
        gaps = liveness.recent_gaps(86400)
        if gaps:
            worst = max(g.get("gap_sec", 0) for g in gaps)
            gap_txt = f"過去24h 偵測到 {len(gaps)} 次離線缺口（最長 {liveness._fmt_duration(worst)}）"
        else:
            gap_txt = "過去24h 無離線缺口"
        if last and last.get("ts"):
            age_min = (time.time() - float(last["ts"])) / 60
            fresh = "正常" if age_min <= 20 else f"⚠️已 {age_min:.0f} 分未更新"
            lines.append(f"🔌 連續性：心跳 {age_min:.0f} 分前（{fresh}）｜{gap_txt}")
        else:
            lines.append(f"🔌 連續性：尚無存活戳記（剛啟動）｜{gap_txt}")
    except Exception:
        pass

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

    # 3.2) 生效風控值透明化（v50 / task #21）—— 把「實際生效」的槓桿與單筆風險攤開。
    #      .env 個人設定會悄悄覆寫出廠保守預設；非機密、僅推到本人系統頻道。
    try:
        from botconfig import CONFIG
        bal = CONFIG.account_balance_usd
        risk = CONFIG.risk_per_trade_usd
        lev = CONFIG.default_leverage
        risk_pct = (risk / bal * 100) if bal else 0.0
        tier = CONFIG.tier
        line = (f"⚙️ 生效風控：每筆風險 ${risk:.0f}（帳戶 {risk_pct:.1f}%）"
                f"｜槓桿 {lev}x｜本金 ${bal:,.0f}（{tier.label}級）")
        if lev != tier.leverage_cap or abs(risk_pct - tier.risk_pct_default) > 0.05:
            line += (f"\n　　↳ 個人設定，已覆寫出廠保守預設"
                     f"（{tier.leverage_cap}x／{tier.risk_pct_default:.1f}%）；純紙上、零真錢")
        lines.append(line)
    except Exception as e:
        lines.append(f"⚙️ 生效風控：讀取失敗（{type(e).__name__}）")

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

    # 4.5) OKX 模擬盤實單驗證（task #4/#39）—— 真實成交、零真錢。
    #      只做透明呈現：demo 樣本走 demo_trades 表，**不**計入上方 Phase 0「真實」門檻
    #      （那條讀 trades 表＝人類親手按下的真錢，紅線①）。demo_n 達標與否由人判讀。
    try:
        from . import demo_journal
        from .demo_operator import is_active as _demo_active
        dn, dev = demo_journal.count_closed_for_phase0()
        ds = demo_journal.get_demo_stats(30)
        n_live = ds.get("n_open", 0) + ds.get("n_pending", 0)
        state = "運行中" if _demo_active() else "待命（DEMO_OPERATOR_ACTIVE 未開）"
        if dn or n_live:
            lines.append(
                f"🧪 模擬盤實單：已平倉 {dn} 筆／期望 {dev:+.2f}R｜在場 {n_live} 筆｜{state}"
                f"（OKX 真實成交·零真錢；不計入上方真實門檻）")
        else:
            lines.append(f"🧪 模擬盤實單：尚無樣本｜{state}（OKX 真實成交·零真錢）")
    except Exception:
        pass

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


def _synthesize_bottleneck(paper_n, paper_min, live_n, live_min,
                           demo_n, demo_rejected, paper_t=None) -> str:
    """task#7 CEO 深度綜合：純函式、確定性跨 session 關聯推理（可離線測試）。
    把『樣本供給 × 模擬盤下單健康 × 復盤優化器晉升狀態』綜合成單一瓶頸歸因——這才是
    真綜合分析，非欄位回音。資料不足就誠實說無法綜合（紅線③不臆測）。

    治本 v101（止損復盤稽核發現的自評誤報）：舊版 sample_short = paper<min OR live<min，
    因真錢 live 永遠 0/30（紅線①人工閘恆成立）→ 不論紙上累積多少都謊報『樣本供給不足』，
    掩蓋真瓶頸（樣本其實已足、是 edge 未達統計顯著 t<2）。改成把三條獨立的軸分開歸因。
    paper_t：紙上 EV 的名目 t 值（_paper_edge_tstat），None=未提供則退回舊式描述。"""
    if paper_n < 8:
        return (f"  本輪樣本過少（紙上 {paper_n}/{paper_min}），尚無足夠基礎做跨 session "
                "綜合分析——誠實不臆測。")
    # 優先序：①紙上樣本真不足 ②紙上足但 edge 未顯著(t<2)＝真瓶頸 ③紙上足待真錢人工閘 ④全達標
    if paper_n < paper_min:
        bottleneck = "樣本供給不足（非策略失效）"
    elif paper_t is not None and abs(paper_t) < 2.0:
        bottleneck = (f"紙上樣本已足（{paper_n}≥{paper_min}）但 edge 未達統計顯著"
                      f"（名目 t≈{paper_t:.2f}<2，未證實）——真瓶頸是 edge 大小、非樣本量；"
                      "衝量無用，需把 edge 做大")
    elif live_n < live_min:
        bottleneck = f"紙上樣本足、待真錢人工逐筆驗證（{live_n}/{live_min}，紅線①）"
    else:
        bottleneck = "樣本達標，待品質/顯著性驗證"
    demo_note = ""
    attempts = demo_n + demo_rejected
    if demo_rejected and attempts > 0 and demo_rejected / attempts >= 0.5:
        demo_note = (f"；⚠️ 模擬盤拒單率偏高（成交 {demo_n}/拒 {demo_rejected}）卡住實倉樣本"
                     "——v84 槓桿效率+品質篩選已治本，觀察是否回升")
    # 注意：L2 閘在「對齊樣本 n_aligned」非原始 paper_n（分桶碎裂使 n_aligned≪paper_n），
    #   故不以 paper_n 推斷晉升進度（會樂觀高估）；只誠實描述把關機制（對齊全文版口徑）。
    opt_line = ("復盤優化器：晉升由 L2 四關把關（對齊樣本 n_aligned≥30）——對齊樣本不足時 "
                "0 晉升（健康 fail-closed、非策略失效，詳見每日優化器報告）")
    return "\n".join([
        f"  • 樣本：紙上 {paper_n}/{paper_min}、模擬實倉 {demo_n}、真實 {live_n}/{live_min}{demo_note}",
        f"  • {opt_line}",
        f"  → <b>當前瓶頸＝{bottleneck}</b>。把關靠統計嚴謹度，真錢仍人工（紅線①）。",
    ])


def _section_self_assessment() -> str:
    """🧠 系統自評（CEO 深度綜合層 Layer 2 的最小落地，task#7）。"""
    try:
        p = phase0_status()
    except Exception:
        return ""
    demo_n = demo_rejected = 0
    try:
        from . import demo_journal
        demo_n, _ = demo_journal.count_closed_for_phase0()
        demo_rejected, _ = demo_journal.count_rejected()
    except Exception:
        pass
    body = _synthesize_bottleneck(
        p.get("paper_n", 0), p.get("paper_min", 100),
        p.get("live_n", 0), p.get("live_min", 30), demo_n, demo_rejected,
        paper_t=_paper_edge_tstat("paper_trades"))
    return "🧠 <b>系統自評</b>（跨 session 綜合·確定性推理非欄位回音）：\n" + body


def build_ceo_brief() -> str:
    """產生完整 CEO 簡報（三段式）。純函式，可離線測試。"""
    now = dt.datetime.now(tz=dt.timezone.utc) + dt.timedelta(hours=8)  # 台北時間
    header = (f"🧭 <b>CEO 每日簡報</b>　{now.strftime('%Y-%m-%d %H:%M')} 台北\n"
              f"<i>由 Claude Code（監督人角色）自動彙整</i>")
    parts = [header, _section_normal(), _section_self_assessment(), _section_decisions()]
    return "\n\n".join(p for p in parts if p)


# ===========================================================================
# Worker 主迴圈
# ===========================================================================
# ── task#79：每日 CEO 簡報啟動補跑（與 task#78 auto_tuner 同 bug-class 治本）──────────
# 根因：舊 run_ceo_loop 只在每日固定 01:00 UTC 觸發、無補跑、無 run_on_startup；daemon 因
# 開發迭代＋watchdog 頻繁重啟，幾乎從不存活滿一天剛好跨過該秒 → 每日 CEO 彙整簡報幾乎從不
# 可靠送達。治本＝以 UTC 日期戳記持久化「今日是否已送」，啟動暖機後若今日尚未送且已過觸發點
# 則『立即補送一次』，之後回正常每日節奏；至多每 UTC 日一次（不像 daily_macro 那樣每次重啟都
# 推，避免洗版）。純內部簡報（送使用者自己的 TG），非對外、非真錢，無紅線。
_CEO_STATE_NAME = "ceo_session_state.json"
_WARMUP_S_DEFAULT = 300


def _now_utc() -> dt.datetime:
    """UTC 現在（抽成函式供測試注入；勿用本地時間）。"""
    return dt.datetime.now(tz=dt.timezone.utc)


def _ceo_state_path():
    return data_dir() / _CEO_STATE_NAME


def _load_last_brief_date() -> str | None:
    """上次 CEO 簡報送出的 UTC 日期字串（YYYY-MM-DD），無則 None。讀失敗→None（保守＝會補送）。"""
    try:
        d = json.loads(_ceo_state_path().read_text(encoding="utf-8"))
        v = d.get("last_brief_date")
        return v if isinstance(v, str) else None
    except Exception:
        return None


def _stamp_brief_date(date_str: str) -> None:
    """戳記今日已送（UTC 日期）。寫失敗只印警告、不擋流程（最壞＝重啟後多補送一次，可接受）。"""
    try:
        p = _ceo_state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"last_brief_date": date_str}, ensure_ascii=False),
                     encoding="utf-8")
    except Exception as e:
        print(f"[ceo_session] 警告：寫入 brief 狀態失敗（不影響執行）：{type(e).__name__}: {e}")


async def _send_ceo_brief(tg) -> None:
    """產生並送出一次 CEO 簡報（純讀彙整 → 推使用者 TG）。例外只 log、不擋迴圈。"""
    try:
        brief = build_ceo_brief()
        if tg is not None:
            await tg.send_message(brief, parse_mode="HTML")
            print("[ceo_session] brief sent")
    except Exception as e:
        print(f"[ceo_session] error: {type(e).__name__}: {e}")


async def run_ceo_loop(tg, target_hour_utc: int = 1,
                       warmup_seconds: float = _WARMUP_S_DEFAULT):
    """每日一次彙整簡報（預設 01:00 UTC = 09:00 台北），含啟動補跑（task#79）。

    控制流（至多每 UTC 日送一次）：
      1. seed 已知待決策事項（idempotent）＋暖機 warmup_seconds（避開開機洗版）。
      2. **啟動補跑**：若「今日(UTC)尚未送」且「已過今日觸發點 target_hour_utc」→ 立即補送一次
         並戳記；解 daemon 在 01:00 UTC 之後才啟動／頻繁重啟而永遠跨不過觸發點的結構性失能。
      3. 否則睡到下一個 target_hour_utc，醒來送、戳記當日，無限循環。
    冪等：以 data_dir/ceo_session_state.json 的 last_brief_date(UTC) 去重；狀態檔遺失最壞只多
    補送一次（可接受，純內部簡報）。
    """
    print("[ceo_session] loop online（每日 CEO 彙整簡報；含啟動補跑 task#79）")
    # 啟動時把已知待決策事項種進佇列（idempotent）
    try:
        _dec.seed_known_decisions()
    except Exception as e:
        print(f"[ceo_session] seed decisions error: {type(e).__name__}: {e}")
    if warmup_seconds:
        await asyncio.sleep(warmup_seconds)  # 啟動後延後，避開開機洗版
    while True:
        now = _now_utc()
        today = now.date().isoformat()
        sent_today = (_load_last_brief_date() == today)
        past_fire = now.hour >= target_hour_utc
        if past_fire and not sent_today:
            # 啟動補送：今日已過觸發點但尚未送（多因 daemon 在 01:00 UTC 後才起／剛重啟）
            print(f"[ceo_session] 啟動補送：{today} 今日尚未送出 CEO 簡報（已過 {target_hour_utc:02d}:00 UTC）")
            await _send_ceo_brief(tg)
            _stamp_brief_date(today)
            continue  # 重算下一觸發點（此時 sent_today 已 True → 走排程睡眠分支）
        # 排程睡眠到下一個 target_hour_utc
        nxt = now.replace(hour=target_hour_utc, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += dt.timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        await _send_ceo_brief(tg)
        _stamp_brief_date(_now_utc().date().isoformat())


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
