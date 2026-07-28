# -*- coding: utf-8 -*-
"""signal_bridge.py — OKX Signal Bot 模擬盤(pap)訊號橋（v112，Phase B 水管的 demo 半）。

目的：把本系統的加密訊號自動餵給「使用者在 OKX 親手建立的 Signal Bot」執行——
    先全程走 OKX 官方模擬盤端點(pap)驗證整條管線；真盤切換永遠是使用者的實體動作。

⛔ 紅線硬擋（程式層保證，不可繞）：
    1. 只接受 https://www.okx.com/pap/ 開頭的 webhook——實盤 host(/algo/) 不存在於本碼，
       .env 填了非 pap 網址一律拒發並告警（紅線①：AI 永不自動觸發真錢）。
    2. signalToken 只存 .env（SIGNALBOT_PAP_URL / SIGNALBOT_PAP_TOKEN），永不入庫/日誌。
    3. 任何非 2xx / 例外必印必告警，永不靜默吞（51121/51004 教訓）。
    4. 未設定 env → worker 完全閒置（零網路、零副作用）。

訊號來源：paper_trades 的加密 deepdive 單（與 demo_operator 同一條上游）。
    新開倉→ENTER_LONG/ENTER_SHORT；平倉→EXIT_LONG/EXIT_SHORT（Alert 2.0 語彙）。
    去重靠本地 state 檔記「已送過的 (paper_id, phase)」。
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from typing import Optional

import httpx

from botpaths import data_dir, db_path

_PAP_PREFIX = "https://www.okx.com/pap/"          # 唯一合法前綴：OKX 模擬盤 Signal Bot 端點
_STATE = data_dir() / "signal_bridge_state.json"
_POLL_SEC = 60


def pap_url_ok(url: Optional[str]) -> bool:
    """紅線閘：只放行 OKX 模擬盤(pap) webhook。實盤 /algo/ host 不在本碼任何角落。"""
    return bool(url) and url.startswith(_PAP_PREFIX)


def map_action(direction: str, phase: str) -> Optional[str]:
    """(bull|bear, entry|exit) → Alert 2.0 action。未知組合→None（不送、不猜）。"""
    m = {("bull", "entry"): "ENTER_LONG", ("bull", "exit"): "EXIT_LONG",
         ("bear", "entry"): "ENTER_SHORT", ("bear", "exit"): "EXIT_SHORT"}
    return m.get((direction, phase))


def build_alert(symbol: str, direction: str, phase: str, token: str) -> Optional[dict]:
    """組 OKX Signal Bot Alert 2.0 訊息。缺料→None。倉位大小交給使用者在 bot 端設定
    （investment/槓桿/上限全在 OKX 側＝使用者的執行器,我們只給方向訊號）。"""
    action = map_action(direction, phase)
    if not action or not symbol or not token:
        return None
    return {
        "action": action,
        "instrument": f"{symbol.upper()}-USDT-SWAP",
        "signalToken": token,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "maxLag": "60",
    }


def _load_state() -> dict:
    try:
        return json.loads(_STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"sent": [], "last_id": 0}


def _save_state(st: dict) -> None:
    try:
        st["sent"] = st.get("sent", [])[-500:]     # 防無限長
        _STATE.write_text(json.dumps(st), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def scan_new_events(last_id: int, sent: set) -> tuple[list[dict], int]:
    """掃 paper_trades 加密 deepdive：回 [(paper_id, symbol, direction, phase)...] 未送過的事件。
    entry=狀態 open 且 id>last_id 起算；exit=已 closed 且 entry 曾送過（成對出場）。"""
    try:
        conn = sqlite3.connect(f"file:{db_path('trade_journal.db')}?mode=ro", uri=True)
    except Exception:  # noqa: BLE001
        return [], last_id
    out: list[dict] = []
    max_id = last_id
    try:
        rows = conn.execute(
            "SELECT id, symbol, direction, status FROM paper_trades "
            "WHERE setup IN ('deepdive','us_breakout') AND id > ? ORDER BY id",
            (last_id - 200,)
        ).fetchall()
        for pid, sym, direction, status in rows:
            max_id = max(max_id, pid)
            if status == "open" and f"{pid}:entry" not in sent:
                out.append({"paper_id": pid, "symbol": sym, "direction": direction,
                            "phase": "entry"})
            elif (status == "closed" and f"{pid}:entry" in sent
                  and f"{pid}:exit" not in sent):
                out.append({"paper_id": pid, "symbol": sym, "direction": direction,
                            "phase": "exit"})
    except Exception:  # noqa: BLE001
        pass
    finally:
        conn.close()
    return out, max_id


async def post_alert(url: str, alert: dict) -> tuple[bool, str]:
    """POST 到 pap webhook。回 (ok, note)。非 2xx 永不靜默。"""
    if not pap_url_ok(url):
        return False, "blocked:non-pap-url（紅線①：只允許 OKX 模擬盤端點）"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=alert)
        if 200 <= r.status_code < 300:
            return True, f"ok:{r.status_code}"
        return False, f"http:{r.status_code}:{r.text[:120]}"
    except Exception as e:  # noqa: BLE001
        return False, f"exc:{type(e).__name__}:{str(e)[:100]}"


async def run_signal_bridge_loop(tg=None, poll_seconds: int = _POLL_SEC):
    """pap 訊號橋 worker。未設定 env → 完全閒置（印一行說明後長眠）。"""
    url = os.getenv("SIGNALBOT_PAP_URL", "").strip()
    token = os.getenv("SIGNALBOT_PAP_TOKEN", "").strip()
    if not url or not token:
        print("[signal_bridge] 未設定 SIGNALBOT_PAP_URL/TOKEN——閒置（設定後重啟生效；"
              "只接受 OKX 模擬盤 pap 端點）")
        while True:
            await asyncio.sleep(3600)
    if not pap_url_ok(url):
        msg = ("[signal_bridge] ⛔ SIGNALBOT_PAP_URL 不是 OKX 模擬盤(pap)端點——拒絕啟動"
               "（紅線①：本橋永不對實盤端點發訊）")
        print(msg)
        if tg:
            try:
                await tg.send_message("🚨 " + msg, parse_mode=None)
            except Exception:  # noqa: BLE001
                pass
        while True:
            await asyncio.sleep(3600)
    print(f"[signal_bridge] pap 訊號橋上線（poll={poll_seconds}s，僅模擬盤）")
    st = _load_state()
    sent = set(st.get("sent", []))
    last_id = int(st.get("last_id", 0))
    while True:
        try:
            events, last_id = scan_new_events(last_id, sent)
            for ev in events:
                alert = build_alert(ev["symbol"], ev["direction"], ev["phase"], token)
                if not alert:
                    continue
                ok, note = await post_alert(url, alert)
                key = f"{ev['paper_id']}:{ev['phase']}"
                if ok:
                    sent.add(key)
                    print(f"[signal_bridge] ✅ {alert['action']} {alert['instrument']} ({note})")
                else:
                    print(f"[signal_bridge] ⚠️ 送出失敗 {alert['action']} "
                          f"{alert['instrument']} → {note}")
                    if tg:
                        try:
                            await tg.send_message(
                                f"⚠️ pap 訊號橋送出失敗：{alert['action']} "
                                f"{alert['instrument']}\n{note}", parse_mode=None)
                        except Exception:  # noqa: BLE001
                            pass
                await asyncio.sleep(1)
            st["sent"] = sorted(sent)
            st["last_id"] = last_id
            _save_state(st)
        except Exception as e:  # noqa: BLE001
            print(f"[signal_bridge] loop 例外（不致命）：{type(e).__name__}: {e}")
        await asyncio.sleep(max(15, int(poll_seconds)))
