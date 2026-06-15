"""決策佇列（P0-B / task #9）— 「需發起人決策」的單一真實來源。

設計理念：
    使用者（發起人）最大的痛點是「埋在細節、忘記全局」。CEO 監督 Session 每天把
    所有需要「人類拍板」的事項彙整成一段，推到系統主題的單一視窗。本模組就是那段
    內容的資料來源 —— 一個 JSON 檔背書的決策清單。

紅線對齊：
    AI 只能「提出決策請求 + 呈現選項 + 記錄結果」，永遠不能自己拍板。
    （真錢下單／對外發布／Phase 0 解鎖宣告 三條永久紅線見 docs/PROJECT_CHARTER.md）

資料結構（每筆 decision）：
    id        遞增整數
    key       穩定識別字串（用於 idempotent 去重 —— 同 key 不重複建立）
    title     一句話標題
    detail    多行說明（可含 HTML，會在 render 時原樣帶出）
    options   建議選項清單（純文字，給人看；AI 不替使用者選）
    status    'open' | 'resolved'
    created_at / resolved_at  epoch 秒
    resolution  解決時的註記（使用者選了什麼）

並發：daemon 為單進程 asyncio，指令與 worker 同一 event loop，read-modify-write 無真並發。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from botpaths import data_dir as _data_dir

_PATH = _data_dir() / "decisions.json"


def _load() -> dict:
    if not _PATH.exists():
        return {"seq": 0, "items": []}
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        # 損毀時不讓決策佇列拖垮整個 CEO Session —— 回空集合，下次寫入會重建
        return {"seq": 0, "items": []}


def _save(db: dict) -> None:
    tmp = _PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_PATH)  # 原子寫入，避免半截檔


def add_decision(key: str, title: str, detail: str = "",
                 options: list[str] | None = None) -> int:
    """新增一筆待決策（idempotent：同 key 已存在且 open → 不重複建，回原 id）。

    回 decision id。"""
    db = _load()
    for it in db["items"]:
        if it["key"] == key and it["status"] == "open":
            return it["id"]
    db["seq"] += 1
    new_id = db["seq"]
    db["items"].append({
        "id": new_id,
        "key": key,
        "title": title,
        "detail": detail,
        "options": options or [],
        "status": "open",
        "created_at": int(time.time()),
        "resolved_at": None,
        "resolution": None,
    })
    _save(db)
    return new_id


def list_open() -> list[dict]:
    """所有未決策事項，最舊在前。"""
    return [it for it in _load()["items"] if it["status"] == "open"]


def resolve(decision_id: int, resolution: str = "") -> dict:
    """標記某決策已處理（使用者拍板後）。回 {ok, msg}。"""
    db = _load()
    for it in db["items"]:
        if it["id"] == decision_id:
            if it["status"] == "resolved":
                return {"ok": True, "msg": f"決策 #{decision_id} 先前已處理"}
            it["status"] = "resolved"
            it["resolved_at"] = int(time.time())
            it["resolution"] = resolution
            _save(db)
            return {"ok": True, "msg": f"決策 #{decision_id}「{it['title']}」已標記處理"}
    return {"ok": False, "msg": f"找不到決策 #{decision_id}"}


def render_open(decisions: list[dict] | None = None) -> str:
    """文字化未決策清單（給 CEO 簡報 / /decisions 指令）。"""
    items = decisions if decisions is not None else list_open()
    if not items:
        return "（目前沒有需要你拍板的事項）"
    lines = []
    for it in items:
        lines.append(f"<b>#{it['id']}　{it['title']}</b>")
        if it.get("detail"):
            lines.append(it["detail"])
        if it.get("options"):
            for i, opt in enumerate(it["options"], 1):
                lines.append(f"  {i}. {opt}")
        lines.append("")  # 空行分隔
    return "\n".join(lines).rstrip()


# ===========================================================================
# 已知決策種子 —— 對話中已浮現、等使用者拍板的事項
# （idempotent，重啟不會重複塞）
# ===========================================================================
def seed_known_decisions() -> None:
    """把目前已浮現、等發起人拍板的事項寫入佇列（若尚未存在）。"""
    add_decision(
        key="revenue_waterfall_v1",
        title="收益分配四階段瀑布是否核准",
        detail=("章程建議：30% 發起人／50% 貢獻者／20% 公益，但<b>改成四階段瀑布</b> —\n"
                "①先扣成本 ②預留池上限 6 個月成本 ③發起人代墊回補 ④才套 30/50/20；\n"
                "且 <b>Phase 0 解鎖前完全不啟動任何分潤</b>。"),
        options=["核准此版本", "調整比例（告訴我新數字）", "暫不定案，先擱置"],
    )
    # v42：舊「15x→3x 一刀切」決策已被「依預算分級」框架取代 → 退場改種新版
    for it in list_open():
        if it["key"] == "default_leverage_3x":
            resolve(it["id"], "已由 v42『依預算自適應風控分級』取代（見 leverage_tier_v42）")
    add_decision(
        key="leverage_tier_v42",
        title="你自己這台要不要改吃「依預算分級」的槓桿/風險預設",
        detail=(
            "v42 已上線「依本金分級」風控框架：本金是可設定參數，<b>只有沒設定的鍵</b>才套"
            "保守 tier 預設（micro&lt;1000U:3x／small・standard:5x；風險 1.0%／"
            "日開倉 2-3／總曝險 5-6%）。\n"
            "<b>你目前的部署在 .env 明確設了 15x 與 1R=$100，依「明確值優先」原則完全沒被"
            "改動（零行為改變）。</b>\n"
            "這題只關乎<b>你自己這台</b>：\n"
            "• 維持現狀＝繼續 15x／$100（明確值優先，最自由）\n"
            "• 改吃 tier＝把 .env 的 DEFAULT_LEVERAGE／RISK_PER_TRADE_USD 拿掉，"
            "讓本金自動決定（$5000＝Standard＝5x／1.0%＝$50）\n"
            "（開源給別人自架時，預設就是 tier；別人本金多少就吃對應護欄。）"
        ),
        options=[
            "維持現狀（明確 15x／$100 不動）",
            "我這台改吃 tier 保守預設（5x／1.0%）",
            "用其他數字（請指定槓桿與風險）",
        ],
    )


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    seed_known_decisions()
    import re
    print(f"decisions.json -> {_PATH}")
    print(re.sub(r"<[^>]+>", "", render_open()))
