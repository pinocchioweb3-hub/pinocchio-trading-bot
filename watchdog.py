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
    - 暫停開關：data_dir 下若存在 `watchdog.disabled` 檔，watchdog 直接退出、不重啟
      （讓你能「刻意關掉 bot」而不被 watchdog 一直拉起來）。

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
          "@{N='Age';E={[int]((Get-Date)-$_.CreationDate).TotalSeconds}} | "
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
                out.append((int(p["ProcessId"]), float(p.get("Age") or 0)))
            except Exception as exc:              # 單筆壞掉≠整批未知，但也不許靜音
                raise _RunnerProbeError(f"欄位解析失敗: {exc}") from exc
    return out


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
    for pid, _age in victims:
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
        stale = [v for v in runners if v[1] >= MEM_MIN_AGE_MIN * 60]
        if len(stale) < 4:
            return
        state = read_json(STATE)
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
    state = read_json(STATE)
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
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def write_state(state: dict) -> None:
    try:
        STATE.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass


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
    純靠 powershell 查 CommandLine（watchdog 重啟本來就用 powershell，依賴一致）。"""
    ps = ("$p = Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
          "-ErrorAction SilentlyContinue | "
          "Where-Object { $_.CommandLine -like '*run_bot*' }; "
          "if ($p) { 'ALIVE' } else { 'DEAD' }")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, timeout=30,
        )
        s = (out.stdout or "").strip()
        if "ALIVE" in s:
            return True
        if "DEAD" in s:
            return False
    except Exception:
        pass
    return None


def main() -> int:
    now = time.time()

    # 暫停開關：使用者刻意關 bot 時，建這個檔就不會被拉起來
    if DISABLED_FLAG.exists():
        log("[skip] 偵測到 watchdog.disabled → 暫停自動重啟（這是刻意的）")
        return 0

    # 記憶體防衛（獨立於 daemon 健康檢查；緊急線才動手，內建冷卻與留痕）
    try:
        memory_guard()
    except Exception as e:  # noqa: BLE001 — 防衛失敗不可拖垮重啟主功能
        log(f"[memguard] 例外（不影響主功能）：{type(e).__name__}: {e}")

    # 健康訊號：liveness 新鮮度
    live = read_json(LIVENESS)
    last_ts = float(live.get("ts", 0) or 0)
    age = now - last_ts if last_ts else 1e9   # 無戳記＝視為很舊

    state = read_json(STATE)
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
