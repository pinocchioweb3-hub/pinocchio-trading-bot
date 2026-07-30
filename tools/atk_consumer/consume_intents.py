# -*- coding: utf-8 -*-
"""consume_intents.py — trade-intent → OKX Agent Trade Kit(CLI) 消費腳本【使用者側範本】。

角色：這支腳本是「使用者自有執行器」——由使用者審閱後自行排程執行。
    讀 intent_outbox 的 JSON 訊號 → 冪等去重 → 張數換算 → 呼叫 `okx` CLI 下單。
    金鑰只存在使用者的 ~/.okx/config.toml（Agent Kit 本地簽名），本腳本不碰金鑰值。

⛔ 安全鐵則（程式層硬寫死，不可用參數繞過）：
    1. PROFILE 常數 = "demo"，且會先跑 `okx config show` 驗證該 profile 是 demo=true，
       驗不到就拒絕執行任何下單——不存在 live 模式的程式路徑。
       （要上真盤＝使用者親手複製此腳本、自行修改、自行承擔——原檔永遠是 demo。）
    2. 只執行 execution_policy == "demo_only" 的 intent；human_gated 只列印。
    3. 冪等雙鎖：本地 state 檔記已處理 intent_id ＋ OKX clOrdId 去重（同 ID 重送會被拒）。
    4. 過期即棄：now > expires_at 的 intent 直接標 skip（防斷線後補執行過時價位）。
    5. 單筆風險上限 RISK_USD_CAP、名義值上限 NOTIONAL_CAP_USD 雙夾層。

用法（使用者手動首跑，確認無誤後再排程）：
    python consume_intents.py --once      # 消費一輪後退出
    python consume_intents.py --dry-run   # 只列印將執行的指令，不真的下單
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Windows 排程器/cp950 環境下 emoji 輸出防呆（本機環境陷阱：cp950 吃不下 UTF-8 符號）
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

# ── 使用者可調（demo 安全值） ─────────────────────────────────────────
PROFILE = "demo"                 # ⛔硬寫死。原檔永遠 demo；真盤=使用者自建副本自改自擔。
RISK_USD = 100.0                 # 每筆風險預算（1R）
RISK_USD_CAP = 150.0             # 風險絕對上限
NOTIONAL_CAP_USD = 3000.0        # 單筆名義值上限（防張數換算出錯爆倉）
LEVERAGE = 5                     # 保守槓桿（美股代幣永續上限 25x，取遠低於上限）
TIMEOUT_HOURS = 24.0             # 持倉逾時強制平倉（對齊紙上 us_breakout 24h 口徑）
DAILY_STOP_USD = 300.0           # 日虧熔斷（≈3R）：當日已實現虧損達此值→今日不再接新單
OUTBOX = Path(os.path.expandvars(r"%LOCALAPPDATA%\TradingBot\intent_outbox"))
STATE = Path(os.path.expandvars(r"%LOCALAPPDATA%\TradingBot\atk_consumer_state.json"))
POS_STATE = Path(os.path.expandvars(r"%LOCALAPPDATA%\TradingBot\atk_positions.json"))
HEALTH = Path(os.path.expandvars(r"%LOCALAPPDATA%\TradingBot\atk_consumer_health.json"))

# ── 連續 fail-closed 告警（v143；2026-07-30 教訓） ─────────────────────
# fail-closed 本身是對的（認證失敗就不下單），錯的是「沒有出口」：
# 那晚 OKX 因浮動 IP 換掉回 401 共 121 次、整盤零成交，卻只寫在 log 裡沒人知道，
# 靠肉眼撞見才發現——與 halt 殘閂 19 天、週報斷檔 16.4 天同一物種（無聲失敗）。
FAIL_ALERT_AFTER = 3            # 連續幾輪有故障才告警（單輪抖動不吵）
FAIL_ALERT_REPEAT_SEC = 3600.0  # 同類故障持續時的重複提醒間隔
ENV_FILE = Path(r"C:\Users\user\OneDrive\桌面\交易機器人\.env")  # 只讀 TG 憑證，永不列印值

# 良性回應：查無此單是「冪等查詢」的正常答案，不是故障
_BENIGN_MARKERS = ("51603", "doesn't exist", "does not exist")
# 故障嚴重度排序（同輪多類時取最前者當代表）
_CLASS_PRIORITY = ("cli_missing", "auth_ip_whitelist", "auth", "rate_limit",
                   "timeout", "query_fail", "other")
_ROUND_FAILS: dict[str, str] = {}   # 本輪故障 {類別: 樣本}；每輪開頭清空
# v151：本輪「成功呼叫數」；每輪開頭歸零。沒有它就無法分辨「全部呼叫都成功」
# 與「這輪根本沒呼叫」——後者被當成乾淨輪會蓋出假痊癒（見 update_health）。
_ROUND_OKS: dict[str, int] = {"ok": 0}


# Windows 陷阱：npm 全域裝的 okx 是 okx.cmd shim，subprocess 不走 shell 找不到裸名
# → 用 shutil.which 解析完整路徑（會依 PATHEXT 找到 .cmd）
import shutil
_OKX_BIN = shutil.which("okx")


def redact_secrets(text: str) -> str:
    """遮蔽 API key 識別碼（UUID）。
    ⚠️ IP 不遮——那正是使用者要拿去補白名單的唯一有用資訊；key id 對他無診斷價值。"""
    return re.sub(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                  r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                  "<key-id-redacted>", text or "")


def classify_failure(code: int, out: str) -> str | None:
    """把 okx CLI 失敗分流成「可行動的類別」（純函式）。回 None＝良性非故障。

    分流的意義：401 白名單要人去後台補、cli_missing 要重裝、rate_limit 會自癒、
    query_fail 是下游症狀——混在一起就只剩「有錯」這種沒人會動作的資訊。"""
    t = out or ""
    low = t.lower()
    if any(s in t for s in _BENIGN_MARKERS):
        return None                      # 查無此單＝冪等查詢的正常答案
    if code == 127 or "未安裝" in t:
        return "cli_missing"
    if "401" in t and "not included in" in low:
        return "auth_ip_whitelist"       # 浮動 IP 換掉→白名單失效（會復發）
    if ("401" in t or "invalid sign" in low or "50111" in t or "50113" in t
            or "50102" in t):
        return "auth"
    if code == 124 or "timeout" in low:
        return "timeout"
    if "50011" in t or "429" in t or "too many requests" in low:
        return "rate_limit"
    return "other"


def _note_fail(cls: str | None, sample: str) -> None:
    """記一筆本輪故障（同類只留第一個樣本＝class_counts 以「輪」為單位不重複計）。"""
    if cls and cls not in _ROUND_FAILS:
        _ROUND_FAILS[cls] = redact_secrets(sample)[:300]


def _okx(args: list[str], timeout: int = 30) -> tuple[int, str]:
    """呼叫 okx CLI（--json 輸出）。回 (exit_code, stdout)。

    失敗一律登記到 _ROUND_FAILS、成功一律計入 _ROUND_OKS（單一掛鉤攔到槓桿/查單/
    下單/對帳全部路徑）——只加副作用，回傳值與交易邏輯完全不變。"""
    if not _OKX_BIN:
        out = "okx CLI 未安裝（npm install -g @okx_ai/okx-trade-cli）"
        _note_fail("cli_missing", out)
        return 127, out
    cmd = [_OKX_BIN, "--profile", PROFILE, *args, "--json"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        out = (r.stdout or r.stderr or "")
        if r.returncode != 0:
            cls = classify_failure(r.returncode, out)
            _note_fail(cls, out)
            # 良性回應（查無此單）代表呼叫確實通到 OKX 並拿到可理解的答覆＝算通
            if cls is None:
                _ROUND_OKS["ok"] += 1
        else:
            _ROUND_OKS["ok"] += 1        # v151：成功也要記，否則無從證明「真的通了」
        return r.returncode, out
    except subprocess.TimeoutExpired:
        _note_fail("timeout", "okx CLI timeout")
        return 124, "okx CLI timeout"


def verify_demo_profile() -> bool:
    """開單前硬驗證：直接讀 ~/.okx/config.toml，PROFILE 段必須 demo=true，
    否則拒絕一切下單（不信任 CLI 輸出格式，讀設定檔本身最可靠）。"""
    try:
        import tomllib
        cfg_path = Path.home() / ".okx" / "config.toml"
        cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
        prof = (cfg.get("profiles") or {}).get(PROFILE) or {}
        if prof.get("demo") is True:
            return True
        print(f"⛔ profile '{PROFILE}' 不是模擬盤（demo≠true）——本腳本永不對非 demo 帳戶下單")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"⛔ 無法驗證 profile（{type(e).__name__}: {e}）——拒絕執行")
        return False


# ── 健康狀態／告警（v143） ────────────────────────────────────────────
def _load_health() -> dict:
    try:
        return json.loads(HEALTH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_health(h: dict) -> None:
    try:
        HEALTH.write_text(json.dumps(h, ensure_ascii=False, indent=1),
                          encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def worst_class(classes) -> str | None:
    """同輪多類故障時取代表（純函式）：依 _CLASS_PRIORITY 取最嚴重者。"""
    for c in _CLASS_PRIORITY:
        if c in classes:
            return c
    return next(iter(sorted(classes)), None)


def update_health(h: dict, fails: dict, now_s: float, oks: int = 0) -> dict:
    """把本輪結果併進健康狀態（純函式，不做 I/O）。

    connsecutive_fail_rounds 只在「有故障的輪」累加；乾淨輪歸零並在曾告警過時
    留下 recovered_from＝讓恢復也有出口（無聲恢復同樣會讓人誤判）。

    v151【假痊癒治本】「沒故障」不等於「通了」——本輪一次呼叫都沒發生（intent 全
    過期／無倉可管）時 fails 也是空的。舊版把這種**空轉輪**當乾淨輪：連續故障歸零、
    送出「✅已恢復」，但故障其實一步都沒好。2026-07-31 就是這樣：401 白名單斷流
    291 輪 → 兩個 intent 剛好過期 → 空轉一輪 → 假恢復通知 → 下一輪立刻又 401。
    因此改成三態：有故障→累加；無故障但 oks==0→**空轉，維持原狀**（不歸零、不報
    恢復）；無故障且 oks>0→真的通了才算乾淨輪。oks 預設 0＝呼叫端沒證明就不算通
    （fail-closed 方向：寧可晚報恢復，不可假報恢復）。"""
    h = dict(h)
    h["rounds_seen"] = int(h.get("rounds_seen", 0)) + 1
    h["updated_at"] = now_s
    h["updated_at_local"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(now_s))
    h["profile"] = PROFILE
    h.pop("recovered_from", None)
    if not fails and int(oks or 0) <= 0:
        # 空轉輪：沒故障也沒成功呼叫＝本輪對「是否已恢復」零資訊，維持原判
        h["idle_rounds"] = int(h.get("idle_rounds", 0)) + 1
        h["last_idle_ts"] = now_s
        return h
    if not fails:
        streak = int(h.get("consecutive_fail_rounds", 0))
        if streak >= FAIL_ALERT_AFTER and h.get("last_alert_ts"):
            h["recovered_from"] = {"class": h.get("last_fail_class"),
                                   "fail_rounds": streak}
            h.pop("last_alert_ts", None)
            h.pop("last_alert_class", None)
        h["consecutive_fail_rounds"] = 0
        h["last_ok_ts"] = now_s
        h.pop("first_fail_ts", None)
        return h
    cls = worst_class(fails.keys())
    h["consecutive_fail_rounds"] = int(h.get("consecutive_fail_rounds", 0)) + 1
    h.setdefault("first_fail_ts", now_s)
    h["last_fail_ts"] = now_s
    h["last_fail_class"] = cls
    h["last_fail_sample"] = fails.get(cls, "")
    counts = dict(h.get("class_counts") or {})
    for c in fails:
        counts[c] = int(counts.get(c, 0)) + 1
    h["class_counts"] = counts
    return h


def should_alert(h: dict, now_s: float, threshold: int = FAIL_ALERT_AFTER,
                 repeat_sec: float = FAIL_ALERT_REPEAT_SEC) -> bool:
    """要不要現在告警（純函式）：連續故障達門檻，且（未告警過／換了故障類別／
    距上次提醒超過 repeat_sec）。故障類別變了立刻再報＝新故障不被舊冷卻蓋掉。"""
    if int(h.get("consecutive_fail_rounds", 0)) < threshold:
        return False
    last_ts = h.get("last_alert_ts")
    if not last_ts:
        return True
    if h.get("last_alert_class") != h.get("last_fail_class"):
        return True
    return (now_s - float(last_ts)) >= repeat_sec


_CLASS_HINT = {
    "auth_ip_whitelist": "呼叫端 IP 不在 API key 白名單（住宅浮動 IP 換掉會復發）"
                         "→ 到 OKX 後台把下方錯誤訊息中的 IP 加進白名單，"
                         "消費器每分鐘自動重試、不需重啟",
    "auth": "認證失敗（金鑰／簽章／權限）→ 檢查 ~/.okx/config.toml 與後台權限設定",
    "cli_missing": "okx CLI 不存在 → npm install -g @okx_ai/okx-trade-cli",
    "rate_limit": "被限流 → 通常自癒；持續出現才需降頻",
    "timeout": "呼叫逾時 → 檢查網路；持續出現代表對外連線有問題",
    "query_fail": "查單失敗導致 fail-closed（下游症狀，先看同時段的認證／網路類別）",
    "other": "未分類錯誤 → 讀 atk_live.log 原文",
}


def alert_text(h: dict, now_s: float) -> str:
    """組告警文字（純函式，繁中可行動）。⛔只講執行器連線狀態，不含任何績效宣稱。"""
    cls = h.get("last_fail_class") or "other"
    rounds = int(h.get("consecutive_fail_rounds", 0))
    since = h.get("first_fail_ts") or now_s
    mins = max(0, int((now_s - float(since)) / 60))
    return (
        f"🚨 真錢執行器連線異常（profile={h.get('profile')}）\n"
        f"已連續 {rounds} 輪 fail-closed，持續約 {mins} 分鐘。\n"
        f"故障類別：{cls}\n"
        f"處置：{_CLASS_HINT.get(cls, _CLASS_HINT['other'])}\n"
        f"錯誤原文：{(h.get('last_fail_sample') or '')[:200]}\n"
        f"⚠️ 期間未下任何單（fail-closed 正確），也未平既有倉——"
        f"這是「沒下單」不是「虧損」。"
    )


def recovery_text(h: dict) -> str:
    rec = h.get("recovered_from") or {}
    return (f"✅ 真錢執行器已恢復（profile={h.get('profile')}）"
            f"——先前 {rec.get('fail_rounds')} 輪 {rec.get('class')} 故障已消失，"
            f"本輪呼叫全部成功。")


def _tg_creds() -> tuple[str | None, str | None]:
    """取 TG 憑證：先環境變數，再讀 .env（排程器環境沒有這兩個變數）。
    ⛔只回傳給送信函式使用，永不列印值。"""
    tk, cid = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tk and cid:
        return tk, cid
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k == "TELEGRAM_BOT_TOKEN" and not tk:
                tk = v
            elif k == "TELEGRAM_CHAT_ID" and not cid:
                cid = v
    except Exception:  # noqa: BLE001
        pass
    return (tk or None), (cid or None)


def send_alert(text: str, dry: bool = False) -> tuple[str, str | None]:
    """送告警到 Telegram（stdlib urllib，維持本腳本零依賴）。
    回 (channel, error)：channel ∈ {telegram, dry, none}。
    ⚠️ 告警管道自己失敗也要留痕——否則就變成「告警的無聲失敗」同一個坑。"""
    if dry:
        return "dry", None
    tk, cid = _tg_creds()
    if not tk or not cid:
        return "none", "TG 憑證缺失（環境變數與 .env 都沒有）"
    try:
        import urllib.request
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{tk}/sendMessage",
            data=json.dumps({"chat_id": cid, "text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read().decode("utf-8", "replace")
        return ("telegram", None) if '"ok":true' in body.replace(" ", "") \
            else ("none", redact_secrets(body)[:200])
    except Exception as e:  # noqa: BLE001
        return "none", f"{type(e).__name__}: {str(e)[:160]}"


def finish_round(fails: dict, now_s: float | None = None,
                 dry: bool = False, oks: int = 0) -> dict:
    """每輪收尾：更新健康檔、必要時告警。永不對外拋例外——
    告警層絕不可以把交易執行器弄掛（它的職責只是「讓失敗有出口」）。

    oks＝本輪成功呼叫數；沒有它就分不出「空轉輪」與「乾淨輪」（見 update_health）。"""
    now_s = now_s or time.time()
    try:
        h = update_health(_load_health(), fails, now_s, oks)
        if h.get("recovered_from"):
            ch, err = send_alert(recovery_text(h), dry=dry)
            h["last_alert_channel"], h["last_alert_error"] = ch, err
            print(f"✅ 執行器已恢復（告警管道={ch}）")
        elif should_alert(h, now_s):
            ch, err = send_alert(alert_text(h, now_s), dry=dry)
            h["last_alert_ts"] = now_s
            h["last_alert_class"] = h.get("last_fail_class")
            h["last_alert_channel"], h["last_alert_error"] = ch, err
            print(f"🚨 連續 {h['consecutive_fail_rounds']} 輪 fail-closed"
                  f"（{h.get('last_fail_class')}）——已告警，管道={ch}"
                  + (f"，管道錯誤={err}" if err else ""))
        elif not fails and int(oks or 0) <= 0 \
                and int(h.get("consecutive_fail_rounds", 0)) > 0:
            # v151：斷流中的空轉輪要留痕，否則日誌看起來像「不吵了＝好了」
            print(f"⏸ 本輪零呼叫（空轉，無 intent 可執行／無倉可管）——"
                  f"不當作恢復，維持連續故障第 "
                  f"{h.get('consecutive_fail_rounds')} 輪判定")
        elif fails:
            streak = int(h.get("consecutive_fail_rounds", 0))
            tail = (f"（已告警過，冷卻中：同類故障每 {FAIL_ALERT_REPEAT_SEC / 60:.0f} "
                    f"分鐘才再提醒一次）" if h.get("last_alert_ts")
                    else f"（達 {FAIL_ALERT_AFTER} 輪才告警）")
            print(f"⚠️ 本輪故障 {sorted(fails)}；連續第 {streak} 輪{tail}")
        _save_health(h)
        return h
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ 健康狀態更新失敗（不影響交易路徑）：{type(e).__name__}: {e}")
        return {}


def contracts_for(inst_id: str, entry: float, stop: float, ct_val_cache: dict) -> float | None:
    """風險預算→張數。sz=風險USD÷|entry−stop|÷ctVal，向下取整到 lotSz。錯→None 不下單。"""
    spec = ct_val_cache.get(inst_id)
    if spec is None:
        code, out = _okx(["market", "instruments", "--instType", "SWAP",
                          "--instId", inst_id])
        if code != 0:
            return None
        try:
            raw = json.loads(out)
            # CLI --json 頂層可能是 list（實測 v1.4.2）或 {"data":[...]}，兩者都容
            items = raw.get("data") if isinstance(raw, dict) else raw
            item = items[0]
            spec = {"ctVal": float(item["ctVal"]), "lotSz": float(item["lotSz"]),
                    "minSz": float(item["minSz"])}
            ct_val_cache[inst_id] = spec
        except Exception:  # noqa: BLE001
            return None
    risk = min(RISK_USD, RISK_USD_CAP)
    dist = abs(entry - stop)
    if dist <= 0 or spec["ctVal"] <= 0:
        return None
    units = risk / dist                      # 標的單位數
    sz = units / spec["ctVal"]               # 合約張數
    lot = spec["lotSz"]
    sz = int(sz / lot) * lot                 # 向下取整到 lotSz
    if sz < spec["minSz"]:
        return None
    if sz * spec["ctVal"] * entry > NOTIONAL_CAP_USD:   # 名義值夾層
        sz = int(NOTIONAL_CAP_USD / (spec["ctVal"] * entry) / lot) * lot
        if sz < spec["minSz"]:
            return None
    return round(sz, 8)


_LEV_SET: set = set()


def leverage_for_trade(entry: float, stop: float, max_lev: int | None = None) -> int:
    """清算永不先於止損（v84 哲學）：止損距離 × 槓桿 ≤ 70%清算距離。
    lev = min(上限, 70/止損%)，下限 3。槓桿只影響保證金效率，風險由止損距離決定。"""
    max_lev = max_lev or LEVERAGE
    if not entry or not stop or entry <= 0:
        return min(max_lev, 5)
    stop_pct = abs(entry - stop) / entry * 100.0
    if stop_pct <= 0:
        return min(max_lev, 5)
    return max(3, min(int(max_lev), int(70.0 / stop_pct)))


def ensure_leverage(inst_id: str, pos_side: str, dry: bool,
                    lev: int | None = None) -> None:
    """開單前設槓桿（v99 教訓：OKX 預設 3x，hedge 模式 isolated 必須帶 posSide 逐邊設，
    否則靜默沿用預設）。失敗只警告不擋單——槓桿影響資金效率，風險由止損距離決定。"""
    lev = lev or LEVERAGE
    key = (inst_id, pos_side, lev)
    if dry or key in _LEV_SET:
        return
    code, out = _okx(["swap", "leverage", "--instId", inst_id,
                      "--lever", str(lev), "--mgnMode", "isolated",
                      "--posSide", pos_side])
    if code == 0:
        _LEV_SET.add(key)
    else:
        print(f"⚠️ 設槓桿失敗 {inst_id}/{pos_side}（沿用現值繼續）：{out[:120]}")


# ── 倉位管理（v139：對帳／逾時平倉／日虧熔斷） ──────────────────────────
def _load_positions() -> dict:
    try:
        return json.loads(POS_STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"open": {}, "day_pnl": {}}


def _save_positions(ps: dict) -> None:
    try:
        POS_STATE.write_text(json.dumps(ps, ensure_ascii=False, indent=1),
                             encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _day_key(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts or time.time()))


def timed_out(placed_at_s: float, now_s: float,
              limit_h: float = TIMEOUT_HOURS) -> bool:
    """持倉逾時判定（純函式）。"""
    return (now_s - placed_at_s) > limit_h * 3600


WEEKLY_STOP_USD = 750.0          # 週虧熔斷（≈7.5R）：近 7 日合計虧損達此值→停接新單


def breaker_tripped(day_pnl: dict, now_s: float | None = None,
                    stop_usd: float = DAILY_STOP_USD,
                    week_stop_usd: float = WEEKLY_STOP_USD) -> bool:
    """日/週雙層熔斷（純函式）：今日(UTC)已實現虧損 ≤ −stop_usd，
    或近 7 日(UTC)合計 ≤ −week_stop_usd → True。"""
    now_s = now_s or time.time()
    if float(day_pnl.get(_day_key(now_s), 0.0)) <= -abs(stop_usd):
        return True
    week_keys = {_day_key(now_s - d * 86400) for d in range(7)}
    week_total = sum(float(v) for k, v in day_pnl.items() if k in week_keys)
    return week_total <= -abs(week_stop_usd)


def _realized_pnl_since(inst_id: str, since_s: float) -> float | None:
    """粗算該 instId 自 since 起的已實現損益（fillPnl+fee 合計）。查失敗回 None。"""
    code, out = _okx(["swap", "fills", "--instId", inst_id])
    if code != 0 or not out.strip().startswith(("[", "{")):
        return None
    try:
        fills = json.loads(out)
        fills = fills if isinstance(fills, list) else fills.get("data", [])
        total = 0.0
        for f in fills:
            ts = float(f.get("ts") or 0) / 1000.0
            if ts >= since_s:
                total += float(f.get("fillPnl") or 0) + float(f.get("fee") or 0)
        return total
    except Exception:  # noqa: BLE001
        return None


def manage_positions(dry: bool) -> None:
    """每輪管理：①對帳（OKX 上已消失＝TP/SL 已了結→記日損益）②逾時強平。
    任何查詢失敗→本輪跳過該倉不猜（下輪重試）。"""
    ps = _load_positions()
    if not ps.get("open"):
        return
    code, out = _okx(["account", "positions"])
    if code != 0 or not out.strip().startswith(("[", "{")):
        return                                   # 查不到就不動，別誤判平倉
    plist = json.loads(out)
    plist = plist if isinstance(plist, list) else plist.get("data", [])
    live = {(p.get("instId"), p.get("posSide")): float(p.get("pos") or 0)
            for p in plist}
    now_s = time.time()
    for iid, rec in list(ps["open"].items()):
        key = (rec["inst_id"], rec["pos_side"])
        if live.get(key, 0.0) == 0.0:
            # 已了結（TP/SL/手動）→ 記日損益後移出
            pnl = _realized_pnl_since(rec["inst_id"], rec["placed_at"])
            if pnl is not None:
                dk = _day_key()
                ps["day_pnl"][dk] = float(ps["day_pnl"].get(dk, 0.0)) + pnl
                print(f"🏁 {rec['inst_id']} {rec['pos_side']} 已了結，"
                      f"已實現≈{pnl:+.2f} USDT（今日累計 {ps['day_pnl'][dk]:+.2f}）")
            else:
                print(f"🏁 {rec['inst_id']} {rec['pos_side']} 已了結（損益查詢失敗，"
                      "不計入熔斷口徑）")
            ps["open"].pop(iid, None)
        elif timed_out(rec["placed_at"], now_s):
            if dry:
                print(f"DRY-RUN: 逾時平倉 {rec['inst_id']} {rec['pos_side']}")
                continue
            code, out = _okx(["swap", "close", "--instId", rec["inst_id"],
                              "--mgnMode", "isolated",
                              "--posSide", rec["pos_side"], "--autoCxl"])
            print(("⏱ 逾時平倉已送出 " if code == 0 else "❌ 逾時平倉失敗 ")
                  + f"{rec['inst_id']}（持有 {(now_s - rec['placed_at']) / 3600:.1f}h）"
                  + ("" if code == 0 else f"：{out[:120]}"))
            # 不立即移出：下輪對帳確認消失後才記損益
    # 舊日損益只留 14 天
    ps["day_pnl"] = {k: v for k, v in ps["day_pnl"].items()
                     if k >= _day_key(time.time() - 14 * 86400)}
    _save_positions(ps)


TP_WEIGHTS3 = (0.40, 0.30, 0.30)   # 對齊 demo 帳 TP1/2/3 分腿口徑（尾腿吃餘數）


def split_tp_levels(sz: float, lot: float, min_sz: float,
                    tps: list[float]) -> list[tuple[float, float]]:
    """分批止盈分腿（純函式）。tps=有價位的 TP 清單（1~3 個）。
    回 [(觸發價, 腿張數)...]：權重 3腿=40/30/30、2腿=50/50、1腿=100%；
    各腿 floor 到 lot、尾腿吃餘數；不足 minSz 的腿併入尾腿。"""
    tps = [p for p in tps if p]
    if not tps or sz <= 0:
        return []
    weights = {1: (1.0,), 2: (0.5, 0.5), 3: TP_WEIGHTS3}[min(len(tps), 3)]
    legs: list[tuple[float, float]] = []
    used = 0.0
    for i, (px, w) in enumerate(zip(tps, weights)):
        if i == len(weights) - 1:
            leg = round(sz - used, 8)                    # 尾腿吃餘數
        else:
            leg = int(sz * w / lot) * lot
        if leg < min_sz:
            continue                                      # 太小的腿讓尾腿吸收
        legs.append((px, round(leg, 8)))
        used = round(used + leg, 8)
    if not legs:                                          # 全部太小→單腿 100%
        return [(tps[0], sz)]
    # 若中間腿被跳過導致餘數沒分完，把差額補到最後一腿
    diff = round(sz - sum(l for _, l in legs), 8)
    if diff > 0:
        px, l = legs[-1]
        legs[-1] = (px, round(l + diff, 8))
    return legs


def _leg_args(intent: dict, leg_sz: float, tp_px: float, cl_ord_id: str) -> list[str]:
    """單腿下單參數：市價進場＋附掛該腿 TP＋整段 SL（OCO，已驗證原語）。"""
    return ["swap", "place",
            "--instId", intent["inst_id"],
            "--side", intent["side"],
            "--posSide", intent["pos_side"],
            "--ordType", "market",
            "--sz", str(leg_sz),
            "--tdMode", "isolated",
            "--clOrdId", cl_ord_id,
            "--tpTriggerPx", str(tp_px), "--tpOrdPx=-1",
            "--slTriggerPx", str(intent["stop"]), "--slOrdPx=-1"]


def _leg_ok(code: int, out: str) -> bool:
    """腿級下單成功判定：sCode=0。"""
    return code == 0 and ('"sCode": "0"' in out or '"sCode":"0"' in out)


def _order_exists(inst_id: str, cl_ord_id: str) -> bool | None:
    """該 clOrdId 的單是否已存在（含已成交）。回 True/False/None(查詢失敗)。

    ⚠️ 真冪等的關鍵（2026-07-29 demo 活測教訓）：OKX 只對「掛單中」的 clOrdId
    擋重複——市價單成交後同 clOrdId 可再下＝重試會加倍持倉。所以每腿下單前
    必須先查單，查到任何狀態（含 filled）都算已處理；查詢失敗→不下單（fail-closed）。"""
    code, out = _okx(["swap", "get", "--instId", inst_id, "--clOrdId", cl_ord_id])
    if code == 0 and '"ordId"' in out:
        return True
    if "51603" in out or "doesn't exist" in out or "does not exist" in out:
        return False
    return None


def place(intent: dict, sz: float, dry: bool,
          spec: dict | None = None) -> bool:
    """分批止盈下單（v140）：拆成多筆市價進場單，各帶「自己那腿的 TP＋同一止損價」
    的附掛 OCO——只用已驗證的單附掛原語（CLI --tpLevel 多腿會丟失 SL，activedemo
    實測 50015/無SL，不可用）。OKX 同向合併成一倉，各 OCO 管自己那段：
    TP1 觸發→只平那腿、其餘腿的 SL 續存＝與紙上 40/30/30 階梯同語義。
    每腿 clOrdId 加尾碼 a/b/c 冪等；部分失敗→回 False 由外層重試（已成腿撞
    51016 重複視為成功，不會重複開倉）。單一 TP 時維持原單筆路徑。"""
    lev = leverage_for_trade(intent.get("entry"), intent.get("stop"))
    ensure_leverage(intent["inst_id"], intent["pos_side"], dry, lev=lev)
    tps = [intent.get("tp1"), intent.get("tp2"), intent.get("tp3")]
    legs = (split_tp_levels(sz, spec["lotSz"], spec["minSz"], tps)
            if spec else [])
    if len(legs) < 2:
        legs = [(float(intent["tp1"]), sz)]
    all_ok = True
    for i, (tp_px, leg_sz) in enumerate(legs):
        cl = (f"{intent['cl_ord_id']}{chr(97 + i)}" if len(legs) > 1
              else intent["cl_ord_id"])[:24]
        args = _leg_args(intent, leg_sz, tp_px, cl)
        if dry:
            print("DRY-RUN:", "okx --profile demo " + " ".join(args))
            continue
        # 真冪等：下單前先查此腿 clOrdId 是否已存在（含已成交）——
        # OKX 不擋已成交市價單的 clOrdId 重用，重試盲下會加倍持倉
        exists = _order_exists(intent["inst_id"], cl)
        if exists is True:
            print(f"↩️ 腿{i + 1}/{len(legs)} clOrdId={cl} 已存在（上輪已成）——跳過")
            continue
        if exists is None:
            print(f"⚠️ 腿{i + 1}/{len(legs)} 查單失敗——本輪不下這腿（fail-closed，下輪重試）")
            # 這裡是真正「該下卻沒下」的那一刻——一定要進健康帳，否則整盤零成交無聲
            _note_fail("query_fail", f"{intent['inst_id']} clOrdId={cl} 查單失敗")
            all_ok = False
            continue
        code, out = _okx(args)
        ok = _leg_ok(code, out)
        all_ok = all_ok and ok
        print(("✅" if ok else "❌")
              + f" {intent['inst_id']} {intent['side']} 腿{i + 1}/{len(legs)} "
              f"sz={leg_sz} tp={tp_px} → {out[:160]}")
    return all_ok


def selftest_fail(rounds: int) -> int:
    """製造假故障實證告警路徑（零網路、零下單）。用臨時健康檔，絕不動真實狀態。

    重現 2026-07-30 那晚：OKX 回 401「IP 不在白名單」→ 查單失敗 → 腿全不下。
    預期：前 FAIL_ALERT_AFTER-1 輪只提示，第 FAIL_ALERT_AFTER 輪告警，
    之後冷卻不重複吵，乾淨輪送恢復通知。"""
    global HEALTH
    real, HEALTH = HEALTH, HEALTH.with_name("atk_consumer_health_selftest.json")
    try:
        HEALTH.unlink(missing_ok=True)
        fake_401 = ("Error: HTTP 401 from OKX: Your IP 203.0.113.7 is not "
                    "included in your API key's "
                    "00000000-0000-4000-8000-000000000000 whitelist")
        print(f"— 假故障實證開始（門檻={FAIL_ALERT_AFTER} 輪，告警走 dry 不真的送出）—")
        print(f"  分類結果：{classify_failure(1, fake_401)}"
              f"｜遮蔽後樣本：{redact_secrets(fake_401)[:90]}…")
        now = time.time()
        for i in range(rounds):
            _ROUND_FAILS.clear()
            _note_fail(classify_failure(1, fake_401), fake_401)
            _note_fail("query_fail", "SOXL-USDT-SWAP clOrdId=xxx 查單失敗")
            print(f"[假第 {i + 1} 輪]", end=" ")
            finish_round(dict(_ROUND_FAILS), now + i * 60, dry=True)
        _ROUND_FAILS.clear()
        # v151：先驗「空轉輪不得假痊癒」——零呼叫的一輪不可歸零、不可送恢復通知
        print("[假空轉輪]", end=" ")
        hi = finish_round({}, now + rounds * 60, dry=True, oks=0)
        print(f"— 空轉輪檢查：連續故障維持={hi.get('consecutive_fail_rounds')} 輪"
              f"（應仍為 {rounds}），未誤送恢復通知={not hi.get('recovered_from')} —")
        print("[假恢復輪]", end=" ")
        h = finish_round({}, now + (rounds + 1) * 60, dry=True, oks=1)
        print(f"— 實證結束：連續故障歸零={h.get('consecutive_fail_rounds') == 0}，"
              f"恢復通知已送={bool(h.get('recovered_from'))}，"
              f"告警冷卻已重置={not h.get('last_alert_ts')} —")
        print(f"  健康檔（臨時）：{HEALTH}")
        return 0
    finally:
        HEALTH = real


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest-fail", type=int, metavar="N",
                    help="製造 N 輪假故障走完整告警路徑（零網路、零下單、不寫健康檔"
                         "的真實 profile 欄位以外的任何交易狀態）——驗收告警是否真的有出口")
    a = ap.parse_args()

    if a.selftest_fail:
        return selftest_fail(a.selftest_fail)
    if not a.dry_run and not verify_demo_profile():
        return 1
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        state = {"done": []}
    done = set(state.get("done", []))
    ct_cache: dict = {}

    while True:
        now_ms = time.time() * 1000
        _ROUND_FAILS.clear()             # v143：本輪故障帳從零開始
        _ROUND_OKS["ok"] = 0             # v151：成功帳同步歸零（分辨空轉輪用）
        # v139：先管理在場倉位（對帳/逾時平倉），再看要不要接新單
        manage_positions(a.dry_run)
        halted_today = breaker_tripped(_load_positions().get("day_pnl", {}))
        if halted_today:
            print(f"⛔ 日虧熔斷已觸發（≤ -{DAILY_STOP_USD:.0f} USDT）——今日不接新單，"
                  "既有倉位照常管理")
        for p in sorted(OUTBOX.glob("*.json")):
            try:
                intent = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            iid = intent.get("intent_id")
            if not iid or iid in done:
                continue
            if intent.get("execution_policy") != "demo_only":
                print(f"⏸ {iid} {intent.get('symbol')} human_gated——僅列印不執行")
                done.add(iid)
                continue
            if now_ms > intent.get("expires_at", 0):
                print(f"⏭ {iid} {intent.get('symbol')} 已過期——跳過")
                done.add(iid)
                continue
            if halted_today:
                continue                     # 熔斷日不接新單；intent 未記 done，明日過期自清
            sz = contracts_for(intent["inst_id"], intent["entry"], intent["stop"], ct_cache)
            if sz is None:
                print(f"❌ {iid} 張數換算失敗——跳過（不猜）")
                done.add(iid)
                continue
            # 只在成功時記已處理：失敗留給下輪重試（OKX clOrdId 冪等擋重複成交，
            # intent 過期窗兜底——永久性錯誤最多重試到 expires_at）
            if place(intent, sz, a.dry_run, spec=ct_cache.get(intent["inst_id"])):
                done.add(iid)
                if not a.dry_run:
                    ps = _load_positions()
                    ps["open"][iid] = {"inst_id": intent["inst_id"],
                                       "pos_side": intent["pos_side"],
                                       "symbol": intent.get("symbol"),
                                       "contracts": sz,
                                       "placed_at": time.time()}
                    _save_positions(ps)
        state["done"] = sorted(done)[-500:]
        try:
            STATE.write_text(json.dumps(state), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        # v143：本輪收尾——把 fail-closed 記帳並在連續失敗時告警（dry-run 不真送）
        finish_round(dict(_ROUND_FAILS), dry=a.dry_run, oks=_ROUND_OKS["ok"])
        if a.once or a.dry_run:
            return 0
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
