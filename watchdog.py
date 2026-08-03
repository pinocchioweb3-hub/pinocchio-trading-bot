"""watchdog.py — daemon 斷線自動偵測 + 自動重啟（單次執行版）。

解決的真實問題（2026-06-17 事故）：
    daemon 在 19:31 心跳停止、20:24 整個進程消失，**沒有任何崩潰痕跡**
    （bot.err.log 0 bytes、stdout 收在乾淨的 "[supervisor] all good"、
    Windows 事件檢視器無關機/休眠/Defender/當機事件）—— 即「被外部靜默終止」。
    當時沒有任何東西會把它救回來：boot 自啟任務（XBot）是停用的、也沒有 watchdog。
    結果：單一次終止 = 永久離線，直到人工重啟。

設計（OS 級監督，不靠 daemon 自己）：
    本檔是「單次執行」的健康檢查 + 條件式重啟。由 Windows Task Scheduler 每 3 分鐘
    （外加 開機 / 登入）觸發跑一次就結束。Task Scheduler 本身永不死 → 它監督 watchdog，
    watchdog 監督 daemon。三層下來，任何一層被殺都會在數分鐘內自癒。

健康判定（雙訊號）：
    ① liveness.json 新鮮度：daemon 每輪掃描（預設 900s）寫一次戳記。
       now - ts 超過 STALE_SEC（預設 1800s＝30 分）→ 視為「死掉或卡住（zombie）」。
       這同時涵蓋「進程還在但某執行緒掛了」的情況（正是 19:31→20:24 那段）。
    ② 防抖：剛重啟過 GRACE_SEC（預設 300s）內不重複動作（daemon 啟動暖機要時間，
       第一筆心跳還沒寫）。
    ③ 防無限重啟：1 小時內重啟達 MAX_RESTARTS_HOUR（預設 5）次仍失敗 → 停手 + 告警，
       交人工（代表是啟動就崩，狂重啟無益）。

安全邊界（明確）：
    - 本 watchdog 只重啟「訊號/紙上 daemon」（run_bot.py），它**不下任何真錢單**。
      真錢路徑是另一條（人工逐筆確認的 trade-intent），watchdog 永不碰。
    - 重啟＝純本機行為，不推送公開、不碰金鑰（只讀 .env 取 Telegram token 發告警）。
    - 暫停開關：data_dir 下若存在 `watchdog.disabled` 檔，watchdog **不重啟 daemon**
      （讓你能「刻意關掉 bot」而不被 watchdog 一直拉起來）。
      ⚠️ v237 起，這個開關的射程僅限「自動重啟」：記憶體防衛照常執行（它清的是
      Claude 排程殭屍，與 daemon 重不重啟無關），且處於暫停狀態這件事會定期
      推播出來（見 PAUSE_ALERT_COOLDOWN_SEC）。⛔ watchdog 永不自己刪除這個檔。

純標準庫，零第三方依賴 → 即使 bot 的程式壞掉，watchdog 仍能跑、仍能把它拉回來。
"""
from __future__ import annotations

import json
import locale
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# --- 路徑（不依賴 bot 的程式碼，bot 壞了 watchdog 也要能跑）---
ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("TRADINGBOT_DATA_DIR") or
                (Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TradingBot"))
LIVENESS = DATA_DIR / "liveness.json"
STATE = DATA_DIR / "watchdog_state.json"
WLOG = DATA_DIR / "watchdog.log"
DISABLED_FLAG = DATA_DIR / "watchdog.disabled"
START_SCRIPT = ROOT / "start_bot.ps1"

# --- 門檻（可用 env 覆寫）---
STALE_SEC = int(os.getenv("WATCHDOG_STALE_SEC", "1800"))         # 心跳超過此秒數沒更新 = 死/卡
GRACE_SEC = int(os.getenv("WATCHDOG_GRACE_SEC", "300"))          # 剛重啟後的暖機/冷卻窗
MAX_RESTARTS_HOUR = int(os.getenv("WATCHDOG_MAX_RESTARTS_HOUR", "5"))

# --- 記憶體防衛（2026-07-29，使用者授權的自動排除）---
# 真實事故：Claude App 排程 session 的 claude.exe 完工後不退出，每 30 分漏 ~390MB，
# 一天內把 16GB 機器的 commit charge 推到 94%（=任何程式隨時崩潰=無聲當機機制）。
# App 端 bug 我們修不了 → OS 級 watchdog 在「緊急線」時強制清理殭屍 runner 進程。
# 只殺 Roaming\Claude\claude-code 路徑（排程 runner）、年齡超過門檻者，
# 永不碰 GUI App（WindowsApps）、python daemon、其他程式。緊急線才動手＝
# 平時使用者的互動 session 絕不會被誤殺；到了緊急線，整機凍死的替代結局更糟。
MEMGUARD_ON = os.getenv("WATCHDOG_MEMGUARD", "1") == "1"
MEM_EMERGENCY_PCT = float(os.getenv("WATCHDOG_MEM_EMERGENCY_PCT", "88"))   # commit 觸發線
MEM_TARGET_PCT = float(os.getenv("WATCHDOG_MEM_TARGET_PCT", "78"))         # 清到此線停手
MEM_MIN_AGE_MIN = float(os.getenv("WATCHDOG_MEM_MIN_AGE_MIN", "90"))       # 只殺 ≥90 分鐘老進程
MEM_COOLDOWN_SEC = int(os.getenv("WATCHDOG_MEM_COOLDOWN_SEC", "600"))      # 兩次清理間隔
_RUNNER_MARK = os.path.join("Roaming", "Claude", "claude-code")            # 唯一可殺的路徑指紋

# --- memguard 的 Telegram 節流（2026-07-31 量測後補）---
# 實測：watchdog 每 3 分觸發、清理冷卻 600s ⇒ 約每 12 分一則 Telegram。
# 07-31 單日推播 101 則（57「無可清」＋44「已清理」）、全部送達。問題是同一個
# Telegram 頻道也是真錢執行器「401 恢復」告警的唯一出口——當時全系統唯一在等的
# 訊號，會被埋在這 101 則例行噪音裡。兩則 memguard 訊息在同一小時內重複時完全
# 不帶新資訊（無可清＝請人工重啟 App；已清理＝自癒成功），被稀釋掉的那一則卻是
# 唯一需要人立刻行動的 ⇒ 節流「推播」，本機 log 仍每輪全寫。
# ⛔ 這裡只動通知，不動 MEM_COOLDOWN_SEC（清理本體的節奏）。
MEM_ALERT_COOLDOWN_SEC = int(os.getenv("WATCHDOG_MEM_ALERT_COOLDOWN_SEC", "21600"))  # 6h
MEM_ALERT_ESCALATE_PCT = float(os.getenv("WATCHDOG_MEM_ALERT_ESCALATE_PCT", "3"))    # 惡化穿透

# --- 暫停狀態的推播節流（2026-08-03 事故後補）---
# watchdog 每 3 分觸發一次，暫停狀態若每輪推一則就是每小時 20 則＝洗版，會把
# 真錢路徑的告警埋掉（memguard 節流學過的同一課）。本機 log 仍每輪全寫。
PAUSE_ALERT_COOLDOWN_SEC = int(os.getenv("WATCHDOG_PAUSE_ALERT_COOLDOWN_SEC", "21600"))  # 6h
# 寬限期：部署窗（建旗標→重啟→驗證→移除）本來就會讓旗標存在數分鐘，那是正常流程。
# 每次部署都推一則＝把使用者訓練成忽略這則告警，那正是我們要治的失明本身。
# 只有「留下來了」才出聲——08-01 11:13→08-02 02:13 那次留了 15 小時，事後才被
# CEO 日報回溯發現，期間 daemon 掛掉不會有任何東西拉它起來。
PAUSE_ALERT_GRACE_SEC = int(os.getenv("WATCHDOG_PAUSE_ALERT_GRACE_SEC", "3600"))  # 1h


def _commit_pct() -> float | None:
    """Windows commit charge 使用率（%）。失敗回 None（不動作）。純標準庫 ctypes。"""
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None
        if not st.ullTotalPageFile:
            return None
        used = st.ullTotalPageFile - st.ullAvailPageFile
        return used / st.ullTotalPageFile * 100.0
    except Exception:
        return None


class _RunnerProbeError(RuntimeError):
    """『列不出 runner 清單』（未知）—— 與『量到 0 個』是兩件不同的事實。

    2026-08-01（r80）踩到的坑：本函式原本 `except Exception: return []`，於是
    任何量測失敗都對外表現成「機器上沒有殭屍」，exit code 還是 0。實際在線上的
    是 4 個。⛔ 未知不可折成 0 —— 本專案同物種第 8 次，這次連偵測器自己都中招。
    """


# 解碼 PowerShell 輸出的候選編碼順序（測試會覆寫此元組以模擬 locale 不一致）。
# locale 優先＝跟隨排程實際環境；cp950/utf-8 為退路，⛔ 缺一個都可能整批失明。
_CONSOLE_ENCODINGS = (locale.getpreferredencoding(False) or "utf-8", "cp950", "utf-8")


def _decode_console(raw) -> str:
    """把子行程 stdout 解成字串。⛔ 這一層永不拋例外、也永不用空字串假裝成功。

    原本靠 `subprocess.run(text=True)`（跟隨 locale）：一旦行程被以 `-X utf8` /
    `PYTHONUTF8=1` 起動，就會拿 UTF-8 去解 cp950 的中文輸出 → UnicodeDecodeError
    → 沿舊路徑被吞成空表。這裡改為「多編碼依序嘗試，全失敗才 ascii+replace」，
    讓 JSON 的 ASCII 骨架至少保得住（真的解不出來會在 json.loads 那關現形）。
    """
    if isinstance(raw, str):
        return raw
    if not raw:
        return ""
    for enc in _CONSOLE_ENCODINGS:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("ascii", errors="replace")


def _stale_claude_runners() -> list[tuple[int, float]]:
    """列出符合『可殺指紋』的 claude.exe：(pid, 年齡秒)。只認 Roaming\\Claude\\claude-code
    路徑（排程 runner）；GUI App / npm bin / 其他一律不列。

    ⛔ 量不到時**拋 _RunnerProbeError**，不回空表——呼叫端必須自己決定「未知」怎麼辦
    （現行決定：不動作 + 留痕）。回 [] 只代表一件事：真的一個符合指紋的都沒有。
    """
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='claude.exe'\" | "
          "Select-Object ProcessId,CommandLine,"
          "@{N='Age';E={[int]((Get-Date)-$_.CreationDate).TotalSeconds}},"
          "@{N='Min';E={$_.CreationDate.Minute}} | "
          "ConvertTo-Json -Compress")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=60)
    except Exception as exc:                      # 逾時／powershell 不存在／被擋
        raise _RunnerProbeError(f"{type(exc).__name__}: {exc}") from exc
    if r.returncode != 0:
        err = _decode_console(r.stderr).strip().replace("\n", " ")[:160]
        raise _RunnerProbeError(f"powershell exit={r.returncode} {err}")
    text = _decode_console(r.stdout).strip()
    if not text:
        return []                                 # 退出碼 0 + 空輸出＝真的沒有行程
    try:
        data = json.loads(text)
    except Exception as exc:
        raise _RunnerProbeError(f"輸出非 JSON（{len(text)} 字元）: {exc}") from exc
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise _RunnerProbeError(f"輸出結構非預期: {type(data).__name__}")
    out = []
    for p in data:
        if not isinstance(p, dict):
            continue
        cmd = p.get("CommandLine") or ""
        if _RUNNER_MARK.lower() in cmd.lower():
            try:
                out.append((int(p["ProcessId"]), float(p.get("Age") or 0),
                            int(p.get("Min") if p.get("Min") is not None else -1)))
            except Exception as exc:              # 單筆壞掉≠整批未知，但也不許靜音
                raise _RunnerProbeError(f"欄位解析失敗: {exc}") from exc
    return out


def _is_cadence_spawn(minute: int) -> bool:
    """排程殭屍的啟動指紋：整點 :03-:07 / :33-:37（排程+抖動窗）。
    使用者的互動/Remote Control session 啟動分鐘是隨機的——2026-08-02 事故：
    常態清掃沒看指紋,把使用者整晚閒置的 RC 宿主進程當殭屍殺了。"""
    return 3 <= minute <= 7 or 33 <= minute <= 37


def _probe_runners_or_log(pct: float) -> list[tuple[int, float]] | None:
    """量 runner 清單；量不到回 None 並在本機 log 留痕（fail-loud，不 fail-silent）。

    ⛔ 回 None 時呼叫端一律「不動作」：未知狀態下殺行程與不殺，只有不殺是安全的。
    ⛔ 也不寫 memguard_last_ts —— 量測失敗不該吃掉下一輪（3 分鐘後）的重試機會。
    """
    try:
        return _stale_claude_runners()
    except _RunnerProbeError as exc:
        log(f"[memguard] 殭屍 runner 清單量測失敗（{exc}）——⛔ 未知不折成 0 個，"
            f"本輪不動作（commit {pct:.0f}%）")
        return None


def _memguard_notify(state: dict, pct: float, text: str, now: float) -> None:
    """memguard 專用的 Telegram 節流。本機 log 由呼叫端無條件寫，這裡只管推播。

    送出條件（滿足其一）：① 距上次送出已過 MEM_ALERT_COOLDOWN_SEC；
    ② 惡化：commit% 比「上次送出時的水位」再高 MEM_ALERT_ESCALATE_PCT 以上。
    被壓下的則數累計在 state，於下次真正送出時一併揭露——永不無聲吞掉。
    """
    last_ts = float(state.get("memguard_alert_ts", 0) or 0)
    last_pct = state.get("memguard_alert_pct")
    escalated = (last_pct is not None
                 and pct >= float(last_pct) + MEM_ALERT_ESCALATE_PCT)
    if last_ts and (now - last_ts) < MEM_ALERT_COOLDOWN_SEC and not escalated:
        state["memguard_alert_suppressed"] = \
            int(state.get("memguard_alert_suppressed", 0) or 0) + 1
        return
    n = int(state.get("memguard_alert_suppressed", 0) or 0)
    if n:
        mins = int((now - last_ts) / 60) if last_ts else 0
        text += (f"\n（過去 {mins} 分鐘內另有 {n} 則同類提醒未推播，"
                 "已完整寫入本機 watchdog.log）")
    telegram_alert(text)
    state["memguard_alert_ts"] = now
    state["memguard_alert_pct"] = pct
    state["memguard_alert_suppressed"] = 0


def _paused_notify(state: dict, now: float) -> None:
    """「自動重啟目前是關閉的」推播（節流）。本機 log 由呼叫端每輪無條件寫。

    ⛔ 對「誰建的」這件事，本檔沒有任何證據——全 repo 只有 watchdog.py 會讀這個
    旗標，沒有任何程式會建立它。所以措辭一律用條件句，只附上它出現的時間讓
    使用者自己認領，不得斷言是他設的。
    ⛔ 也不得順手把它刪掉：使用者刻意關掉的 bot 被自動拉回來，比旗標留著更糟。
    """
    try:
        mtime: float | None = DISABLED_FLAG.stat().st_mtime
    except OSError:
        mtime = None

    # 寬限期內＝正在跑的部署窗，安靜；⛔ 但「讀不到出現時間」不折成「剛建的」，
    # 未知一律當成可能留很久了 → 照常出聲。
    if mtime is not None and (now - mtime) < PAUSE_ALERT_GRACE_SEC:
        return

    last_ts = float(state.get("paused_alert_ts", 0) or 0)
    if last_ts and (now - last_ts) < PAUSE_ALERT_COOLDOWN_SEC:
        state["paused_alert_suppressed"] = \
            int(state.get("paused_alert_suppressed", 0) or 0) + 1
        write_state(state)
        return

    if mtime is None:
        when, age = "未知", ""
    else:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
        age = f"（已持續 {max(0.0, (now - mtime) / 3600.0):.1f} 小時）"

    n = int(state.get("paused_alert_suppressed", 0) or 0)
    tail = (f"\n（過去 {int((now - last_ts) / 60)} 分鐘內另有 {n} 則同類提醒未推播，"
            "已完整寫入本機 watchdog.log）") if n else ""

    telegram_alert(
        "⏸️ <b>daemon 自動重啟目前是關閉的</b>\n"
        f"偵測到暫停旗標 <code>watchdog.disabled</code>，"
        f"出現於 <code>{when}</code> 台北{age}。\n"
        "在它存在期間，daemon 若當掉或心跳停滯，<b>不會有任何東西把它拉回來</b>。\n"
        "記憶體防衛（清 Claude 排程殭屍）不受影響，仍照常執行。\n"
        f"若這不是你刻意設的，刪除 <code>{DISABLED_FLAG}</code> 即可恢復自動重啟。"
        + tail
    )
    state["paused_alert_ts"] = now
    state["paused_alert_suppressed"] = 0
    write_state(state)


def _taskkill(pid: int) -> tuple[bool, str]:
    """殺一個行程。回 (真的殺掉了嗎, 失敗原因)。

    ⛔ taskkill 非零退出**不會**拋例外（subprocess.run 沒帶 check=True），而非零
    正是它回報「沒殺成」的正常方式：rc=128 行程根本不在、rc=1 存取被拒。舊碼因此
    把「送出過 taskkill」當成「殭屍已清」，log 與 Telegram 會宣稱清了 N 個而實際
    可能 0 個——同物種第 9 次（未驗證的結果被當成已完成的事實）。
    """
    try:
        r = subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=15)
    except Exception as exc:                      # 逾時／找不到 taskkill
        return False, f"{pid}:{type(exc).__name__}"
    if r.returncode == 0:
        return True, ""
    err = _decode_console(r.stderr or r.stdout).strip().replace("\n", " ")[:60]
    return False, f"{pid}:rc={r.returncode} {err}".strip()


def _kill_all(victims, *, stop_at_target: bool = False) -> tuple[list[int], list[str], bool]:
    """依序清 victims，回 (真的殺掉的 pid, 失敗描述, 是否提早收手)。

    ⛔ 只有 rc=0 才計入 killed——這一行就是本函式存在的全部理由。
    stop_at_target 只給緊急線用：每殺成 2 個回頭量一次 commit%，降到目標線就收手
    （殺夠了就別多殺）。⛔ 常態清掃**不可**帶這個旗標：它是純衛生清掃，觸發時
    commit 本來就在緊急線以下，帶了會變成「殺 2 個就收工」而放生其餘殭屍。
    """
    killed: list[int] = []
    failed: list[str] = []
    for pid, _age, *_rest in victims:   # v189：容 3 元組（啟動分鐘欄）
        ok, why = _taskkill(pid)
        if not ok:
            failed.append(why)
            continue
        killed.append(pid)
        if stop_at_target and len(killed) % 2 == 0:
            cur = _commit_pct()
            if cur is not None and cur <= MEM_TARGET_PCT:
                return killed, failed, True
    return killed, failed, False


def _fail_tail(failed: list[str], limit: int = 160) -> str:
    """把失敗清單接成一句可讀的尾巴；沒有失敗就回空字串。"""
    if not failed:
        return ""
    return f"；另 {len(failed)} 個殺不掉（{'; '.join(failed)[:limit]}）"


def memory_guard() -> None:
    """兩段式：①常態清掃——殭屍 runner ≥4 個且皆老於 90 分鐘就清（不等記憶體告急，
    治「離開一天越來越卡」）②緊急線——commit ≥88% 強制清到目標線。每次動作必留痕。"""
    if not MEMGUARD_ON:
        return
    pct = _commit_pct()
    if pct is None:
        return
    if pct < MEM_EMERGENCY_PCT:
        # ①常態清掃（2026-08-01 使用者反映效能日衰後加入）
        runners = _probe_runners_or_log(pct)
        if runners is None:                       # 未知：不動作（已留痕）
            return
        # v188：常態清掃只殺「排程指紋」進程（:03-:07/:33-:37 生成）——使用者的
        # 互動/Remote Control session 啟動分鐘隨機,不再被當殭屍誤殺（2026-08-02 事故）。
        # 緊急線（②）不受此限:整機要凍死時保命優先。
        stale = [v for v in runners
                 if v[1] >= MEM_MIN_AGE_MIN * 60 and _is_cadence_spawn(v[2])]
        if len(stale) < 4:
            return
        state, serr = read_state()
        if serr and serr != "missing":
            # ⛔ 冷卻戳記讀不出來≠「冷卻已過」。在沒有冷卻保證的情況下清理行程，
            #    就是每 3 分鐘殺一輪。純衛生的動作，跳過一輪毫無代價。
            log(f"[memguard] 狀態檔讀不出來（{serr}）——冷卻戳記未知，本輪不清掃")
            return
        now = time.time()
        if now - float(state.get("memguard_last_ts", 0) or 0) < MEM_COOLDOWN_SEC:
            return
        killed, failed, _early = _kill_all(sorted(stale, key=lambda v: -v[1]))
        if not killed:
            # ⛔ 一個都沒殺成≠清掃過了：不寫冷卻戳記，下一輪（3 分鐘後）還要再試。
            #    沿用「量測失敗不吃掉重試機會」的同一原則（見 _probe_runners_or_log）。
            log(f"[memguard] 常態清掃：{len(stale)} 個殭屍**一個都沒殺成**"
                f"（{'; '.join(failed)[:200]}）——本輪不算清掃過，下輪重試")
            return
        # PID 一定要印：只寫個數字的話，事後沒有任何辦法回頭驗證它到底殺了誰
        # （2026-08-01 07:34 那筆「清 4 個」就是因為沒印 PID 而無從查證）。
        log(f"[memguard] 常態清掃：清 {len(killed)} 個殭屍 runner（PID {killed}）"
            f"（commit {pct:.0f}%,未達緊急線,純衛生）" + _fail_tail(failed))
        state["memguard_last_ts"] = now
        write_state(state)
        return
    state, serr = read_state()
    if serr and serr != "missing":
        # 緊急線上也一樣不賭：告警節流戳記同樣讀不出來，硬做會變成每 3 分鐘
        # 又殺又吵。狀態檔的隔離重建由 main() 負責，下一輪就會回到正常路徑。
        log(f"[memguard] commit {pct:.0f}% 超緊急線，但狀態檔讀不出來（{serr}）"
            "——冷卻與告警節流皆未知，本輪不動作")
        return
    now = time.time()
    if now - float(state.get("memguard_last_ts", 0) or 0) < MEM_COOLDOWN_SEC:
        return
    runners = _probe_runners_or_log(pct)
    if runners is None:
        # 緊急線上又量不到＝最需要出聲的組合：本機已留痕，這裡再推一則誠實告警。
        # ⛔ 不可沿用下面「無殭屍可清」那句——那是「量到 0 個」的說法，會誤導人。
        _memguard_notify(state, pct,
                         f"🚨 記憶體 commit {pct:.0f}% 超緊急線，但 watchdog "
                         "<b>列不出殭屍清單（量測失敗）</b>——本輪未動作，"
                         "無法判斷有無可清，請人工檢視 watchdog.log", now)
        write_state(state)                          # 只存告警節流戳記，不寫清理冷卻
        return
    victims = sorted([v for v in runners if v[1] >= MEM_MIN_AGE_MIN * 60],
                     key=lambda v: -v[1])            # 最老的先
    if not victims:
        log(f"[memguard] commit {pct:.0f}% 超緊急線但無符合指紋的殭屍可清（App 端請人工處理）")
        _memguard_notify(state, pct,
                         f"🚨 記憶體 commit {pct:.0f}% 且 watchdog 無殭屍可清——"
                         "請重啟 Claude App 或關閉大型程式", now)
        state["memguard_last_ts"] = now
        write_state(state)
        return
    killed, failed, _early = _kill_all(victims[:12], stop_at_target=True)
    after = _commit_pct()
    if not killed:
        # 緊急線上有殭屍、卻一個都殺不掉＝最該出聲的組合之一：告警措辭必須與
        # 「清成功了」不同，且不寫清理冷卻（沒發生的動作不該換來 10 分鐘失明）。
        log(f"[memguard] commit {pct:.0f}% 超緊急線，{len(victims)} 個殭屍"
            f"**一個都沒殺成**（{'; '.join(failed)[:200]}）——本輪未清理，下輪重試")
        _memguard_notify(state, pct,
                         f"🚨 記憶體 commit {pct:.0f}% 超緊急線，watchdog 找到 "
                         f"{len(victims)} 個殭屍但<b>一個都殺不掉</b>"
                         "（權限不足或行程已消失）——本輪未清理，請人工檢視 "
                         "watchdog.log", now)
        write_state(state)                          # 只存告警節流戳記，不寫清理冷卻
        return
    log(f"[memguard] commit {pct:.0f}%→{(after or 0):.0f}%，"
        f"清理殭屍 runner {len(killed)} 個（PID {killed}）" + _fail_tail(failed))
    _memguard_notify(state, pct,
                     f"🧹 <b>watchdog 記憶體防衛</b>\ncommit {pct:.0f}% → {(after or 0):.0f}%，"
                     f"自動清理 {len(killed)} 個殭屍 AI 排程進程。\n"
                     + (f"（另有 {len(failed)} 個殺不掉）\n" if failed else "")
                     + "（交易 daemon 不受影響；若頻繁出現請每日重啟 Claude App）", now)
    state["memguard_last_ts"] = now
    write_state(state)


def log(msg: str) -> None:
    """寫本機 log（append）。永不拋例外。"""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        with WLOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line)


def read_json(p: Path) -> dict:
    """寬鬆讀（給 liveness 用）。壞檔→{}，方向上會被判成「心跳很舊」＝偏向重啟，
    對 daemon 健康而言是保守的一邊；但**不可無聲**，否則 liveness 永久壞掉時
    watchdog 會每輪重啟卻沒人知道為什麼。⛔ 狀態檔請改用 read_state()。"""
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"[read] {p.name} 存在但讀不出來（{type(exc).__name__}）"
            "——本輪視為心跳過舊（偏保守），已留痕")
        return {}
    return data if isinstance(data, dict) else {}


def read_state() -> tuple[dict, str | None]:
    """讀 watchdog 自己的狀態檔，回 (state, err)。

    v195（監督員 r89）：⛔ 這裡**不可**把兩種狀態折成同一個 {}——
      * 檔案**不存在** → err="missing"：那是合法的第一次啟動（新機器、剛清過資料
        目錄），必須保持安靜，否則變成每台新機都收到的假告警。
      * 檔案**在**、卻讀不出來（半截 JSON／權限／編碼／被寫成 list）→ 那是**故障**。
        舊碼一律回 {}，於是 main() 讀到
            last_restart = 0   ⇒ GRACE_SEC 暖機窗直接跳過
            restarts     = []  ⇒ MAX_RESTARTS_HOUR「停手交人工」的煞車失效
        兩道煞車同時解除，而且畫面上與「乾淨的第一次啟動」一模一樣。同物種第 15 次
        （未知被折成確認沒有），這次挖空的是 watchdog 自己的最後一道人工介入閘。
    """
    if not STATE.exists():
        return {}, "missing"
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception as exc:                 # 半截檔／權限／編碼：檔在但讀不出來＝故障
        return {}, type(exc).__name__
    if not isinstance(data, dict):           # 合法 JSON 但不是 dict（例如被寫成 list）
        return {}, "NotADict"
    return data, None


def write_state(state: dict) -> bool:
    """原子寫（tmp + os.replace）並回報成敗。

    ⛔ 舊碼用 STATE.write_text() 直接覆蓋＝非原子：斷電／被殺在寫到一半，留下的就是
    read_state() 讀不出來的半截檔。也就是說 watchdog **有能力親手做出**那個壞檔，
    再自己誤讀成「第一次啟動」（v157/v162-v166 同一根因）。回傳值是給呼叫端據以
    fail-closed 用的——寫不回去代表重啟次數永遠累計不起來。
    """
    tmp = STATE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, STATE)
        return True
    except Exception as exc:
        log(f"[state] 狀態檔寫入失敗（{type(exc).__name__}: {exc}）")
        try:
            tmp.unlink(missing_ok=True)      # 不留半截 tmp 給下輪誤會
        except Exception:
            pass
        return False


def recover_corrupt_state(err: str, *, now: float) -> dict | None:
    """狀態檔壞掉時：隔離壞檔 → 出聲 → 重建。回 {} 代表已可繼續；None 代表修不好。

    ⛔ 不可「安靜地重建」：那等於把「煞車曾經失效」這件事藏起來，正是本物種每次
    的真正傷害。也⛔不可憑空填一個重啟次數（那是捏造）——誠實的說法是「這小時的
    重啟史已遺失，預算從現在重新起算」。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    quarantine = DATA_DIR / f"watchdog_state.corrupt-{stamp}.json"
    try:
        os.replace(STATE, quarantine)
        kept = quarantine.name
    except Exception as exc:
        kept = f"（隔離失敗：{type(exc).__name__}）"
    log(f"[state] 狀態檔存在但讀不出來（{err}）→ 已隔離為 {kept}，重建中")
    if not write_state({}):
        log("[state] ⛔ 重建也失敗＝重啟次數無法累計＝1 小時 5 次的煞車形同永久失效，"
            "本輪起停止自動重啟，交人工")
        return None
    telegram_alert(
        "⚠️ <b>watchdog 狀態檔損毀（已自動重建）</b>\n"
        f"<code>watchdog_state.json</code> 存在但讀不出來（{err}），已隔離為 "
        f"<code>{kept}</code> 並重建。\n"
        "影響：<b>這一小時的自動重啟次數紀錄已遺失</b>，防無限重啟的煞車（1 小時 5 次）"
        "從現在重新起算——若這段期間 daemon 一直起不來，實際重啟次數會比告警看到的多。\n"
        "<i>常見成因是斷電／行程被殺在寫檔途中。若反覆出現，請人工檢視 watchdog.log。</i>"
    )
    return {}


def _read_env_value(key: str) -> str | None:
    """從專案根 .env 最小解析一個鍵（不載入整包、不 echo）。"""
    envf = ROOT / ".env"
    try:
        for raw in envf.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            k, v = raw.split("=", 1)
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    except Exception:
        pass
    return None


def telegram_alert(text: str) -> None:
    """盡力把告警推到 Telegram（取不到 token 就靜默放棄）。"""
    token = _read_env_value("TELEGRAM_BOT_TOKEN")
    chat = _read_env_value("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("[alert] 無 Telegram token/chat，略過推播（僅寫本機 log）")
        return
    try:
        data = urllib.parse.urlencode({
            "chat_id": chat, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        urllib.request.urlopen(req, timeout=15).read()
        log("[alert] 已推送 Telegram 告警")
    except Exception as e:
        log(f"[alert] 推播失敗（不影響重啟）：{e!r}")


def restart_daemon() -> bool:
    """用 start_bot.ps1 重啟（它自己冪等：先殺舊 run_bot 再起新的）。"""
    if not START_SCRIPT.exists():
        log(f"[restart] 找不到 {START_SCRIPT} → 無法重啟")
        return False
    try:
        subprocess.run(
            ["powershell", "-ExecutionPolicy", "Bypass", "-NoProfile",
             "-File", str(START_SCRIPT)],
            cwd=str(ROOT), timeout=120,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        log("[restart] 已執行 start_bot.ps1")
        return True
    except Exception as e:
        log(f"[restart] 執行 start_bot.ps1 失敗：{e!r}")
        return False


def daemon_process_alive():
    """是否有正在跑 run_bot.py 的 python 行程。
    回傳 True／False；查詢失敗回 None（＝不確定，退回只用心跳判斷，避免誤殺）。
    純靠 powershell 查 CommandLine（watchdog 重啟本來就用 powershell，依賴一致）。

    ⛔ 回 None 的方向是安全的（不確定就不重啟），但**不可無聲**：這支是 2026-06-18
    事故後補的第二訊號，用來把 30 分鐘心跳盲點縮到 3 分鐘。它若永久壞掉而沒人看見，
    watchdog 會靜靜退化成只看心跳＝盲點復活。故查不到一律在本機 log 留痕
    （不發告警，避免每 3 分鐘吵一次）；正常兩條路徑則保持安靜。
    """
    ps = ("$p = Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
          "-ErrorAction SilentlyContinue | "
          "Where-Object { $_.CommandLine -like '*run_bot*' }; "
          "if ($p) { 'ALIVE' } else { 'DEAD' }")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, timeout=30,
        )
    except Exception as exc:                      # 逾時／powershell 不存在／被擋
        log(f"[detect] daemon 行程探測失敗（{type(exc).__name__}: {exc}）"
            "——⛔ 未知不折成『已死』，本輪退回只看心跳")
        return None
    s = _decode_console(out.stdout).strip()       # ⛔ 不用 text=True：解碼不跟隨 locale
    if "ALIVE" in s:
        return True
    if "DEAD" in s:
        return False
    err = _decode_console(out.stderr).strip().replace("\n", " ")[:160]
    log(f"[detect] daemon 行程探測結果無法判讀（exit={out.returncode} "
        f"stdout={s[:60]!r} stderr={err}）——⛔ 未知不折成『已死』，本輪退回只看心跳")
    return None


def main() -> int:
    now = time.time()

    # 暫停開關：使用者刻意關 bot 時，建這個檔就不會被拉起來。
    #
    # ⚠️ v237 收斂射程（2026-08-03 線上實證）：舊碼在這裡直接 return 0，於是這個
    #    寫著「不要自動重啟」的開關，順手把**記憶體防衛**也一起關掉了——而且
    #    「被關掉」只寫進一行沒人會讀的本機 log。當天旗標 12:09 出現後 memguard
    #    歸零，2.5 小時內 commit 從 67% 爬到 71.4%、11 個 runner 堆積（最老 34.8h）、
    #    可用實體記憶體只剩 1.16GB，而系統對外表現得完全正常。
    #    memguard 清的是 Claude 排程殭屍，跟 daemon 要不要重啟無關 ⇒ 不該被連坐。
    #    ⛔ 但暫停「重啟」這件事本身不得弱化，也⛔不得自己把旗標刪掉。
    paused = DISABLED_FLAG.exists()
    if paused:
        log("[skip] 偵測到 watchdog.disabled → 暫停自動重啟"
            "（這是刻意的；記憶體防衛不受影響，仍照常執行）")

    # v195：狀態檔健檢擺在最前面——memory_guard 與下面的重啟煞車都吃這個檔，
    # 壞檔要在任何人讀它之前就處理掉（隔離+重建+出聲），而不是各自折成 {}。
    _state0, _serr = read_state()
    if _serr and _serr != "missing":
        if recover_corrupt_state(_serr, now=now) is None:
            # ⛔ 修不好＝重啟預算永遠是「零次」＝可無限重啟且無人得知。
            #    寧可這輪不自癒（人工還救得回來），也不要開一場無聲的重啟風暴。
            return 3

    # 記憶體防衛（獨立於 daemon 健康檢查；緊急線才動手，內建冷卻與留痕）
    try:
        memory_guard()
    except Exception as e:  # noqa: BLE001 — 防衛失敗不可拖垮重啟主功能
        log(f"[memguard] 例外（不影響主功能）：{type(e).__name__}: {e}")

    # 暫停中：記憶體防衛已經跑過了，重啟這一段就此打住（但要讓人知道它關著）。
    if paused:
        _pstate, _perr = read_state()
        _paused_notify({} if (_perr and _perr != "missing") else _pstate, now)
        return 0

    # 健康訊號：liveness 新鮮度
    live = read_json(LIVENESS)
    last_ts = float(live.get("ts", 0) or 0)
    age = now - last_ts if last_ts else 1e9   # 無戳記＝視為很舊

    state, serr = read_state()
    if serr and serr != "missing":
        # 上面才剛重建過還是壞的（例如另一個行程正在寫）：同樣不賭煞車。
        log(f"[state] 重建後仍讀不出來（{serr}）——本輪不自動重啟")
        return 3
    last_restart = float(state.get("last_restart_ts", 0) or 0)
    restarts = [t for t in state.get("restart_times", []) if now - float(t) < 3600]

    # 第二訊號：行程是否還在（補心跳盲點）。
    # 真實事故 2026-06-18：Claude App 更新重啟把 daemon（它的子行程）一起殺掉，
    # 但最後一筆心跳還很新（<30 分），watchdog 只看心跳 → 要乾等 30 分才動手。
    # 加這個：行程「確定不存在」就立刻重啟，不必等心跳放到 STALE_SEC。
    proc_alive = daemon_process_alive()   # True / False / None(查詢失敗→不確定)
    stale = age >= STALE_SEC
    dead = proc_alive is False

    # 健康（心跳新 且 行程在/查不到）→ 安靜退出
    if not stale and not dead:
        return 0

    # 剛重啟過 → 暖機/冷卻，先別動（daemon 還沒寫第一筆心跳）
    if now - last_restart < GRACE_SEC:
        log(f"[wait] 心跳已 {int(age)}s 未更新，但 {int(now - last_restart)}s 前才重啟過，"
            f"暖機中（grace={GRACE_SEC}s），暫不動作")
        return 0

    # 防無限重啟：一小時內已重啟太多次仍失敗 → 停手交人工
    if len(restarts) >= MAX_RESTARTS_HOUR:
        log(f"[giveup] 過去 1 小時已自動重啟 {len(restarts)} 次仍未恢復 → 停止自動重啟，需人工介入")
        telegram_alert(
            "🚨 <b>watchdog 放棄自動重啟</b>\n"
            f"過去 1 小時已嘗試自動重啟 <b>{len(restarts)}</b> 次，daemon 仍無法維持心跳。\n"
            "這通常代表「一啟動就崩」（程式錯誤 / 缺套件 / 設定問題），狂重啟無益。\n"
            "需要你人工看一下 <code>bot.err.log</code> / <code>watchdog.log</code>。"
        )
        return 1

    # === 判定為斷線/停滯 → 自動重啟 ===
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(last_ts)) if last_ts else "未知"
    reason = "行程已消失" if dead else f"心跳停滯約 {age/60:.0f} 分"
    log(f"[detect] {reason}（心跳上次 {when}，已 {int(age)}s）→ 判定離線，執行自動重啟")
    ok = restart_daemon()

    state["last_restart_ts"] = now
    restarts.append(now)
    state["restart_times"] = restarts
    write_state(state)

    if ok:
        telegram_alert(
            "🤖 <b>watchdog 自動恢復</b>\n"
            f"偵測到交易機器人離線（{reason}，心跳上次 <code>{when}</code> 台北），"
            "已<b>自動重啟</b>。\n"
            "<i>正在暖機，數分鐘內應恢復掃描。若反覆出現此訊息，代表啟動有問題，會再單獨告警。</i>"
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:   # watchdog 本身絕不能因例外而靜默死掉
        log(f"[fatal] watchdog 主流程例外：{e!r}")
        sys.exit(2)
