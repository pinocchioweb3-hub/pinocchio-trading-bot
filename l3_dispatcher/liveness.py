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
import tempfile
import time

from botpaths import data_dir

_LIVENESS_FILE = "liveness.json"
_GAPS_FILE = "liveness_gaps.json"   # v50: 偵測到的離線缺口事件（供 CEO 日報「連續性」欄）
_GAPS_KEEP = 50                      # 只保留最近 N 筆，避免無限長大

# 斷層告警門檻：上次戳記距今超過這麼久 → 視為「daemon 曾經斷線」。
# 預設 3600 秒（1 小時）：掃描間隔預設 900 秒，正常重啟只隔幾秒，
# 隔超過 1 小時幾乎必然代表電腦休眠 / 當機 / 長時間關閉。可用 env 覆寫。
DEFAULT_GAP_THRESHOLD_SEC = int(os.getenv("OFFLINE_GAP_ALERT_SEC", "3600"))


def _path():
    return data_dir() / _LIVENESS_FILE


def _gaps_path():
    return data_dir() / _GAPS_FILE


# v199（監督員 r93）：讀取三態。⛔ 勿再折回單一 None／[]。
LOAD_OK = "ok"
LOAD_MISSING = "missing"          # 真的沒有檔＝真·第一次啟動，唯一可當「沒有歷史」用的一態
LOAD_UNREADABLE = "unreadable"    # 檔在但讀不出／壞檔／形狀不對＝內容**未知**


def _atomic_write(p, text: str) -> None:
    """temp + flush + fsync + os.replace。失敗向上拋，由呼叫端決定怎麼收斂。

    fsync 不可省：只有 os.replace 的話內容可能還在作業系統快取裡就換名，斷電後目的地
    留下零長度／半截檔——那正是本模組所有誤判的自產來源（本機有斷電事件史，v177 才補
    電力哨兵）。而這個模組偏偏就是**用來抓斷電造成的停機**的，等於自己挖自己的偵測器。
    """
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".liveness_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _preserve_bad(p, err: Exception, what: str) -> None:
    """壞檔留一份鑑識副本（原檔下一輪就會被蓋掉）並出聲。⛔ 不刪不改原檔。"""
    kept = ""
    try:
        bad = p.with_suffix(".bad")
        if not bad.exists():           # 只留最早那一份（後續覆蓋會沖掉第一現場）
            bad.write_bytes(p.read_bytes())
        kept = f"；壞檔已留證於 {bad.name}"
    except Exception:  # noqa: BLE001 留證是 best-effort，不可反過來壓掉主訊息
        kept = "；（留證失敗）"
    print(f"🚨 [liveness] {what}存在但讀不出來（{type(err).__name__}: {err}）{kept}"
          "——⛔ 不當成『第一次啟動／沒有斷層』")


def stamp(extra: dict | None = None) -> None:
    """寫入「現在還活著」戳記。原子寫。永不拋例外（不可拖垮掃描迴圈）。"""
    try:
        rec = {"ts": time.time()}
        if extra:
            rec.update(extra)
        _atomic_write(_path(), json.dumps(rec))
    except Exception:
        pass  # 戳記失敗無所謂，下次再寫；絕不影響主流程


def read_last_status() -> tuple[dict | None, str]:
    """讀上次戳記，回 (rec, status)，status ∈ {ok, missing, unreadable}。

    為何要三態：舊版把「沒有檔」與「檔在但壞掉」折成同一個 None，而 check_gap 把 None
    讀成「第一次啟動＝沒有斷線」。本模組存在的唯一理由就是抓當機／斷電造成的停機，而
    斷電正好會把非原子寫的戳記檔留成半截——⇒ 最嚴重的那一次停機，剛好是唯一被判成
    「沒有斷層」而全程靜默的一次。偵測器在它最該出聲的時候啞掉。
    """
    p = _path()
    try:
        if not p.exists():
            return None, LOAD_MISSING
    except OSError:
        return None, LOAD_UNREADABLE      # 連「在不在」都問不出來 → 未知，不可當沒有
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(rec, dict):
            raise ValueError(f"頂層是 {type(rec).__name__} 不是物件")
        float(rec["ts"])                  # 能解析但缺 ts／型別不對＝內容仍是未知
    except Exception as e:  # noqa: BLE001
        _preserve_bad(p, e, "存活戳記")
        return None, LOAD_UNREADABLE
    return rec, LOAD_OK


def read_last() -> dict | None:
    """讀上次戳記（相容投影）。檔案不存在 / 壞掉 → None。

    ⚠️ 呼叫端若要區分「沒有」與「不知道」，請改用 read_last_status()。
    """
    rec, _status = read_last_status()
    return rec


def check_gap(threshold_sec: int = DEFAULT_GAP_THRESHOLD_SEC,
              now: float | None = None) -> dict:
    """比對上次戳記與現在，判斷是否有運行斷層。

    回 {gap: bool, last_ts: float|None, gap_sec: float|None, reason: str, unreadable: bool}。
    gap=True 代表「上次戳記距今 > 門檻」= daemon 曾經斷線。
    last_ts=None（首次啟動、無歷史戳記）→ gap=False（不告警，這不是斷線）。
    unreadable=True（戳記在但讀不出來）→ **時長不明的可能斷線**：gap 仍是 False（我們
    沒有根據斷言斷過線），但呼叫端必須出聲說「不知道」，⛔ 不可沿用 no_prior_stamp 的
    靜默路徑——那等於把未知講成「沒事」。
    """
    now = time.time() if now is None else now
    rec, status = read_last_status()
    if status == LOAD_UNREADABLE:
        return {"gap": False, "last_ts": None, "gap_sec": None,
                "reason": "stamp_unreadable", "unreadable": True}
    if not rec or "ts" not in rec:
        return {"gap": False, "last_ts": None, "gap_sec": 0.0,
                "reason": "no_prior_stamp", "unreadable": False}
    last_ts = float(rec["ts"])
    gap_sec = now - last_ts
    if gap_sec > threshold_sec:
        return {"gap": True, "last_ts": last_ts, "gap_sec": gap_sec,
                "reason": "threshold_exceeded", "unreadable": False}
    return {"gap": False, "last_ts": last_ts, "gap_sec": gap_sec,
            "reason": "within_threshold", "unreadable": False}


# ---------------------------------------------------------------------------
# v50: 離線缺口事件帳本 —— 啟動時偵測到斷層就記一筆，供 CEO 日報「連續性」欄回顧。
# 純本地、append-only、上限 _GAPS_KEEP 筆。永不拋例外（不可拖垮啟動流程）。
# ---------------------------------------------------------------------------
def record_gap(last_ts: float | None, gap_sec: float | None,
               now: float | None = None, unknown: bool = False) -> bool:
    """把一次偵測到的離線缺口寫入帳本（在 check_gap 回 gap=True 後呼叫）。回是否寫成功。

    ⛔ 帳本讀不出來時**停手不寫**：這裡是 read-modify-write，舊版壞檔那輪 _read_gaps()
    回 []，append 一筆之後把整份帳本寫回去 ⇒ 最多 50 筆歷史缺口被原子且乾淨地抹掉，
    原位元組不留、事後無從還原（與 v196／v197／v198 同型：一次讀失敗被兌現成不可逆的
    寫抹除）。停手＝維持現狀，原檔原封不動留給人工檢視。
    """
    detected_at = time.time() if now is None else now
    events, status = _read_gaps_status()
    if status == LOAD_UNREADABLE:
        print("🚨 [liveness] 離線缺口帳本讀不出來 → 本次**不寫入**（原檔未動）；"
              "⛔ 若照舊寫入會把整份歷史抹成只剩這一筆。本次缺口未記錄，請人工檢視 "
              f"{_gaps_path().with_suffix('.bad').name}")
        return False
    try:
        events.append({"detected_at": float(detected_at),
                       "last_ts": None if last_ts is None else float(last_ts),
                       "gap_sec": None if gap_sec is None else float(gap_sec),
                       "unknown_duration": bool(unknown)})
        events = events[-_GAPS_KEEP:]
        _atomic_write(_gaps_path(), json.dumps(events))
        return True
    except Exception as e:  # noqa: BLE001 記錄失敗不影響主流程，但⛔不可無聲
        print(f"🚨 [liveness] 離線缺口寫入失敗（{type(e).__name__}: {e}）——本次缺口未記錄")
        return False


def _read_gaps_status() -> tuple[list[dict], str]:
    """讀缺口帳本，回 (events, status)。⛔ 壞檔不得折成空清單（＝「沒有缺口」）。"""
    p = _gaps_path()
    try:
        if not p.exists():
            return [], LOAD_MISSING
    except OSError:
        return [], LOAD_UNREADABLE
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"頂層是 {type(data).__name__} 不是陣列")
    except Exception as e:  # noqa: BLE001
        _preserve_bad(p, e, "離線缺口帳本")
        return [], LOAD_UNREADABLE
    return data, LOAD_OK


def _read_gaps() -> list[dict]:
    """相容投影（壞檔→[]）。⛔ 呈現端／寫入端一律改用 _read_gaps_status。"""
    events, _status = _read_gaps_status()
    return events


def recent_gaps_status(within_sec: float = 86400,
                       now: float | None = None) -> tuple[list[dict], str]:
    """回 (過去 within_sec 內的缺口事件, status)。status=unreadable 代表**不知道**，
    ⛔ 呈現端不得渲染成「無離線缺口」。"""
    now = time.time() if now is None else now
    cutoff = now - within_sec
    events, status = _read_gaps_status()
    kept = [e for e in events
            if isinstance(e, dict) and float(e.get("detected_at", 0) or 0) >= cutoff]
    return kept, status


def recent_gaps(within_sec: float = 86400, now: float | None = None) -> list[dict]:
    """回傳 detected_at 落在過去 within_sec 內的缺口事件（預設過去 24h）。相容投影。"""
    kept, _status = recent_gaps_status(within_sec, now)
    return kept


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


def render_unknown_stamp_alert() -> str:
    """組「戳記讀不出來」告警（繁中、HTML）。純函式，可離線測試。

    ⛔ 這則刻意要出聲：舊版在這個情形走的是「第一次啟動」的靜默路徑，而戳記壞掉最可能
    的成因就是斷電／當機——也就是它本來該告訴你的那件事。
    """
    return (
        "⚠️ <b>存活戳記讀不出來（無法判斷剛剛有沒有斷線）</b>\n"
        "檔案在，但內容壞掉或格式不對（常見成因：斷電／當機時寫到一半）。\n"
        "因此<b>這次無法算出中斷時長</b>——可能完全沒斷，也可能斷了很久。\n"
        "<i>（系統已照常啟動並重寫戳記；壞檔已另存一份供事後查看。"
        "下次起判斷會恢復正常。）</i>"
    )


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
