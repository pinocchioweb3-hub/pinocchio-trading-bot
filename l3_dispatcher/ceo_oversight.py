"""👁 監督員 Session — Layer 1（task #40）：CEO 之上的「純讀守望者」。

使用者痛點（原話）：「我常不知道目前進度在哪、思緒中斷；希望有個環節專門監督 CEO
是否主動推進、掌握現狀、規劃下一步，並定期檢查；若 CEO 停滯就 Push 他。」

監督員設計成 **兩層**：

  Layer 1（本模組）＝ 進程內、確定性、無 LLM、重開機後仍在的「心跳 + 帳本」。
      它每 ~30 分鐘做一次純讀盤點：git 最後 commit 時效、Phase 0 進度、demo 實倉
      樣本、待決策/待審佇列、daemon 連續性；判定 CEO 目前是
          ADVANCING（最近有產出）/ STALLED（沒產出又沒卡在你身上）/
          BLOCKED_ON_USER（在等你拍板或核准）/ IDLE（剛啟動、資料不足）
      把結論寫進 oversight_ledger.json（單一真實來源，Layer 2 取用），
      並在「真的停滯」且超過冷卻時間時，發一則**私人** TG 提醒給發起人。

  Layer 2（排程喚醒的 Claude）＝ 真正能「推進 CEO」的那一層：每 15/30 分鐘
      重新喚起 Claude（監督人角色），讀本帳本，規劃下一步、必要時開新 Session。
      （Layer 2 由 scheduled task 建立，須使用者明確同意——本模組不建立它。）

紅線：
  • 純讀。不下任何單（真錢/模擬皆不碰）、不改參數、不碰 daemon。
  • TG 提醒只發到發起人**私人**頻道＝自我通知，非對外發布（不踩紅線②）。
  • 不自我宣告 Phase 0 解鎖（紅線③）；只把確定性數字攤開供人判讀。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from datetime import date as _date, datetime as _datetime

from botpaths import PROJECT_ROOT, data_dir

# 治理策略層（task #51）：PDP 新鮮度前置門 + 工具 allowlist + 死 schema。
# 純策略、零 IO，import 失敗也不可拖垮守望迴圈，故包 try。
try:
    from . import overseer_policy as _op
except Exception:
    _op = None

LEDGER_PATH = data_dir() / "oversight_ledger.json"

# 停滯門檻：超過這麼久沒有新 commit（且沒卡在使用者身上）＝ STALLED。
STALL_SEC = int(os.getenv("OVERSIGHT_STALL_HOURS", "12") or 12) * 3600
# 提醒冷卻：同一狀態最短間隔，避免每輪洗版（預設 6h）。
NUDGE_COOLDOWN_SEC = int(os.getenv("OVERSIGHT_NUDGE_COOLDOWN_HOURS", "6") or 6) * 3600
# Phase 0 demo 實倉樣本目標（與 ceo_session PHASE0_LIVE_MIN 同義，但 demo 僅供判讀）。
DEMO_SAMPLE_TARGET = 30

# --- 真錢執行器健康（監督員 r26 交棒）-------------------------------------
# v143 已讓消費器把「連續 fail-closed」寫進健康檔並自行 telegram 告警，但本帳本
# **沒有任何消費者**：真錢執行器連續 113 輪全被交易所 401 擋掉時，Layer 1 仍回報
# state=ADVANCING / blockers=[]（因為 git 有 commit）——與「宇宙留痕零消費者」同物種
# （有訊號、無消費者）。這裡把該訊號接進判定：連續故障 ≥ 門檻即進 blockers/system_faults。
LIVE_HEALTH_PATH = data_dir() / "atk_consumer_live_health.json"
# 健康檔太舊＝真錢消費器現在根本沒在跑（例如未啟用），舊 streak 不可當「現在的故障」。
LIVE_HEALTH_MAX_AGE_SEC = int(os.getenv("OVERSIGHT_LIVE_HEALTH_MAX_AGE_SEC", "900") or 900)
# 連續幾輪 fail-closed 才算故障（單輪可能只是網路抖動；消費器每分鐘一輪）。
LIVE_FAIL_BLOCK_ROUNDS = int(os.getenv("OVERSIGHT_LIVE_FAIL_ROUNDS", "3") or 3)
# 這些故障類別只有本人能解（金鑰／白名單／權限／餘額）→ 球在使用者，歸 blockers。
# 其餘（網路、程式例外…）＝系統故障，該 push CEO 修，歸 system_faults。
USER_ACTIONABLE_FAIL_CLASSES = {
    "auth_ip_whitelist": "OKX API 金鑰 IP 白名單未含目前出口 IP",
    "auth_key_invalid": "OKX API 金鑰無效或已過期",
    "auth_permission": "OKX API 金鑰缺少交易權限",
    "insufficient_balance": "帳戶餘額不足",
}

# --- 組織產出斷檔（監督員 r30）---------------------------------------------
# 同物種第四例（有訊號、無消費者）：2026-07-12～07-28 各席週報無聲斷檔 16.4 天，
# 無人發現。根因＝排程層的 lastRunAt **只記「觸發」不記「成功」**，所以排程看起來
# 一切正常，實際上那一輪什麼都沒產出；唯一的檢查是監督員 SKILL.md 的散文指示
# （靠 LLM 每輪肉眼比對檔齡）——LLM 漏看就沒有第二道防線。這裡把它變成程式：
# 直接量「各席最新 digest 檔的日期 vs 該席節奏」，連缺 ≥2 期即進 system_faults。
#
# 節奏表（機器可讀單一來源）。目前同一份節奏散落在三處：本表、排程 MCP 的 cron、
# docs/org/00-團隊章程總覽.md 的表格；其中章程表格**漏列** CoinGlass 稽核官（週二），
# 已於本輪一併補上。日後改節奏三處都要動——這是已知技術債，非本輪範圍。
ORG_DIGEST_DIR = PROJECT_ROOT / "docs" / "org" / "digests"
ORG_ROLE_CADENCE_DAYS = {
    "ceo": ("CEO 日報", 1),           # cron 0 9 * * *
    "pm": ("產品總監週報", 7),         # cron 30 9 * * 1（週一）
    "coinglass": ("CoinGlass 稽核官週報", 7),  # cron 25 9 * * 2（週二）
    "design": ("創意總監週報", 7),      # cron 30 9 * * 3（週三）
    "eng": ("高級程式設計師週報", 7),    # cron 30 9 * * 5（週五）
}
# 連缺幾期才算斷檔（SKILL.md 原文：「連缺 2 期以上」）。取 2 是為了容忍單次跳過
# （機器沒開／jitter 跨日），只在「真的連續沒產出」時才叫——寧可晚一期，不可狼來了。
ORG_DIGEST_MISS_PERIODS = int(os.getenv("OVERSIGHT_ORG_DIGEST_MISS_PERIODS", "2") or 2)


# ===========================================================================
# 純決策層（可離線測試，不碰 IO）
# ===========================================================================
def next_step(*, paper_n, paper_min, live_n, live_min,
              demo_n, demo_active, demo_rejected=0, demo_reject_hint=None) -> str:
    """依目前進度推「下一步該做什麼」（確定性階梯，非 LLM）。"""
    if not demo_active:
        return ("啟用 OKX 模擬盤操盤手：先做單次 --cycle-once 監督試跑，確認真實成交與"
                "帳本回寫無誤後再常駐，開始累積實倉樣本（零真錢）")
    if demo_n < DEMO_SAMPLE_TARGET:
        # 誠實卡點（v82 治本）：拒單率過高就揭露下單故障，不論已有幾筆成交——一筆僥倖成交
        #   不可掩蓋結構性下單被拒（原條件 demo_n==0 會被「一筆成交」繞過而謊稱『累積中』，
        #   把「下單 34/35 被拒」說成樂觀「累積中 1/30」）。
        _attempts = demo_n + demo_rejected
        _high_reject = _attempts >= 3 and demo_rejected / _attempts >= 0.5
        if demo_rejected > 0 and _high_reject:
            hint = f"（最常見拒因：{demo_reject_hint}）" if demo_reject_hint else ""
            # 治本（監督員 r53，承接 v93 task#12 誠實化）：不再硬寫「改帳戶模式 51010」當唯一卡點。
            #   親驗 demo_trades 原文：51010 僅佔少數且全為早期、改模式後已不再復發（成交可正常平倉）；
            #   真正大宗是系統端下單參數（標的不在 OKX 永續清單 / 張數規格 / 槓桿層級持倉上限），屬程式
            #   修整範圍（CEO/RB-1 待辦），非帳戶設定。據『實際最常見拒因』給對應建議，避免要本人白做工。
            _h = demo_reject_hint or ""
            if "51010" in _h or "帳戶模式" in _h:
                advice = ("拒因多為 OKX 帳戶模式（51010）：模擬盤帳戶須改為單幣種/跨幣種保證金"
                          "模式才可交易永續——這須由你在 OKX 後台設定。")
            else:
                advice = ("拒因多屬系統端下單參數（標的是否在 OKX 永續清單／張數規格／槓桿層級持倉"
                          "上限），屬程式修整範圍（CEO/RB-1 待辦），非帳戶設定；你無須調整 OKX。")
            return (f"⚠️ 模擬盤操盤手已下單 {_attempts} 次、其中 {demo_rejected} 次被 OKX 拒絕"
                    f"（實倉成交僅 {demo_n}/{DEMO_SAMPLE_TARGET}）{hint}——這是目前卡住實倉樣本的點。"
                    + advice)
        tail = f"；同時續累積紙上樣本（{paper_n}/{paper_min}）" if paper_n < paper_min else ""
        return f"模擬盤操盤手運行中，續累積 OKX 實倉樣本（{demo_n}/{DEMO_SAMPLE_TARGET}）{tail}"
    if live_n < live_min:
        return ("模擬盤實倉樣本已達階段目標；下一步是「真錢小額」驗證——"
                "依紅線①須由本人逐筆親手下單，非系統自動（待你拍板＋律師）")
    return "樣本接近 Phase 0 門檻，準備由人判讀是否對外宣告（系統不自我宣告，紅線③）"


def live_exec_verdict(health: dict | None, *, now_s: float | None = None,
                      max_age_sec: int = LIVE_HEALTH_MAX_AGE_SEC,
                      min_rounds: int = LIVE_FAIL_BLOCK_ROUNDS) -> dict | None:
    """把真錢消費器健康檔翻成「要不要當成阻塞」（純函式，可離線測）。

    回 None＝沒有現行故障（檔不存在／太舊／streak 未達門檻）；
    否則回 {rounds, cls, user_actionable, text}。
    """
    if not health:
        return None
    now_s = now_s if now_s is not None else time.time()
    try:
        rounds = int(health.get("consecutive_fail_rounds", 0) or 0)
        updated_at = float(health.get("updated_at", 0) or 0)
    except Exception:
        return None
    if rounds < min_rounds:
        return None
    # 新鮮度閘：舊檔＝消費器沒在跑，不可拿昨天的 streak 當今天的阻塞（舊快照陷阱）。
    age = now_s - updated_at
    if updated_at <= 0 or age > max_age_sec:
        return None

    cls = str(health.get("last_fail_class") or "unknown")
    known = USER_ACTIONABLE_FAIL_CLASSES.get(cls)
    first_fail = float(health.get("first_fail_ts", 0) or 0)
    dur = f"、已持續 {_fmt_age(now_s - first_fail)}" if first_fail > 0 else ""
    if known:
        text = (f"真錢執行器連續 {rounds} 輪被擋（{known}）{dur}——"
                f"每輪皆 fail-closed 未下單（零損失），但在你修好前一筆都送不出去")
    else:
        text = (f"真錢執行器連續 {rounds} 輪 fail-closed（故障類別：{cls}）{dur}——"
                f"未下單（零損失），但管線實質停擺，須查明修復")
    return {"rounds": rounds, "cls": cls, "user_actionable": bool(known), "text": text}


def org_digest_verdict(latest_by_role: dict | None, *, today,
                       cadence: dict | None = None,
                       miss_periods: int = ORG_DIGEST_MISS_PERIODS) -> dict | None:
    """把「各席最新 digest 日期」翻成「有沒有斷檔」（純函式，可離線測）。

    latest_by_role: {role: datetime.date}；today: datetime.date。
    回 None＝無斷檔；否則回 {roles, text, worst_age_days}。

    兩個不誤報的守則：
      • latest_by_role 為空（目錄不存在／零檔）＝環境問題（換機器、淺 clone），
        不是斷檔——回 None，交由人工，不製造假故障。
      • 某席「從未產出過」＝沒有基準可算「遲了幾期」，跳過該席（同理不誤報）。
    """
    if not latest_by_role:
        return None
    cadence = cadence or ORG_ROLE_CADENCE_DAYS
    late = []
    for role, (label, cad_days) in cadence.items():
        d = latest_by_role.get(role)
        if d is None:
            continue
        age_days = (today - d).days
        if age_days > cad_days * miss_periods:
            late.append({
                "role": role, "label": label, "age_days": age_days,
                "cadence_days": cad_days, "missed_periods": age_days // cad_days,
            })
    if not late:
        return None
    late.sort(key=lambda x: -x["age_days"])
    parts = [f"{x['label']}最新為 {x['age_days']} 天前（節奏每 {x['cadence_days']} 天，"
             f"已缺約 {x['missed_periods']} 期）" for x in late]
    text = ("組織產出斷檔：" + "；".join(parts)
            + "——排程 lastRunAt 只記『觸發』不記『成功』，須查該席排程是否無聲失敗（並代補產）")
    return {"roles": late, "text": text, "worst_age_days": late[0]["age_days"]}


def assess(*, now_ms, commit_age_sec, paper_n, paper_min, live_n, live_min,
           demo_n, demo_live, demo_active, open_decisions, pending_outbox,
           demo_rejected=0, demo_reject_hint=None, real_output_age_sec=None,
           live_exec=None, org_digest=None,
           last_nudge_ms=0, stall_sec=STALL_SEC, nudge_cooldown_sec=NUDGE_COOLDOWN_SEC) -> dict:
    """核心判定（純函式）。回 state / next_step / blockers / should_nudge。

    state 邏輯：
      有待你拍板/核准         → BLOCKED_ON_USER（球在你那；CEO 不算停滯）
      最近有 commit           → ADVANCING
      沒產出又沒卡你身上       → STALLED（這才是該 Push CEO 的情況）
      commit 時效未知（無 git）→ IDLE
    """
    blockers: list[str] = []
    system_faults: list[str] = []
    if open_decisions:
        blockers.append(f"{open_decisions} 項決策待你拍板")
    if pending_outbox:
        blockers.append(f"{pending_outbox} 則對外內容待你核准（/approve）")
    # r26：真錢執行器持續 fail-closed＝管線實質停擺，不可再回報「一切推進中」。
    #   只有本人能解的（白名單/金鑰/餘額）歸 blockers（球在你）；其餘歸 system_faults（該 push CEO）。
    if live_exec:
        if live_exec.get("user_actionable"):
            blockers.append(live_exec["text"])
        else:
            system_faults.append(live_exec["text"])
    # r30：組織產出斷檔＝系統故障（該 push CEO／監督員代補產），球不在使用者。
    if org_digest:
        system_faults.append(org_digest["text"])

    ns = next_step(paper_n=paper_n, paper_min=paper_min, live_n=live_n,
                   live_min=live_min, demo_n=demo_n, demo_active=demo_active,
                   demo_rejected=demo_rejected, demo_reject_hint=demo_reject_hint)

    # v84 task#7（CEO 深度綜合）：ADVANCING 不再只看 git commit 時效——補「實質產出代理」
    #   （近期紙上活動：新進場/平倉）。無關 commit 不再謊報推進；有真產出即使無 commit 也算推進；
    #   兩者皆停滯才 STALLED（這才該 push CEO）。
    recent_commit = commit_age_sec is not None and commit_age_sec < stall_sec
    recent_output = real_output_age_sec is not None and real_output_age_sec < stall_sec
    if commit_age_sec is None and real_output_age_sec is None:
        state = "IDLE"
    elif blockers:
        state = "BLOCKED_ON_USER"
    elif recent_commit or recent_output:
        state = "ADVANCING"
    else:
        state = "STALLED"

    # 系統故障蓋過 ADVANCING：commit 照常不代表管線活著（r26 實例＝真錢側連續 113 輪全滅
    #   卻回報 ADVANCING）。STALLED 語意＝「該 push CEO 修」，正是這種情況。
    if system_faults and state == "ADVANCING":
        state = "STALLED"

    cooldown_ok = (now_ms - last_nudge_ms) >= nudge_cooldown_sec * 1000
    # 只在「STALLED（該 push CEO）」或「BLOCKED_ON_USER（提醒你有待辦）」且過冷卻才提醒。
    should_nudge = state in ("STALLED", "BLOCKED_ON_USER") and cooldown_ok

    return {
        "state": state,
        "next_step": ns,
        "blockers": blockers,
        "system_faults": system_faults,
        "live_exec": live_exec,
        "org_digest": org_digest,
        "should_nudge": should_nudge,
        "commit_age_sec": commit_age_sec,
        "real_output_age_sec": real_output_age_sec,
    }


def render_nudge(snap: dict) -> str:
    """把判定渲染成一則私人 TG 提醒（HTML）。"""
    state = snap["state"]
    icon = {"STALLED": "⏳", "BLOCKED_ON_USER": "🙋", "ADVANCING": "🟢", "IDLE": "⚪"}.get(state, "👁")
    head = {
        "STALLED": "CEO 進度檢視：偵測到停滯",
        "BLOCKED_ON_USER": "CEO 進度檢視：球在你這邊",
        "ADVANCING": "CEO 進度檢視：推進中",
        "IDLE": "CEO 進度檢視：資料累積中",
    }.get(state, "CEO 進度檢視")
    lines = [f"{icon} <b>{head}</b>", "━━━━━━━━━━━━━━━━"]

    age = snap.get("commit_age_sec")
    if age is not None:
        lines.append(f"🧱 最後產出：{_fmt_age(age)}前（git commit）")
    p = snap.get("phase0", {})
    if p:
        lines.append(f"🔬 進度：紙上 {p.get('paper_n', 0)}/{p.get('paper_min', 0)}"
                     f"｜模擬已平倉 {snap.get('demo_n', 0)}/{DEMO_SAMPLE_TARGET}"
                     f"（在場 {snap.get('demo_live', 0)} 筆）"
                     f"｜真實 {p.get('live_n', 0)}/{p.get('live_min', 0)}")
    if snap.get("system_faults"):
        lines.append("🛑 系統故障：" + "；".join(snap["system_faults"]))
    if snap.get("blockers"):
        lines.append("🙋 待你處理：" + "；".join(snap["blockers"]))
    lines.append(f"➡️ 建議下一步：{snap['next_step']}")
    if state == "STALLED":
        lines.append("<i>（監督員：若你忙，可讓我重新喚起 CEO Session 接手推進此步。）</i>")
    return "\n".join(lines)


def _fmt_age(sec: float) -> str:
    sec = max(0, int(sec))
    if sec < 3600:
        return f"{sec // 60} 分"
    if sec < 86400:
        return f"{sec // 3600} 小時"
    return f"{sec // 86400} 天"


# ===========================================================================
# IO 層（連線/讀檔；都包 try，永不拋例外）
# ===========================================================================
def _git_last_commit_age_sec() -> int | None:
    """repo 最後一筆 commit 距今秒數（無 git / 非 repo → None）。"""
    try:
        out = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "log", "-1", "--format=%ct"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        commit_ts = int(out.stdout.strip())
        return max(0, int(time.time()) - commit_ts)
    except Exception:
        return None


def _read_ledger() -> dict:
    try:
        with open(LEDGER_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _read_live_exec_health() -> dict:
    """真錢消費器（v143）寫的健康檔；不存在／壞檔 → {}（純讀，永不拋）。"""
    try:
        with open(LIVE_HEALTH_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _read_org_digest_latest() -> dict:
    """掃 docs/org/digests/，回 {role: 最新日期(date)}（純讀，永不拋）。

    取檔名裡的日期而非 mtime——mtime 會被 clone / 同步 / 編輯洗掉，檔名日期才是
    「那一期報告」的真身（README.md 等非 digest 檔自然不match，直接略過）。
    """
    latest: dict = {}
    try:
        if not ORG_DIGEST_DIR.is_dir():
            return {}
        for p in ORG_DIGEST_DIR.glob("*.md"):
            stem = p.stem
            role, _, datestr = stem.rpartition("-20")
            if not role or "-" not in datestr:
                continue
            try:
                d = _date.fromisoformat("20" + datestr)
            except Exception:
                continue
            if role not in ORG_ROLE_CADENCE_DAYS:
                continue
            if role not in latest or d > latest[role]:
                latest[role] = d
    except Exception:
        return {}
    return latest


def _write_ledger(snap: dict) -> None:
    """原子寫（tmp → replace），避免半截檔被 Layer 2 讀到。"""
    try:
        tmp = LEDGER_PATH.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
        os.replace(tmp, LEDGER_PATH)
    except Exception as e:
        print(f"[ceo_oversight] ledger write error: {type(e).__name__}: {e}")


def build_snapshot(now_ms: int | None = None) -> dict:
    """跑一輪純讀盤點，回完整 snapshot（含 assess 結論）。永不拋例外。"""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    prev = _read_ledger()
    last_nudge_ms = int(prev.get("last_nudge_ms", 0) or 0)

    # --- 純讀各來源（每塊獨立 try）---
    paper_n = paper_min = live_n = live_min = 0
    try:
        from .ceo_session import phase0_status
        p = phase0_status()
        paper_n, paper_min = p["paper_n"], p["paper_min"]
        live_n, live_min = p["live_n"], p["live_min"]
        phase0 = p
    except Exception:
        phase0 = {}

    demo_n = 0
    demo_live = 0
    demo_rejected = 0
    demo_reject_hint = None
    try:
        from . import demo_journal
        demo_n, _ = demo_journal.count_closed_for_phase0()
        ds = demo_journal.get_demo_stats(30)
        demo_live = ds.get("n_open", 0) + ds.get("n_pending", 0)
        # 監督員 r65：只看近 72h 的拒單當『當前卡點』，杜絕早已修好的舊拒因（not_on_okx 全為
        #   5+ 天前、task#8 後近 72h 為 0）被當成現在的 blocker 長期誤報進 next_step（舊快照陷阱）。
        demo_rejected, demo_reject_hint = demo_journal.count_rejected(window_sec=72 * 3600)
    except Exception:
        pass

    demo_active = False
    try:
        from .demo_operator import is_active
        demo_active = is_active()
    except Exception:
        pass

    open_decisions = 0
    try:
        from . import decision_registry as _dec
        open_decisions = len(_dec.list_open() or [])
    except Exception:
        pass

    pending_outbox = 0
    try:
        from . import outbox as _outbox
        pending_outbox = int(_outbox.count_pending() or 0)
    except Exception:
        pass

    commit_age_sec = _git_last_commit_age_sec()

    # task#7：實質產出代理＝最近紙上活動(進場/平倉)距今秒數。無關 commit 不再謊報推進。
    real_output_age_sec = None
    try:
        from . import paper_journal as _pj
        last_ms = _pj.most_recent_activity_ms()
        if last_ms:
            real_output_age_sec = max(0, int((now_ms - last_ms) / 1000))
    except Exception:
        pass

    # r26：真錢執行器健康（v143 健康檔）。純讀＋新鮮度閘，讀不到就當「無故障」。
    live_exec = None
    try:
        live_exec = live_exec_verdict(_read_live_exec_health(), now_s=now_ms / 1000)
    except Exception:
        pass

    # r30：組織產出斷檔（各席 digest 檔齡 vs 節奏）。「今天」用**本地日期**，因為
    #   digest 檔名與排程 cron 都是台北時間的日期，拿 UTC 日期比會在 08:00 前差一天。
    org_digest = None
    try:
        org_digest = org_digest_verdict(
            _read_org_digest_latest(),
            today=_datetime.fromtimestamp(now_ms / 1000).date(),
        )
    except Exception:
        pass

    verdict = assess(
        now_ms=now_ms, commit_age_sec=commit_age_sec,
        paper_n=paper_n, paper_min=paper_min, live_n=live_n, live_min=live_min,
        demo_n=demo_n, demo_live=demo_live, demo_active=demo_active,
        open_decisions=open_decisions, pending_outbox=pending_outbox,
        demo_rejected=demo_rejected, demo_reject_hint=demo_reject_hint,
        real_output_age_sec=real_output_age_sec,
        live_exec=live_exec, org_digest=org_digest,
        last_nudge_ms=last_nudge_ms,
    )

    snap = {
        "generated_at": int(now_ms / 1000),
        "generated_at_ms": now_ms,
        **verdict,
        "phase0": phase0,
        "demo_n": demo_n,
        "demo_live": demo_live,
        "demo_active": demo_active,
        "demo_rejected": demo_rejected,
        "demo_reject_hint": demo_reject_hint,
        "open_decisions": open_decisions,
        "pending_outbox": pending_outbox,
        "last_nudge_ms": last_nudge_ms,  # 沿用；發送後才更新
    }
    # PDP 契約區塊（task #51）：把新鮮度合約寫進帳本，讓 Layer 2 知道
    # 「多舊就不該據此行動」。Layer 2 讀取時須以 pdp_check_freshness() 重新校驗。
    if _op is not None:
        try:
            snap["pdp"] = _op.build_pdp_block(now_ms)
        except Exception:
            pass
    return snap


def pdp_check_freshness():
    """PDP 確定性前置門（供 Layer 2 監督員在「行動前」自我把關，task #51）。

    讀現有 oversight_ledger，以本機 UTC 時鐘校驗其 generated_at_ms 新鮮度。
    回 (allow: bool, reasons: list[str])。allow=False ＝ 帳本不夠新鮮，
    Layer 2 不應據此舊快照下結論／派工（「不拿舊快照下結論」的程式化版本）。

    fail-closed：策略層缺席或讀檔失敗一律回 False（與全系統閘門 fail-closed 收斂一致）。
    """
    if _op is None:
        return False, ["治理策略層 overseer_policy 缺席（fail-closed）"]
    try:
        d = _op.ledger_freshness(_read_ledger())
        return d.allow, list(d.reasons)
    except Exception as e:
        return False, [f"PDP 校驗異常（fail-closed）：{type(e).__name__}: {e}"]


# ===========================================================================
# Worker 主迴圈
# ===========================================================================
async def run_oversight_loop(tg, interval_s: int = 1800):
    """每 ~30 分鐘盤點一次，寫帳本；真停滯且過冷卻才發私人提醒。"""
    print("[ceo_oversight] loop online（監督員 Layer 1：純讀守望）")
    await asyncio.sleep(120)  # 啟動後沉澱，避開開機洗版
    while True:
        try:
            snap = build_snapshot()
            if snap.get("should_nudge") and tg is not None:
                await tg.send_message(render_nudge(snap), parse_mode="HTML")
                snap["last_nudge_ms"] = snap["generated_at_ms"]
                print(f"[ceo_oversight] nudge sent（state={snap['state']}）")
            _write_ledger(snap)
        except Exception as e:
            print(f"[ceo_oversight] error: {type(e).__name__}: {e}")
        await asyncio.sleep(max(60, interval_s))


# ===========================================================================
# 自測 / CLI
# ===========================================================================
def _selftest():
    ok = 0
    fail = 0

    def check(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  ✅ {name}")
        else:
            fail += 1
            print(f"  ❌ {name}")

    HOUR = 3600_000  # ms
    now = 1_000_000_000_000

    # next_step 階梯
    s1 = next_step(paper_n=38, paper_min=100, live_n=0, live_min=30, demo_n=0, demo_active=False)
    check("demo 未啟用 → 建議 --cycle-once 試跑", "cycle-once" in s1)
    s2 = next_step(paper_n=38, paper_min=100, live_n=0, live_min=30, demo_n=5, demo_active=True)
    check("demo 運行中且樣本不足 → 續累積實倉", "5/30" in s2)
    s3 = next_step(paper_n=120, paper_min=100, live_n=0, live_min=30, demo_n=30, demo_active=True)
    check("demo 達標但真實 0 → 指向真錢小額(紅線①)", "真錢" in s3 and "紅線①" in s3)
    # v82：拒單率高就揭露卡點，不可謊稱「累積中」；且一筆僥倖成交不可繞過揭露
    s4 = next_step(paper_n=45, paper_min=100, live_n=0, live_min=30, demo_n=0,
                   demo_active=True, demo_rejected=5, demo_reject_hint="51010")
    check("成交0+全被拒 → 揭露卡點(非謊稱累積)", "被 OKX 拒絕" in s4 and "51010" in s4 and "0/30" in s4)
    check("成交0+全被拒 → 不沿用『累積中』樂觀句", "續累積 OKX 實倉樣本（0/30）" not in s4)
    # 治本核心：一筆僥倖成交但拒單率高(2成交/5拒=71%) → 仍揭露，不被一筆成交繞過
    s5 = next_step(paper_n=45, paper_min=100, live_n=0, live_min=30, demo_n=2,
                   demo_active=True, demo_rejected=5)
    check("僥倖成交但高拒單率 → 仍揭露(不繞過)", "被 OKX 拒絕" in s5 and "2/30" in s5)
    check("僥倖成交但高拒單率 → 不謊稱純累積句", "續累積 OKX 實倉樣本（2/30）" not in s5)
    # 低拒單率(正常累積) → 才用樂觀累積句
    s5b = next_step(paper_n=45, paper_min=100, live_n=0, live_min=30, demo_n=2,
                    demo_active=True, demo_rejected=0)
    check("低拒單率 → 正常累積句", "續累積 OKX 實倉樣本（2/30）" in s5b)

    # assess：BLOCKED_ON_USER（有待決策）
    a1 = assess(now_ms=now, commit_age_sec=999999, paper_n=38, paper_min=100,
                live_n=0, live_min=30, demo_n=0, demo_live=0, demo_active=False,
                open_decisions=1, pending_outbox=0, last_nudge_ms=0)
    check("有待決策 → BLOCKED_ON_USER", a1["state"] == "BLOCKED_ON_USER")
    check("BLOCKED 過冷卻 → 提醒", a1["should_nudge"] is True)
    check("BLOCKED → blockers 有內容", len(a1["blockers"]) == 1)

    # assess：ADVANCING（最近有 commit，蓋過待決策？不——待決策優先序低於停滯判定）
    a2 = assess(now_ms=now, commit_age_sec=600, paper_n=38, paper_min=100,
                live_n=0, live_min=30, demo_n=0, demo_live=0, demo_active=False,
                open_decisions=0, pending_outbox=0, last_nudge_ms=0)
    check("最近有 commit 且無待辦 → ADVANCING", a2["state"] == "ADVANCING")
    check("ADVANCING → 不提醒", a2["should_nudge"] is False)

    # assess：STALLED（久無 commit 又沒卡使用者）
    a3 = assess(now_ms=now, commit_age_sec=STALL_SEC + 1, paper_n=38, paper_min=100,
                live_n=0, live_min=30, demo_n=0, demo_live=0, demo_active=False,
                open_decisions=0, pending_outbox=0, last_nudge_ms=0)
    check("久無 commit 且無待辦 → STALLED", a3["state"] == "STALLED")
    check("STALLED 過冷卻 → 提醒", a3["should_nudge"] is True)

    # 冷卻：剛提醒過 → 不再提醒
    a4 = assess(now_ms=now, commit_age_sec=STALL_SEC + 1, paper_n=38, paper_min=100,
                live_n=0, live_min=30, demo_n=0, demo_live=0, demo_active=False,
                open_decisions=0, pending_outbox=0, last_nudge_ms=now - HOUR)  # 1h 前剛提醒
    check("冷卻內（1h<6h）→ 不重複提醒", a4["should_nudge"] is False)

    # IDLE：無 git 時效
    a5 = assess(now_ms=now, commit_age_sec=None, paper_n=0, paper_min=100,
                live_n=0, live_min=30, demo_n=0, demo_live=0, demo_active=False,
                open_decisions=0, pending_outbox=0, last_nudge_ms=0)
    check("無 git 時效 → IDLE", a5["state"] == "IDLE")
    check("IDLE → 不提醒", a5["should_nudge"] is False)

    # 待決策優先於停滯：久無 commit 但有待決策 → 仍歸 BLOCKED_ON_USER（球在使用者）
    a6 = assess(now_ms=now, commit_age_sec=STALL_SEC + 1, paper_n=38, paper_min=100,
                live_n=0, live_min=30, demo_n=0, demo_live=0, demo_active=False,
                open_decisions=2, pending_outbox=1, last_nudge_ms=0)
    check("久無commit+有待辦 → BLOCKED_ON_USER(非STALLED)", a6["state"] == "BLOCKED_ON_USER")
    check("blockers 含決策+對外兩項", len(a6["blockers"]) == 2)

    # --- r26：真錢執行器健康接進判定 ---------------------------------------
    NOW_S = now / 1000
    h_live = {"consecutive_fail_rounds": 113, "updated_at": NOW_S - 30,
              "first_fail_ts": NOW_S - 6700, "last_fail_class": "auth_ip_whitelist"}
    v_live = live_exec_verdict(h_live, now_s=NOW_S)
    check("連續故障達門檻 → 判為阻塞", v_live is not None and v_live["rounds"] == 113)
    check("白名單類 → 標記為本人可解", v_live["user_actionable"] is True)
    check("無健康檔 → 無故障", live_exec_verdict({}, now_s=NOW_S) is None)
    check("單輪失敗(未達門檻) → 不算故障",
          live_exec_verdict({**h_live, "consecutive_fail_rounds": 1}, now_s=NOW_S) is None)
    # 新鮮度閘：舊檔＝消費器沒在跑，不可拿舊 streak 當現在的阻塞
    check("健康檔過舊 → 不當現行故障",
          live_exec_verdict({**h_live, "updated_at": NOW_S - 99999}, now_s=NOW_S) is None)
    v_sys = live_exec_verdict({**h_live, "last_fail_class": "network"}, now_s=NOW_S)
    check("未知類別 → 歸系統故障(非本人可解)", v_sys is not None and v_sys["user_actionable"] is False)

    # 核心迴歸：有 commit（原本 ADVANCING）但真錢側全滅 → 不可再回報一切正常
    a7 = assess(now_ms=now, commit_age_sec=600, paper_n=38, paper_min=100,
                live_n=0, live_min=30, demo_n=0, demo_live=0, demo_active=False,
                open_decisions=0, pending_outbox=0, last_nudge_ms=0, live_exec=v_live)
    check("有commit+真錢401全滅 → BLOCKED_ON_USER(非ADVANCING)", a7["state"] == "BLOCKED_ON_USER")
    check("真錢阻塞進 blockers", any("真錢執行器" in b for b in a7["blockers"]))
    check("真錢阻塞 → 解除提醒抑制", a7["should_nudge"] is True)
    a8 = assess(now_ms=now, commit_age_sec=600, paper_n=38, paper_min=100,
                live_n=0, live_min=30, demo_n=0, demo_live=0, demo_active=False,
                open_decisions=0, pending_outbox=0, last_nudge_ms=0, live_exec=v_sys)
    check("有commit+系統類故障 → STALLED(該push CEO)", a8["state"] == "STALLED")
    check("系統故障不誤標成『待你處理』", a8["blockers"] == [] and len(a8["system_faults"]) == 1)
    a9 = assess(now_ms=now, commit_age_sec=600, paper_n=38, paper_min=100,
                live_n=0, live_min=30, demo_n=0, demo_live=0, demo_active=False,
                open_decisions=0, pending_outbox=0, last_nudge_ms=0, live_exec=None)
    check("真錢側健康 → 維持 ADVANCING(不誤報)", a9["state"] == "ADVANCING")

    # --- r30：組織產出斷檔接進判定 ---------------------------------------
    TODAY = _date(2026, 7, 31)
    # 現況（本輪實測）：各席都在期 → 不可誤報
    ok_now = {"ceo": _date(2026, 7, 30), "pm": _date(2026, 7, 30),
              "coinglass": _date(2026, 7, 28), "design": _date(2026, 7, 29),
              "eng": _date(2026, 7, 29)}
    check("各席在期 → 無斷檔(不誤報)", org_digest_verdict(ok_now, today=TODAY) is None)
    # 真實迴歸：7/12–7/28 那次 16.4 天無聲斷檔，若當時有本消費者是否會叫？
    outage = {"ceo": _date(2026, 7, 11), "pm": _date(2026, 6, 22),
              "coinglass": _date(2026, 6, 23), "design": _date(2026, 7, 8),
              "eng": _date(2026, 6, 19)}
    v_gap = org_digest_verdict(outage, today=_date(2026, 7, 28))
    check("7/12–7/28 真實斷檔 → 會被抓到", v_gap is not None)
    check("斷檔列出全部逾期席次", v_gap is not None and len(v_gap["roles"]) == 5)
    check("最嚴重者排最前(eng 39天)", v_gap["roles"][0]["role"] == "eng"
          and v_gap["roles"][0]["age_days"] == 39)
    check("斷檔文字點出 lastRunAt 根因", "lastRunAt" in v_gap["text"])
    # 邊界：CEO 日報缺 1 天不叫（容忍單次跳過），缺 3 天才叫
    check("CEO 缺1天 → 不叫(容忍跳過)",
          org_digest_verdict({**ok_now, "ceo": _date(2026, 7, 30)}, today=TODAY) is None)
    check("CEO 缺2天 → 仍不叫(剛好門檻)",
          org_digest_verdict({**ok_now, "ceo": _date(2026, 7, 29)}, today=TODAY) is None)
    v_ceo = org_digest_verdict({**ok_now, "ceo": _date(2026, 7, 28)}, today=TODAY)
    check("CEO 缺3天 → 叫(超過2期)", v_ceo is not None and v_ceo["roles"][0]["role"] == "ceo")
    # 不誤報守則
    check("目錄空/不存在 → 不當斷檔", org_digest_verdict({}, today=TODAY) is None)
    check("None → 不當斷檔", org_digest_verdict(None, today=TODAY) is None)
    check("某席從未產出 → 跳過該席(不誤報)",
          org_digest_verdict({k: v for k, v in ok_now.items() if k != "eng"},
                             today=TODAY) is None)
    # 接進 assess：有 commit 但組織產出斷檔 → 不可再回報 ADVANCING
    a10 = assess(now_ms=now, commit_age_sec=600, paper_n=38, paper_min=100,
                 live_n=0, live_min=30, demo_n=0, demo_live=0, demo_active=False,
                 open_decisions=0, pending_outbox=0, last_nudge_ms=0, org_digest=v_gap)
    check("有commit+組織斷檔 → STALLED(該push CEO)", a10["state"] == "STALLED")
    check("組織斷檔歸系統故障非待你處理",
          a10["blockers"] == [] and any("組織產出斷檔" in s for s in a10["system_faults"]))
    a11 = assess(now_ms=now, commit_age_sec=600, paper_n=38, paper_min=100,
                 live_n=0, live_min=30, demo_n=0, demo_live=0, demo_active=False,
                 open_decisions=0, pending_outbox=0, last_nudge_ms=0, org_digest=None)
    check("組織產出正常 → 維持 ADVANCING(不誤報)", a11["state"] == "ADVANCING")
    # 檔名解析（真目錄，唯讀）
    real = _read_org_digest_latest()
    check("真目錄可解析出各席最新日期", isinstance(real, dict) and "ceo" in real)
    check("解析結果不含 README 等非 digest 檔",
          all(k in ORG_ROLE_CADENCE_DAYS for k in real))

    # render 不爆
    r = render_nudge({**a3, "phase0": {"paper_n": 38, "paper_min": 100, "live_n": 0, "live_min": 30},
                      "demo_n": 0})
    check("render_nudge 產出非空 HTML", "<b>" in r and len(r) > 20)
    r2 = render_nudge({**a8, "phase0": {}, "demo_n": 0})
    check("render_nudge 揭露系統故障", "🛑 系統故障" in r2)

    print(f"\nceo_oversight 自測：{ok}/{ok + fail} 通過")
    return fail == 0


def _print_status():
    snap = build_snapshot()
    print("=== 監督員快照（oversight snapshot）===")
    print(f"  state        : {snap['state']}")
    print(f"  commit_age   : {_fmt_age(snap['commit_age_sec']) if snap['commit_age_sec'] is not None else '—'}")
    print(f"  demo_active  : {snap['demo_active']}（樣本 {snap['demo_n']}/{DEMO_SAMPLE_TARGET}，在場 {snap['demo_live']}）")
    print(f"  blockers     : {snap['blockers'] or '（無）'}")
    print(f"  system_faults: {snap.get('system_faults') or '（無）'}")
    print(f"  should_nudge : {snap['should_nudge']}")
    print(f"  next_step    : {snap['next_step']}")
    print(f"\n--- 提醒預覽（不發送）---")
    import re
    print(re.sub(r"<[^>]+>", "", render_nudge(snap)))
    print(f"\nledger 路徑：{LEDGER_PATH}")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        ok = _selftest()
        sys.exit(0 if ok else 1)
    elif "--status" in sys.argv:
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / ".env")
        _print_status()
    else:
        print("用法：python -m l3_dispatcher.ceo_oversight [--selftest|--status]")
