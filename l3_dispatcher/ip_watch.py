# -*- coding: utf-8 -*-
"""v176: 對外 IP 變更哨兵——住宅浮動 IP 換掉時「事前」告警。

背景：2026-07-30 家用 IP 輪換→OKX 白名單 401→實盤執行器 fail-closed 20+ 小時,
既有告警(v143)是「被拒絕之後」才響。本哨兵每 10 分鐘量一次對外 IP,一偵測到
變更立刻推 TG(含新 IP 與補白名單指示),把「發現→修復」的延遲從小時級壓到分鐘級。

鐵則：⛔ IP 值只進本地狀態檔與私人 TG,永不寫進 repo/commit(repo 是 PUBLIC,
r71 曾把真實出口 IP 推上公開 repo 的教訓)。查詢失敗靜默等下輪,永不誤報。
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from botpaths import data_dir

_STATE = data_dir() / "ip_watch_state.json"
_POLL_S = 600
_PROBES = ("https://api.ipify.org", "https://ifconfig.me/ip")


async def _current_ip() -> str | None:
    """量對外 IPv4。兩個探針任一成功即回;全失敗回 None(不告警不記錄)。"""
    for url in _PROBES:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(url)
            ip = (r.text or "").strip()
            if r.status_code == 200 and 7 <= len(ip) <= 45 and "." in ip:
                return ip
        except Exception:  # noqa: BLE001
            continue
    return None


def _load() -> dict:
    try:
        return json.loads(_STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save(st: dict) -> None:
    try:
        _STATE.write_text(json.dumps(st), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


async def run_ip_watch_loop(tg=None, poll_seconds: int = _POLL_S):
    """worker：IP 變更→立即 TG(老 IP→新 IP+補白名單指示)。首輪只記基線不告警。"""
    print("[ip_watch] loop online（10min 哨兵,IP 變更事前告警）")
    while True:
        try:
            ip = await _current_ip()
            if ip:
                st = _load()
                old = st.get("ip")
                if old and old != ip:
                    msg = ("🌐 <b>對外 IP 已變更</b>（住宅浮動 IP 輪換）\n"
                           f"舊：<code>{old}</code> → 新：<code>{ip}</code>\n"
                           "⚠️ 實盤 API 白名單將開始擋單——請到 OKX App→API 管理→"
                           f"白名單加入 <code>{ip}</code>（加完自動恢復,不用重啟）")
                    print(f"[ip_watch] IP 變更偵測（詳見 TG）")
                    if tg:
                        try:
                            await tg.send_message(msg, parse_mode="HTML")
                        except Exception as e:  # noqa: BLE001
                            print(f"[ip_watch] TG 告警失敗：{type(e).__name__}: {e}")
                if old != ip:
                    # ⛔ 首輪基線 vs 真的輪換：光看 changed_at 分不出來——哨兵第一次
                    # 開機也會寫一個「剛剛」的 changed_at。r78 監督員差點據此推出
                    # 「IP 在 01:41 換過」的假結論（實際是 v176 上線後的基線寫入）。
                    # 判據是 rotations：==0 ⇒ 從未觀測到輪換，不論 changed_at 幾點。
                    st = {
                        "ip": ip,
                        "changed_at": time.time(),
                        "baseline": not old,
                        "rotations": int(st.get("rotations") or 0) + (1 if old else 0),
                    }
                # 每輪都落 last_seen_at：舊碼只在「變更時」寫檔 ⇒「IP 穩定沒變」與
                # 「哨兵已死」在檔案上長得一模一樣，判活只能靠 mtime＝代理值當事實。
                # 缺 rotations/baseline 鍵者＝v180 之前的舊紀錄，一律讀作「未知」，
                # ⛔ 不得補寫 0 冒充「已證實沒輪換」。
                st["last_seen_at"] = time.time()
                _save(st)
        except Exception as e:  # noqa: BLE001
            print(f"[ip_watch] loop 例外（不致命）：{type(e).__name__}: {e}")
        await asyncio.sleep(max(120, int(poll_seconds)))
