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
故障分流：IP 白名單 401 / profile 缺失 → 每小時最多一行 log（等使用者補 IP 自癒,
    與 atk 執行器 v143 同因同醫）。
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


def _okx_news(args: list[str], timeout: int = 45) -> tuple[int, str]:
    """呼叫 okx CLI——子指令硬鎖 news。回 (exit_code, stdout)。

    ⛔ 本函式是全 daemon 唯一以 live profile 呼叫 CLI 的地方：
    第一個參數必須是 "news"，否則 raise（防未來誤用擴權）。"""
    if not args or args[0] != "news":
        raise ValueError("okx_news 模組只允許 news 子指令")
    if not _OKX_BIN:
        return 127, "okx CLI not installed"
    env = dict(os.environ, NODE_OPTIONS="--dns-result-order=ipv4first")
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
    if "401" in low or "credential" in low:
        return "auth"
    if "not available in demo" in low:
        return "profile"
    if "timeout" in low:
        return "timeout"
    return "other"


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
    """CLI --json 輸出 → item list。頂層 list 或 {"data":[...]} 都容。"""
    try:
        data = json.loads(out)
    except Exception:  # noqa: BLE001
        return []
    if isinstance(data, dict):
        data = data.get("data") or data.get("list") or []
    return data if isinstance(data, list) else []


def _ts_ms(it: dict) -> float:
    v = it.get("publishTime") or it.get("publish_time") or it.get("time") or 0
    try:
        v = float(v)
    except Exception:  # noqa: BLE001
        return 0.0
    return v if v > 1e12 else v * 1000.0


def render_card(it: dict) -> str:
    title = _esc((it.get("title") or "").strip())
    summary = _esc((it.get("summary") or it.get("description") or "").strip())
    coins = it.get("coins") or it.get("relatedCoins") or []
    if isinstance(coins, list):
        coins = ",".join(str(c) for c in coins[:5])
    senti = (it.get("sentiment") or "").lower()
    senti_mark = {"bullish": "🟢", "bearish": "🔴"}.get(senti, "")
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
    while True:
        try:
            code, out = await asyncio.to_thread(
                _okx_news, ["news", "important", "--lang", "zh-CN", "--limit", "20"])
            if code != 0:
                now = time.time()
                if now - last_fail_log > _QUIET_LOG_S:
                    last_fail_log = now
                    print(f"[okx_news] 來源不可用（{_fail_class(out)}）——每小時提醒一次,"
                          "補 IP 白名單後自癒")
            else:
                pushed = 0
                now_ms = time.time() * 1000
                for it in parse_items(out):
                    if pushed >= MAX_PUSH_PER_CYCLE:
                        break
                    pid = _item_id(it)
                    if already_seen(source=SOURCE, handle="okx", post_id=pid):
                        continue
                    if now_ms - _ts_ms(it) > MAX_AGE_S * 1000:
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
        except Exception as e:  # noqa: BLE001
            print(f"[okx_news] loop 例外（不致命）：{type(e).__name__}: {e}")
        await asyncio.sleep(max(60, int(poll_seconds)))
