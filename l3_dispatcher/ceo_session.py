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
# v150：自評敘事要宣稱「edge 已證實」時，扣費後（net_r）配對樣本至少要這麼多筆才算證據。
#   沿用本專案既有的 n≥30 慣例（task#27 加密 EV 顯著性定案：n_eff≈20<30＝未證實）。
PAPER_NET_MIN_N = 30


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


def _count_closed_net(table: str) -> tuple[int, float | None]:
    """該表「已平倉且**有淨值口徑**」的配對子集：筆數 + 平均 net_r。

    net_r ＝毛 R − 費用 R − 止損滑價 R（`paper_journal.compute_net_r`，v118 起落帳）。
    目前只有 paper_trades 有這一欄；trades（真錢）**沒有** → 回 `(0, None)`＝「無淨值
    證據」，而不是 `(0, 0.0)`——0.0 會被下游誤讀成「淨期望值恰為零」。

    排除規則與 `_count_closed` 一致（entry_expired 不是一筆真實交易）。另外要求 realized_r
    也非空，讓這個子集能與毛口徑**成對比較**（不同子集比較毛/淨等於比兩批不同的單）。

    為什麼需要它：Phase 0 若用毛 R 判期望值，等於假設手續費與滑價為零。實測配對子集
    （同一批 165 筆同時有毛與淨）毛 +0.116R、淨 +0.052R——費用吃掉 0.065R、超過一半；
    拆引擎後加密毛 +0.005R、淨 −0.047R（點估計翻負，n=109、t=−0.52 兩者皆不顯著）。
    真錢側費用更不可迴避，故 live 閘門改為「必須有淨值證據才可能成立」（見 phase0_status）。
    """
    try:
        conn = sqlite3.connect(_TJ_DB, timeout=5)
        try:
            row = conn.execute(
                f"SELECT COUNT(*), AVG(net_r) FROM {table} WHERE status='closed' "
                f"AND IFNULL(exit_reason,'') != 'entry_expired' "
                f"AND net_r IS NOT NULL AND realized_r IS NOT NULL"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return 0, None          # 表或 net_r 欄不存在＝無淨值證據
    except Exception:
        return 0, None
    n = int(row[0] or 0)
    if n == 0 or row[1] is None:
        return 0, None
    return n, round(float(row[1]), 3)


def _paper_edge_tstat_ex(table: str = "paper_trades",
                         setup: str | None = None,
                         basis: str = "gross") -> tuple[int, float | None]:
    """紙上『真實已平倉』單樣本 t 值 + 樣本數（檢定 EV 是否顯著異於 0）。

    t = mean / (sd/√n)。n<2 或 sd=0 → t=None（無法檢定，誠實不報）。與 _count_closed 同
    口徑（排除 entry_expired）。供 CEO 自評誠實區分『樣本不足』與『樣本足但 edge 未顯著
    (t<2)』——治本 _synthesize_bottleneck 把『真錢 0/30 人工閘恆成立』誤報成『樣本供給不足』。
    註：此為名目 t，未做 n_eff 叢聚校正（叢聚會讓真 t 更低），故為樂觀上界；t<2 即未證實。

    v150 新增 basis：
      - "gross"：realized_r（毛 R，未扣費用/滑價）——與歷史口徑相同。
      - "net"  ：net_r（扣費後，`paper_journal.compute_net_r`，v118 起落帳）。額外要求
        realized_r 也非空＝**配對子集**，讓毛/淨兩個 t 建立在同一批交易上、可直接對照
        （不配對的話會拿 348 筆的毛去比 166 筆的淨，差異分不清是費用還是換了樣本）。
    回傳 n 是為了讓呼叫端能判「淨值覆蓋是否足夠」——覆蓋太少時 t 再漂亮也不算證據。"""
    col = "net_r" if basis == "net" else "realized_r"
    try:
        conn = sqlite3.connect(_TJ_DB, timeout=5)
        try:
            # v131：支援分引擎口徑——混引擎單一 t（曾測得 2.93）會把「美股已過閘、
            #   加密未過」兩個相反真相攪成一個誤導數字（獵捕workflow口徑稽核發現）。
            _sql = (f"SELECT {col} FROM {table} WHERE status='closed' "
                    f"AND IFNULL(exit_reason,'') != 'entry_expired' "
                    f"AND {col} IS NOT NULL")
            if basis == "net":
                _sql += " AND realized_r IS NOT NULL"      # 配對子集
            _args: tuple = ()
            if setup:
                _sql += " AND setup=?"
                _args = (setup,)
            rows = conn.execute(_sql, _args).fetchall()
        finally:
            conn.close()
    except Exception:
        return 0, None                     # 表或 net_r 欄不存在＝無此口徑的證據
    rs = [float(r[0]) for r in rows if r[0] is not None]
    n = len(rs)
    if n < 2:
        return n, None
    mean = sum(rs) / n
    var = sum((x - mean) ** 2 for x in rs) / (n - 1)
    sd = var ** 0.5
    if sd <= 0:
        return n, None
    return n, mean / (sd / (n ** 0.5))


def _paper_edge_tstat(table: str = "paper_trades",
                      setup: str | None = None,
                      basis: str = "gross") -> float | None:
    """`_paper_edge_tstat_ex` 的 t-only 薄包裝（保留既有呼叫端）。"""
    return _paper_edge_tstat_ex(table, setup, basis)[1]


def phase0_status() -> dict:
    """回 Phase 0 解鎖進度（純偵測）。

    硬門檻（寫死）：模擬盤 ≥100 筆 且 真實小額 ≥30 筆且整體期望值為正。
    ready=True 也 **只代表「達標、可由人類考慮宣告」**，本系統永遠不會自動宣告解鎖。

    v149 口徑修正（fail-closed）：`*_ev_r` 一律是**毛 R**（未扣費用與滑價）。真錢那 30 筆
    的費用是不可迴避的真實成本，用毛 R 判「期望值為正」會系統性高估——實測紙上配對子集
    費用吃掉 0.065R，加密引擎點估計因此由正翻負。故 live 閘門加上第三個條件：**必須有
    淨值證據且淨期望值為正**。`trades` 表目前沒有 net_r 欄 → `live_ok` 恆為 False，且以
    `live_gate_reason='live_net_missing'` **明說原因**，不讓這個缺口靜默通過。
    """
    paper_n, paper_ev = _count_closed("paper_trades")
    live_n, live_ev = _count_closed("trades")
    paper_net_n, paper_net_ev = _count_closed_net("paper_trades")
    live_net_n, live_net_ev = _count_closed_net("trades")

    paper_ok = paper_n >= PHASE0_PAPER_MIN
    live_net_ok = live_net_n >= PHASE0_LIVE_MIN and (live_net_ev or 0) > 0
    live_ok = live_n >= PHASE0_LIVE_MIN and live_ev > 0 and live_net_ok

    if live_n < PHASE0_LIVE_MIN:
        reason = "live_sample_short"          # 真錢樣本未達 30 筆（目前 0＝紅線①人工閘）
    elif live_net_n == 0:
        reason = "live_net_missing"           # 有毛R無淨R＝淨值會計尚未落地，不得放行
    elif live_net_n < PHASE0_LIVE_MIN:
        reason = "live_net_coverage_short"    # 淨值只覆蓋一部分樣本
    elif not live_net_ok:
        reason = "live_net_ev_not_positive"   # 扣費後期望值不為正
    elif live_ev <= 0:
        reason = "live_gross_ev_not_positive"
    else:
        reason = None
    return {
        "ready": paper_ok and live_ok,
        "paper_n": paper_n, "paper_ev_r": paper_ev, "paper_min": PHASE0_PAPER_MIN,
        "paper_ok": paper_ok,
        "paper_net_n": paper_net_n, "paper_ev_r_net": paper_net_ev,
        "live_n": live_n, "live_ev_r": live_ev, "live_min": PHASE0_LIVE_MIN,
        "live_ok": live_ok,
        "live_net_n": live_net_n, "live_ev_r_net": live_net_ev,
        "live_net_ok": live_net_ok,
        "ev_basis": "gross",                  # *_ev_r 的口徑；*_ev_r_net 才是扣費後
        "live_gate_reason": reason,
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
        # v199：兩個讀取端都改成三態。⛔ 讀不出來不得渲染成「無離線缺口／剛啟動」——
        # 那是把未知講成正面保證（紅線③）。
        last, last_status = liveness.read_last_status()
        gaps, gaps_status = liveness.recent_gaps_status(86400)
        if gaps_status == liveness.LOAD_UNREADABLE:
            gap_txt = "⚠️ 離線缺口帳本讀不出來（≠ 沒有缺口）"
        elif gaps:
            known = [float(g["gap_sec"]) for g in gaps if g.get("gap_sec") is not None]
            unknown_n = len(gaps) - len(known)
            worst_txt = (f"（最長 {liveness._fmt_duration(max(known))}）" if known else "")
            tail = f"，其中 {unknown_n} 次時長不明" if unknown_n else ""
            gap_txt = f"過去24h 偵測到 {len(gaps)} 次離線缺口{worst_txt}{tail}"
        else:
            gap_txt = "過去24h 無離線缺口"
        if last_status == liveness.LOAD_UNREADABLE:
            lines.append(f"🔌 連續性：⚠️ 存活戳記讀不出來（≠ 剛啟動；無法判斷心跳新鮮度）｜{gap_txt}")
        elif last and last.get("ts"):
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
    # v149：毛/淨兩個口徑並列。只報數字不下結論——費用吃掉多少由你自己看（模擬盤樣本）
    if p.get("paper_net_n"):
        lines.append(
            f"　　└ 模擬盤期望值：毛 {p['paper_ev_r']:+.3f}R"
            f"｜淨（扣費用滑價）{p['paper_ev_r_net']:+.3f}R（n={p['paper_net_n']}）")
    if p.get("live_gate_reason") == "live_net_missing":
        lines.append("　　└ ⚠️ 真錢帳尚無淨值欄位 → Phase 0 真實閘 fail-closed（不以毛R放行）")

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
                           demo_n, demo_rejected, paper_t=None,
                           paper_t_net=None, net_n=0) -> str:
    """task#7 CEO 深度綜合：純函式、確定性跨 session 關聯推理（可離線測試）。
    把『樣本供給 × 模擬盤下單健康 × 復盤優化器晉升狀態』綜合成單一瓶頸歸因——這才是
    真綜合分析，非欄位回音。資料不足就誠實說無法綜合（紅線③不臆測）。

    治本 v101（止損復盤稽核發現的自評誤報）：舊版 sample_short = paper<min OR live<min，
    因真錢 live 永遠 0/30（紅線①人工閘恆成立）→ 不論紙上累積多少都謊報『樣本供給不足』，
    掩蓋真瓶頸（樣本其實已足、是 edge 未達統計顯著 t<2）。改成把三條獨立的軸分開歸因。
    paper_t：紙上 EV 的名目 t 值（_paper_edge_tstat），None=未提供則退回舊式描述。

    v150 口徑修正（與 v149 phase0 閘同一species、第二道門）：舊版只看**毛 R** 的 t，
    但費用是不可迴避的真實成本——實測紙上配對子集費用吃掉 0.065R，美股毛 t≈2.64 扣費後
    淨 t≈1.70（跌破 2）、加密毛 +0.004 扣費後翻負。若哪天毛 t 先過 2 而淨 t 沒過，舊版會
    把瓶頸敘事從『edge 未證實』翻成『只差真錢人工閘』＝對本人謊報一個假的準備就緒。
    故『已顯著』改成**毛與淨都要過**，且淨值覆蓋須 ≥`PAPER_NET_MIN_N`；沒有淨值證據時
    一律當作**未證實**（fail-closed），並明說是哪一邊沒過，不讓缺口靜默通過。
    paper_t_net/net_n：淨口徑 t 與其配對樣本數（_paper_edge_tstat_ex(basis="net")）。"""
    if paper_n < 8:
        return (f"  本輪樣本過少（紙上 {paper_n}/{paper_min}），尚無足夠基礎做跨 session "
                "綜合分析——誠實不臆測。")
    # 毛/淨兩道顯著性（任一沒過就不算已證實；無證據＝沒過，fail-closed）
    sig_gross = paper_t is not None and abs(paper_t) >= 2.0
    net_cov_ok = net_n >= PAPER_NET_MIN_N
    sig_net = paper_t_net is not None and net_cov_ok and abs(paper_t_net) >= 2.0
    # 兩個口徑都沒給＝退回舊式描述（不臆測顯著與否），直接落到人工閘/達標分支
    _has_t = not (paper_t is None and paper_t_net is None)
    # 優先序：①紙上樣本真不足 ②紙上足但 edge 未證實(毛或淨沒過)＝真瓶頸 ③待真錢人工閘 ④全達標
    if paper_n < paper_min:
        bottleneck = "樣本供給不足（非策略失效）"
    elif _has_t and not sig_gross:
        _t_txt = f"名目 t≈{paper_t:.2f}<2" if paper_t is not None else "毛口徑 t 無法檢定"
        bottleneck = (f"紙上樣本已足（{paper_n}≥{paper_min}）但 edge 未達統計顯著"
                      f"（{_t_txt}，未證實）——真瓶頸是 edge 大小、非樣本量；"
                      "衝量無用，需把 edge 做大")
    elif _has_t and not sig_net:
        # 毛口徑過了、淨口徑沒過或沒證據——這是 v150 要擋住的假準備就緒
        if paper_t_net is None or not net_cov_ok:
            _why = (f"但**扣費後口徑的證據不足**（配對淨值樣本 {net_n}<{PAPER_NET_MIN_N} 筆）"
                    "——費用是否吃掉這個 edge 未知，不得視為已證實")
        else:
            _why = (f"但扣費後跌破門檻（淨 t≈{paper_t_net:.2f}<2，n={net_n}）"
                    "——毛利上的 edge 被費用與滑價吃掉，未證實")
        bottleneck = (f"紙上毛口徑已顯著（名目 t≈{paper_t:.2f}≥2）{_why}；"
                      "真瓶頸是**扣費後**的 edge，非樣本量")
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
    _net_n, _net_t = _paper_edge_tstat_ex("paper_trades", setup="deepdive", basis="net")
    body = _synthesize_bottleneck(
        p.get("paper_n", 0), p.get("paper_min", 100),
        p.get("live_n", 0), p.get("live_min", 30), demo_n, demo_rejected,
        paper_t=_paper_edge_tstat("paper_trades", setup="deepdive"),
        paper_t_net=_net_t, net_n=_net_n)
    # v131：分引擎附註（瓶頸敘事以加密 deepdive 為主體＝OKX 路徑的鑰匙；美股另列）
    # v150：毛/淨並列。美股毛 t 過 2 但扣費後跌破——只報毛會讓人以為這條線已經成立。
    _us_t = _paper_edge_tstat("paper_trades", setup="us_breakout")
    _us_net_n, _us_net_t = _paper_edge_tstat_ex(
        "paper_trades", setup="us_breakout", basis="net")
    if _us_t is not None:
        _net_txt = (f"扣費後淨 t≈{_us_net_t:.2f}（配對 n={_us_net_n}）"
                    if _us_net_t is not None else f"扣費後淨值證據不足（n={_us_net_n}）")
        body += (f"\n（分引擎：美股毛 t≈{_us_t:.2f}（預註冊統計閘 PSRc≥0.95 是在**毛利口徑**"
                 f"上通過的）、{_net_txt}——真錢 0 筆，兩個口徑都不作對外宣稱）")
    body += _dp2_scoreboard_line()
    return "🧠 <b>系統自評</b>（跨 session 綜合·確定性推理非欄位回音）：\n" + body


def _dp2_scoreboard_line() -> str:
    """v184：dp2（數據面修復後,v178 起）世代單獨記分——「修好沒有,數字自己說話」。
    每日一行:n_closed/淨R合計/在場數;n<30 明標未驗證。失敗回空字串不擋日報。"""
    try:
        import json as _json
        import sqlite3
        from botpaths import db_path
        from l3_dispatcher import universe_provenance as up
        conn = sqlite3.connect(f"file:{db_path('trade_journal.db')}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT status, net_r, plan_snapshot FROM paper_trades "
            "WHERE setup='deepdive' AND plan_snapshot IS NOT NULL").fetchall()
        conn.close()
        n_closed = n_open = 0
        net_sum = 0.0
        for status, net_r, snap in rows:
            if up.data_plane_of_row({"plan_snapshot": snap}) != up.DATA_PLANE:
                continue
            if status == "closed":
                n_closed += 1
                net_sum += float(net_r or 0)
            elif status in ("open", "pending"):
                n_open += 1
        if n_closed == 0 and n_open == 0:
            return ""
        tag = "未驗證(n<30)" if n_closed < 30 else "可初讀"
        return (f"\n📊 dp2 世代記分（數據面修復後,獨立結算）：已平 {n_closed} 筆 "
                f"淨 {net_sum:+.2f}R｜在場 {n_open}｜{tag}")
    except Exception:  # noqa: BLE001
        return ""


def probe_learning_loop(window_hours: float = 26.0, data_dir_fn=None) -> dict:
    """v120（稽核rank10）：學習迴圈健康探針——掃兩優化器最新一輪審計，找出
    『統計已放行（l2_passed=True / l2_summary ✅）但 promote=False』的卡住晉升。

    背景：v114 事故＝一個已過 L2 四關的晉升被 self-check bug 卡死，在 audit jsonl 裡
    沉默數週無人聞問。此探針把「統計放行、工程卡住」這類最貴的靜默阻塞縮到隔日可見。
    回 {"stuck": [{file,bucket,reasons}], "rounds_checked": n,
        "examined": {file: n_in_window}, "unexamined": [{file, why}]}。

    v228：examined／unexamined 是「鍵在＝答案」——某支優化器沒進 examined，代表本次
    **根本沒看過它**（審計檔讀不到、或近窗一輪都沒跑），不是「看過且沒問題」。舊版把兩支
    的筆數加總成單一 rounds_checked，只要其中一支有量就足以讓產出端喊全體健康。"""
    import json as _json
    import time as _time
    if data_dir_fn is None:
        from botpaths import data_dir as data_dir_fn
    cutoff = (_time.time() - window_hours * 3600) * 1000
    stuck, checked = [], 0
    examined: dict = {}
    unexamined: list = []
    for fname in ("entry_policy_audit.jsonl", "auto_params_audit.jsonl"):
        try:
            lines = (data_dir_fn() / fname).read_text(encoding="utf-8").splitlines()
        except Exception:  # noqa: BLE001
            unexamined.append({"file": fname, "why": "read_fail"})
            continue
        seen_buckets = set()
        n_here = 0
        for line in reversed(lines[-800:]):        # 最新一輪在檔尾
            try:
                r = _json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if r.get("at_ms", 0) < cutoff:
                break                               # 出窗即停（檔案按時序 append）
            checked += 1
            n_here += 1
            b = r.get("bucket") or r.get("l2_bucket_key") or "?"
            if b in seen_buckets:
                continue
            seen_buckets.add(b)
            l2_ok = (r.get("l2_passed") is True
                     or str(r.get("l2_summary", "")).startswith("✅"))
            if l2_ok and not r.get("promote"):
                stuck.append({"file": fname, "bucket": b,
                              "reasons": (", ".join(map(str, r.get("reasons") or []))
                                          or r.get("note") or "未知非統計阻因")[:120]})
        if n_here:
            examined[fname] = n_here
        else:
            unexamined.append({"file": fname, "why": "no_rounds_in_window"})
    return {"stuck": stuck, "rounds_checked": checked,
            "examined": examined, "unexamined": unexamined}


_OPT_LABEL_ZH = {"entry_policy_audit.jsonl": "入場策略優化器",
                 "auto_params_audit.jsonl": "參數自動優化器"}
_UNEXAMINED_WHY_ZH = {"read_fail": "審計檔讀不到",
                      "no_rounds_in_window": "近窗內一輪都沒跑"}


def _section_learning_loop() -> str:
    """學習迴圈健康段（v120）：無卡住→一行安靜確認；有卡住→醒目列出。

    v228（同物種第 48 次）：兩支優化器只要**有一支沒被看過**，就不再喊「✅ 統計閘與晉升
    機構一致」。舊版判準是加總後的 rounds_checked>0，於是 7/30 起入場策略優化器整整
    三天沒跑（近窗零審計）時，這行仍每天對使用者宣稱全體一致——把「沒看」講成「看過沒事」。"""
    try:
        p = probe_learning_loop()
    except Exception:  # noqa: BLE001
        return ""
    if not p.get("rounds_checked"):
        return ""      # 兩支都沒量到（優化輪未跑）→ 不佔版面，也不宣稱健康
    unexamined = p.get("unexamined") or []
    gap_zh = "；".join(
        f"{_OPT_LABEL_ZH.get(u.get('file'), u.get('file'))}"
        f"（{_UNEXAMINED_WHY_ZH.get(u.get('why'), u.get('why'))}）"
        for u in unexamined)
    done_zh = "／".join(_OPT_LABEL_ZH.get(f, f) for f in (p.get("examined") or {}))
    if not p["stuck"]:
        if unexamined:
            return ("🔄 <b>學習迴圈健康</b>：🟡 <b>只檢了一部分，不等於全體健康</b>"
                    f"——已檢的{done_zh}近窗 {p['rounds_checked']} 筆審計中無「已放行被卡」"
                    f"晉升；但 {gap_zh} ⇒ 這部分<b>狀態未知，不是沒問題</b>")
        return ("🔄 <b>學習迴圈健康</b>：✅ 統計閘與晉升機構一致"
                f"（近窗掃 {p['rounds_checked']} 筆審計，無「已放行被卡」晉升）")
    lines = [f"🔄 <b>學習迴圈健康</b>：⚠️ <b>{len(p['stuck'])} 個晉升已過統計閘但被非統計原因卡住</b>"
             "（v114 教訓：這類靜默阻塞最貴，請優先排查）"]
    for s in p["stuck"][:5]:
        lines.append(f"　• {s['bucket']}（{s['file'].split('_audit')[0]}）：{s['reasons']}")
    if unexamined:
        lines.append(f"　⚠️ 另有未檢：{gap_zh} ⇒ 狀態未知，上列不是全貌")
    return "\n".join(lines)


def build_ceo_brief() -> str:
    """產生完整 CEO 簡報（四段式）。純函式，可離線測試。"""
    now = dt.datetime.now(tz=dt.timezone.utc) + dt.timedelta(hours=8)  # 台北時間
    header = (f"🧭 <b>CEO 每日簡報</b>　{now.strftime('%Y-%m-%d %H:%M')} 台北\n"
              f"<i>由 Claude Code（監督人角色）自動彙整</i>")
    parts = [header, _section_normal(), _section_self_assessment(),
             _section_learning_loop(), _section_decisions()]
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
