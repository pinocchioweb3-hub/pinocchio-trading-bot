"""對外內容待審佇列（P0-B / task #9）— 紅線 2 的技術護欄。

永久紅線 2：**任何對外發布（Threads / 信件 / 社群貼文）AI 只能草擬，永不自動送出。**
本模組把「草稿」與「送出」之間插入一道人工閘門：

    生產者（threads_publisher、build-log、未來的社群回覆…）→ enqueue() 進待審
    CEO 監督 Session 每天在簡報「需發起人決策」段落把待審件數秀出來
    使用者用 /approve <id> 核准 → 狀態轉 approved（真正送出由各生產者下次輪詢自取，
        或由專責 sender 處理；本模組只管「批准與否」這個決定，不直接呼叫外部 API）
    /reject <id> 則丟棄

這樣設計的好處：即使某個生產者被誤改成「自動發」，只要它老實走 outbox，
未經 /approve 的內容狀態永遠是 pending，不會外流。

資料結構（每筆 item）：
    id        遞增整數
    channel   去向（'threads' | 'email' | 'community' | …）
    kind      內容類型（'build_log' | 'reply' | 'announcement' | …）
    content   草稿全文（純文字 / 該渠道格式）
    meta      附加資訊 dict（收件人、引用來源 id 等）
    status    'pending' | 'approved' | 'rejected' | 'sent'
    created_at / decided_at  epoch 秒
    note      審核註記
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from botpaths import data_dir as _data_dir

_PATH = _data_dir() / "outbox.json"

VALID_STATUS = ("pending", "approved", "rejected", "sent")


def _load() -> dict:
    if not _PATH.exists():
        return {"seq": 0, "items": []}
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"seq": 0, "items": []}


def _save(db: dict) -> None:
    tmp = _PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_PATH)


def enqueue(channel: str, content: str, kind: str = "generic",
            meta: dict | None = None) -> int:
    """生產者把草稿丟進待審佇列。回 item id。"""
    db = _load()
    db["seq"] += 1
    new_id = db["seq"]
    db["items"].append({
        "id": new_id,
        "channel": channel,
        "kind": kind,
        "content": content,
        "meta": meta or {},
        "status": "pending",
        "created_at": int(time.time()),
        "decided_at": None,
        "note": None,
    })
    _save(db)
    return new_id


def list_by_status(status: str = "pending") -> list[dict]:
    return [it for it in _load()["items"] if it["status"] == status]


def count_pending() -> int:
    return len(list_by_status("pending"))


def approve(item_id: int, note: str = "") -> dict:
    """使用者核准某草稿（pending → approved）。回 {ok, msg, item}。"""
    return _decide(item_id, "approved", note)


def reject(item_id: int, note: str = "") -> dict:
    """使用者退回某草稿（pending → rejected）。"""
    return _decide(item_id, "rejected", note)


def mark_sent(item_id: int) -> dict:
    """生產者把已核准內容實際送出後，標記 sent（approved → sent）。"""
    return _decide(item_id, "sent", "", from_status="approved")


def _decide(item_id: int, new_status: str, note: str,
            from_status: str = "pending") -> dict:
    db = _load()
    for it in db["items"]:
        if it["id"] == item_id:
            if it["status"] == new_status:
                return {"ok": True, "msg": f"#{item_id} 已是 {new_status}", "item": it}
            if it["status"] != from_status:
                return {"ok": False,
                        "msg": f"#{item_id} 目前狀態 {it['status']}，不能轉 {new_status}"}
            it["status"] = new_status
            it["decided_at"] = int(time.time())
            if note:
                it["note"] = note
            _save(db)
            return {"ok": True, "msg": f"#{item_id} → {new_status}", "item": it}
    return {"ok": False, "msg": f"找不到待審內容 #{item_id}"}


def render_pending(items: list[dict] | None = None, limit: int = 10) -> str:
    """文字化待審清單（給 /approve 無參數時 / CEO 簡報）。"""
    import html as _html
    pend = items if items is not None else list_by_status("pending")
    if not pend:
        return "📭 沒有待審的對外內容"
    lines = ["📤 <b>待審對外內容</b>（紅線 2：須你核准才送）", "━━━━━━━━━━━━━━━━"]
    for it in pend[:limit]:
        preview = _html.escape((it["content"] or "")[:120])
        lines.append(f"<b>#{it['id']}</b>　[{it['channel']}/{it['kind']}]\n{preview}…")
    lines.append("\n核准：<code>/approve 編號</code>　退回：<code>/reject 編號</code>")
    return "\n".join(lines)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    print(f"outbox.json -> {_PATH}")
    print(f"pending: {count_pending()}")
    import re
    print(re.sub(r"<[^>]+>", "", render_pending()))
