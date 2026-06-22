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
            hint = f"（最近拒因 {demo_reject_hint}）" if demo_reject_hint else ""
            return (f"⚠️ 模擬盤操盤手已下單 {_attempts} 次、其中 {demo_rejected} 次被 OKX 拒絕"
                    f"（實倉成交僅 {demo_n}/{DEMO_SAMPLE_TARGET}）{hint}——這是目前卡住實倉樣本的點。"
                    "請先排除下單被拒原因：多為 OKX 模擬盤帳戶須改為『單幣種/跨幣種保證金』"
                    "模式才可交易永續（錯誤碼 51010），或張數規格取整（51121，已修待重啟生效）")
        tail = f"；同時續累積紙上樣本（{paper_n}/{paper_min}）" if paper_n < paper_min else ""
        return f"模擬盤操盤手運行中，續累積 OKX 實倉樣本（{demo_n}/{DEMO_SAMPLE_TARGET}）{tail}"
    if live_n < live_min:
        return ("模擬盤實倉樣本已達階段目標；下一步是「真錢小額」驗證——"
                "依紅線①須由本人逐筆親手下單，非系統自動（待你拍板＋律師）")
    return "樣本接近 Phase 0 門檻，準備由人判讀是否對外宣告（系統不自我宣告，紅線③）"


def assess(*, now_ms, commit_age_sec, paper_n, paper_min, live_n, live_min,
           demo_n, demo_live, demo_active, open_decisions, pending_outbox,
           demo_rejected=0, demo_reject_hint=None, real_output_age_sec=None,
           last_nudge_ms=0, stall_sec=STALL_SEC, nudge_cooldown_sec=NUDGE_COOLDOWN_SEC) -> dict:
    """核心判定（純函式）。回 state / next_step / blockers / should_nudge。

    state 邏輯：
      有待你拍板/核准         → BLOCKED_ON_USER（球在你那；CEO 不算停滯）
      最近有 commit           → ADVANCING
      沒產出又沒卡你身上       → STALLED（這才是該 Push CEO 的情況）
      commit 時效未知（無 git）→ IDLE
    """
    blockers: list[str] = []
    if open_decisions:
        blockers.append(f"{open_decisions} 項決策待你拍板")
    if pending_outbox:
        blockers.append(f"{pending_outbox} 則對外內容待你核准（/approve）")

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

    cooldown_ok = (now_ms - last_nudge_ms) >= nudge_cooldown_sec * 1000
    # 只在「STALLED（該 push CEO）」或「BLOCKED_ON_USER（提醒你有待辦）」且過冷卻才提醒。
    should_nudge = state in ("STALLED", "BLOCKED_ON_USER") and cooldown_ok

    return {
        "state": state,
        "next_step": ns,
        "blockers": blockers,
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
        demo_rejected, demo_reject_hint = demo_journal.count_rejected()
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

    verdict = assess(
        now_ms=now_ms, commit_age_sec=commit_age_sec,
        paper_n=paper_n, paper_min=paper_min, live_n=live_n, live_min=live_min,
        demo_n=demo_n, demo_live=demo_live, demo_active=demo_active,
        open_decisions=open_decisions, pending_outbox=pending_outbox,
        demo_rejected=demo_rejected, demo_reject_hint=demo_reject_hint,
        real_output_age_sec=real_output_age_sec,
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

    # render 不爆
    r = render_nudge({**a3, "phase0": {"paper_n": 38, "paper_min": 100, "live_n": 0, "live_min": 30},
                      "demo_n": 0})
    check("render_nudge 產出非空 HTML", "<b>" in r and len(r) > 20)

    print(f"\nceo_oversight 自測：{ok}/{ok + fail} 通過")
    return fail == 0


def _print_status():
    snap = build_snapshot()
    print("=== 監督員快照（oversight snapshot）===")
    print(f"  state        : {snap['state']}")
    print(f"  commit_age   : {_fmt_age(snap['commit_age_sec']) if snap['commit_age_sec'] is not None else '—'}")
    print(f"  demo_active  : {snap['demo_active']}（樣本 {snap['demo_n']}/{DEMO_SAMPLE_TARGET}，在場 {snap['demo_live']}）")
    print(f"  blockers     : {snap['blockers'] or '（無）'}")
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
