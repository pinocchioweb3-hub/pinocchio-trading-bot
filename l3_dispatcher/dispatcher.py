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

from . import symbol_gate
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

    # === v47: 跨來源 per-symbol 收斂閘（唯讀檢查）===
    # 若同 (symbol, direction) 近期已被任一 FIRE 來源（scheduler/deepdive/us）推到 🎯 →
    # 靜默略過，避免短時間同幣重複單。標記只在「真正送出 🎯」後才寫（見下方 symbol_gate.mark_sent）。
    if not symbol_gate.should_send(sym, direction):
        mark_failed(fire_id, "suppressed: symbol_gate cooldown")
        print(f"[dispatcher] #{fire_id} {sym}/{direction} SUPPRESSED (symbol_gate 跨來源冷卻)")
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
    # v23-2: SL/TP/風險全部走 botconfig 單一來源（修三份複製的不同步隱患）
    from botconfig import CONFIG
    snap = decision["snapshot"]
    entry_price = snap["price"]
    sl_pct = CONFIG.sl_pct(setup)
    # v48: 交易計畫計算就地保護 — compute_position 在邊界情況（價格異常導致 stop==entry）
    # 會拋 ValueError；過去無保護，疊加「dispatching 卡死」會放大漏單。捕到例外即 mark_failed，
    # 讓此筆乾淨退場、不落入孤兒狀態。
    try:
        if direction == "bull":
            stop = round(entry_price * (1 - sl_pct / 100), 6)
        else:
            stop = round(entry_price * (1 + sl_pct / 100), 6)
        lev = choose_leverage(sym, snap.get("atr_pct_7d"))
        pos = compute_position(entry_price, stop, CONFIG.risk_per_trade_usd, lev)
        tp_r = CONFIG.tp_r(setup)
        tps = compute_tp_prices(entry_price, stop, direction, tp_r)
    except Exception as e:
        mark_failed(fire_id, f"plan_compute_error: {e}")
        print(f"[dispatcher] #{fire_id} {sym}/{setup}/{direction} PLAN ERROR: {e}")
        return True
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
        # v48: 原子 claim — 送出前一刻搶 symbol_gate 槽；搶不到代表並發來源（deepdive/scheduler）
        # 已搶先送同幣同向 → 本筆不送，避免重複（關閉原 should_send→await→mark_sent 的 TOCTOU 窗）。
        if not symbol_gate.claim(sym, direction):
            mark_failed(fire_id, "suppressed: symbol_gate race lost (waiting)")
            print(f"[dispatcher] #{fire_id} {sym}/{direction} SUPPRESSED (symbol_gate claim race, waiting)")
            return True
        try:
            resp = await tg.send_message(wait_text, parse_mode="HTML")
            msg_id = resp.get("result", {}).get("message_id") if resp.get("ok") else None
        except Exception:
            msg_id = None
        mark_sent(fire_id, tg_message_id=msg_id)
        # v48: claim 已寫入 symbol_gate（原 symbol_gate.mark_sent 移除）
        try:
            tid = record_entry(EntryRecord(
                symbol=sym, setup=setup, direction=direction,
                entry_price=entry_price, stop_price=stop,
                tp1=tps["tp1"], tp2=tps["tp2"], tp3=tps["tp3"],
                risk_usd=CONFIG.risk_per_trade_usd, leverage=lev,
                margin_usd=pos["margin_usd"], notional_usd=pos["notional_usd"],
                fire_id=fire_id, tg_message_id=msg_id,
                decision_snapshot=decision,    # v45: 存完整 decision（供 /intent 與「複製 JSON」忠實重建意圖）
                cross_check_confidence=cc.get("confidence") if cc else None,
                tags=[f"regime:{regime}"],
                entry_kind="wait_trigger",                      # v23-3
                entry_zone_lo=zone_lo, entry_zone_hi=zone_hi,
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
    # v45: 加「📋 複製可執行 JSON」→ intent:{fire_id}（通用 trade-intent，跨所 AI agent 可讀）
    buttons = [
        [
            {"text": "✅ 已下單", "callback_data": f"fill:{fire_id}"},
            {"text": "⏭ 略過", "callback_data": f"skip:{fire_id}"},
        ],
        [
            {"text": "📋 複製可執行 JSON", "callback_data": f"intent:{fire_id}"},
        ],
    ]
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
    # v48: 原子 claim — 送出前一刻搶槽；搶不到代表並發來源已代表此幣此向 → 不送（關閉 TOCTOU 窗）。
    if not symbol_gate.claim(sym, direction):
        mark_failed(fire_id, "suppressed: symbol_gate race lost")
        print(f"[dispatcher] #{fire_id} {sym}/{setup}/{direction} SUPPRESSED (symbol_gate claim race)")
        return True
    try:
        resp = await tg.send_message(text, parse_mode="HTML", inline_buttons=buttons)
    except Exception as e:
        symbol_gate.release(sym, direction)   # v48: 送失敗 → 歸還槽，下一輪可重試（不靜默漏單）
        mark_failed(fire_id, str(e))
        print(f"[dispatcher] #{fire_id} {sym}/{setup}/{direction} EXC: {e}")
        return True

    if not resp.get("ok"):
        symbol_gate.release(sym, direction)   # v48: 送失敗 → 歸還槽
        mark_failed(fire_id, resp.get("description", "unknown"))
        print(f"[dispatcher] #{fire_id} FAILED: {resp.get('description')}")
        return True

    msg_id = resp.get("result", {}).get("message_id")
    mark_sent(fire_id, tg_message_id=msg_id)
    # v48: claim 已在送出前寫入 symbol_gate（原 symbol_gate.mark_sent 移除）

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
            risk_usd=CONFIG.risk_per_trade_usd, leverage=lev,
            margin_usd=pos["margin_usd"], notional_usd=pos["notional_usd"],
            fire_id=fire_id, tg_message_id=msg_id,
            decision_snapshot={"snapshot": snap, "reason": decision.get("reason", "")},
            cross_check_confidence=cc.get("confidence") if cc else None,
            tags=[f"regime:{regime}"],
            entry_kind="direct_fire",                           # v23-3
            entry_zone_lo=zone_lo, entry_zone_hi=zone_hi,
        ), initial_status="signal")

        # v16: 同步開紙上倉（Stage 0 — 每筆訊號自動追蹤，驗證引擎期望值）
        # v26: 分批限價模式 — 進場區拆兩格，價格逐格觸及才計成交，持倉主題顯示進場進度
        # v56: 同步捕捉進場計畫快照（復盤引擎前置；純觀測，失敗回 None 不影響建單）
        from .paper_journal import record_paper_entry
        try:
            from .plan_snapshot import build_plan_snapshot
            # v56 step4：影子層 — 把進場當下已算好的 regime/context 觀測值打包（純資料，
            # 零下單數學；失敗回兩個空向量不影響建單）。
            from .regime_vector import assemble as _assemble_regime
            _rv, _ctx = _assemble_regime(snap, direction=direction)
            plan_snap = build_plan_snapshot(
                source="direct_fire", direction=direction,
                entry_price=entry_price, planned_stop=stop,
                tp1=tps["tp1"], tp2=tps["tp2"], tp3=tps["tp3"],
                fire_id=fire_id, signal_msg_id=msg_id, regime=regime,
                thesis=decision.get("reason", ""),
                confidence=(cc.get("confidence") if cc else None),
                regime_vector=_rv, context=_ctx,
            )
        except Exception as _e:
            plan_snap = None
            print(f"[dispatcher] #{fire_id} plan_snapshot build skipped: {_e}")
        pid = record_paper_entry(
            symbol=sym, setup=setup, direction=direction,
            entry_price=entry_price, stop_price=stop,
            tp1=tps["tp1"], tp2=tps["tp2"], tp3=tps["tp3"],
            fire_id=fire_id, regime=regime,
            zone_lo=zone_lo, zone_hi=zone_hi, split_mode=True,
            plan_snapshot=plan_snap,
        )
        print(f"[dispatcher] #{fire_id} {sym}/{setup}/{direction} -> sent "
              f"(msg_id={msg_id}, trade_id={tid} signal 等待確認, paper_id={pid} 自動追蹤)")
    except Exception as e:
        print(f"[dispatcher] #{fire_id} TG sent OK but journal write failed: {e}")

    return True


async def run_dispatcher(tg: TelegramClient, tg_aux: TelegramClient | None = None,
                         poll_seconds: int = 3):
    """Worker 主迴圈。"""
    # v48: 啟動回收 — 把上次崩潰/斷電/重啟遺留在 'dispatching' 中間態的 FIRE
    # 重設回 'queued'，避免進場訊號被靜默吞掉。單一 dispatcher worker，啟動當下
    # 必無真正在途的派發，回收安全。supervisor 重啟本 worker 時也會再跑一次。
    try:
        from .fire_queue import reclaim_orphans
        n = reclaim_orphans()
        if n:
            print(f"[dispatcher] 啟動回收 {n} 筆卡在 dispatching 的孤兒 FIRE → 重新排入 queued")
    except Exception as e:
        print(f"[dispatcher] reclaim_orphans 失敗（不影響啟動）: {e}")
    while True:
        handled = await dispatch_once(tg, tg_aux)
        if not handled:
            await asyncio.sleep(poll_seconds)
