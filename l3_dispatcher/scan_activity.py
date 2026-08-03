"""訊號引擎的「產出量」觀測層（v239）。

治本對象：2026-07-08 → 2026-08-01，FIRE 引擎連續 **24 天零產出**，無人發現。

事故經過
--------
CoinGlass 訂閱 7/08 到期 → BTC 大盤閘讀不到 → `evaluate()` 走
`btc_gate_stale` 分支 → 每一檔每一輪都 HOLD → 引擎整包停產。
在此之前四天，每天穩定產出 16/32/24/27 筆 FIRE。斷崖就落在到期那一天。

沒人發現的原因（同物種第 58 次：量不到 → 折成正常）
--------------------------------------------------
`engine.py:68-70` **本來就分得出** 兩件事：
    btc_gate_closed  = 量到了，BTC 在 200MA 之下，閘該關   ← 濾網正常工作
    btc_gate_stale   = 讀不到，不知道 BTC 在哪              ← 失明
但 `scheduler.py` 收到 decision 之後只做 `summary.holds += 1`，reason 當場丟掉。
於是上游能看到的只剩一個沒有分別的數字「15 holds」——濾網盡責和引擎瞎掉，
在監控面上長得一模一樣。

而監控的不對稱更刺眼：v123 加過 `setup_rate_surge`（開單**太多**會告警，因為
2026-07-06 那次是使用者自己看持倉畫面發現的），但開單掉到**零**沒有任何偵測。

本模組做的事
------------
1. 每輪把 hold 的**理由分布**與閘的來源落檔（`scan_activity.json`，原子寫）。
2. `drought_verdict()` 純函式：把「多久沒 FIRE」翻成三種**互不折疊**的結論——
       gated   可解釋乾旱（閘關著，而且是真的量出來的）→ 觀測，不是故障
       blind   失明乾旱（閘/濾網讀不到）→ system_faults，工程端要修
       unknown 連活動檔都讀不到 → 也是 system_faults，⛔ 絕不可折成「沒有乾旱」
3. supervisor（Telegram）與 ceo_oversight（帳本）共用這一個判準，不各判各的。

⛔ 本模組**不改任何計分、不改任何進場條件**。它只讓已經發生的事情看得見。
   「乾旱時要不要放寬閘」是進場濾網數學的改動，要走回測閘（PSR/DSR）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from botpaths import data_dir

ACTIVITY_FILENAME = "scan_activity.json"

# 多久沒 FIRE 算乾旱。基線：斷流前四天每天 16-32 筆，所以 24h 掛零已經是異常訊號。
DROUGHT_WARN_SEC = int(os.getenv("SCAN_DROUGHT_WARN_SEC", str(24 * 3600)))
DROUGHT_ALERT_SEC = int(os.getenv("SCAN_DROUGHT_ALERT_SEC", str(72 * 3600)))
# 活動檔多舊算「現在根本沒在掃」。scheduler 預設每輪 ~30 分，給三輪的寬容。
ACTIVITY_MAX_AGE_SEC = int(os.getenv("SCAN_ACTIVITY_MAX_AGE_SEC", str(3 * 3600)))

# 「讀不到」類的 hold 理由。engine 用固定前綴標它們，這裡只認前綴不認全字串，
# 因為 filter_stale:<name> 的 name 是可變的。
_BLIND_PREFIXES = ("btc_gate_stale", "filter_stale", "oi_fuel_stale")


def activity_path() -> Path:
    return data_dir() / ACTIVITY_FILENAME


def write_activity(payload: dict) -> None:
    """原子寫（tmp + os.replace）。

    ⛔ 不可退回「直接開檔覆寫」：v162-v166 那五處「未知 vs 確認沒有」的共同成因
       就是非原子寫先造出半截壞檔，然後自己再把壞檔誤讀成「本來就沒有」。
    """
    p = activity_path()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def read_activity() -> dict | None:
    """回活動檔內容；檔不存在回 None。

    ⛔ 壞檔**不**回 None——那等於把「讀不到」講成「還沒開始掃」。壞檔回
       `{"_read_error": ...}`，讓 drought_verdict 走 unknown 分支。
    """
    p = activity_path()
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"_read_error": f"{type(e).__name__}: {e}"}
    if not isinstance(d, dict):
        return {"_read_error": f"活動檔不是 dict（{type(d).__name__}）"}
    return d


def classify_holds(hold_reasons: dict | None) -> dict:
    """把 hold 理由分布拆成 blind / gated / other 三堆（純函式）。

    ⛔ btc_gate_closed 與 btc_gate_stale 永遠分開數。把它們併成「btc_gate 相關」
       就是把這次事故的唯一線索抹掉。
    """
    blind = gated = other = 0
    if isinstance(hold_reasons, dict):
        for k, v in hold_reasons.items():
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue
            key = str(k)
            if key.startswith(_BLIND_PREFIXES):
                blind += n
            elif key == "btc_gate_closed":
                gated += n
            else:
                other += n
    return {"blind": blind, "gated": gated, "other": other,
            "total": blind + gated + other}


def drought_verdict(activity: dict | None, last_fire_ts, *,
                    now_s: float | None = None,
                    warn_sec: int = DROUGHT_WARN_SEC,
                    alert_sec: int = DROUGHT_ALERT_SEC,
                    max_age_sec: int = ACTIVITY_MAX_AGE_SEC) -> dict | None:
    """「引擎多久沒產出、為什麼」——純函式，可離線測。

    回 None ＝ 沒有乾旱（近期有 FIRE）。否則回：
        {hours, cls, severity, fault, text, counts}

    cls 三種，**互不折疊**：
        "gated"   閘關著且是真的量出來的 → 濾網在做事。fault=False（不是故障，
                  但仍要每輪出現在帳本上：零產出是使用者有權知道的事實）。
        "blind"   有 hold 是「讀不到」造成的 → 失明。fault=True。
        "unknown" 活動檔缺/壞/過舊，或最後 FIRE 時間讀不到 → 我們根本看不到引擎。
                  fault=True。⛔ 絕不可因為「沒量到問題」就回 None。

    ⛔ last_fire_ts 傳 None 有兩種意思，呼叫端必須先分好：
       「表是空的、從來沒 FIRE 過」與「DB 讀不到」是完全不同的事實。本函式把
       None 一律當 unknown 處理（保守），呼叫端若確知是空表，請傳 0。
    """
    now_s = now_s if now_s is not None else time.time()

    # --- ① 活動檔本身可信嗎 -------------------------------------------------
    if activity is None:
        return _verdict("unknown", None, {},
                        "掃描活動檔不存在——無法判斷訊號引擎是否還在產出", now_s)
    err = activity.get("_read_error")
    if err:
        return _verdict("unknown", None, {},
                        f"掃描活動檔讀不出來（{err}）——⛔ 這不等於「引擎沒問題」", now_s)
    try:
        act_ts = float(activity.get("ts") or 0)
    except (TypeError, ValueError):
        act_ts = 0.0
    if act_ts <= 0:
        return _verdict("unknown", None, {},
                        "掃描活動檔沒有可信的時間戳——無法判斷引擎狀態", now_s)
    act_age = now_s - act_ts
    if act_age > max_age_sec:
        return _verdict("unknown", None, {},
                        f"掃描活動檔已 {act_age / 3600:.1f}h 未更新"
                        f"（門檻 {max_age_sec / 3600:.0f}h）＝掃描迴圈本身可能已停",
                        now_s)

    # --- ② 最後一筆 FIRE 是什麼時候 -----------------------------------------
    if last_fire_ts is None:
        return _verdict("unknown", None, classify_holds(activity.get("hold_reasons")),
                        "最後一筆 FIRE 的時間讀不到（fire_queue 不可讀）"
                        "——⛔ 這不等於「沒有乾旱」", now_s)
    try:
        lf = float(last_fire_ts)
    except (TypeError, ValueError):
        return _verdict("unknown", None, classify_holds(activity.get("hold_reasons")),
                        f"最後一筆 FIRE 的時間格式無法解讀（{last_fire_ts!r}）", now_s)

    # lf == 0 ＝ 呼叫端確認過「表是空的、從來沒 FIRE 過」。此時用活動檔的起算點，
    # 沒有起算點就當 unknown（不能拿 epoch 0 算出 56 年乾旱這種假事實）。
    if lf <= 0:
        since = float(activity.get("first_seen_ts") or 0) or act_ts
        elapsed = now_s - since
    else:
        elapsed = now_s - lf

    if elapsed < warn_sec:
        return None

    # --- ③ 乾旱成因分類 ------------------------------------------------------
    counts = classify_holds(activity.get("hold_reasons"))
    gate_open = activity.get("btc_gate_open")
    hours = elapsed / 3600.0
    blocked, blocked_note = _blocked_by_check(activity)

    if counts["blind"] > 0:
        return _verdict(
            "blind", hours, counts,
            f"訊號引擎已 {hours:.0f}h（{hours / 24:.1f} 天）零產出，而且最近一輪有 "
            f"{counts['blind']} 檔是**因為讀不到資料**才 HOLD"
            f"（btc_gate_stale／filter_stale 類）——這是失明，不是市場沒機會。"
            f"補上資料源之前，引擎不會產出任何東西。", now_s)

    if counts["gated"] > 0 and counts["gated"] >= counts["other"]:
        return _verdict(
            "gated", hours, counts,
            f"訊號引擎已 {hours:.0f}h（{hours / 24:.1f} 天）零產出：最近一輪 "
            f"{counts['gated']}/{counts['total']} 檔 HOLD 在 BTC 大盤閘"
            f"（btc_gate_open={gate_open}，來源 {activity.get('btc_gate_source') or '?'}）。"
            f"閘是**量出來的**、不是讀不到——濾網在正常做事，非故障。"
            f"但零產出持續中，這件事本身你有權每天看到。{blocked_note}", now_s)

    if counts["total"] == 0:
        # v240：零 hold 不一定是成因不明——「每檔都 FIRE 了、但每筆都被 cross-check
        # 擋下」也長這樣。這種情況成因一清二楚，指名它，別歸到 unknown 去。
        if blocked is not None and blocked > 0:
            return _verdict(
                "gated", hours, counts,
                f"訊號引擎已 {hours:.0f}h（{hours / 24:.1f} 天）零產出：最近一輪有 "
                f"{blocked} 筆 FIRE 被 cross-check 擋下"
                f"（{_top_reason(activity.get('check_block_reasons'), '（擋單理由未記錄）')}）"
                f"——**不是冷卻、也不是市場沒機會**，是一致性檢查量到東西否決了它們。",
                now_s)
        # 有乾旱、卻連一筆 hold 都沒有。⛔ 不可讓它掉進下面那句含糊的
        #    「最大宗 HOLD 理由：（無）」——那讀起來像「市場很安靜」，
        #    但真相通常是**一檔都沒掃到**（watchlist refresh 失敗／宇宙來源掛了）。
        #    實測時這一格先騙過我一次：探針掃 0 檔，訊息卻寫得像沒事。
        try:
            scanned = int(activity.get("scanned") or 0)
        except (TypeError, ValueError):
            scanned = -1        # 連 scanned 都讀不出來 → 更不能講「沒事」
        if scanned <= 0:
            return _verdict(
                "unknown", hours, counts,
                f"訊號引擎已 {hours:.0f}h（{hours / 24:.1f} 天）零產出，而且最近一輪"
                f"**一檔都沒掃到**（scanned={scanned if scanned >= 0 else '讀不出'}）"
                f"——這不是市場沒機會，是標的宇宙讀不出來"
                f"（watchlist refresh 失敗／資料源掛了）。", now_s)
        return _verdict(
            "unknown", hours, counts,
            f"訊號引擎已 {hours:.0f}h（{hours / 24:.1f} 天）零產出：掃了 {scanned} 檔、"
            f"零 FIRE、卻也一筆 HOLD 都沒記到——成因不明，不可當成「市場很安靜」。",
            now_s)

    top = _top_reason(activity.get("hold_reasons"))
    return _verdict(
        "gated", hours, counts,
        f"訊號引擎已 {hours:.0f}h（{hours / 24:.1f} 天）零產出，"
        f"最大宗 HOLD 理由：{top}{blocked_note}", now_s)


def _blocked_by_check(activity: dict) -> tuple[int | None, str]:
    """讀「本輪被 cross-check 擋下幾筆」。回 (數量或 None, 要附進訊息的字串)。

    三種狀態要分清楚，⛔ 一種都不准折成另一種：
      * 欄位不存在  → None。意思是「這一版活動檔量不到」，**不是**「確認 0 筆被擋」。
                      這時訊息不加任何字——不宣稱有、也不宣稱沒有。
      * 讀不出來    → None，但訊息要明講讀不出，否則又變成靜默。
      * 有值        → int。>0 才附上「被擋了幾筆、是哪一項擋的」。
    """
    if "fires_blocked_check" not in activity:
        return None, ""
    raw = activity.get("fires_blocked_check")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None, f"（⚠️ cross-check 擋單數讀不出：{raw!r}，本輪成因可能不完整）"
    if n <= 0:
        return n, ""
    return n, (f"（另有 {n} 筆 FIRE 被 cross-check 擋下："
               f"{_top_reason(activity.get('check_block_reasons'), '（擋單理由未記錄）')}）")


def _top_reason(hold_reasons, empty: str = "（無 hold 理由紀錄）") -> str:
    if not isinstance(hold_reasons, dict) or not hold_reasons:
        return empty
    items = sorted(hold_reasons.items(), key=lambda kv: -int(kv[1] or 0))
    return "、".join(f"{k}×{v}" for k, v in items[:3])


def _verdict(cls: str, hours, counts: dict, text: str, now_s: float) -> dict:
    # unknown 一律 alert：看不見引擎，比看見引擎在休息嚴重。
    if cls == "unknown":
        severity, fault = "alert", True
    elif cls == "blind":
        severity, fault = "alert", True
    else:
        severity, fault = "warn", False
    return {"cls": cls, "hours": round(hours, 1) if hours is not None else None,
            "severity": severity, "fault": fault, "text": text,
            "counts": counts, "checked_at": int(now_s)}
