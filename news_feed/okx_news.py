# -*- coding: utf-8 -*-
"""v175: OKX 官方新聞源 — ATK CLI news 模組（important 端點,原生繁/簡中文）。

背景：CoinGlass 訂閱 2026-07-08 到期後 cg_news 斷源；OKX ATK CLI 內建完整新聞
模組（latest/important/by-coin/search/detail,含重要度與情緒標籤）＝免費替代源。
使用者 2026-08-01 指定接入（同時要求 CoinDesk RSS,見 coindesk_rss.py）。

⛔ 安全鐵則（本模組唯一的交易所接觸面）：
    * News 端點需要 live profile（demo 模式被 OKX 拒絕）——本模組因此持有
      「用實盤 profile 呼叫 CLI」的能力,故 _okx_news() 把子指令硬鎖死為
      "news"：任何其他子指令一律 raise。永不下單、永不查帳、永不碰倉位。
    * 顯示鐵則不變：新聞 100% display_only,永不進開單數學（紅隊定案 task#66）。

管線：10 分鐘輪詢 important（OKX 端已預過濾高影響）→ 原生 id 去重 →
    冷啟動舊聞不推（3h 窗）→ 每輪最多 2 則 → 📰 新聞快訊主題。
    內容原生中文 → 不經 LLM 翻譯（省成本＋少一個失效點）。
故障分流：每小時最多一行 log。⛔ v220 起處方由 _fail_class() 推導,只有
    auth_ip_whitelist 這一類才准提白名單；認不出來的類別一律講「原因未知」附原文。

v220 修（線上實證 2026-08-02,上線 32h 內從未推過任何一則、且全程沒有任何一行
    日誌說它壞了）：
    ① parse_items 只認 data/list,OKX 實際頂層 key 是 "details" ⇒ 恆解析 0 則。
    ② _ts_ms 不認 cTime ⇒ 回 0 ⇒ 每一則都被 3h 窗判 too_old（第①段修好也還是推 0）。
    ③ render_card 找 coins/sentiment,實際是 ccyList/ccySentiments。
    ④ exit=0＋解析 0 則＝完全靜默 ⇒ 新增 parse_anomaly()/cycle_summary(),
       「解析不出來」與「推 0 則」都必須自己講出理由。
    ⑤ 故障行寫死「補 IP 白名單後自癒」,不分類別照貼（使用者 8/01 已取消白名單,
       這句話把他送去改一個不存在的設定）。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time

from .news_db import already_seen, mark_seen

SOURCE = "okxnews"
POLL_S = 600                  # 10 分鐘
MAX_AGE_S = 3 * 3600          # 冷啟動防灌水
MAX_PUSH_PER_CYCLE = 2        # 防洪水（對齊 cg_news）
_OKX_BIN = shutil.which("okx")
_QUIET_LOG_S = 3600           # 故障期每小時最多出聲一次


_CRED_NAME_TOKENS = ("KEY", "SECRET", "PASSPHRASE", "TOKEN")


def _child_env() -> dict:
    """給 CLI 的子行程環境。⛔ 只**刪空值**，永遠不注入任何憑證。

    v220 真因（線上實證）：.env 帶了 OKX_API_KEY／OKX_API_SECRET／
    OKX_API_PASSPHRASE 三個**空字串佔位**。CLI 只看「變數在不在」，
    於是判定使用者要用 env 憑證 → partial → 整個拒絕，而且**不回退**到
    config.toml 的 [profiles.live]（那裡三欄都填好了）。
    空字串不是憑證——同物種（「有這個 key」被折成「有這個值」）在環境變數層。

    只清「名稱像憑證」且值為空白的 OKX_* 變數；⛔ 有值的一律不動，
    ⛔ 不掃非憑證形狀的變數（那些空值可能有「明示關閉」語意）。"""
    env = dict(os.environ, NODE_OPTIONS="--dns-result-order=ipv4first")
    for k in [k for k, v in env.items()
              if k.startswith("OKX_")
              and any(t in k for t in _CRED_NAME_TOKENS)
              and not (v or "").strip()]:
        env.pop(k, None)
    return env


def _okx_news(args: list[str], timeout: int = 45) -> tuple[int, str]:
    """呼叫 okx CLI——子指令硬鎖 news。回 (exit_code, stdout)。

    ⛔ 本函式是全 daemon 唯一以 live profile 呼叫 CLI 的地方：
    第一個參數必須是 "news"，否則 raise（防未來誤用擴權）。"""
    if not args or args[0] != "news":
        raise ValueError("okx_news 模組只允許 news 子指令")
    if not _OKX_BIN:
        return 127, "okx CLI not installed"
    env = _child_env()
    try:
        r = subprocess.run([_OKX_BIN, "--profile", "live", *args, "--json"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env)
        return r.returncode, (r.stdout or r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def _fail_class(out: str) -> str:
    """故障分類（對齊 atk 執行器 v143 語彙）。"""
    low = (out or "").lower()
    if "ip" in low and "whitelist" in low:
        return "auth_ip_whitelist"
    # v220：⛔ 必須排在 auth 之前。這句話含 "credential"，舊碼會歸成 auth
    #       ⇒ 讀起來像「金鑰壞了」，實際是環境變數被空值污染，方向完全相反。
    if "partial api credentials" in low:
        return "credentials_partial"
    if "401" in low or "credential" in low:
        return "auth"
    if "not available in demo" in low:
        return "profile"
    if "timeout" in low:
        return "timeout"
    return "other"


# v220：處方必須由類別推導。⛔ 舊碼不管哪一類都硬貼「補 IP 白名單後自癒」，
#       使用者 8/01 已取消白名單，那句話把他送去改一個不存在的設定。
#       ⛔ 只有 auth_ip_whitelist 這一類可以提白名單；其餘一律不得提。
_FAIL_HINT = {
    "auth_ip_whitelist": "OKX 明確回報 IP 白名單阻擋——把目前出口 IP 加回白名單後自癒。",
    "auth": ("OKX 回報認證失敗（401／credential）＝live profile 的金鑰被拒。"
             "⛔ 這不是 IP 限制那一類（那會另外分類、另外提示），"
             "請勿據此更動交易所端的存取設定。"),
    "credentials_partial": ("CLI 判定「憑證只給了一半」而拒絕，且不回退到 "
                            "config.toml 的 profile。⛔ 這不是金鑰壞掉：多半是環境變數裡有"
                            "**空字串佔位**的 OKX_* 憑證變數被讀成「有提供」。"
                            "_child_env() 已負責清掉空值；若此訊息仍出現，"
                            "代表有非空但不完整的一組憑證變數。"),
    "profile": "此端點需要 live profile，demo 被 OKX 拒絕。",
    "timeout": "呼叫逾時——多為暫時性，下一輪自動再試。",
}
_UNKNOWN_HINT = "原因未知——⛔ 這裡不猜原因；附上原文供追查。"


def _redact(s: str) -> str:
    """⛔ repo 是 public，故障原文可能夾金鑰 id／token。"""
    import re
    return re.sub(r"[A-Za-z0-9_\-]{20,}", "[已遮罩]", s or "")


def fail_message(code: int, out: str) -> str:
    """組故障行：類別 → 處方 → 原文證據。認不出來就說認不出來。"""
    cls = _fail_class(out)
    hint = _FAIL_HINT.get(cls, _UNKNOWN_HINT)
    excerpt = _redact(" ".join((out or "").split()))[:200]
    return (f"[okx_news] 來源不可用（exit={code}／{cls}）——每小時提醒一次。"
            f"{hint} 原文：{excerpt or '(空)'}")


def parse_anomaly(out: str) -> str | None:
    """呼叫成功卻一則都解析不出來 ⇒ 回一句話；正常 ⇒ None。

    這是 v220 的元兇狀態：exit=0 不算故障、解析 0 則不印 pushed ⇒ 整條源
    靜默死亡 32 小時無人知。⛔ 「解析不出來」永遠不得折成「今天沒有重要新聞」。"""
    raw = (out or "").strip()
    tail = "⛔ 這是解析不出來，不等於「今天沒有重要新聞」。"
    if not raw:
        return f"[okx_news] 呼叫成功（exit=0）但回應是空的——{tail}"
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return (f"[okx_news] 呼叫成功（exit=0）但回應不是 JSON——{tail}"
                f"原文開頭：{_redact(raw)[:120]}")
    if parse_items(raw):
        return None
    if isinstance(data, dict):
        keys = "、".join(list(data.keys())[:8]) or "(無)"
        return (f"[okx_news] 呼叫成功（exit=0）但解析出 0 則；回應頂層 key＝[{keys}]"
                f"——{tail}若 key 不在預期清單內，代表上游換了外殼。")
    return f"[okx_news] 呼叫成功（exit=0）但回應是空清單——{tail}"


def cycle_summary(parsed: int, dup: int, too_old: int, pushed: int,
                  ts_unknown: int = 0) -> str | None:
    """解析得到東西卻一則都沒推時，把理由講出來（推 0 則 ≠ 沒東西）。"""
    if pushed or not parsed:
        return None
    extra = f"／時間讀不到 {ts_unknown}" if ts_unknown else ""
    return (f"[okx_news] 本輪解析 {parsed} 則、推 0 則"
            f"（已推過 {dup}／超過 {MAX_AGE_S // 3600}h 舊聞 {too_old}{extra}）"
            f"——推 0 則是有理由的，不是來源沒東西。")


def _item_id(it: dict) -> str:
    """原生 id 優先；缺則 (標題+時間) 雜湊。"""
    nid = it.get("id") or it.get("newsId") or it.get("news_id")
    if nid:
        return f"okx{nid}"[:40]
    import hashlib
    raw = f"{it.get('title')}|{it.get('publishTime') or it.get('publish_time')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def parse_items(out: str) -> list[dict]:
    """CLI --json 輸出 → item list。

    v220：線上實測 OKX ATK 回的頂層 key 是 **"details"**（另有 nextCursor），
    舊碼只認 data/list ⇒ 恆得 0 則 ⇒ 整條源自 v175 上線起從未推過任何一則。
    ⛔ data/list 相容保留，不得為了修這個而弄壞既有外殼。"""
    try:
        data = json.loads(out)
    except Exception:  # noqa: BLE001
        return []
    if isinstance(data, dict):
        data = data.get("details") or data.get("data") or data.get("list") or []
    return data if isinstance(data, list) else []


def _ts_ms(it: dict) -> float | None:
    """發布時間（ms）。⛔ 讀不到回 None——折成 0 等於謊稱它是 1970 年的舊聞
    （舊碼正是如此：cTime 不在候選鍵裡 ⇒ 每一則都被 too_old 濾掉）。"""
    for k in ("cTime", "publishTime", "publish_time", "ts", "time"):
        v = it.get(k)
        if v in (None, ""):
            continue
        try:
            f = float(v)
        except Exception:  # noqa: BLE001
            continue
        if f <= 0:
            continue
        return f if f > 1e12 else f * 1000.0
    return None


def _sentiment_mark(it: dict) -> str:
    """情緒標記。v220：線上樣本的 ccySentiments 恆為空清單，元素形狀無從觀測
    ⇒ ⛔ 不臆造形狀；只認得出來才顯示，認不出來就不顯示（純裝飾，非量測值）。"""
    senti = it.get("sentiment")
    if not isinstance(senti, str):
        arr = it.get("ccySentiments")
        if isinstance(arr, list) and arr:
            first = arr[0]
            if isinstance(first, str):
                senti = first
            elif isinstance(first, dict):
                senti = first.get("sentiment") or first.get("type")
    if not isinstance(senti, str):
        return ""
    return {"bullish": "🟢", "bearish": "🔴"}.get(senti.strip().lower(), "")


def render_card(it: dict) -> str:
    title = _esc((it.get("title") or "").strip())
    summary = _esc((it.get("summary") or it.get("description") or "").strip())
    # v220：OKX 實際欄位是 ccyList（舊碼找 coins/relatedCoins，永遠是空的）
    coins = it.get("ccyList") or it.get("coins") or it.get("relatedCoins") or []
    if isinstance(coins, list):
        coins = ",".join(str(c) for c in coins[:5])
    senti_mark = _sentiment_mark(it)
    lines = [f"🟠 <b>OKX 快訊</b>{('　' + senti_mark) if senti_mark else ''}"
             f"{('　<code>' + _esc(str(coins)) + '</code>') if coins else ''}",
             f"<b>{title}</b>"]
    if summary and summary != title:
        lines.append(summary[:400])
    lines.append("<i>來源: OKX News（官方聚合）· 資訊僅供參考，非交易訊號</i>")
    return "\n".join(lines)


async def run_okx_news_loop(tg, poll_seconds: int = POLL_S):
    """worker：輪詢 OKX important 新聞 → 去重 → 推 📰。IP 未補白名單時安靜等待。"""
    print("[okx_news] loop online（OKX ATK news/important,10min,原生中文,live 唯讀）")
    last_fail_log = 0.0
    last_quiet_log = 0.0
    while True:
        try:
            code, out = await asyncio.to_thread(
                _okx_news, ["news", "important", "--lang", "zh-CN", "--limit", "20"])
            if code != 0:
                now = time.time()
                if now - last_fail_log > _QUIET_LOG_S:
                    last_fail_log = now
                    print(fail_message(code, out))
            else:
                # v220：exit=0 不代表拿到東西。解析不出來要出聲，否則靜默死亡。
                anomaly = parse_anomaly(out)
                if anomaly:
                    now = time.time()
                    if now - last_quiet_log > _QUIET_LOG_S:
                        last_quiet_log = now
                        print(anomaly)
                pushed = dup = too_old = ts_unknown = 0
                items = parse_items(out)
                now_ms = time.time() * 1000
                for it in items:
                    if pushed >= MAX_PUSH_PER_CYCLE:
                        break
                    pid = _item_id(it)
                    if already_seen(source=SOURCE, handle="okx", post_id=pid):
                        dup += 1
                        continue
                    ts = _ts_ms(it)
                    if ts is None:
                        # ⛔ 時間讀不到 ≠ 舊聞。不推（防冷啟動灌水），但要記錄可見理由。
                        ts_unknown += 1
                        mark_seen(source=SOURCE, handle="okx", post_id=pid,
                                  push_reason="ts_unknown")
                        continue
                    if now_ms - ts > MAX_AGE_S * 1000:
                        too_old += 1
                        mark_seen(source=SOURCE, handle="okx", post_id=pid,
                                  push_reason="too_old")
                        continue
                    try:
                        await tg.send_message(render_card(it), parse_mode="HTML")
                        mark_seen(source=SOURCE, handle="okx", post_id=pid, pushed=True, push_reason="pushed")
                        pushed += 1
                    except Exception as e:  # noqa: BLE001
                        print(f"[okx_news] 推送失敗（下輪重試）：{type(e).__name__}: {e}")
                        break
                if pushed:
                    print(f"[okx_news] pushed {pushed}")
                else:
                    line = cycle_summary(len(items), dup, too_old, pushed, ts_unknown)
                    if line and time.time() - last_quiet_log > _QUIET_LOG_S:
                        last_quiet_log = time.time()
                        print(line)
        except Exception as e:  # noqa: BLE001
            print(f"[okx_news] loop 例外（不致命）：{type(e).__name__}: {e}")
        await asyncio.sleep(max(60, int(poll_seconds)))
