"""組織產出「期別連續性」盤點（純函式 + 離線報告，**不被 daemon 匯入**）。

為什麼要有這支
--------------
現行 `ceo_oversight.org_digest_verdict()` 是**檔齡制**：只看該席「最新一份自產 digest
有多舊」。它答得了「這席現在是不是停擺中」，但答不了「過去 N 期到底交了幾期」——
任何一份新檔都會把它前面的歷史缺口一起蓋掉。

實例（2026-07-31 量測）：創意總監排程是每週三，7/08 交過一次、7/15 與 7/22 兩期
零產出、7/29 又交一次。檔齡制看到「最新 = 7/29、才 2 天前」→ 判無斷檔，那兩期缺報
從未上過任何一輪帳本，是人工逐檔清點才看見的。

這是「拿代理值當事實」同物種第四次（前三：demo_active 只讀閘①旗標、demo_operator
skipped 分支永不出聲、排程 lastRunAt 只記觸發不記成功）。檔齡是「連續性」的代理值，
不是連續性本身。

為什麼獨立成一支、而不是直接改 ceo_oversight
--------------------------------------------
ceo_oversight.py 是 daemon 常駐模組，動它必走 RB-1 防踩踏全流程（停 daemon → 改 →
py_compile → 測 → 單次乾淨重啟 → 驗 → 復原 watchdog）。真錢主線正卡在 OKX 401
期間，不值得為了一個觀測性改良去動常駐迴圈。故先把邏輯落在這支**零 import 進 daemon**
的純模組裡（daemon 不認識它 ⇒ 不可能影響常駐行為），拿到現在就缺的事實；日後真的
要接進帳本時，RB-1 那次改動只剩「import + 呼叫」一行，風險面積最小。

口徑（刻意保守，兩條不誤報守則沿用 org_digest_verdict）
------------------------------------------------------
• 期別視窗＝以「今天」為右端、往回每 cadence 天切一格：第 k 格 = (today-(k+1)*cad,
  today-k*cad]。不依賴 cron 的星期錨點，換排程日也不會整排錯位。
• 某席**從未自產過** ⇒ 沒有基準，整席略過（不誤報）。
• 該席**首份自產檔之前**的視窗一律不算缺（那時這席還沒上線，不該被追溯記過）。
• r73：第 0 格（右端＝今天）**還沒走完**。這一格沒有自產檔時記為 `pending_period`
  而非缺報——「尚未到期」是未知，不是「確認沒交」。代價是最新一期會晚一格才記上
  （滾動視窗看不出班已經在格子裡跑過沒交），這符合本模組「寧可晚叫不可誤報」，
  且**現況停擺本來就由檔齡制負責**。已經交了的第 0 格是確定事實，照算 hit。
• 代補產（檔頭含「代補」）**單獨計一軌**：補的是內容，不是排程。一格若只有代補產檔，
  記為 backfill_only，不算該席自產有交。
"""
from __future__ import annotations

import json
from datetime import date as _date
from pathlib import Path

from .ceo_oversight import (
    ORG_DIGEST_DIR,
    ORG_HEADER_LINES,
    ORG_ROLE_CADENCE_DAYS,
    is_backfill_header,
)

DEFAULT_LOOKBACK_PERIODS = 12


# ---------------------------------------------------------------------------
# 純函式層（可離線測，不碰 IO）
# ---------------------------------------------------------------------------
def coverage(dates_by_role: dict | None, *, today,
             cadence: dict | None = None,
             lookback_periods: int = DEFAULT_LOOKBACK_PERIODS) -> dict:
    """把「各席全部 digest 日期」翻成「近 N 期逐期有沒有交」。

    dates_by_role: {role: [(date, is_backfill), ...]}——該席**全部**檔，不是只有最新。
    回 {roles: [...], any_gap: bool}；每席一筆：
        {role, label, cadence_days, periods, self_hits, backfill_only_hits,
         missed, coverage_text, missed_windows, longest_miss_streak}
    """
    cadence = cadence or ORG_ROLE_CADENCE_DAYS
    dates_by_role = dates_by_role or {}
    out = []
    for role, (label, cad) in cadence.items():
        entries = dates_by_role.get(role) or []
        self_dates = sorted(d for d, bf in entries if not bf)
        if not self_dates:
            continue                      # 從未自產 ⇒ 無基準，略過（不誤報）
        bf_dates = sorted(d for d, bf in entries if bf)
        first_self = self_dates[0]

        windows = []
        pending = None
        for k in range(lookback_periods):
            hi = today - _td(k * cad)
            lo = today - _td((k + 1) * cad)
            if hi < first_self:
                continue                  # 該席上線前的期別，不追溯記過
            has_self = any(lo < d <= hi for d in self_dates)
            has_bf = any(lo < d <= hi for d in bf_dates)
            w = {"start": lo, "end": hi, "self": has_self,
                 "backfill_only": (not has_self) and has_bf}
            # r73：第 0 格的右端就是「今天」＝**這一期還沒走完**。沒有自產檔時，
            #   事實是「尚未到期」（未知），不是「沒交」（確認沒有）——記成缺報
            #   就是拿沒考完的試卷當不及格。CEO 節奏每天、排程 09:08 才跑，等於
            #   每天 00:00–09:08 都被灌水 +1 期，且連缺數恆 ≥1＝慢性假警報。
            #   ⛔ 只有「沒交」才轉 pending；已經交了的那格是確定的事實，照算 hit。
            #   ⛔ 不可因此漏掉「今天真的沒交」——現況停擺由檔齡制
            #      （org_digest_verdict）負責，期別制只管已結束的期別。
            if k == 0 and not has_self:
                pending = w
                continue
            windows.append(w)
        if not windows:
            continue
        windows.reverse()                 # 由舊到新，讀起來順

        n = len(windows)
        hits = sum(1 for w in windows if w["self"])
        bf_only = sum(1 for w in windows if w["backfill_only"])
        missed = [w for w in windows if not w["self"]]
        streak = best = 0
        for w in windows:
            streak = 0 if w["self"] else streak + 1
            best = max(best, streak)
        out.append({
            "role": role, "label": label, "cadence_days": cad,
            "periods": n, "self_hits": hits, "backfill_only_hits": bf_only,
            "missed": len(missed), "longest_miss_streak": best,
            "coverage_text": f"{hits}/{n}",
            "missed_windows": [(w["start"].isoformat(), w["end"].isoformat(),
                                "代補產填過" if w["backfill_only"] else "全無")
                               for w in missed],
            # r73：未到期的那一期不算缺報，但也**不准就這樣消失**——帳本會原樣夾帶
            #   roles，留在這裡才分得出「這期還沒到」與「這期根本沒這一格」。
            "pending_period": (
                (pending["start"].isoformat(), pending["end"].isoformat(),
                 "尚未到期·僅代補產填過" if pending["backfill_only"]
                 else "尚未到期·尚無產出")
                if pending else None),
        })
    out.sort(key=lambda x: (-x["missed"], x["role"]))
    return {"roles": out, "any_gap": any(x["missed"] for x in out)}


def _td(days: int):
    from datetime import timedelta
    return timedelta(days=days)


def render(cov: dict) -> str:
    """把 coverage() 結果翻成繁中人話（給人看的，不是給機器解析的）。"""
    rows = cov.get("roles") or []
    if not rows:
        return "（無可盤點的席次——各席皆從未自產過 digest，或目錄不存在）"
    lines = []
    for x in rows:
        s = (f"・{x['label']}（每 {x['cadence_days']} 天）：近 {x['periods']} 期自產 "
             f"{x['coverage_text']}")
        if x["missed"]:
            s += f"，缺 {x['missed']} 期（最長連缺 {x['longest_miss_streak']} 期）"
            if x["backfill_only_hits"]:
                s += f"；其中 {x['backfill_only_hits']} 期由監督員代補產填過內容（排程仍未自產）"
            s += "\n    缺報期別：" + "、".join(
                f"{a}~{b}［{why}］" for a, b, why in x["missed_windows"])
        else:
            s += "，無缺報"
        if x.get("pending_period"):
            a, b, why = x["pending_period"]
            s += f"\n    本期（{a}~{b}）［{why}］——未走完，不計入上列數字"
        lines.append(s)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# IO 層
# ---------------------------------------------------------------------------
def scan_all(dirpath: Path | None = None) -> dict:
    """掃 digests 目錄，回 {role: [(date, is_backfill), ...]}（純讀，永不拋）。

    與 ceo_oversight._read_org_digest_latest 同口徑（檔名取日期、檔頭 12 行判代補），
    差別只在這裡保留**每一份**檔而非只留最新——連續性需要全歷史。
    """
    dirpath = dirpath or ORG_DIGEST_DIR
    res: dict = {}
    try:
        if not dirpath.is_dir():
            return {}
        for p in sorted(dirpath.glob("*.md")):
            role, _, datestr = p.stem.rpartition("-20")
            if not role or "-" not in datestr:
                continue
            try:
                d = _date.fromisoformat("20" + datestr)
            except Exception:
                continue
            if role not in ORG_ROLE_CADENCE_DAYS:
                continue
            try:
                with open(p, "r", encoding="utf-8") as f:
                    head = "".join(f.readline() for _ in range(ORG_HEADER_LINES))
            except Exception:
                head = ""      # 讀不到檔頭就當自產（與既有口徑一致：寧可晚叫不可誤報）
            # r73：用 is_backfill_header()（會剔除「非監督員代補」這種否定句），
            #   與 ceo_oversight._read_org_digest_latest 保持同一口徑。
            res.setdefault(role, []).append((d, is_backfill_header(head)))
    except Exception:
        return {}
    return res


# ---------------------------------------------------------------------------
# 自我測試
# ---------------------------------------------------------------------------
def _selftest() -> bool:
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("  [OK] " if cond else "  [FAIL] ") + name)
        ok = ok and bool(cond)

    TODAY = _date(2026, 7, 31)

    # --- 差分回歸鎖：本模組存在的唯一理由 ---------------------------------
    # 創意總監真實史：7/08 自產、7/15 與 7/22 兩期零產出、7/29 自產。
    # 舊檔齡制看「最新 7/29 才 2 天前」⇒ 判無斷檔；期別制必須看得見那兩期。
    # 若哪天有人把本模組退化回檔齡制，這條會紅。
    from .ceo_oversight import org_digest_verdict
    design_hist = {"design": [(_date(2026, 7, 8), False), (_date(2026, 7, 29), False)]}
    old = org_digest_verdict({"design": _date(2026, 7, 29)}, today=TODAY)
    check("舊檔齡制對 design 的歷史缺口回報 None（＝這正是要補的盲點）", old is None)
    new = coverage(design_hist, today=TODAY, cadence={"design": ("創意總監週報", 7)})
    d = new["roles"][0]
    check("期別制看見 design 缺 2 期", d["missed"] == 2)
    check("期別制算出最長連缺 2 期", d["longest_miss_streak"] == 2)
    check("上線前期別不追溯記過（7/08 之前不算）", d["periods"] == 4)
    check("自產覆蓋率 2/4", d["coverage_text"] == "2/4")

    # --- 代補產單獨計一軌，不得洗成痊癒 -----------------------------------
    # r73：代補產日期用 7/20（落在已結束的 (7/17, 7/24] 那格）。7/30 會落進「今天
    # 這一格」，而那格 r73 起算 pending 不算期別——那件事由下一段專門驗。
    cad_pm = {"pm": ("產品總監週報", 7)}
    pm_hist = {"pm": [(_date(2026, 7, 6), False), (_date(2026, 7, 20), True)]}
    p = coverage(pm_hist, today=TODAY, cadence=cad_pm)["roles"][0]
    check("代補產不算該席自產有交", p["self_hits"] == 1)
    check("代補產另計一軌 backfill_only=1", p["backfill_only_hits"] == 1)
    # r73：分母不含未走完的本期 ⇒ 3 期（原本 4 期含今天那格）、缺 2 期（原本 3）。
    check("pm 已結束的 3 期裡缺 2 期", p["missed"] == 2 and p["periods"] == 3)

    # --- r73：未到期的那一期是「未知」不是「沒交」 -------------------------
    # CEO 節奏每天、排程 09:08 才跑 ⇒ 每天 00:00–09:08 舊碼都會多記一期缺報，
    # 且連缺數恆 ≥1（天天準時交也會被寫成「缺 1 期」）＝慢性假警報。
    cad_ceo = {"ceo": ("CEO 日報", 1)}
    ceo_hist = {"ceo": [(_date(2026, 7, d0), False) for d0 in range(20, 31)]}
    c = coverage(ceo_hist, today=TODAY, cadence=cad_ceo)["roles"][0]
    check("天天準時交 → 未到期的今天不算缺報", c["missed"] == 0)
    check("未到期的那期不進分母", c["coverage_text"] == "11/11")
    check("但那一期沒有消失（pending_period 留著）",
          c["pending_period"][:2] == ("2026-07-30", "2026-07-31"))
    c2 = coverage({"ceo": ceo_hist["ceo"] + [(_date(2026, 7, 31), False)]},
                  today=TODAY, cadence=cad_ceo)["roles"][0]
    check("今天已經交了 → 那格是確定事實不是 pending", c2["pending_period"] is None)

    # --- 兩條不誤報守則 ---------------------------------------------------
    check("從未自產過的席次整席略過",
          coverage({"pm": [(_date(2026, 7, 30), True)]}, today=TODAY,
                   cadence=cad_pm)["roles"] == [])
    check("空輸入不製造假故障", coverage({}, today=TODAY)["roles"] == [])
    check("None 輸入不製造假故障", coverage(None, today=TODAY)["roles"] == [])
    full = {"eng": [(_date(2026, 7, d0), False) for d0 in (3, 10, 17, 24, 31)]}
    e = coverage(full, today=TODAY, cadence={"eng": ("高級程式設計師週報", 7)})
    check("每期都交 → 零缺報", e["roles"][0]["missed"] == 0 and not e["any_gap"])

    # --- 真實目錄跑得動（不驗內容，只驗不炸） -----------------------------
    real = scan_all()
    check("真實 digests 目錄掃得到檔", isinstance(real, dict) and len(real) > 0)
    check("render 不炸", isinstance(render(coverage(real, today=TODAY)), str))
    return ok


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(0 if _selftest() else 1)
    from datetime import datetime
    # r73：用**本地日期**，與 daemon 呼叫端（ceo_oversight 傳 fromtimestamp().date()）
    #   以及 digest 檔名/cron 的台北時間口徑一致。原本用 UTC ⇒ 每天 00:00–08:00 這支
    #   CLI 會比帳本少算一天，拿它驗帳本會得到對不起來的數字（驗證工具本身失真）。
    today = datetime.now().date()
    cov = coverage(scan_all(), today=today)
    if "--json" in sys.argv:
        print(json.dumps(cov, ensure_ascii=False, default=str, indent=2))
    else:
        print(f"組織產出期別連續性盤點（today={today.isoformat()}，"
              f"近 {DEFAULT_LOOKBACK_PERIODS} 期）")
        print(render(cov))
