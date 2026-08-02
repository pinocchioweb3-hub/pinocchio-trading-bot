"""訊號預檢閘（v219）── 出卡前對「數據面到底有沒有料」做確定性盤點。

與 v217/v218 的差別（為什麼逐點治本還不夠）：
    v217/v218 把每一個**列印點**都改誠實了——缺料印 n/a、算不出來就明講「這不是確認沒有」。
    但整張卡**沒有總帳**：證據桶全亮的卡，跟只剩一個桶還活著的卡，讀起來一樣權威，
    最後都用同一種語氣寫出 entry/stop/tp。使用者說「感覺都不太對」的落點就在這裡。

    更關鍵的是 synthesizer.py 的訊號模式規則本身就寫著「獨立確認 ≥2 桶」——
    但**這個「桶」的數目從來沒有人算給 LLM 看**。一條數值門檻，被交給讀者目測。
    本模組把那個數目算出來、印出來，並在「一個桶都沒有」時於程式端強制 actionable=False。

刻意不做的事（避免把「補上觀測」偷渡成「改變政策」）：
    ⛔ 不改 SIGNAL_MODE 的門檻語意，也不因為 data_n 低於門檻就擋卡——那會在沒有證據支持的
       情況下減少樣本數，而樣本數正是目前唯一的瓶頸（n≥30 才有裁決權）。低於門檻只標註、
       只記錄進 meta，留待日後**用資料**裁決該不該硬擋。
    ⛔ 只有 data_n == 0（或盤點本身失敗）才硬擋。那不是我新訂的門檻，是使用者三步法的
       第二步「數據面佐證」在物理上無法執行——零個桶就沒有東西可以佐證。
    ⛔ 不擋卡片本身、不讓標的整個消失（那會被讀成「這輪沒訊號」＝本專案重犯 N 次的物種）；
       只降級 actionable 並在文末明講降級原因。

fail-closed 的方向：
    盤點不出來（sym_state 壞掉／型別不對）⇒ status="unknown" ⇒ 與 0 同樣硬擋，**但措辭必須
    是「盤點失敗」而不是「沒有數據」**。⛔ 不可回一個漂亮的 0 或 4。
"""
from __future__ import annotations

# 數據面四桶＝使用者三步法第二步的核心（OI／資金費率／多空比／CVD）。
# ⛔ 清算不列入必要桶：CoinGlass 停權後它結構性長期 n/a，列入等於每張卡都硬擋。
_DATA_BUCKETS = (
    ("oi", "OI 未平倉"),
    ("funding", "資金費率"),
    ("ls_ratio", "多空比"),
    ("cvd", "CVD"),
)

STATUS_OK = "ok"
STATUS_UNKNOWN = "unknown"          # 盤點失敗（⛔ 不等於「沒有數據」）


def _nonempty_list(v) -> bool:
    return isinstance(v, (list, tuple)) and len(v) > 0


def _bucket_state(key: str, cg: dict, snap: dict) -> tuple[bool, str]:
    """回 (有沒有真值, 來源標籤)。真值＝有量到，量到 0 也是真值。"""
    if key == "oi":
        if _nonempty_list(cg.get("oi")):
            return True, "CoinGlass"
        if snap.get("oi") is not None:
            return True, "備援"
    elif key == "funding":
        if cg.get("funding") is not None:
            return True, "CoinGlass"
        if snap.get("funding") is not None:
            return True, "備援"
    elif key == "ls_ratio":
        if cg.get("ls_ratio") is not None:
            return True, "CoinGlass"
        if snap.get("ls_ratio") is not None or snap.get("top_trader_ratio") is not None:
            return True, "備援"
    elif key == "cvd":
        if _nonempty_list(cg.get("cvd")) or cg.get("cvd_slope") is not None:
            return True, "CoinGlass"
        if snap.get("cvd_slope") is not None:
            return True, "備援"
    return False, ""


def _form_state(sym_state: dict) -> tuple[bool, str]:
    """型態面（三步法第一步）有沒有東西可看。回 (ok, 說明)。"""
    got = []
    pattern = sym_state.get("pattern") or {}
    if isinstance(pattern, dict) and not pattern.get("error") and pattern.get("consensus"):
        got.append("多時框型態")
    smc = sym_state.get("smc_levels") or {}
    if isinstance(smc, dict):
        for tf in ("4h", "1d"):
            lv = smc.get(tf)
            if isinstance(lv, dict) and lv and not lv.get("error"):
                got.append(f"SMC {tf}")
    if got:
        return True, "、".join(got)
    return False, "多時框型態與 SMC 4h/1d 這輪都沒有可用結構"


def preflight_verdict(sym_state) -> dict:
    """確定性盤點；純函式、零 I/O。任何盤點不了的情況一律回 STATUS_UNKNOWN。"""
    try:
        cg = sym_state.get("coinglass") or {}
        snap = sym_state.get("snapshot") or {}
        if not isinstance(cg, dict) or not isinstance(snap, dict):
            raise TypeError("coinglass/snapshot 型別非 dict")
        if snap.get("error"):
            snap = {}                      # 快照自報壞掉＝這一路的值都不可採信
        buckets = {}
        for key, label in _DATA_BUCKETS:
            live, src = _bucket_state(key, cg, snap)
            buckets[key] = {"label": label, "live": live, "source": src}
        form_ok, form_note = _form_state(sym_state)
    except Exception as e:                 # noqa: BLE001 — 任何例外都必須落在 unknown 而非 0
        return {
            "status": STATUS_UNKNOWN,
            "data_n": None,
            "buckets": {},
            "form_ok": None,
            "form_note": "",
            "block_actionable": True,
            "reason": (f"訊號預檢閘：資料盤點失敗（{type(e).__name__}: {e}）"
                       f"——⛔ 這是**盤點不出來**，不等於「沒有數據」。"
                       f"在盤點恢復前本輪不出可執行計畫。"),
        }

    data_n = sum(1 for b in buckets.values() if b["live"])
    if data_n == 0:
        reason = ("訊號預檢閘：數據面四桶（OI／資金費率／多空比／CVD）**這輪一個都沒有真值**"
                  "——三步法第二步「數據面佐證」在物理上無法執行，本輪不出可執行計畫。"
                  "⛔ 這是缺料，不等於「數據面中性」或「沒有訊號」。")
    elif not form_ok:
        reason = (f"訊號預檢閘：型態面這輪沒有可用結構（{form_note}）"
                  f"——三步法第一步「形態確認」無法執行，本輪不出可執行計畫。"
                  f"⛔ 這是缺料，不等於「沒有型態」。")
    else:
        reason = ""

    return {
        "status": STATUS_OK,
        "data_n": data_n,
        "buckets": buckets,
        "form_ok": form_ok,
        "form_note": form_note,
        "block_actionable": bool(reason),
        "reason": reason,
    }


def render_preflight_block(verdict: dict) -> str:
    """給 LLM 看的總帳。放在 prompt 前段——模式規則講「≥2 桶」時要有數目可讀。"""
    if verdict.get("status") == STATUS_UNKNOWN:
        return ("## 🧮 訊號預檢閘：⚠️ **資料盤點失敗**"
                "——⛔ 這不是「數據面沒東西」，是這一輪盤點不出來。"
                f"\n{verdict.get('reason', '')}\n")
    lines = [f"## 🧮 訊號預檢閘（確定性盤點，非目測）："
             f"數據面 **{verdict['data_n']}/4 桶**有真值"]
    for b in verdict.get("buckets", {}).values():
        lines.append(f"- {b['label']}：" +
                     (f"✅ 有值（{b['source']}）" if b["live"]
                      else "❌ 這輪無真值（⛔ 缺料，不等於中性／不等於 0）"))
    lines.append(f"- 型態面：" + ("✅ " + verdict["form_note"] if verdict["form_ok"]
                                 else "❌ " + verdict["form_note"]))
    lines.append("（上面這個桶數就是訊號模式規則所說的「獨立確認 N 桶」——"
                 "請直接用這個數字判斷，不要自己重數，也不要把 ❌ 的桶當成中性證據。）")
    if verdict.get("block_actionable"):
        lines.append(f"\n⛔ **本輪強制 actionable=false**：{verdict['reason']}"
                     f"\n（仍請完整輸出盤面觀察與「要等什麼條件才值得做」，"
                     f"但 PLAN_JSON 的 actionable 必須是 false。）")
    return "\n".join(lines) + "\n"


def enforce_plan(plan: dict | None, verdict: dict) -> dict | None:
    """程式端強制：閘說不行就不行。⛔ 不靠 LLM 自律（prompt 是請求，不是把關）。"""
    if not isinstance(plan, dict) or not verdict.get("block_actionable"):
        return plan
    if plan.get("actionable"):
        plan["preflight_downgraded"] = True
    plan["actionable"] = False
    plan["preflight_reason"] = verdict.get("reason", "")
    return plan
