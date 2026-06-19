"""Trade Monitor Worker：每 N 分鐘 poll 所有 open trades 並判定 TP/SL/timeout。

設計：
- 每 15 分鐘掃所有 status='open' trades
- 對每筆，抓 OKX 5m 最近 4 根 candles（涵蓋 20 分鐘 = poll 間隔 + buffer）
- 用 max(high)/min(low) 判定觸發點：
    * bull：high ≥ tp1/2/3 → record_leg(tp, size)；low ≤ stop → record_leg(stop, remaining)
    * bear：low ≤ tp1/2/3 → record_leg(tp, size)；high ≥ stop → record_leg(stop, remaining)
- 進場後超過 hold_max_hours 且未平倉 → record_leg(timeout, remaining)

TP/SL 分批比例：
    TP1: 50%  TP2: 30%  TP3: 20%
    SL / timeout：剩餘全平

每次觸發都推 Telegram 通知（reply 到原 FIRE 訊息）。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import time
from dataclasses import dataclass
from typing import Any

from .trade_journal import get_open_trades, record_leg


# TP 分批比例（必須加總 = 1.0）— v23-2: botconfig 同源
from botconfig import CONFIG as _CFG

TP_SIZE_PCT = {"tp1": _CFG.tp_size_split[0], "tp2": _CFG.tp_size_split[1],
               "tp3": _CFG.tp_size_split[2]}

# 預設 hold_max_hours（intraday 用 48h，與 loose profile 一致）
DEFAULT_HOLD_MAX_HOURS = int(os.getenv("HOLD_MAX_HOURS", "48"))

# v17: per-setup 持倉時限（美股突破 24h）
HOLD_MAX_BY_SETUP = {"us_breakout": 24}

# v33: 分批限價掛單「從未成交（0% filled）」的逾時作廢時限。
#   超過此時數仍 pending 就取消（紙上，0R），避免未成交掛單永久佔用 open/pending 計數。
#   partial（已部分成交）不適用此限，改由 HOLD_MAX_* 的 timeout 流程處理。
PENDING_MAX_HOURS = int(os.getenv("PENDING_MAX_HOURS", "12"))


@dataclass
class MonitorEvent:
    """單一觸發事件"""
    trade_id: int
    symbol: str
    direction: str
    event: str               # 'tp1' / 'tp2' / 'tp3' / 'stop' / 'timeout'
    size_pct: float
    trigger_price: float
    realized_r: float
    cumulative_pnl_usd: float
    trade_closed: bool       # True 代表這筆 trade 已完全平倉
    tg_message_id: int | None


async def _get_recent_5m_bars(source, symbol: str, n: int = 4) -> dict[str, Any] | None:
    """抓最近 n 根 5m candles。回 None 代表失敗。"""
    try:
        from market_intel_mcp.sources.okx_candles import OkxCandlesSource
        # 若 source 是 CoinGlass，用獨立的 OKX client（OKX 5m 比 CoinGlass 即時）
        if not hasattr(source, "get_candles") or "okx" not in source.__class__.__name__.lower():
            okx = OkxCandlesSource()
            try:
                d = await okx.get_candles(symbol, "5m", n)
            finally:
                await okx.close()
        else:
            d = await source.get_candles(symbol, "5m", n)
        if not isinstance(d, dict) or "candles" not in d:
            return None
        return d
    except Exception as e:
        print(f"[trade_monitor] fetch 5m {symbol} error: {type(e).__name__}: {e}")
        return None


def _check_trade(trade: dict, bars: list[dict]) -> list[tuple[str, float, float]]:
    """純函式：給定 trade + 5m bars，回所有應觸發的 (event_label, trigger_price, size_pct) tuples。

    順序：先檢查 TP（依 entry 後價格高低）→ 再檢查 stop → 再檢查 timeout。
    若已 hit 的 leg label 就跳過。
    """
    direction = trade["direction"]
    legs_hit = set(trade["legs_hit"] or [])
    size_remaining = trade["size_remaining"]
    if size_remaining <= 0.001:
        return []  # 已完全平倉

    entry = trade["entry_price"]
    stop = trade["stop_price"]
    tps = {"tp1": trade.get("tp1"), "tp2": trade.get("tp2"), "tp3": trade.get("tp3")}

    # 4 根 5m 的高低聚合（涵蓋 poll 間隔）
    bar_high = max(b["high"] for b in bars)
    bar_low = min(b["low"] for b in bars)

    events: list[tuple[str, float, float]] = []

    # === TP 檢查（依序：TP1 → TP2 → TP3）===
    for tp_label, tp_price in tps.items():
        if tp_price is None or tp_label in legs_hit:
            continue
        size_pct = TP_SIZE_PCT[tp_label]
        if direction == "bull" and bar_high >= tp_price:
            events.append((tp_label, tp_price, size_pct))
            legs_hit.add(tp_label)
            size_remaining -= size_pct
        elif direction == "bear" and bar_low <= tp_price:
            events.append((tp_label, tp_price, size_pct))
            legs_hit.add(tp_label)
            size_remaining -= size_pct

    # === Stop 檢查（剩餘全平）===
    stop_triggered = False
    if direction == "bull" and bar_low <= stop:
        stop_triggered = True
    elif direction == "bear" and bar_high >= stop:
        stop_triggered = True

    if stop_triggered and "stop" not in legs_hit and size_remaining > 0.001:
        events.append(("stop", stop, round(size_remaining, 3)))

    return events


def _check_timeout(trade: dict, hold_max_hours: int = DEFAULT_HOLD_MAX_HOURS) -> tuple[float, float] | None:
    """檢查是否 timeout。回 (current_price_estimate, size_remaining) 或 None。"""
    entry_at_ms = trade["entry_at"]
    age_ms = int(time.time() * 1000) - entry_at_ms
    if age_ms < hold_max_hours * 3600 * 1000:
        return None
    if trade["size_remaining"] <= 0.001:
        return None
    return trade["size_remaining"]


async def monitor_once(source, tg=None, tg_fire=None, tg_us=None,
                       coach_state=None) -> list[MonitorEvent]:
    """掃一次所有 open trades（實倉 + 紙上）。回實倉觸發事件。

    coach_state: 由呼叫端維護的 in-memory dict（含 'seen' set）→ 啟用教練層節流。
                 None = 不跑教練（selftest 路徑保持純粹）。"""
    opens = get_open_trades()
    events: list[MonitorEvent] = []
    bars_cache: dict[str, list] = {}  # v16: 實倉/紙上共用同輪 K 線

    for trade in opens:
        sym = trade["symbol"]
        # 抓 5m bars（最近 4 根 = 20 分鐘窗口）
        d = await _get_recent_5m_bars(source, sym, n=4)
        if not d:
            continue
        bars = d["candles"]
        if not bars:
            continue
        bars_cache[sym] = bars

        # === TP/Stop 檢查 ===
        triggered = _check_trade(trade, bars)
        for event_label, trigger_price, size_pct in triggered:
            try:
                result = record_leg(
                    trade_id=trade["id"],
                    leg_label=event_label,
                    size_pct=size_pct,
                    exit_price=trigger_price,
                )
                events.append(MonitorEvent(
                    trade_id=trade["id"], symbol=sym, direction=trade["direction"],
                    event=event_label, size_pct=size_pct,
                    trigger_price=trigger_price,
                    realized_r=result["leg_r"],
                    cumulative_pnl_usd=result["cumulative_pnl_usd"],
                    trade_closed=(result["trade_status"] == "closed"),
                    tg_message_id=trade.get("tg_message_id"),
                ))
                # in-memory update（避免同一輪重複觸發）
                trade["legs_hit"].append(event_label)
                trade["size_remaining"] = round(trade["size_remaining"] - size_pct, 3)
            except Exception as e:
                print(f"[trade_monitor] record_leg {sym} {event_label} error: {e}")

        # === Timeout 檢查（在 TP/SL 之後，因為若已部分 TP 不應 timeout 全部）===
        remain = _check_timeout(trade)
        if remain is not None and remain > 0.001:
            # 用最後一根 close 作 exit price（timeout = 平倉收掉）
            last_close = bars[-1]["close"]
            try:
                result = record_leg(
                    trade_id=trade["id"],
                    leg_label="timeout",
                    size_pct=remain,
                    exit_price=last_close,
                )
                events.append(MonitorEvent(
                    trade_id=trade["id"], symbol=sym, direction=trade["direction"],
                    event="timeout", size_pct=remain,
                    trigger_price=last_close,
                    realized_r=result["leg_r"],
                    cumulative_pnl_usd=result["cumulative_pnl_usd"],
                    trade_closed=True,
                    tg_message_id=trade.get("tg_message_id"),
                ))
            except Exception as e:
                print(f"[trade_monitor] record_leg {sym} timeout error: {e}")

    # === 推 Telegram 通知 ===
    if tg and events:
        for ev in events:
            try:
                await _push_event(tg, ev)
            except Exception as e:
                print(f"[trade_monitor] push event error: {e}")

    # === v16: 紙上交易追蹤（每筆訊號自動開倉，驗證引擎期望值）===
    try:
        await _monitor_paper(source, tg, bars_cache, tg_us=tg_us)
    except Exception as e:
        print(f"[trade_monitor] paper monitor error: {type(e).__name__}: {e}")

    # === v18-D: 等待觸發檢查（價格回進場區 → 轉正式訊號）===
    try:
        await _check_waiting_trades(source, tg, bars_cache, tg_fire=tg_fire)
    except Exception as e:
        print(f"[trade_monitor] waiting check error: {type(e).__name__}: {e}")

    # === v41: 教練層（task #8 ⑤）— 凹單/追高/重壓/連續開倉的當下踩煞車 ===
    if coach_state is not None and tg is not None:
        try:
            await _run_coach(tg, bars_cache, coach_state)
        except Exception as e:
            print(f"[trade_monitor] coach error: {type(e).__name__}: {e}")

    return events


async def _run_coach(tg, bars_cache: dict[str, list], coach_state: dict) -> None:
    """教練層：只針對使用者真正下單的實倉（get_open_trades），於紀律破口的當下提醒。

    價格取自本輪已抓的 5m K 線（bars_cache 最後一根 close），不額外發網路請求。
    節流：coach_state['seen'] 記已推 key；每筆單每型一生一次、帳戶級每日一次。"""
    from .coach import build_coaching
    from .risk_manager import get_risk_status

    seen: set = coach_state.setdefault("seen", set())
    # 重新讀實倉狀態（本輪已平倉的單已從清單移除，避免對已關單的提醒）
    opens = get_open_trades()
    if not opens:
        # 仍要跑帳戶級教練（如「今天別再交易了」即使此刻無持倉）
        prices: dict[str, float] = {}
    else:
        prices = {}
        for o in opens:
            bars = bars_cache.get(o["symbol"])
            if bars:
                prices[o["symbol"]] = bars[-1]["close"]

    utc_date = dt.datetime.now(tz=dt.timezone.utc).strftime("%Y-%m-%d")
    try:
        risk = get_risk_status()
    except Exception as e:
        print(f"[trade_monitor] coach risk read error: {e}")
        risk = {}

    # 節流集合防無限長：清掉非今日的帳戶級 key（位置級 key 無日期，量小不清）
    for k in [x for x in seen
              if (x.startswith("daily_stop:") or x.startswith("exposure:"))
              and not x.endswith(utc_date)]:
        seen.discard(k)

    msgs = build_coaching(opens, prices, risk, utc_date)
    for m in msgs:
        if m.key in seen:
            continue
        try:
            await tg.send_message(m.text, parse_mode="HTML")
            seen.add(m.key)
            print(f"[trade_monitor] coach pushed: {m.kind} ({m.key})")
        except Exception as e:
            print(f"[trade_monitor] coach push error: {e}")


async def _check_waiting_trades(source, tg, bars_cache: dict[str, list],
                                tg_fire=None) -> None:
    """waiting trades：回進場區 → signal + 推正式 FIRE 訊息 + 開紙上倉；6h 未觸 → expired

    v22 路由修正：觸發成功的正式 FIRE（含按鈕）發 tg_fire（🎯 交易訊號主題），
    與 dispatcher 首發訊號同主題；逾時放棄等雜務留在 tg（📈 持倉與績效）。"""
    from .dispatcher import compute_entry_zone
    from .trade_journal import expire_stale_waiting, get_waiting_trades, trigger_waiting

    # 超時放棄（安靜一行）
    expired = expire_stale_waiting(6.0)
    if expired and tg is not None:
        names = ", ".join(f"{e['symbol']} {e['direction']}" for e in expired)
        try:
            await tg.send_message(
                f"⏳ <i>等待觸發逾時放棄（價格未回進場區）：{names}</i>",
                parse_mode="HTML")
        except Exception:
            pass

    waiting = get_waiting_trades()
    for w in waiting:
        sym = w["symbol"]
        bars = bars_cache.get(sym)
        if bars is None:
            d = await _get_recent_5m_bars(source, sym, n=1)
            bars = d["candles"] if d and d.get("candles") else None
            bars_cache[sym] = bars or []
        if not bars:
            continue
        live = bars[-1]["close"]
        zone_lo, zone_hi = compute_entry_zone(w["entry_price"], w["direction"], w["setup"])
        if not (zone_lo <= live <= zone_hi):
            continue

        # 觸發！waiting → signal
        r = trigger_waiting(w["id"])
        if not r.get("ok"):
            continue

        dir_zh = "做多" if w["direction"] == "bull" else "做空"
        text = (
            f"🔥 <b>{sym} {dir_zh} — 等待觸發成功！</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"價格已回到進場區 <code>${zone_lo:,.6g} – ${zone_hi:,.6g}</code>"
            f"（現價 <code>${live:,.6g}</code>）\n"
            f"進場：<code>${w['entry_price']:,.6g}</code> 附近\n"
            f"止損：<code>${w['stop_price']:,.6g}</code>\n"
            f"止盈：TP1 <code>${w['tp1']:,.6g}</code>(50%) → "
            f"TP2 <code>${w['tp2']:,.6g}</code>(30%) → "
            f"TP3 <code>${w['tp3']:,.6g}</code>(20%)\n\n"
            f"⏳ <i>請按按鈕回報：按「已下單」才會計入持倉與績效（4 小時未按自動過期）</i>"
        )
        buttons = [[
            {"text": "✅ 已下單", "callback_data": f"fill:{w['fire_id']}"},
            {"text": "⏭ 略過", "callback_data": f"skip:{w['fire_id']}"},
        ]]
        fire_client = tg_fire or tg   # v22: 正式訊號回到 🎯 交易訊號主題
        if fire_client is not None:
            try:
                await fire_client.send_message(text, parse_mode="HTML",
                                               inline_buttons=buttons)
            except Exception as e:
                print(f"[trade_monitor] waiting-trigger send error: {e}")

        # 紙上倉此時才開（觸發價 = 真實可成交的時點）
        try:
            from .paper_journal import record_paper_entry
            regime = "unknown"
            for t in (w.get("tags") or "").split(","):
                if t.startswith("regime:"):
                    regime = t.split(":", 1)[1]
            # v56 step1：觸發成交那刻凍結計畫快照（純觀測，失敗回 None 不阻塞建單）
            try:
                from .plan_snapshot import build_plan_snapshot
                # v56 step4：觸發成交當下補市場層 context（廣度/均資費，本地 DB 讀）。
                # 此刻才是真實進場時點，市場廣度比訊號當時更貼近實況。純觀測，零下單數學。
                from .regime_vector import assemble as _asm_rg
                _rv, _ctx = _asm_rg(None, direction=w["direction"])
                plan_snap = build_plan_snapshot(
                    source="waiting_trigger", direction=w["direction"],
                    entry_price=live, planned_stop=w["stop_price"],
                    tp1=w["tp1"], tp2=w["tp2"], tp3=w["tp3"],
                    fire_id=w["fire_id"], regime=regime,
                    regime_vector=_rv, context=_ctx)
            except Exception:
                plan_snap = None
            record_paper_entry(
                symbol=sym, setup=w["setup"], direction=w["direction"],
                entry_price=live, stop_price=w["stop_price"],
                tp1=w["tp1"], tp2=w["tp2"], tp3=w["tp3"],
                fire_id=w["fire_id"], regime=regime,
                # v47-2: 等待觸發是「先前已承諾的等待單兌現」，非新單 → 豁免 post-close 冷卻，
                #        否則同向同 setup 剛平倉時會誤殺正當觸發。
                skip_cooldown=True,
                plan_snapshot=plan_snap,
            )
        except Exception as e:
            print(f"[trade_monitor] waiting paper entry error: {e}")
        print(f"[trade_monitor] ⏳→🔥 waiting triggered: {sym} {w['direction']} @ {live}")


def _signal_link(tg, msg_id) -> str:
    """v33：建回連原始訊號的 t.me 連結（私有超級群 c/ 形式）。無 id 回空字串。"""
    if not msg_id:
        return ""
    try:
        cid = str(getattr(tg, "chat_id", "") or "")
        if cid.startswith("-100"):
            cid = cid[4:]
        elif cid.startswith("-"):
            cid = cid[1:]
        if not cid:
            return ""
        return f'　<a href="https://t.me/c/{cid}/{msg_id}">🔗原始訊號</a>'
    except Exception:
        return ""


async def _monitor_paper(source, tg, bars_cache: dict[str, list],
                         tg_us=None) -> None:
    """紙上倉位 TP/SL/timeout 判定。事件彙整成單則低噪訊息。

    v23-2 美股斷層修復：us_breakout 的事件發 tg_us（🇺🇸 美股主題）—
    進場通知在哪、出場事件就在哪；加密事件留 tg（📈 持倉與績效）。"""
    from .paper_journal import (apply_entry_fill, apply_paper_event, expire_pending,
                                get_open_paper, get_paper_stats, get_pending_entries)

    # v26: 先檢查分批限價單的進場成交 — 達到某格區間就推「進場進度」到持倉主題
    pending = get_pending_entries()
    now_ms = int(time.time() * 1000)
    for pe in pending:
        sym = pe["symbol"]
        bars = bars_cache.get(sym)
        if bars is None:
            d = await _get_recent_5m_bars(source, sym, n=4)
            bars = d["candles"] if d and d.get("candles") else None
            bars_cache[sym] = bars or []
        fill = None
        if bars:
            live = bars[-1]["close"]
            fill = apply_entry_fill(pe["id"], live)
            if fill and (tg or tg_us) is not None:
                dir_zh = "做多" if pe["direction"] == "bull" else "做空"
                filled_pct = int(fill["filled_pct"] * 100)
                done = "✅ 全部進場完成" if fill["state"] == "full" else f"⏳ 已進場 {filled_pct}%（其餘掛單等待中）"
                legs = "、".join(f"{int(s['frac']*100)}% @ <code>${s['price']:,.6g}</code>"
                                 for s in fill["newly_filled"])
                txt = (f"📥 <b>{sym} {dir_zh} 分批進場觸發</b>\n"
                       f"━━━━━━━━━━━━━━━━\n"
                       f"本次成交：{legs}\n現價 <code>${live:,.6g}</code>　{done}\n"
                       f"<i>已轉入持倉追蹤，每 15 分鐘更新進度與損益（紙上）</i>")
                target = tg_us if (pe.get("setup") == "us_breakout" and tg_us) else tg
                try:
                    await target.send_message(txt, parse_mode="HTML")
                except Exception as e:
                    print(f"[trade_monitor] entry-fill push error: {e}")

        # v33: 掛單逾時作廢 — 僅「仍 0% 成交（pending）」且掛單已超過 PENDING_MAX_HOURS。
        #   時間判定不需現價，故即使抓不到 K 線（下市／無報價）也能作廢，避免永久殘留。
        #   partial（已部分成交）不在此處作廢，由 get_open_paper 的 timeout 流程處理。
        state_now = fill["state"] if fill else pe["entry_state"]
        if state_now == "pending" and (now_ms - pe["entry_at"]) >= PENDING_MAX_HOURS * 3600 * 1000:
            if expire_pending(pe["id"]):
                age_h = (now_ms - pe["entry_at"]) / 3_600_000
                print(f"[trade_monitor] 🗑️ pending expired: {sym} {pe['direction']} "
                      f"({age_h:.1f}h ≥ {PENDING_MAX_HOURS}h, 0% filled)")
                if (tg or tg_us) is not None:
                    dir_zh = "做多" if pe["direction"] == "bull" else "做空"
                    txt = (f"🗑️ <b>{sym} {dir_zh} 掛單逾時作廢</b>\n"
                           f"━━━━━━━━━━━━━━━━\n"
                           f"掛單 {age_h:.0f}h 未觸及進場價（0% 成交），已自動取消（紙上，0R）\n"
                           f"<i>避免未成交掛單長期佔用持倉計數</i>")
                    target = tg_us if (pe.get("setup") == "us_breakout" and tg_us) else tg
                    try:
                        await target.send_message(txt, parse_mode="HTML")
                    except Exception as e:
                        print(f"[trade_monitor] pending-expire push error: {e}")

    papers = get_open_paper()
    if not papers:
        return

    paper_lines: list[str] = []
    us_lines: list[str] = []
    for pt in papers:
        sym = pt["symbol"]
        bars = bars_cache.get(sym)
        if bars is None:
            d = await _get_recent_5m_bars(source, sym, n=4)
            bars = d["candles"] if d and d.get("candles") else None
            bars_cache[sym] = bars or []
        if not bars:
            continue

        is_us = pt.get("setup", "") == "us_breakout"
        lines = us_lines if is_us else paper_lines

        # TP/SL（與實倉同一套純函式）
        for label, price, size in _check_trade(pt, bars):
            r = apply_paper_event(pt["id"], label, size, price)
            icon = "🎯" if label.startswith("tp") else "🛑"
            closed_str = "（已平倉）" if r["closed"] else ""
            lines.append(
                f"{icon} {sym} {pt['direction']} {label.upper()} "
                f"{r['leg_r']:+.2f}R{closed_str}"
                f"{_signal_link(tg, pt.get('signal_msg_id'))}")
            pt["legs_hit"].append(label)
            pt["size_remaining"] = round(pt["size_remaining"] - size, 3)

        # timeout（v17: per-setup 時限，美股突破 24h）
        hold_max = HOLD_MAX_BY_SETUP.get(pt.get("setup", ""), DEFAULT_HOLD_MAX_HOURS)
        remain = _check_timeout(pt, hold_max)
        if remain is not None and remain > 0.001:
            last_close = bars[-1]["close"]
            r = apply_paper_event(pt["id"], "timeout", remain, last_close)
            lines.append(
                f"⏰ {sym} {pt['direction']} TIMEOUT {r['leg_r']:+.2f}R（已平倉）"
                f"{_signal_link(tg, pt.get('signal_msg_id'))}")

    # 加密事件 → 📈 持倉與績效（Stage 0 門檻只算加密引擎）
    if paper_lines and tg is not None:
        stats = get_paper_stats(30, setup_not="us_breakout")
        text = ("📜 <b>紙上驗證事件</b>（自動追蹤，非實倉）\n" +
                "\n".join(f"  {ln}" for ln in paper_lines) +
                f"\n30d：{stats['n_closed']} 筆平倉 / 勝率 {stats['win_rate_pct']}% / "
                f"期望值 {stats['avg_r']:+.2f}R　Stage1 門檻 {stats['stage0_progress']}")
        try:
            await tg.send_message(text, parse_mode="HTML")
        except Exception as e:
            print(f"[trade_monitor] paper push error: {e}")
    # v23-2: 美股事件 → 🇺🇸 美股主題（進場通知在哪、出場就在哪）
    if us_lines and (tg_us or tg) is not None:
        us_stats = get_paper_stats(30, setup="us_breakout")
        text = ("🧪 <b>美股紙上事件</b>（實驗性引擎，非實倉）\n" +
                "\n".join(f"  {ln}" for ln in us_lines) +
                f"\n30d：{us_stats['n_closed']} 筆平倉 / 勝率 {us_stats['win_rate_pct']}% / "
                f"期望值 {us_stats['avg_r']:+.2f}R")
        try:
            await (tg_us or tg).send_message(text, parse_mode="HTML")
        except Exception as e:
            print(f"[trade_monitor] us paper push error: {e}")
    if paper_lines or us_lines:
        print(f"[trade_monitor] paper events: crypto={len(paper_lines)} "
              f"us={len(us_lines)}")


def _render_event_msg(ev: MonitorEvent) -> str:
    """渲染單一觸發事件成 Telegram 訊息"""
    icon_map = {
        "tp1": "🎯", "tp2": "🎯", "tp3": "🏆",
        "stop": "🛑", "timeout": "⏰",
    }
    icon = icon_map.get(ev.event, "📌")

    if ev.event.startswith("tp"):
        verb = "命中止盈" if ev.realized_r > 0 else "止盈價達成"
        color = "🟢"
    elif ev.event == "stop":
        verb = "觸發停損"
        color = "🔴"
    else:  # timeout
        verb = "持倉超時平倉"
        color = "🟡" if ev.realized_r > 0 else "🟠"

    lines = [
        f"{color} <b>{icon} {ev.symbol} {ev.direction.upper()} {verb}</b>",
        f"━━━━━━━━━━━━━━━━",
        f"事件：<code>{ev.event.upper()}</code>  分批比例：<code>{ev.size_pct*100:.0f}%</code>",
        f"觸發價：<code>${ev.trigger_price:.4f}</code>",
        f"本段 R：<code>{ev.realized_r:+.3f}R</code>  累計 PnL：<code>${ev.cumulative_pnl_usd:+.2f}</code>",
    ]
    if ev.trade_closed:
        lines.append(f"\n✅ <b>該筆交易已完全平倉</b>")
        if ev.cumulative_pnl_usd > 0:
            lines.append(f"   最終結果：<code>勝</code>（{ev.cumulative_pnl_usd/100:+.2f}R）")
        elif ev.cumulative_pnl_usd < 0:
            lines.append(f"   最終結果：<code>負</code>（{ev.cumulative_pnl_usd/100:+.2f}R）")
        else:
            lines.append(f"   最終結果：<code>平</code>")
    else:
        # 部分平倉 → 提醒移動 SL
        if ev.event == "tp1":
            lines.append(f"\n💡 <b>建議</b>：TP1 達成，剩餘 50% 倉位的 SL 上移到 <code>進場價</code>（保本）")
        elif ev.event == "tp2":
            lines.append(f"\n💡 <b>建議</b>：TP2 達成，剩餘 20% 倉位的 SL 上移到 <code>TP1 價</code>")
    return "\n".join(lines)


async def _push_event(tg, ev: MonitorEvent) -> None:
    """推單一事件到 Telegram。盡量 reply 到原 FIRE 訊息。
    v23-4: 完全平倉時改推完整訂單卡（型態/時間線/腿狀態/出場劇本）。"""
    if ev.trade_closed:
        try:
            from .trade_journal import get_trade_full, render_order_card
            t = get_trade_full(ev.trade_id)
            if t:
                text = render_order_card(t)
            else:
                text = _render_event_msg(ev)
        except Exception as e:
            print(f"[trade_monitor] order card error: {e}")
            text = _render_event_msg(ev)
    else:
        text = _render_event_msg(ev)
    kwargs = {"parse_mode": "HTML"}
    if ev.tg_message_id:
        kwargs["reply_to_message_id"] = ev.tg_message_id
    try:
        resp = await tg.send_message(text, **kwargs)
        if not resp.get("ok"):
            # reply 失敗（原訊息被刪？）→ 重發不帶 reply
            kwargs.pop("reply_to_message_id", None)
            await tg.send_message(text, **kwargs)
    except Exception as e:
        print(f"[trade_monitor] tg send error: {e}")


async def run_trade_monitor_loop(tg, source, interval_seconds: int = 900,
                                 tg_alert=None, tg_us=None):
    """Worker 主迴圈：每 N 秒（預設 900=15min）掃一次 open trades。

    v15: tg = 持倉與績效主題；tg_alert = 進場訊號主題（熔斷即時警報用，
    熔斷是「停止交易」級事件，必須出現在使用者最關注的頻道）。
    """
    print(f"[trade_monitor] starting loop, interval={interval_seconds}s")
    # 啟動延後 60s，等 daemon 其他 worker 就緒
    await asyncio.sleep(60)

    # v15: 熔斷狀態轉換偵測（False→True 的瞬間推即時警報，只推一次）
    breach_state = {"daily": False, "weekly": False}
    # v41: 教練層節流狀態（跨迴圈常駐；重啟後重新武裝＝最多重提醒一次，可接受）
    coach_state: dict = {"seen": set()}

    while True:
        try:
            # v15: 先把超時未確認的訊號標 expired（4h），並輕量通知
            from .trade_journal import expire_stale_signals
            expired = expire_stale_signals(float(os.getenv("SIGNAL_EXPIRY_HOURS", "4")))
            if expired and tg is not None:
                # v23-4: 每筆過期升級為「錯過卡」— 告知看到了什麼、紙上對照會持續追蹤
                from .trade_journal import get_paper_outcome_by_fire
                for e in expired:
                    paper = get_paper_outcome_by_fire(e.get("fire_id"))
                    if paper and paper["status"] == "closed":
                        tail = (f"👻 紙上對照已平倉：<code>{paper['realized_r']:+.2f}R</code>"
                                f"（你錯過了 ${paper['pnl_usd']:+,.0f}）")
                    elif paper:
                        tail = "👻 紙上對照持續追蹤中 — 結果出爐會回報這筆錯過了多少"
                    else:
                        tail = "（無紙上對照）"
                    try:
                        await tg.send_message(
                            f"⏰ <b>{e['symbol']} {('做多' if e['direction'] == 'bull' else '做空')}"
                            f" 訊號過期</b>（4h 未確認，已自動失效）\n{tail}",
                            parse_mode="HTML")
                    except Exception:
                        pass

            events = await monitor_once(source, tg=tg, tg_fire=tg_alert, tg_us=tg_us,
                                        coach_state=coach_state)
            opens_count = len(get_open_trades())
            if events:
                event_summary = ", ".join(f"{e.symbol}/{e.event}" for e in events)
                print(f"[trade_monitor] {len(events)} events fired: {event_summary} "
                      f"(opens remain: {opens_count})")
            else:
                print(f"[trade_monitor] no events, {opens_count} open trades")

            # v15: 熔斷觸發即時警報（轉換瞬間推，不重複）
            if events:  # 只有平倉事件才可能改變 PnL → 才需要檢查
                from .risk_manager import get_risk_status
                risk = get_risk_status()
                for kind, label, advice in (
                    ("daily", "日線熔斷（今日虧損達上限）",
                     "今日暫停新開倉，明日 UTC 00:00 自動恢復。請勿手動凹單。"),
                    ("weekly", "週線熔斷（本週虧損達上限）",
                     "完全暫停，請人工檢視策略後再恢復。"),
                ):
                    breached = risk[f"{kind}_breached"]
                    if breached and not breach_state[kind]:
                        alert_tg = tg_alert or tg
                        try:
                            await alert_tg.send_message(
                                f"🚨🚨 <b>{label}</b> 🚨🚨\n"
                                f"━━━━━━━━━━━━━━━━\n"
                                f"今日 PnL <code>${risk['today_pnl_usd']:+.2f}</code>"
                                f"（{risk['today_pnl_pct']:+.2f}%）　"
                                f"本週 <code>${risk['week_pnl_usd']:+.2f}</code>\n"
                                f"⛔ {advice}",
                                parse_mode="HTML")
                        except Exception:
                            pass
                    breach_state[kind] = breached
        except Exception as e:
            print(f"[trade_monitor] loop error: {type(e).__name__}: {e}")
        await asyncio.sleep(interval_seconds)


# ===========================================================================
# 自測
# ===========================================================================
if __name__ == "__main__":
    async def selftest():
        from market_intel_mcp.sources import get_source
        source = get_source()
        events = await monitor_once(source, tg=None)
        print(f"events: {len(events)}")
        for e in events:
            print(f"  {e.symbol} {e.direction} {e.event} @ {e.trigger_price} -> {e.realized_r:+.2f}R")
        opens = get_open_trades()
        print(f"\nopen trades after monitor: {len(opens)}")
        for o in opens:
            age_h = (int(time.time() * 1000) - o["entry_at"]) / 3600000
            print(f"  trade#{o['id']} {o['symbol']} {o['direction']} "
                  f"entry={o['entry_price']} age={age_h:.1f}h "
                  f"legs_hit={o['legs_hit']} remain={o['size_remaining']}")

    asyncio.run(selftest())
