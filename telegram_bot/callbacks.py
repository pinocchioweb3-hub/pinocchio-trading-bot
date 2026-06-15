"""v15 互動 worker：FIRE 按鈕回調 + Telegram 指令（單一 getUpdates consumer）。

職責：
    1. callback_query：「✅已下單」/「⏭略過」按鈕 → trade_journal 狀態機
    2. message 指令：/status 儀表板、/stats 績效、/help
    3. 短輪詢（timeout=0 + sleep）— 此網路環境長輪詢必死（v14.2 教訓）

安全：
    - 只接受授權 chat（私聊 chat_id + forum 群組 id）
    - 整個 daemon 只能有這一個 getUpdates consumer
      （setup_telegram_group.py 只在 daemon 停止時手動跑）
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time

from .client import TelegramClient
from .topics import load_topics_config


def _authorized_chats() -> set[str]:
    import os
    allowed = set()
    cid = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if cid:
        allowed.add(cid)
    cfg = load_topics_config()
    if cfg:
        allowed.add(str(cfg["group_chat_id"]))
    return allowed


# ===========================================================================
# 按鈕處理
# ===========================================================================
async def _handle_callback(tg: TelegramClient, cq: dict) -> None:
    """處理一次按鈕點擊。callback_data 格式：'fill:{fire_id}' / 'skip:{fire_id}'"""
    from l3_dispatcher.trade_journal import confirm_trade, find_trade_by_fire, skip_trade

    cq_id = cq.get("id", "")
    data = cq.get("data", "") or ""
    msg = cq.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    message_id = msg.get("message_id")

    if chat_id not in _authorized_chats():
        await tg.answer_callback_query(cq_id, "未授權")
        return

    parts = data.split(":")
    action = parts[0] if parts else ""

    # 舊版按鈕（filled:SYM:setup / details:...）→ 告知已棄用
    if action in ("filled", "details") or (action == "skip" and len(parts) == 3):
        await tg.answer_callback_query(
            cq_id, "此為舊版按鈕（升級前的訊號），請以新訊號為準", show_alert=True)
        return

    if action not in ("fill", "skip") or len(parts) != 2:
        await tg.answer_callback_query(cq_id, f"未知操作 {data[:30]}")
        return

    try:
        fire_id = int(parts[1])
    except ValueError:
        await tg.answer_callback_query(cq_id, "按鈕資料損壞")
        return

    trade = find_trade_by_fire(fire_id)
    if not trade:
        await tg.answer_callback_query(cq_id, f"找不到對應交易（fire #{fire_id}）",
                                       show_alert=True)
        return

    sym, direction = trade["symbol"], trade["direction"]

    if action == "fill":
        result = confirm_trade(trade["id"])
        if result["ok"]:
            await tg.answer_callback_query(cq_id, f"✅ {sym} 已記錄為持倉，開始監控")
            # 移除按鈕 + 回覆確認（讓歷史一眼看懂這筆的結局）
            if message_id:
                try:
                    await tg.edit_message_reply_markup(chat_id, message_id, None)
                except Exception:
                    pass
                follow = (f"✅ <b>#{trade['id']} {sym} {direction.upper()} 已確認進場</b>\n"
                          f"進場價 <code>${trade['entry_price']}</code>（訊號價）\n"
                          f"trade_monitor 開始每 15 分鐘盯 TP/SL")
                try:
                    await tg.send_message(follow, parse_mode="HTML",
                                         message_thread_id=msg.get("message_thread_id"),
                                         reply_to_message_id=message_id)
                except Exception:
                    pass
        else:
            await tg.answer_callback_query(cq_id, f"⚠️ {result['msg']}", show_alert=True)

    elif action == "skip":
        result = skip_trade(trade["id"])
        if result["ok"]:
            await tg.answer_callback_query(cq_id, f"⏭ {sym} 已標記略過")
            if message_id:
                try:
                    await tg.edit_message_reply_markup(chat_id, message_id, None)
                except Exception:
                    pass
        else:
            await tg.answer_callback_query(cq_id, f"⚠️ {result['msg']}", show_alert=True)

    print(f"[callbacks] {action} fire#{fire_id} {sym}/{direction} -> done")


# ===========================================================================
# 指令處理
# ===========================================================================
async def _cmd_status() -> str:
    """/status 系統儀表板 — 一眼看懂全系統現況"""
    from l3_dispatcher.fire_queue import stats as queue_stats
    from l3_dispatcher.risk_manager import get_risk_status
    from l3_dispatcher.trade_journal import (
        get_open_trades, get_pending_signals, get_today_pnl, get_week_pnl,
    )

    risk = get_risk_status()
    opens = get_open_trades()
    pending = get_pending_signals()
    today = get_today_pnl()
    week = get_week_pnl()
    q = queue_stats()

    icon = {"active": "🟢", "paused_daily": "🟡", "halted_weekly": "🔴"}.get(
        risk["status"], "⚪")
    now_ms = int(time.time() * 1000)

    lines = [
        f"📋 <b>系統儀表板</b>　{icon} <code>{risk['status'].upper()}</code>",
        f"━━━━━━━━━━━━━━━━",
        f"💰 今日 PnL <code>${today['total_pnl_usd']:+.2f}</code>"
        f"（{today['pnl_pct_of_account']:+.2f}%）　"
        f"本週 <code>${week['total_pnl_usd']:+.2f}</code>（{week['pnl_pct_of_account']:+.2f}%）",
        f"🛡 熔斷：日 <code>{risk['daily_dd_limit_pct']}%</code> / "
        f"週 <code>{risk['weekly_dd_limit_pct']}%</code>",
        "",
        f"📊 <b>持倉 {len(opens)}/{risk['max_concurrent']}</b>",
    ]
    if opens:
        for o in opens:
            age_h = (now_ms - o["entry_at"]) / 3600000
            legs = f"已過 {','.join(sorted(o['legs_hit']))}" if o["legs_hit"] else "未觸發"
            lines.append(f"  • {o['symbol']} {o['direction']} "
                         f"@<code>${o['entry_price']}</code>（{age_h:.1f}h，{legs}）")
    else:
        lines.append("  （無持倉）")

    lines.append("")
    lines.append(f"⏳ <b>待確認訊號 {len(pending)}</b>")
    if pending:
        for s in pending:
            age_m = (now_ms - s["entry_at"]) / 60000
            lines.append(f"  • {s['symbol']} {s['direction']} "
                         f"@<code>${s['entry_price']}</code>（{age_m:.0f} 分鐘前，4h 後自動過期）")
    else:
        lines.append("  （無）")

    # v16: 紙上驗證進度（引擎期望值 / 自動交易 Stage 1 門檻）
    from l3_dispatcher.paper_journal import get_paper_stats, render_paper_summary
    lines.append("")
    lines.append(render_paper_summary(get_paper_stats(30)))

    # v18: 市場廣度（全市場 356 檔掃描器）
    try:
        from l3_dispatcher.market_scanner import get_latest_breadth, render_breadth_line
        lines.append(render_breadth_line(get_latest_breadth()))
    except Exception:
        pass

    # v31: 自動化 Session 狀態（讓使用者一眼看到全自動在跑）
    lines.append("")
    lines.append("🔄 <b>自動化 Session</b>")
    try:
        import sqlite3 as _sq
        from botpaths import db_path as _dbp
        nowi = int(time.time())

        def _ago(ts):
            if not ts:
                return "—"
            m = (nowi - ts) / 60
            return f"{m:.0f}分前" if m < 90 else f"{m/60:.1f}h前"
        # 稽核
        try:
            c = _sq.connect(_dbp("message_audit.db"))
            au = c.execute("SELECT MAX(sent_at) FROM send_log").fetchone()[0]
            ab = c.execute("SELECT COUNT(*) FROM send_log WHERE blocked=1 AND sent_at>?",
                           (nowi - 86400,)).fetchone()[0]
            c.close()
            lines.append(f"  🔍 稽核：運行中（最近 {_ago(au)}，24h 攔截重複 {ab}）")
        except Exception:
            lines.append("  🔍 稽核：運行中")
        # 敘事
        try:
            c = _sq.connect(_dbp("narrative.db"))
            nu = c.execute("SELECT MAX(last_updated) FROM narratives").fetchone()[0]
            nc = c.execute("SELECT COUNT(*) FROM narratives WHERE status='active'").fetchone()[0]
            c.close()
            lines.append(f"  🧩 敘事：{nc} 條主軸（最近 {_ago(nu)}）")
        except Exception:
            lines.append("  🧩 敘事：運行中")
        # 回測 Session（每週歷史回放，顯示最近一次最強策略結果）
        try:
            from backtest.backtest_session import latest_backtest
            bt = latest_backtest("intraday")
            if bt and bt.get("n_trades"):
                ba = _ago(bt["run_ts"] // 1000)
                lines.append(f"  🔬 回測：intraday {bt['n_trades']}筆 期望"
                             f"{bt['expectancy_r']:+.2f}R／勝率{bt['win_rate']*100:.0f}%"
                             f"（{ba}）")
            else:
                lines.append("  🔬 回測：每週一 11:00 台北（首跑待排程）")
        except Exception:
            lines.append("  🔬 回測：每週一 11:00 台北")
        lines.append("  ⚙️ 調參：每日 10:00 台北　🩺 健康：每 5 分　📊 紙上回測：即時")
    except Exception:
        pass

    lines.append("")
    lines.append(f"📬 Queue：{q}")
    lines.append(f"🕒 {dt.datetime.now().strftime('%Y-%m-%d %H:%M')} "
                 f"（每日績效 08:00 台北推送）")
    return "\n".join(lines)


async def _cmd_stats(args: list[str]) -> str:
    """/stats [days] 績效統計"""
    from l3_dispatcher.trade_journal import get_stats, render_stats_summary
    try:
        days = int(args[0]) if args else 7
    except ValueError:
        days = 7
    days = max(1, min(days, 365))
    return render_stats_summary(get_stats(days), label=f"📊 績效統計")


async def _cmd_profit(args: list[str]) -> str:
    """/profit [天數] 公開績效卡（紙上驗證階段，誠實版）。"""
    from l3_dispatcher.paper_journal import get_paper_stats, get_paper_funnel
    try:
        days = int(args[0]) if args else 30
    except ValueError:
        days = 30
    days = max(1, min(days, 365))

    def _card(title: str, s: dict) -> str:
        pf = s.get("profit_factor")
        pf_s = f"{pf}" if pf is not None else "∞"
        return (f"{title}：{s['n_closed']} 筆平倉｜勝率 {s['win_rate_pct']}%｜"
                f"期望值 {s['avg_r']:+.2f}R｜PF {pf_s}｜累計 {s['total_pnl_usd']:+.0f}U"
                f"（進行中 {s['n_open']}）")
    crypto = get_paper_stats(days, setup_not="us_breakout")
    us = get_paper_stats(days, setup="us_breakout")
    try:
        funnel = get_paper_funnel(days, setup_not="us_breakout")
        fline = (f"漏斗（加密）：提出 {funnel.get('proposed', '?')}／真正進場 "
                 f"{funnel.get('entered', '?')}／未成交 {funnel.get('never_filled', '?')}")
    except Exception:
        fline = ""
    lines = [f"📊 <b>公開績效卡</b>（近 {days} 天・<b>紙上驗證階段、未動真錢</b>）",
             "━━━━━━━━━━━━━━━━",
             _card("🪙 加密", crypto),
             _card("🇺🇸 美股", us)]
    if fline:
        lines.append(fline)
    lines.append(f"Stage1 門檻：{crypto.get('stage0_progress')}"
                 f"（紙上100筆＋實倉30筆正期望才動真錢）")
    lines.append("<i>紙上模擬、回測已扣手續費滑點；不保證獲利、連輸的單都公開。</i>")
    return "\n".join(lines)


def _cmd_help() -> str:
    return (
        "🤖 <b>可用指令</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        "⚙️ /settings — <b>自助餐設定</b>（風險 %/倉位/策略開關，按鈕即時調）\n"
        "📋 /strategies — 完整策略清單（已驗證/紙上/開發中）\n"
        "/status — 系統儀表板（持倉/待確認/PnL/風控）\n"
        "/stats [天數] — 績效統計（預設 7 天）\n"
        "📊 /profit [天數] — 公開績效卡（勝率/期望值/PF，加密+美股分開）\n"
        "🎯 /discipline — 紀律遵守率（決斷率 + 不追高率，系統客觀記錄）\n"
        "/daily ・ /weekly — 當日／本週績效\n"
        "/contrib — 💡 貢獻積分排行榜\n"
        "/myscore — 查自己的積分與占比\n"
        "🧭 /ceo — CEO 每日彙整簡報（今日重點 + 需決策）\n"
        "🧭 /decisions — 待你拍板的事項｜/decided &lt;編號&gt; 標記已決定\n"
        "📤 /approve — 列出/核准待審對外內容｜/reject &lt;編號&gt; 退回\n"
        "/help — 本清單\n\n"
        "💡 在「意見箱」主題發建議即累積積分（採納實裝才給分）\n"
        "FIRE 訊號按鈕：✅ 已下單 → 計入持倉｜⏭ 略過 → 不記錄"
    )


def _ideas_thread_id() -> int | None:
    cfg = load_topics_config()
    return (cfg or {}).get("topics", {}).get("ideas")


async def _handle_suggestion(tg: TelegramClient, msg: dict) -> None:
    """v19.1 採納制：記錄 → AI 評估 → 高分通知管理者。發言本身不給分。"""
    import asyncio as _aio
    import html as _html
    import os

    from .community import ai_evaluate_suggestion, record_suggestion
    user = msg.get("from") or {}
    user_id = user.get("id")
    if not user_id or user.get("is_bot"):
        return
    username = user.get("username") or user.get("first_name") or f"user{user_id}"
    text = (msg.get("text") or "").strip()
    if not text or text.startswith("/"):
        return
    r = record_suggestion(user_id, username, text, msg_id=msg.get("message_id"))
    if r["reason"] == "too_short":
        reply = "ℹ️ 已留存。建議寫得具體一點（≥10 字）才會送 AI 評估哦"
    elif r["reason"] == "cooldown":
        reply = f"ℹ️ 建議 #{r['id']} 已留存（每小時送 AI 評估 1 則，稍後的建議仍會被看到）"
    else:
        reply = (f"📥 建議 <b>#{r['id']}</b> 已收錄，AI 評估中…\n"
                 f"<i>提醒：積分在建議被採納並完成更新後發放（更新公告會註明出自你）</i>")
    thread_id = msg.get("message_thread_id")
    try:
        await tg.send_message(reply, parse_mode="HTML",
                             message_thread_id=thread_id,
                             reply_to_message_id=msg.get("message_id"))
    except Exception as e:
        print(f"[callbacks] suggestion reply error: {e}")
    print(f"[callbacks] suggestion #{r['id']} from {username} eval={r['eligible_for_eval']}")

    # 背景 AI 評估（不阻塞 listener 處理按鈕）
    if r["eligible_for_eval"]:
        async def _eval_task(sid: int):
            try:
                ev = await ai_evaluate_suggestion(sid)
                if not ev:
                    return
                # 回意見箱貼評估摘要
                try:
                    await tg.send_message(
                        f"🤖 建議 #{sid} AI 初評：<b>{_html.escape(ev['verdict'])}</b>"
                        f"（價值 {ev['value']}/10）\n<i>{_html.escape(ev['comment'][:200])}</i>",
                        parse_mode="HTML", message_thread_id=thread_id)
                except Exception:
                    pass
                # 高潛力 → 私訊管理者
                if ev["value"] >= 7:
                    try:
                        admin = TelegramClient()  # 預設 = 管理者私聊
                        await admin.send_message(
                            f"💡 <b>高潛力建議待審核 #{sid}</b>（{_html.escape(ev['username'] or '')}，"
                            f"價值 {ev['value']}/10）\n{_html.escape(ev['comment'][:200])}\n"
                            f"採納指令：<code>/adopt {sid} 分數 說明</code>",
                            parse_mode="HTML")
                    except Exception:
                        pass
            except Exception as e:
                print(f"[callbacks] ai eval error: {e}")
        _aio.create_task(_eval_task(r["id"]))


async def _handle_command(tg: TelegramClient, msg: dict) -> None:
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    if chat_id not in _authorized_chats():
        return
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        # v19: 意見箱主題的一般訊息 → 建議記錄
        ideas_tid = _ideas_thread_id()
        if ideas_tid and msg.get("message_thread_id") == ideas_tid:
            await _handle_suggestion(tg, msg)
        return
    cmd = text.split()[0].lower().lstrip("/").split("@")[0]
    args = text.split()[1:]

    reply: str | None = None
    if cmd == "status":
        reply = await _cmd_status()
    elif cmd == "stats":
        reply = await _cmd_stats(args)
    elif cmd == "profit":
        reply = await _cmd_profit(args)
    elif cmd == "daily":
        reply = await _cmd_profit(["1"])
    elif cmd == "weekly":
        reply = await _cmd_profit(["7"])
    elif cmd == "contrib":
        from .community import render_leaderboard
        reply = render_leaderboard()
    elif cmd == "myscore":
        from .community import get_user_score
        uid = (msg.get("from") or {}).get("id")
        s = get_user_score(uid) if uid else None
        reply = (f"💡 {s['username']}：<b>{s['points']}</b> 分"
                 f"（占比 {s['share_pct']}%，採納 {s['adopted']} 次）"
                 if s else "還沒有積分 — 到 💡 意見箱發建議就會開始累積")
    elif cmd == "adopt":
        # 僅限管理者：/adopt <id> [分數1-10] [說明…] — 採納實裝才給分
        import os
        if str((msg.get("from") or {}).get("id", "")) == os.getenv("TELEGRAM_CHAT_ID", ""):
            from .community import ADOPT_DEFAULT_POINTS, mark_adopted
            try:
                sid = int(args[0])
                pts = int(args[1]) if len(args) > 1 and args[1].isdigit() else ADOPT_DEFAULT_POINTS
                note = " ".join(args[2:]) if len(args) > 2 else (
                    " ".join(args[1:]) if len(args) > 1 and not args[1].isdigit() else "")
                r = mark_adopted(sid, points=pts, note=note)
                reply = (f"✅ 建議 #{sid} 已採納：{r['username']} +{r['points_awarded']} 分"
                         f"（累積 {r['total_points']}）" if r.get("ok") else f"⚠️ {r.get('msg')}")
            except (ValueError, IndexError):
                reply = "用法：/adopt <建議編號> [分數1-10] [實裝說明]"
        else:
            reply = "此指令僅限管理者"
    elif cmd in ("strategies", "strats"):
        from l2_trigger.registry import render_strategy_menu
        reply = render_strategy_menu()
    elif cmd in ("settings", "setting", "set"):
        # v27: 互動式自助餐設定選單（風險/倉位/策略）
        from .settings_menu import render_settings
        text, buttons = render_settings()
        try:
            await tg.send_message(text, parse_mode="HTML", inline_buttons=buttons,
                                  message_thread_id=msg.get("message_thread_id"))
        except Exception as e:
            print(f"[callbacks] settings menu error: {e}")
        return
    elif cmd == "gate_approve":
        # 僅限管理者：舊用戶人工放行（v22-2）
        import os
        if str((msg.get("from") or {}).get("id", "")) == os.getenv("TELEGRAM_CHAT_ID", ""):
            from .invite_gate import admin_approve
            reply = await admin_approve(tg, args)
        else:
            reply = "此指令僅限管理者"
    elif cmd == "review":
        # 僅限管理者：待審核的高潛力建議清單
        import os
        if str((msg.get("from") or {}).get("id", "")) == os.getenv("TELEGRAM_CHAT_ID", ""):
            import html as _html
            from .community import get_pending_review
            pending = get_pending_review(7)
            if not pending:
                reply = "📭 沒有待審核的高潛力建議（AI 評分 ≥7）"
            else:
                lines = ["💡 <b>待審核建議</b>（AI ≥7 分）", "━━━━━━━━━━━━━━━━"]
                for p in pending[:10]:
                    lines.append(f"<b>#{p['id']}</b>（{_html.escape(p['username'] or '')}，"
                                 f"{p['ai_score']}/10）{_html.escape(p['text'][:80])}")
                lines.append("\n採納：<code>/adopt id 分數 說明</code>")
                reply = "\n".join(lines)
        else:
            reply = "此指令僅限管理者"
    elif cmd == "ceo":
        # v40: 隨時拉一份 CEO 彙整簡報（兩段式：今日重點 + 需決策）
        try:
            from l3_dispatcher.ceo_session import build_ceo_brief
            reply = build_ceo_brief()
        except Exception as e:
            reply = f"CEO 簡報產生失敗：{type(e).__name__}: {e}"
    elif cmd in ("decisions", "decision"):
        # v40: 列出等發起人拍板的事項
        from l3_dispatcher.decision_registry import render_open
        reply = "🧭 <b>待決策事項</b>\n━━━━━━━━━━━━━━━━\n" + render_open()
    elif cmd == "decided":
        # v40 僅限管理者：/decided <id> [選了什麼說明] — 標記某決策已拍板
        import os
        if str((msg.get("from") or {}).get("id", "")) == os.getenv("TELEGRAM_CHAT_ID", ""):
            from l3_dispatcher.decision_registry import resolve
            try:
                did = int(args[0])
                note = " ".join(args[1:])
                r = resolve(did, note)
                reply = ("✅ " if r["ok"] else "⚠️ ") + r["msg"]
            except (ValueError, IndexError):
                reply = "用法：/decided <決策編號> [你的決定說明]"
        else:
            reply = "此指令僅限管理者"
    elif cmd == "approve":
        # v40 僅限管理者：/approve（無參數=列待審）/ /approve <id>=核准對外內容（紅線2）
        import os
        if str((msg.get("from") or {}).get("id", "")) == os.getenv("TELEGRAM_CHAT_ID", ""):
            from l3_dispatcher.outbox import approve, render_pending
            if not args:
                reply = render_pending()
            else:
                try:
                    r = approve(int(args[0]), " ".join(args[1:]))
                    reply = ("✅ " if r["ok"] else "⚠️ ") + r["msg"]
                except ValueError:
                    reply = "用法：/approve <編號>（或不帶參數列出待審）"
        else:
            reply = "此指令僅限管理者"
    elif cmd == "reject":
        # v40 僅限管理者：/reject <id> [原因] — 退回對外內容草稿
        import os
        if str((msg.get("from") or {}).get("id", "")) == os.getenv("TELEGRAM_CHAT_ID", ""):
            from l3_dispatcher.outbox import reject
            try:
                r = reject(int(args[0]), " ".join(args[1:]))
                reply = ("✅ " if r["ok"] else "⚠️ ") + r["msg"]
            except (ValueError, IndexError):
                reply = "用法：/reject <編號> [原因]"
        else:
            reply = "此指令僅限管理者"
    elif cmd in ("discipline", "kpi"):
        # v41: 紀律遵守率 KPI（決斷率 + 不追高率，皆由系統客觀記錄）
        try:
            from l3_dispatcher.trade_journal import discipline_stats, render_discipline
            reply = render_discipline(discipline_stats(30))
        except Exception as e:
            reply = f"紀律 KPI 產生失敗：{type(e).__name__}: {e}"
    elif cmd == "help":
        reply = _cmd_help()
    elif cmd == "setup":
        return  # setup 腳本的指令，daemon 忽略
    else:
        reply = f"❓ 未知指令 /{cmd}，輸入 /help 看清單"

    if reply:
        try:
            await tg.send_message(
                reply[:4000], parse_mode="HTML",
                message_thread_id=msg.get("message_thread_id"),
            )
        except Exception as e:
            print(f"[callbacks] reply error: {e}")
    print(f"[callbacks] handled /{cmd}")


# ===========================================================================
# Worker 主迴圈
# ===========================================================================
async def run_interactive_listener(tg: TelegramClient, poll_seconds: float = 2.0):
    """短輪詢 getUpdates，處理按鈕與指令。整個 daemon 唯一的 updates consumer。"""
    print(f"[callbacks] interactive listener online "
          f"(authorized: {sorted(_authorized_chats())}, poll={poll_seconds}s)")
    offset: int | None = None
    error_streak = 0

    while True:
        try:
            resp = await tg.get_updates(offset=offset, timeout=0)
            error_streak = 0
            # 409 = 另一個 getUpdates consumer 在搶（setup 腳本誤跑）→ 退讓重試
            if not resp.get("ok") and resp.get("error_code") == 409:
                print("[callbacks] 409 Conflict: 另一個程序在 getUpdates"
                      "（setup_telegram_group.py？）— 30s 後重試")
                await asyncio.sleep(30)
                continue
            if resp.get("ok"):
                for upd in resp.get("result", []):
                    offset = upd["update_id"] + 1
                    try:
                        if "callback_query" in upd:
                            # v22-2: gate: 前綴按鈕（私訊選單）優先給入群閘門
                            cq = upd["callback_query"]
                            if (cq.get("data") or "").startswith("gate:"):
                                from .invite_gate import handle_gate_callback
                                await handle_gate_callback(tg, cq)
                            elif (cq.get("data") or "").startswith("set:"):
                                from .settings_menu import handle_settings_callback
                                await handle_settings_callback(tg, cq)
                            else:
                                await _handle_callback(tg, cq)
                        elif "chat_join_request" in upd:
                            # v22-2: 入群申請 → 自動核對放行
                            from .invite_gate import handle_join_request
                            await handle_join_request(tg, upd["chat_join_request"])
                        elif "message" in upd:
                            m = upd["message"]
                            chat = m.get("chat") or {}
                            # v22-2: 私訊 → 先給入群閘門（/start 與流程中訊息）；
                            # 沒被消化的（管理員指令等）走原本路徑
                            if chat.get("type") == "private":
                                from .invite_gate import handle_private_message
                                consumed = await handle_private_message(tg, m)
                                if not consumed:
                                    await _handle_command(tg, m)
                            else:
                                await _handle_command(tg, m)
                    except Exception as e:
                        print(f"[callbacks] handle error: {type(e).__name__}: {e}")
        except Exception as e:
            error_streak += 1
            if error_streak in (1, 10, 50):
                print(f"[callbacks] poll error x{error_streak}: {type(e).__name__}")
            await asyncio.sleep(min(poll_seconds * error_streak, 30))
            continue
        await asyncio.sleep(poll_seconds)
