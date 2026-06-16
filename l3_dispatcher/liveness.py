"""運行存活戳記 + 啟動斷層偵測（v49 / CEO 走查安全批次）。

解決的真實問題（使用者在自己 Windows PC 上 24/7 跑這個 daemon）：
    daemon 整個死掉（當機 / 電腦休眠 / 被手動關）時，**沒有任何東西**會告訴你
    「剛剛斷線了多久」。supervisor worker 抓不到——因為它跟 daemon 同進程，
    daemon 死了它也死了，死掉的進程發不出訊息。heartbeat 每小時報平安也沒用——
    斷線期間根本沒人在跑。

正確解法（純本地、零外部依賴、零例行噪音）：
    ① 活著時每輪掃描寫一個「我還活著 @時間T」的本地戳記檔（不發任何 Telegram）。
    ② 下次 daemon 啟動時，比對「現在」與「上次戳記」。若中間隔太久 → 推**一則**
       Telegram 告警到系統主題，明確說「中斷了約 N 小時」。
    這只在真的有斷層時才出聲（高訊號），平常完全安靜——和已在運作的
    CEO 每日簡報（每天 09:00 台北報健康）互補，不重複洗版。

外部 dead-man's-switch（healthchecks.io 之類「daemon 死了還能主動通知你」）
    需網路 + 外部服務，屬另一階段；本模組刻意只做純本地能做到的那半。

純讀寫一個 JSON 檔，不下任何單、不發任何對外內容。
"""
from __future__ import annotations

import json
import os
import time

from botpaths import data_dir

_LIVENESS_FILE = "liveness.json"

# 斷層告警門檻：上次戳記距今超過這麼久 → 視為「daemon 曾經斷線」。
# 預設 3600 秒（1 小時）：掃描間隔預設 900 秒，正常重啟只隔幾秒，
# 隔超過 1 小時幾乎必然代表電腦休眠 / 當機 / 長時間關閉。可用 env 覆寫。
DEFAULT_GAP_THRESHOLD_SEC = int(os.getenv("OFFLINE_GAP_ALERT_SEC", "3600"))


def _path():
    return data_dir() / _LIVENESS_FILE


def stamp(extra: dict | None = None) -> None:
    """寫入「現在還活著」戳記。永不拋例外（不可拖垮掃描迴圈）。"""
    try:
        rec = {"ts": time.time()}
        if extra:
            rec.update(extra)
        _path().write_text(json.dumps(rec), encoding="utf-8")
    except Exception:
        pass  # 戳記失敗無所謂，下次再寫；絕不影響主流程


def read_last() -> dict | None:
    """讀上次戳記。檔案不存在 / 壞掉 → None。"""
    try:
        p = _path()
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def check_gap(threshold_sec: int = DEFAULT_GAP_THRESHOLD_SEC,
              now: float | None = None) -> dict:
    """比對上次戳記與現在，判斷是否有運行斷層。

    回 {gap: bool, last_ts: float|None, gap_sec: float, reason: str}。
    gap=True 代表「上次戳記距今 > 門檻」= daemon 曾經斷線。
    last_ts=None（首次啟動、無歷史戳記）→ gap=False（不告警，這不是斷線）。
    """
    now = time.time() if now is None else now
    rec = read_last()
    if not rec or "ts" not in rec:
        return {"gap": False, "last_ts": None, "gap_sec": 0.0,
                "reason": "no_prior_stamp"}
    last_ts = float(rec["ts"])
    gap_sec = now - last_ts
    if gap_sec > threshold_sec:
        return {"gap": True, "last_ts": last_ts, "gap_sec": gap_sec,
                "reason": "threshold_exceeded"}
    return {"gap": False, "last_ts": last_ts, "gap_sec": gap_sec,
            "reason": "within_threshold"}


def _fmt_duration(sec: float) -> str:
    """秒 → 人話時長（繁中）。"""
    sec = int(sec)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d} 天")
    if h:
        parts.append(f"{h} 小時")
    if m and not d:  # 超過一天就不囉嗦分鐘
        parts.append(f"{m} 分")
    return " ".join(parts) or "不到 1 分鐘"


def render_gap_alert(last_ts: float, gap_sec: float) -> str:
    """組斷層告警訊息（繁中、HTML）。純函式，可離線測試。"""
    import datetime as dt
    tpe = dt.datetime.fromtimestamp(last_ts, tz=dt.timezone.utc) + dt.timedelta(hours=8)
    return (
        "⚠️ <b>偵測到運行斷層（daemon 剛從中斷中恢復）</b>\n"
        f"上次活動：<code>{tpe.strftime('%Y-%m-%d %H:%M')}</code> 台北\n"
        f"中斷時長：約 <b>{_fmt_duration(gap_sec)}</b>\n"
        "這段期間<b>沒有掃描、沒有任何訊號</b>。\n"
        "可能原因：電腦休眠 / 當機 / 手動關閉 / 網路長時間中斷。\n"
        "<i>（系統已自動恢復；此為事後通知，非當前故障。"
        "正常重啟不會出現這則訊息。）</i>"
    )
