"""共用推送工具（v14.1）：全域節流 + 429 退避重試 + 暫時/永久錯誤分類。

設計原則（來自 v14 對抗驗證的教訓）：
- Telegram 限速（單 chat ~1 msg/s、群組 20 msg/min）是必然偶發，不是異常
- 「暫時性失敗」絕不能 mark_seen 永久丟棄 — 留待下輪重試
- 只有「內容性錯誤」（HTML 壞掉 / 訊息過長 / chat 不存在）才放棄
"""
from __future__ import annotations

import asyncio
import time

# 永久性錯誤標記（內容壞掉，重試也沒用 → 可以放棄）
_PERMANENT_MARKERS = (
    "can't parse", "message is too long", "chat not found",
    "bot was blocked", "not enough rights", "topic_deleted",
    "message thread not found",
)

# 全域節流狀態（兩個新聞 worker 共用同一 event loop）
_last_send_mono = 0.0
_throttle_lock = asyncio.Lock()


async def safe_send(tg, text: str, *, parse_mode: str | None = "HTML",
                    min_gap_seconds: float = 1.2) -> tuple[str, dict]:
    """節流發送 + 429 重試一次。

    Returns:
        (status, resp)
        status: 'ok' | 'transient'（不要 mark_seen，下輪重試）| 'permanent'（可放棄）
    """
    global _last_send_mono

    async with _throttle_lock:
        wait = _last_send_mono + min_gap_seconds - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_send_mono = time.monotonic()

    try:
        resp = await tg.send_message(text, parse_mode=parse_mode)
    except Exception as e:
        # httpx timeout / 斷線等 → 暫時性
        return "transient", {"ok": False, "description": f"{type(e).__name__}: {e}"}

    if resp.get("ok"):
        return "ok", resp

    # 429 → 按 retry_after 等待後重試一次
    if resp.get("error_code") == 429:
        retry_after = (resp.get("parameters") or {}).get("retry_after", 3)
        await asyncio.sleep(min(float(retry_after) + 1.0, 65.0))
        try:
            resp = await tg.send_message(text, parse_mode=parse_mode)
        except Exception as e:
            return "transient", {"ok": False, "description": f"{type(e).__name__}: {e}"}
        if resp.get("ok"):
            return "ok", resp

    desc = str(resp.get("description", "")).lower()
    if any(m in desc for m in _PERMANENT_MARKERS):
        return "permanent", resp
    return "transient", resp


def clamp_news_html(build_fn) -> str:
    """渲染長度守門：先試完整版；>4090 改無原文版；仍超長則按行裁切。

    build_fn(include_original: bool) -> str
    """
    text = build_fn(True)
    if len(text) <= 4090:
        return text
    text = build_fn(False)
    if len(text) <= 4090:
        return text
    # 最後手段：按行裁切（避免切斷 HTML entity / tag）
    lines = text.split("\n")
    out: list[str] = []
    total = 0
    for ln in lines:
        if total + len(ln) + 1 > 3900:
            break
        out.append(ln)
        total += len(ln) + 1
    return "\n".join(out) + "\n…（內容過長已截斷）"
