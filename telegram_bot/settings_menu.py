"""v27: 互動式設定選單（自助餐式）— /settings 指令 + inline 按鈕。

讓用戶（或管理員）用 Telegram 按鈕即時自訂：
    • 單筆風險：固定 $ 或 帳戶 %（1/2/3/5%）
    • 最多同時持倉：3 / 5 / 10 / 20
    • 啟用策略：逐項開關（自助餐）

寫入走 botconfig.set_override → bot_settings.json（執行期覆寫，重啟保留），
即時 reload，下一筆訊號就套用。開源用戶各自 self-host 同一套選單。
"""
from __future__ import annotations


def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def render_settings() -> tuple[str, list[list[dict]]]:
    from botconfig import CONFIG, get_str
    from l2_trigger.registry import REGISTRY, enabled_strategies

    enabled_ids = {m.id for m in enabled_strategies()}
    if CONFIG.risk_per_trade_pct > 0:
        risk_line = (f"帳戶 <b>{CONFIG.risk_per_trade_pct:g}%</b>"
                     f"（≈ ${CONFIG.risk_per_trade_usd:,.0f}／筆，本金 ${CONFIG.account_balance_usd:,.0f}）")
    else:
        risk_line = f"固定 <b>${CONFIG.risk_per_trade_usd:,.0f}</b>／筆"

    text = (
        "⚙️ <b>交易設定（自助餐）</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        f"💰 單筆風險（1R）：{risk_line}\n"
        f"📊 最多同時持倉：<b>{CONFIG.max_concurrent_trades}</b> 倉\n"
        f"🎯 啟用策略：<b>{len(enabled_ids)}</b> 個\n"
        "\n<i>點按鈕即時調整，立刻套用到下一筆訊號。</i>"
    )

    buttons: list[list[dict]] = []
    # 風險 % 預設
    buttons.append([_btn("風險 1%", "set:riskpct:1"), _btn("2%", "set:riskpct:2"),
                    _btn("3%", "set:riskpct:3"), _btn("5%", "set:riskpct:5")])
    buttons.append([_btn("固定 $100", "set:riskusd:100"),
                    _btn("$50", "set:riskusd:50"), _btn("$200", "set:riskusd:200")])
    # 最多持倉
    buttons.append([_btn("倉位 3", "set:max:3"), _btn("5", "set:max:5"),
                    _btn("10", "set:max:10"), _btn("20", "set:max:20")])
    # 策略開關（每列一個，含勾選狀態）
    mat = {"live": "🟢", "paper": "🧪", "experimental": "🔬"}
    for m in REGISTRY.values():
        on = "✅" if m.id in enabled_ids else "⬜"
        buttons.append([_btn(f"{on} {mat.get(m.maturity,'')} {m.display_name_zh}",
                             f"set:strat:{m.id}")])
    buttons.append([_btn("🔄 重新整理", "set:refresh")])
    return text, buttons


async def handle_settings_callback(tg, cq: dict) -> bool:
    """處理 set: 前綴按鈕。回 True=已處理。"""
    data = cq.get("data") or ""
    if not data.startswith("set:"):
        return False
    import os

    from botconfig import CONFIG, get_str, set_override
    cq_id = cq.get("id", "")
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    note = ""

    try:
        if action == "riskpct":
            set_override("RISK_PER_TRADE_PCT", float(parts[2]))
            note = f"單筆風險改為帳戶 {parts[2]}%"
        elif action == "riskusd":
            set_override("RISK_PER_TRADE_PCT", 0)   # 關閉 % 模式
            set_override("RISK_PER_TRADE_USD", float(parts[2]))
            note = f"單筆風險改為固定 ${parts[2]}"
        elif action == "max":
            set_override("MAX_CONCURRENT_TRADES", int(parts[2]))
            note = f"最多持倉改為 {parts[2]} 倉"
        elif action == "strat":
            sid = parts[2]
            from l2_trigger.registry import REGISTRY, enabled_strategies
            cur = {m.id for m in enabled_strategies()}
            if sid in cur:
                cur.discard(sid)
            else:
                cur.add(sid)
            set_override("STRATEGIES_ENABLED", ",".join(sorted(cur)))
            nm = REGISTRY[sid].display_name_zh if sid in REGISTRY else sid
            note = f"{'啟用' if sid in cur else '停用'} {nm}"
        elif action == "refresh":
            note = "已重新整理"
    except Exception as e:
        note = f"設定失敗：{type(e).__name__}"

    try:
        await tg.answer_callback_query(cq_id, note or "已更新")
    except Exception:
        pass
    # 重繪選單
    try:
        text, buttons = render_settings()
        await tg._post("editMessageText", {
            "chat_id": chat_id, "message_id": message_id, "text": text,
            "parse_mode": "HTML", "reply_markup": {"inline_keyboard": buttons}})
    except Exception:
        pass
    return True


if __name__ == "__main__":
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    import re
    t, b = render_settings()
    print(re.sub(r"<[^>]+>", "", t))
    for row in b:
        print("  " + " | ".join(x["text"] for x in row))
