"""Worker 2: 訊息分派器（含 risk_manager + trade_journal 整合）。

流程：
    1. dequeue 一筆 FIRE
    2. risk_manager.should_block() → 拒絕則推「⛔ 阻擋通知」並標 cancelled
    3. 通過 → 渲染 FIRE 訊息
    4. send TG
    5. 成功 → trade_journal.record_entry() 寫進場
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from l2_trigger.leverage import choose_leverage, compute_position, compute_tp_prices
from telegram_bot.client import TelegramClient
from telegram_bot.message_format import (
    render_fire_message,
    render_fire_with_checks,
)

from .fire_queue import dequeue_one, mark_failed, mark_sent
from .risk_manager import should_block
from .trade_journal import EntryRecord, record_entry


@dataclass
class _FakeCheck:
    """簡化版 ConsistencyResult 用於從 JSON payload 還原"""
    confidence: int
    pass_: bool
    checks: list
    reason: str


def compute_entry_zone(signal_price: float, direction: str,
                       setup: str = "intraday") -> tuple[float, float]:
    """進場區（與 message_format 口徑一致）。bull: -0.3%~+0.2%；bear 對稱。"""
    if direction == "bull":
        return signal_price * 0.997, signal_price * 1.002
    return signal_price * 0.998, signal_price * 1.003


async def _fetch_live_price(symbol: str) -> float | None:
    """抓最新 5m 收盤價（等待觸發判定用）。失敗回 None（→ 走直接 FIRE 路徑）。"""
    try:
        from market_intel_mcp.sources.okx_candles import OkxCandlesSource
        okx = OkxCandlesSource()
        try:
            d = await okx.get_candles(symbol, "5m", 1)
        finally:
            await okx.close()
        bars = d.get("candles") if isinstance(d, dict) else None
        return bars[-1]["close"] if bars else None
    except Exception:
        return None


async def dispatch_once(tg: TelegramClient, tg_aux: TelegramClient | None = None) -> bool:
    """處理一筆。回 True 代表有處理（不論成敗）；False 代表 queue 空。

    v15: tg = 進場訊號主題（FIRE）；tg_aux = 持倉與績效主題（風控阻擋通知）。
    """
    aux = tg_aux or tg
    item = dequeue_one()
    if item is None:
        return False
    fire_id, decision = item
    sym = decision["snapshot"]["symbol"]
    setup = decision["setup_name"]
    direction = decision["direction"]

    # === v22: 持倉中抑制 — 已確認開單的 symbol 不再推新訊號（使用者規格：
    #     確認開單前重複推送 OK；一旦 open 只追蹤持倉與績效）===
    from .trade_journal import get_open_trade
    open_t = get_open_trade(sym)
    if open_t is not None:
        if open_t["direction"] == direction:
            # 同向重複 → 靜默略過（持倉監控已在追蹤）
            mark_failed(fire_id, f"suppressed: position open #{open_t['id']}")
            print(f"[dispatcher] #{fire_id} {sym}/{direction} SUPPRESSED "
                  f"(open trade #{open_t['id']} same direction)")
        else:
            # 反向訊號 → 不推 FIRE，改推持倉警示（出場參考價值高）
            warn = (
                f"↔️ <b>{sym} 持倉反向訊號警示</b>\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"你目前持有 <code>{'多單' if open_t['direction'] == 'bull' else '空單'}</code>"
                f"（#{open_t['id']}，進場 <code>${open_t['entry_price']:,.6g}</code>），\n"
                f"但引擎剛偵測到 <code>{sym}</code> 的"
                f"<b>{'做空' if direction == 'bear' else '做多'}</b>條件成立。\n"
                f"<i>動能可能反轉 — 建議檢視持倉，考慮提前止盈或收緊止損。</i>"
            )
            try:
                await aux.send_message(warn, parse_mode="HTML")
            except Exception:
                pass
            mark_failed(fire_id, f"converted_to_reversal_warning: open #{open_t['id']}")
            print(f"[dispatcher] #{fire_id} {sym}/{direction} -> REVERSAL WARNING "
                  f"(open trade #{open_t['id']} opposite direction)")
        return True

    # === 風控前置檢查 ===
    blocked, reason, details = should_block(decision)
    if blocked:
        # 推「阻擋通知」到持倉主題（v15: 不再稀釋 FIRE 主題）
        block_text = (
            f"⛔ <b>FIRE 訊號被風控阻擋</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"標的：<code>{sym}</code>  方向：<code>{direction}</code>\n"
            f"原因：<code>{reason}</code>\n"
            f"<i>{details.get('msg', '')[:200]}</i>"
        )
        try:
            await aux.send_message(block_text, parse_mode="HTML")
        except Exception:
            pass
        mark_failed(fire_id, f"blocked_by_risk: {reason}")
        print(f"[dispatcher] #{fire_id} {sym}/{setup}/{direction} BLOCKED: {reason}")
        return True

    # === v18-D: 交易計畫先算（waiting 與 FIRE 兩路徑共用）===
    snap = decision["snapshot"]
    entry_price = snap["price"]
    sl_pct = 4.0 if setup == "intraday" else 5.0
    if direction == "bull":
        stop = round(entry_price * (1 - sl_pct / 100), 6)
    else:
        stop = round(entry_price * (1 + sl_pct / 100), 6)
    lev = choose_leverage(sym, snap.get("atr_pct_7d"))
    pos = compute_position(entry_price, stop, 100.0, lev)
    tp_r = (1.0, 1.5, 2.0) if setup == "intraday" else (1.0, 1.5, 2.5)
    tps = compute_tp_prices(entry_price, stop, direction, tp_r)
    atr = snap.get("atr_pct_7d")
    regime = ("unknown" if atr is None else
              "extreme" if atr >= 8.0 else
              "high" if atr >= 5.0 else "low")
    cc = decision.get("cross_check")

    # === v18-D: 三態判定 — 價格已偏離進場區 → 等待觸發（不立即 FIRE）===
    live = await _fetch_live_price(sym)
    zone_lo, zone_hi = compute_entry_zone(entry_price, direction, setup)
    if live is not None and not (zone_lo <= live <= zone_hi):
        wait_text = (
            f"⏳ <b>{sym} {('做多' if direction == 'bull' else '做空')} — 等待觸發</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"訊號價 <code>${entry_price:,.6g}</code>　"
            f"現價 <code>${live:,.6g}</code>（已偏離進場區）\n"
            f"進場區 <code>${zone_lo:,.6g} – ${zone_hi:,.6g}</code>\n"
            f"💡 價格回到進場區會自動推正式訊號（最多等 6 小時，"
            f"未觸發自動放棄 — 不追價）"
        )
        try:
            resp = await tg.send_message(wait_text, parse_mode="HTML")
            msg_id = resp.get("result", {}).get("message_id") if resp.get("ok") else None
        except Exception:
            msg_id = None
        mark_sent(fire_id, tg_message_id=msg_id)
        try:
            tid = record_entry(EntryRecord(
                symbol=sym, setup=setup, direction=direction,
                entry_price=entry_price, stop_price=stop,
                tp1=tps["tp1"], tp2=tps["tp2"], tp3=tps["tp3"],
                risk_usd=100.0, leverage=lev,
                margin_usd=pos["margin_usd"], notional_usd=pos["notional_usd"],
                fire_id=fire_id, tg_message_id=msg_id,
                decision_snapshot={"snapshot": snap, "reason": decision.get("reason", "")},
                cross_check_confidence=cc.get("confidence") if cc else None,
                tags=[f"regime:{regime}"],
            ), initial_status="waiting")
            print(f"[dispatcher] #{fire_id} {sym}/{direction} -> WAITING "
                  f"(live={live} 偏離 zone [{zone_lo:.6g}, {zone_hi:.6g}], trade_id={tid})")
        except Exception as e:
            print(f"[dispatcher] #{fire_id} waiting journal write failed: {e}")
        return True

    # === 渲染訊息（價在進場區內 → 直接 FIRE）===
    if cc:
        fake = _FakeCheck(
            confidence=cc.get("confidence", 0),
            pass_=True,
            checks=cc.get("checks", []),
            reason=cc.get("reason", ""),
        )
        text, buttons = render_fire_with_checks(decision, fake)
    else:
        text, buttons = render_fire_message(decision)

    # v15: 按鈕改帶 fire_id（callback listener 用它找到對應 trade 做確認/略過）
    buttons = [[
        {"text": "✅ 已下單", "callback_data": f"fill:{fire_id}"},
        {"text": "⏭ 略過", "callback_data": f"skip:{fire_id}"},
    ]]
    # v18-C: 做多訊號附解鎖警告（解鎖前搶跑拋壓）
    if direction == "bull":
        try:
            from news_feed.unlock_calendar import render_unlock_warning
            text += render_unlock_warning(sym)
        except Exception:
            pass
    # v18-B: 相同條件歷史類比（10s timeout，失敗不阻塞推送）
    try:
        from .analogue import analogue_stats, render_analogue_line
        _astats = await analogue_stats(sym, direction)
        text += render_analogue_line(_astats)
    except Exception:
        pass
    # v18-E: 市場廣度軟閘門（極端逆風時警示，不阻擋）
    try:
        from .market_scanner import breadth_caution
        text += breadth_caution(direction)
    except Exception:
        pass
    text += "\n\n⏳ <i>請按按鈕回報：按「已下單」才會計入持倉與績效（4 小時未按自動過期）</i>"

    # === 送 TG ===
    try:
        resp = await tg.send_message(text, parse_mode="HTML", inline_buttons=buttons)
    except Exception as e:
        mark_failed(fire_id, str(e))
        print(f"[dispatcher] #{fire_id} {sym}/{setup}/{direction} EXC: {e}")
        return True

    if not resp.get("ok"):
        mark_failed(fire_id, resp.get("description", "unknown"))
        print(f"[dispatcher] #{fire_id} FAILED: {resp.get('description')}")
        return True

    msg_id = resp.get("result", {}).get("message_id")
    mark_sent(fire_id, tg_message_id=msg_id)

    # v18-F: FIRE 附 SMC 標記圖（4h 結構 + 交易計畫線；失敗不阻塞）
    try:
        from .chart_render import render_symbol_chart
        chart = await render_symbol_chart(sym, "4h", 120, plan={
            "entry": entry_price, "stop": stop,
            "tp1": tps["tp1"], "tp2": tps["tp2"], "tp3": tps["tp3"],
            "direction": direction})
        if chart:
            await tg.send_photo(chart, caption=f"📐 {sym} 4H SMC 結構 + 交易計畫")
    except Exception as e:
        print(f"[dispatcher] chart error: {e}")

    # === 寫 trade journal（v15: status='signal'，按「已下單」才轉 open）===
    # （交易計畫已在三態判定前算好）
    try:
        tid = record_entry(EntryRecord(
            symbol=sym, setup=setup, direction=direction,
            entry_price=entry_price, stop_price=stop,
            tp1=tps["tp1"], tp2=tps["tp2"], tp3=tps["tp3"],
            risk_usd=100.0, leverage=lev,
            margin_usd=pos["margin_usd"], notional_usd=pos["notional_usd"],
            fire_id=fire_id, tg_message_id=msg_id,
            decision_snapshot={"snapshot": snap, "reason": decision.get("reason", "")},
            cross_check_confidence=cc.get("confidence") if cc else None,
            tags=[f"regime:{regime}"],
        ), initial_status="signal")

        # v16: 同步開紙上倉（Stage 0 — 每筆訊號自動追蹤，驗證引擎期望值）
        from .paper_journal import record_paper_entry
        pid = record_paper_entry(
            symbol=sym, setup=setup, direction=direction,
            entry_price=entry_price, stop_price=stop,
            tp1=tps["tp1"], tp2=tps["tp2"], tp3=tps["tp3"],
            fire_id=fire_id, regime=regime,
        )
        print(f"[dispatcher] #{fire_id} {sym}/{setup}/{direction} -> sent "
              f"(msg_id={msg_id}, trade_id={tid} signal 等待確認, paper_id={pid} 自動追蹤)")
    except Exception as e:
        print(f"[dispatcher] #{fire_id} TG sent OK but journal write failed: {e}")

    return True


async def run_dispatcher(tg: TelegramClient, tg_aux: TelegramClient | None = None,
                         poll_seconds: int = 3):
    """Worker 主迴圈。"""
    while True:
        handled = await dispatch_once(tg, tg_aux)
        if not handled:
            await asyncio.sleep(poll_seconds)
