"""美股永續訊號 worker（v17，實驗性 — 僅紙上帳）。

風控隔離不變量：
    1. 不 import fire_queue / dispatcher / risk_manager / trade_journal
    2. 只寫 paper_trades（setup='us_breakout'），訊息無「已下單」按鈕
    3. 美股自有上限 2 倉；econ blackout 沿用；財報日靜態名單停發
    4. 紙上 ≥30 筆平倉且 avg_R>0 前拒絕實單化
"""
from __future__ import annotations

import asyncio
import datetime as dt

import httpx

from l2_trigger.configs.us_breakout import US_BREAKOUT_DEFAULT
from l2_trigger.cooldown import CooldownStore
from l2_trigger.leverage import compute_tp_prices
from l2_trigger.types import TriggerAction
from l2_trigger.us_engine import evaluate_us

from .us_stock_data import US_FIRE_WHITELIST, build_us_snapshot, fetch_qqq_chg_24h

# 財報日（手動維護，每季更新；T-24h ~ T+4h 停發。日期=美東盤後）
EARNINGS_DATES: dict[str, list[str]] = {
    "NVDA": ["2026-08-26"],
    "MU":   ["2026-06-24"],
    "MRVL": ["2026-08-27"],
    "INTC": ["2026-07-23"],
    "ORCL": ["2026-09-08"],
    "SNDK": ["2026-08-05"],
    # SOXL/QQQ 為 ETF，無財報
}


def in_earnings_blackout(sym: str, now: dt.datetime | None = None) -> bool:
    """財報 T-24h ~ T+4h 停發（日期視為當日 16:00 ET）。解析失敗 fail-open。"""
    try:
        from zoneinfo import ZoneInfo
        now = now or dt.datetime.now(dt.timezone.utc)
        for date_str in EARNINGS_DATES.get(sym, []):
            et = dt.datetime.strptime(date_str, "%Y-%m-%d").replace(
                hour=16, tzinfo=ZoneInfo("America/New_York"))
            if et - dt.timedelta(hours=24) <= now <= et + dt.timedelta(hours=4):
                return True
        return False
    except Exception:
        return False


def count_open_us_paper() -> int:
    import sqlite3
    from .paper_journal import DB_PATH, init_db
    init_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE setup='us_breakout' AND status='open'"
        ).fetchone()[0]
    finally:
        conn.close()


def render_us_fire(d, sl_pct: float, stop: float, tps: dict,
                   paper_stats: dict) -> str:
    s = d.snapshot
    dir_zh = "做多" if d.direction.value == "bull" else "做空"
    dir_icon = "📈" if d.direction.value == "bull" else "📉"
    session_zh = {"rth": "盤中", "ext": "延長時段", "wkd": "週末盤",
                  "off": "盤外"}.get(s.us_session, "?")
    entry = s.price
    if d.direction.value == "bull":
        e_lo, e_hi = entry * 0.998, entry * 1.003
    else:
        e_lo, e_hi = entry * 0.997, entry * 1.002

    votes = []
    for r in d.confirmed:
        if r.state.value in ("bull", "bear") and r.name != "qqq_gate":
            ev = r.evidence
            if r.name == "us_breakout":
                votes.append(f"✓ 突破 24h {'高' if d.direction.value == 'bull' else '低'} "
                             f"<code>{ev.get('break_level'):,.2f}</code>"
                             f"（{ev.get('distance_atr', '?')} ATR）")
            elif r.name == "us_volume_surge":
                votes.append(f"✓ 量能 <code>{ev.get('vol_mult')}×</code> 均量")
            elif r.name == "us_funding":
                votes.append(f"✓ funding 極值 <code>{ev.get('funding')}</code>")
            elif r.name == "us_taker":
                votes.append(f"✓ taker 偏向 <code>{ev.get('taker_ratio')}</code>")

    misc = (f"funding <code>{s.funding if s.funding is not None else '—'}</code>"
            f"｜OI 24h <code>{f'{s.oi_delta_pct:+.1f}%' if s.oi_delta_pct is not None else '—'}</code>"
            f"｜QQQ <code>{f'{s.qqq_chg_24h_pct:+.2f}%' if s.qqq_chg_24h_pct is not None else '—'}</code>")

    now_utc = dt.datetime.now(dt.timezone.utc).strftime("%m-%d %H:%M UTC")
    return (
        f"🧪🇺🇸 <b>{s.symbol} 永續</b>［美股突破·實驗性］\n"
        f"{dir_icon} <b>{dir_zh}</b>　綜合分 <code>{d.composite_score:+.2f}</code>\n"
        f"🕐 {now_utc}｜時段：{session_zh}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"進場：<code>{e_lo:,.2f} – {e_hi:,.2f}</code>\n"
        f"止損：<code>{stop:,.2f}</code>（1.5×ATR₁ₕ = {sl_pct:.2f}%）\n"
        f"止盈：TP1 <code>{tps['tp1']:,.2f}</code>(1R·50%) → "
        f"TP2 <code>{tps['tp2']:,.2f}</code>(1.5R·30%) → "
        f"TP3 <code>{tps['tp3']:,.2f}</code>(2R·20%)\n"
        f"槓桿建議：3x｜時限：24h\n"
        f"━━━━━━━━━━━━━━━━\n"
        + "\n".join(votes) + f"\n· {misc}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>實驗性引擎：無回測證據，僅紙上自動追蹤，請勿實單跟隨。\n"
        f"紙上(us_breakout 30d)：{paper_stats['n_closed']} 筆平倉 / "
        f"勝率 {paper_stats['win_rate_pct']}% / ${paper_stats['total_pnl_usd']:+.0f}"
        f"｜升級門檻 {paper_stats['n_closed']}/30</i>"
    )


async def run_us_signal_loop(tg, scan_interval: int = 900):
    """美股訊號 worker：每 15 分鐘掃白名單 8 檔，每根新收盤 1h K 只評估一次。"""
    from market_intel_mcp.sources.okx_candles import OkxCandlesSource

    print(f"[us_signals] starting loop, whitelist={US_FIRE_WHITELIST}, "
          f"interval={scan_interval}s (experimental, paper-only)")
    await asyncio.sleep(90)

    store = CooldownStore(cooldown_seconds=US_BREAKOUT_DEFAULT.cooldown_seconds)
    last_bar_ts: dict[str, int] = {}

    while True:
        try:
            from .us_stock_data import us_session_now
            # v32: OKX 美股永續 24/7 有真實價格波動（實測週末 K 線新鮮、價格在動）。
            # 預設連週末(wkd)/平日夜間(off)都掃，避免漏掉 24/7 行情→零訊號（仍 paper-only）。
            # 可用 US_SCAN_OFFHOURS=false 關回只掃 rth/ext。regime tag 已記時段供分時段分析。
            sess = us_session_now()
            try:
                from botconfig import get_str
                scan_offhours = get_str("US_SCAN_OFFHOURS", "true").strip().lower() \
                    not in ("false", "0", "no", "off")
            except Exception:
                scan_offhours = True
            if sess in ("off", "wkd") and not scan_offhours:
                await asyncio.sleep(scan_interval)
                continue

            okx = OkxCandlesSource()
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    qqq_chg = await fetch_qqq_chg_24h(okx)
                    fired = 0
                    for sym in US_FIRE_WHITELIST:
                        try:
                            snap = await build_us_snapshot(sym, qqq_chg, okx, client)
                            if snap is None:
                                continue
                            if snap.ts == last_bar_ts.get(sym):
                                continue  # 同一根 K 已評估
                            last_bar_ts[sym] = snap.ts

                            d = evaluate_us(snap, US_BREAKOUT_DEFAULT)
                            if d.action != TriggerAction.FIRE:
                                continue

                            # === worker 層風控（順序固定）===
                            from news_feed.econ_calendar import in_blackout
                            bo, ev_name = in_blackout()
                            if bo:
                                print(f"[us_signals] {sym} FIRE skipped: econ blackout ({ev_name})")
                                continue
                            if in_earnings_blackout(sym):
                                print(f"[us_signals] {sym} FIRE skipped: earnings blackout")
                                continue
                            if count_open_us_paper() >= US_BREAKOUT_DEFAULT.max_us_open:
                                print(f"[us_signals] {sym} FIRE skipped: max 2 US paper open")
                                continue
                            if not store.should_emit(d):
                                continue

                            # === 算價位 → 推播 → 開紙上倉 ===
                            entry = snap.price
                            sl_pct = min(max(US_BREAKOUT_DEFAULT.sl_atr_mult *
                                             (snap.atr_1h_pct or 2.0),
                                             US_BREAKOUT_DEFAULT.sl_min_pct),
                                         US_BREAKOUT_DEFAULT.sl_max_pct)
                            if d.direction.value == "bull":
                                stop = entry * (1 - sl_pct / 100)
                            else:
                                stop = entry * (1 + sl_pct / 100)
                            tps = compute_tp_prices(entry, stop, d.direction.value,
                                                    US_BREAKOUT_DEFAULT.tp_r_multiples)

                            from .paper_journal import get_paper_stats, record_paper_entry
                            pstats = get_paper_stats(30, setup="us_breakout")
                            text = render_us_fire(d, sl_pct, stop, tps, pstats)
                            # v18-B: 歷史類比附註
                            try:
                                from .analogue import analogue_stats, render_analogue_line
                                _astats = await analogue_stats(
                                    sym, d.direction.value, snap.us_vol_mult)
                                text += render_analogue_line(_astats)
                            except Exception:
                                pass
                            sig_mid = None
                            try:
                                _r = await tg.send_message(text, parse_mode="HTML")
                                sig_mid = (_r or {}).get("result", {}).get("message_id")
                            except Exception as e:
                                print(f"[us_signals] tg send error: {e}")

                            pid = record_paper_entry(
                                symbol=sym, setup="us_breakout",
                                direction=d.direction.value,
                                entry_price=entry, stop_price=stop,
                                tp1=tps["tp1"], tp2=tps["tp2"], tp3=tps["tp3"],
                                fire_id=None, regime=f"us_{snap.us_session}",
                                signal_msg_id=sig_mid,
                            )
                            store.mark_fired(d)
                            fired += 1
                            print(f"[us_signals] 🧪 FIRE {sym} {d.direction.value} "
                                  f"@{entry} (paper_id={pid}, score={d.composite_score})")
                        except Exception as e:
                            print(f"[us_signals] {sym} error: {type(e).__name__}: {e}")
                    if fired:
                        print(f"[us_signals] cycle done, {fired} fired")
            finally:
                await okx.close()
        except Exception as e:
            print(f"[us_signals] loop error: {type(e).__name__}: {e}")
        await asyncio.sleep(scan_interval)


if __name__ == "__main__":
    # 單輪 dry-run（不推播、不寫紙上帳）
    async def selftest():
        from market_intel_mcp.sources.okx_candles import OkxCandlesSource
        okx = OkxCandlesSource()
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                qqq = await fetch_qqq_chg_24h(okx)
                for sym in US_FIRE_WHITELIST:
                    snap = await build_us_snapshot(sym, qqq, okx, client)
                    if not snap:
                        continue
                    d = evaluate_us(snap, US_BREAKOUT_DEFAULT)
                    print(f"{sym:5s} {d.action.value:4s} {d.reason}")
        finally:
            await okx.close()
    asyncio.run(selftest())
