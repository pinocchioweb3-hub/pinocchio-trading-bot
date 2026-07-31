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
• 代補產（檔頭含「代補」）**單獨計一軌**：補的是內容，不是排程。一格若只有代補產檔，
  記為 backfill_only，不算該席自產有交。
"""
from __future__ import annotations

import json
from datetime import date as _date
from pathlib import Path

from .ceo_oversight import (
    ORG_BACKFILL_MARKER,
    ORG_DIGEST_DIR,
    ORG_HEADER_LINES,
    ORG_ROLE_CADENCE_DAYS,
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
        for k in range(lookback_periods):
            hi = today - _td(k * cad)
            lo = today - _td((k + 1) * cad)
            if hi < first_self:
                continue                  # 該席上線前的期別，不追溯記過
            has_self = any(lo < d <= hi for d in self_dates)
            has_bf = any(lo < d <= hi for d in bf_dates)
            windows.append({"start": lo, "end": hi, "self": has_self,
                            "backfill_only": (not has_self) and has_bf})
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
            res.setdefault(role, []).append((d, ORG_BACKFILL_MARKER in head))
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
    cad_pm = {"pm": ("產品總監週報", 7)}
    pm_hist = {"pm": [(_date(2026, 7, 6), False), (_date(2026, 7, 30), True)]}
    p = coverage(pm_hist, today=TODAY, cadence=cad_pm)["roles"][0]
    check("代補產不算該席自產有交", p["self_hits"] == 1)
    check("代補產另計一軌 backfill_only=1", p["backfill_only_hits"] == 1)
    check("pm 近 4 期缺 3 期", p["missed"] == 3)

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
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date()
    cov = coverage(scan_all(), today=today)
    if "--json" in sys.argv:
        print(json.dumps(cov, ensure_ascii=False, default=str, indent=2))
    else:
        print(f"組織產出期別連續性盤點（today={today.isoformat()}，"
              f"近 {DEFAULT_LOOKBACK_PERIODS} 期）")
        print(render(cov))
