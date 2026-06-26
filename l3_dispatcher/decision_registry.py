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
                 options: list[str] | None = None,
                 seed_once: bool = False) -> int:
    """新增一筆待決策。回 decision id。

    去重語意：
      • 預設（seed_once=False）：同 key 已存在且 **open** → 不重複建，回原 id。
        （允許「同題重提」——一旦舊卡被 resolved，可再開一張新 open 卡。）
      • seed_once=True：同 key **曾經存在**（不論 open / resolved）→ 不再建，回最後一筆 id。
        用於「已知種子」決策：使用者一旦拍板（**含『暫不定案，先擱置』**），
        daemon 重啟不得復活同題反覆騷擾使用者。日後若條件成熟要重議，
        應以明確的新 key（如 revenue_waterfall_v2）或人工重開，而非自動復活。
    """
    db = _load()
    if seed_once:
        last_id = None
        for it in db["items"]:
            if it["key"] == key:
                last_id = it["id"]  # 取最後一筆同 key（不論狀態）
        if last_id is not None:
            return last_id
    else:
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
    # seed_once=True：使用者已三度（id=1 2026-06-19、id=4 2026-06-26、id=5）就此題
    # 表態「暫不定案，先擱置」（理由：機器人尚未成型／Phase 0 未解鎖，待成型後再議）。
    # 舊版去重只比對 open → 每次 daemon 重啟都把已 resolved 的卡復活成新 open 卡，
    # 致 ledger 反覆 open_decisions=1 / should_nudge=true，對「分潤比例」這種使用者
    # 早有定論、且 Phase 0 前根本不啟動的題目反覆騷擾。改用 seed_once 治本：曾拍板過
    # 就不復活；日後條件成熟要重議，走明確新 key 或人工重開。
    add_decision(
        key="revenue_waterfall_v1",
        title="收益分配四階段瀑布是否核准",
        detail=("章程建議：30% 發起人／50% 貢獻者／20% 公益，但<b>改成四階段瀑布</b> —\n"
                "①先扣成本 ②預留池上限 6 個月成本 ③發起人代墊回補 ④才套 30/50/20；\n"
                "且 <b>Phase 0 解鎖前完全不啟動任何分潤</b>。"),
        options=["核准此版本", "調整比例（告訴我新數字）", "暫不定案，先擱置"],
        seed_once=True,
    )
    # v42：舊「15x→3x 一刀切」決策已被「依預算分級」框架取代 → 退場改種新版
    for it in list_open():
        if it["key"] == "default_leverage_3x":
            resolve(it["id"], "已由 v42『依預算自適應風控分級』取代（見 leverage_tier_v42）")
    # v50：leverage_tier_v42 由使用者 2026-06-16 明確授權 CEO 全權決定
    #      （原話「這部分就交給你，由你進行專業的分析與評估就好」）。
    #      CEO 專業評估結論 ＝ 維持現狀（明確 15x／$100 不動）。理由：
    #        ① 出廠 tier 預設本就保守（standard 5x／1.0%），自架者預設即受保護，
    #           安全目的已達成，無需去蓋使用者個人旋鈕；
    #        ② 15x／$100 是使用者個人部署的明確選擇，且純紙上、零真錢，
    #           v44 後槓桿為純顯示、不改變交易數學 → 留著無害（勿為改而改）；
    #        ③ 真問題是「生效值被藏起來」而非「數值錯」→ 治本＝CEO 日報透明化（task #21），
    #           已落地於 ceo_session._section_normal 的「生效風控」行。
    #      故不再把此題當待拍板事項呈現；已 open 者就地標記為「依授權維持現狀」。
    #      註：此非 AI 自行拍板——是記錄使用者的明確授權 + CEO 專業判斷；且本題純紙上、
    #      非真錢/非對外/非 Phase0 解鎖，不觸及三條永久紅線。
    for it in list_open():
        if it["key"] == "leverage_tier_v42":
            resolve(it["id"],
                    "使用者 2026-06-16 授權 CEO 全權決定 → CEO 評估：維持現狀"
                    "（明確 15x／$100 不動；純紙上零真錢；治本以日報透明化處理）")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    seed_known_decisions()
    import re
    print(f"decisions.json -> {_PATH}")
    print(re.sub(r"<[^>]+>", "", render_open()))
