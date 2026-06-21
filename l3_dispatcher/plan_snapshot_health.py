"""plan_snapshot 捕捉健康度（唯讀遙測）。

治本的是一整類 bug，不是單一錯誤：daemon 重啟若載入過期的工作樹程式碼，
進場 plan_snapshot 會「靜默退化」——要嘛根本沒寫進去（NULL），要嘛帶著舊
regime 碼的殘留簽名（regime_at_entry.vol_trend == "deepdive"，context 全空、
oi_price_quadrant 恆 None）。退化不會報錯、不會中斷，只會讓復盤引擎的優化器
日後拿不到象限標籤——過去要靠鑑識考古才查得出來（與 task#78 / task#79「重啟後
靜默退化」同一 bug-class）。

本模組讓這類退化「自動浮現」：每輪 supervisor 健康檢查量一次近窗 deepdive 進場
的捕捉品質，退化即推 Telegram 告警（節流由 supervisor 既有 _alert 處理）。

設計守則：
  - 純唯讀、零下單數學、例外安全：任何錯誤回 verdict="unknown"，永不擲出，
    絕不拖垮 supervisor 主迴圈。
  - 只看「近窗」（預設 48h）的 deepdive 進場——歷史上 pre-fix 的 NULL/殘留列
    永遠不會自癒，若全時段統計會變成永久誤報（正是 v34 data_quality_low 口徑
    修正要避免的噪音）。近窗自清：退化發生後 48h 內新進場會把比率拉高而告警。
  - quadrant=None 但 OI 在場（價格盤整）＝誠實 None（紅線③），不是退化、不告警。
  - oi_gap（OI 取值失敗導致象限不可判）是資料源降級、非碼退化：列出但不單獨告警，
    與 data_quality_low「可接受降級」同哲學。不靜默隱藏（無 silent cap）。
  - 樣本不足（< min_sample）一律「不予判讀」（同 task#27 n<門檻 fail-closed），
    避免小樣本誤報。
"""
from __future__ import annotations

import json
import sqlite3
import time

from botpaths import db_path as _db_path

# 退化告警門檻（保守，避免單列 flake 誤報）
_NULL_RATE_WARN = 0.5      # 近窗一半以上 snapshot 沒寫進去 = 接線壞
_STALE_LEAK_RATE_WARN = 0.25  # 近窗 1/4 以上帶過期碼殘留簽名 = 跑舊碼

_DEFAULT_WINDOW_SEC = 48 * 3600
_DEFAULT_MIN_SAMPLE = 8


def _classify(plan_snapshot: str | None) -> str:
    """單列分類。與 auto_optimizer._quadrant_of 的鍵路徑一致。"""
    if plan_snapshot is None:
        return "null"
    try:
        d = json.loads(plan_snapshot)
    except Exception:
        return "parse_err"
    rg = d.get("regime_at_entry") or {}
    if rg.get("vol_trend") == "deepdive":
        # 舊 regime 碼殘留簽名：build_plan_snapshot 收到 regime="deepdive"
        # 且沒有真 regime_vector → vol_trend 被填成字面 "deepdive"，context 全空。
        return "stale_leak"
    if rg.get("oi_price_quadrant"):
        return "quadrant_ok"
    # quadrant 為 None：區分「誠實盤整」vs「OI 取值缺口」
    ctx = d.get("context_at_entry") or {}
    if ctx.get("oi_delta_pct") is None:
        return "oi_gap"
    return "none_rangebound"


def capture_health(
    *,
    db_path: str | None = None,
    window_seconds: int = _DEFAULT_WINDOW_SEC,
    min_sample: int = _DEFAULT_MIN_SAMPLE,
    now: float | None = None,
    since_ts: float | None = None,
) -> dict:
    """量近窗 deepdive 進場的 plan_snapshot 捕捉品質。

    語意是「**目前正在跑的** daemon 寫出來的 snapshot 健不健康」——故 supervisor
    呼叫時須傳 since_ts=daemon 啟動時刻，把「前一個 daemon 化身（可能跑過期碼）」
    寫的歷史列排除掉，只看當前碼的產出。否則固定 48h 窗會把已治癒的 pre-fix 歷史
    （NULL / vol=deepdive 殘留）一直算進來，對「已修好的舊退化」永久誤報（正是
    v34 data_quality_low 口徑要避免的噪音）。

    有效起點 cutoff = max(now - window_seconds, since_ts)：兼顧近 48h 新鮮度 +
    「不看 daemon 啟動之前」相關性。剛重啟時窗內樣本不足 → "insufficient" 不告警，
    待當前碼累積足量進場才判讀（fail-closed）。

    回傳 dict（永不擲出）：
        verdict: "ok" | "degraded" | "insufficient" | "unknown"
        sample, window_hours, null_rate, stale_leak_rate, oi_gap_rate,
        counts={null,parse_err,stale_leak,quadrant_ok,none_rangebound,oi_gap},
        offenders=[symbol,...]（NULL/parse_err/stale_leak 的標的，供告警明細）
    """
    out: dict = {
        "verdict": "unknown",
        "sample": 0,
        "window_hours": round(window_seconds / 3600, 1),
        "null_rate": 0.0,
        "stale_leak_rate": 0.0,
        "oi_gap_rate": 0.0,
        "counts": {},
        "offenders": [],
    }
    try:
        now = time.time() if now is None else now
        cutoff_sec = now - window_seconds
        if since_ts is not None:
            cutoff_sec = max(cutoff_sec, since_ts)
        cutoff_ms = int(cutoff_sec * 1000)
        path = db_path or _db_path("trade_journal.db")
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT symbol, plan_snapshot FROM paper_trades "
                "WHERE regime='deepdive' AND entry_at >= ? ORDER BY entry_at",
                (cutoff_ms,),
            ).fetchall()
        finally:
            con.close()

        counts = {
            "null": 0, "parse_err": 0, "stale_leak": 0,
            "quadrant_ok": 0, "none_rangebound": 0, "oi_gap": 0,
        }
        offenders: list[str] = []
        for r in rows:
            cls = _classify(r["plan_snapshot"])
            counts[cls] = counts.get(cls, 0) + 1
            if cls in ("null", "parse_err", "stale_leak"):
                offenders.append(f"{r['symbol']}:{cls}")

        n = len(rows)
        out["sample"] = n
        out["counts"] = counts
        out["offenders"] = offenders[:20]
        if n < min_sample:
            out["verdict"] = "insufficient"
            return out

        broken = counts["null"] + counts["parse_err"]
        leak = counts["stale_leak"]
        out["null_rate"] = round(broken / n, 3)
        out["stale_leak_rate"] = round(leak / n, 3)
        out["oi_gap_rate"] = round(counts["oi_gap"] / n, 3)

        degraded = (
            out["null_rate"] >= _NULL_RATE_WARN
            or out["stale_leak_rate"] >= _STALE_LEAK_RATE_WARN
        )
        out["verdict"] = "degraded" if degraded else "ok"
        return out
    except Exception as e:  # 唯讀遙測絕不拖垮 supervisor
        out["verdict"] = "unknown"
        out["error"] = f"{type(e).__name__}: {e}"
        return out
