"""經濟數據日曆 Worker（v16）：CPI/PPI/初領失業金/FOMC + 降息機率。

三源組合（2026-06-11 實測選定）：
    排程表  ForexFactory 週曆 JSON（免費；rate limit 嚴格 ~2次/5min → 每小時抓 1 次）
            ※ 實測確認此源「沒有 actual 欄位」，只能當排程
    實際值  TradingView 隱藏日曆 API（免金鑰、帶 Origin header；actual 秒~分鐘級更新）
            只在事件發布窗（T 到 T+30min）內以 20s 間隔輪詢
    降息機率 Kalshi 公開 API（免認證；預測市場價格 ≈ FedWatch ±1-3pp）

功能：
    1. 每日 00:10 UTC 推「今日美國重要數據預告」+ Kalshi 利率機率
    2. 高影響事件 T-30min 推預警 + 進入「訊號靜默期」
    3. 發布後抓 actual → Claude 判利好/利空 crypto/美股 → 即時推播
    4. in_blackout()：高影響事件 -30min ~ +15min 暫停新 FIRE（risk_manager 引用）
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import time

import httpx

from .news_db import _conn as _news_conn, init_db as _news_init

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
TV_URL = "https://economic-calendar.tradingview.com/events"
KALSHI_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"

# 高影響事件關鍵字（用於分級加強：這些一定推 + 設靜默期）
KEY_EVENTS = ("CPI", "PPI", "Nonfarm", "Non-Farm", "Unemployment Claims",
              "Federal Funds Rate", "FOMC", "GDP", "Retail Sales", "PCE",
              "ISM", "Consumer Sentiment")

BLACKOUT_BEFORE_MIN = 30   # 事件前 30 分鐘暫停新訊號
BLACKOUT_AFTER_MIN = 15    # 事件後 15 分鐘


# ===========================================================================
# DB（econ_events 表，放 news_feed.db）
# ===========================================================================
def init_econ_db() -> None:
    _news_init()
    conn = _news_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS econ_events (
                key TEXT PRIMARY KEY,          -- title|ts 去重鍵
                title TEXT NOT NULL,
                ts_utc INTEGER NOT NULL,       -- 事件時間 epoch 秒
                impact TEXT,                   -- High / Medium / Low
                forecast TEXT, previous TEXT, actual TEXT,
                preview_pushed INTEGER DEFAULT 0,
                prealert_pushed INTEGER DEFAULT 0,
                actual_pushed INTEGER DEFAULT 0,
                updated_at INTEGER
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_econ_ts ON econ_events(ts_utc)")
    finally:
        conn.close()


def _upsert_event(title: str, ts_utc: int, impact: str,
                  forecast: str = "", previous: str = "") -> None:
    conn = _news_conn()
    try:
        key = f"{title}|{ts_utc}"
        conn.execute(
            """INSERT INTO econ_events (key, title, ts_utc, impact, forecast, previous, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 impact=excluded.impact, forecast=excluded.forecast,
                 previous=excluded.previous, updated_at=excluded.updated_at""",
            (key, title, ts_utc, impact, str(forecast or ""), str(previous or ""),
             int(time.time())),
        )
    finally:
        conn.close()


# ===========================================================================
# 數據源
# ===========================================================================
async def fetch_ff_schedule() -> int:
    """抓 ForexFactory 週曆（排程表）→ upsert USD High/Medium 事件。回新增/更新數。"""
    init_econ_db()
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(FF_URL, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        events = r.json()
    n = 0
    for ev in events:
        if ev.get("country") != "USD":
            continue
        impact = ev.get("impact", "")
        if impact not in ("High", "Medium"):
            continue
        try:
            ts = int(dt.datetime.fromisoformat(ev["date"]).timestamp())
        except Exception:
            continue
        _upsert_event(ev.get("title", "?"), ts, impact,
                      ev.get("forecast", ""), ev.get("previous", ""))
        n += 1
    return n


async def fetch_tv_actual(window_hours: float = 6.0) -> list[dict]:
    """抓 TradingView 日曆（含 actual）— 只查最近 window 小時內的美國事件。"""
    now = dt.datetime.now(dt.timezone.utc)
    frm = (now - dt.timedelta(hours=window_hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    to = (now + dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(TV_URL, params={"from": frm, "to": to, "countries": "US"},
                        headers={"Origin": "https://www.tradingview.com",
                                 "User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        body = r.json()
    return body.get("result", []) if isinstance(body, dict) else []


async def fetch_kalshi_fed() -> dict | None:
    """Kalshi FOMC 決議市場 → 利率決策機率。回 {label: pct} 或 None。"""
    try:
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        async with httpx.AsyncClient(timeout=15) as c:
            # 步驟 1：列出 series 下所有市場，找「最近的未來」event_ticker
            r = await c.get(KALSHI_URL, params={
                "series_ticker": "KXFEDDECISION", "limit": 100})
            r.raise_for_status()
            markets = r.json().get("markets", [])
            future = sorted(
                {m["event_ticker"]: m.get("close_time", "")
                 for m in markets
                 if m.get("status") == "active" and m.get("close_time", "") > now_iso
                 }.items(), key=lambda x: x[1])
            if not future:
                return None
            event_ticker = future[0][0]

            # 步驟 2：用 event_ticker 查報價（last_price_dollars 才有值）
            r2 = await c.get(KALSHI_URL, params={"event_ticker": event_ticker})
            r2.raise_for_status()
            ev_markets = r2.json().get("markets", [])

        out = {}
        for m in ev_markets:
            try:
                pct = float(m.get("last_price_dollars") or 0) * 100
            except (TypeError, ValueError):
                continue
            if pct <= 0:
                continue
            label = m.get("yes_sub_title") or m.get("subtitle") or m.get("ticker", "?")
            out[label] = round(pct, 1)
        return {"event": event_ticker, "probs": out} if out else None
    except Exception as e:
        print(f"[econ] kalshi error: {type(e).__name__}")
        return None


# ===========================================================================
# Blackout（risk_manager 引用）
# ===========================================================================
def in_blackout(now_ts: int | None = None) -> tuple[bool, str]:
    """高影響事件 -30min ~ +15min → (True, 事件名)。任何錯誤回 (False, '')。"""
    try:
        init_econ_db()
        now = now_ts or int(time.time())
        conn = _news_conn()
        try:
            row = conn.execute(
                "SELECT title, ts_utc FROM econ_events "
                "WHERE impact='High' AND ts_utc BETWEEN ? AND ? LIMIT 1",
                (now - BLACKOUT_AFTER_MIN * 60, now + BLACKOUT_BEFORE_MIN * 60),
            ).fetchone()
        finally:
            conn.close()
        if row:
            return True, row[0]
        return False, ""
    except Exception as e:
        print(f"[econ] in_blackout 降級放行（DB/查詢故障）：{type(e).__name__}")
        return False, ""


# ===========================================================================
# LLM 利好/利空分析
# ===========================================================================
async def _analyze_release(title: str, actual: str, forecast: str,
                            previous: str) -> str:
    """Claude 判讀數據：利好/利空 crypto 與美股 + 一句話理由。失敗回空字串。"""
    from .llm_filter import _call_claude, _extract_json
    prompt = (
        "你是宏觀交易分析師。美國經濟數據剛公布，判斷對「加密貨幣」與「美股」"
        "各是利好/利空/中性，並用一句話解釋傳導機制（聯準會政策路徑→流動性→風險資產）。\n"
        "只輸出 JSON：{\"crypto\": \"利好|利空|中性\", \"stocks\": \"利好|利空|中性\", "
        "\"reason\": \"繁中一句話\", \"strength\": 1-10}\n\n"
        f"數據：{title}\n實際值：{actual}　預期：{forecast}　前值：{previous}"
    )
    raw = await _call_claude(prompt, timeout_sec=60)
    if not raw:
        return ""
    obj = _extract_json(raw)
    if not obj:
        return ""
    icon = {"利好": "🟢", "利空": "🔴", "中性": "⚪"}
    c, s = obj.get("crypto", "中性"), obj.get("stocks", "中性")
    return (f"{icon.get(c, '⚪')} 加密 <b>{c}</b>　{icon.get(s, '⚪')} 美股 <b>{s}</b>"
            f"（強度 {obj.get('strength', '?')}/10）\n💡 {obj.get('reason', '')}")


# ===========================================================================
# Worker 主迴圈
# ===========================================================================
async def run_econ_calendar_loop(tg, tick_seconds: int = 60):
    """經濟數據 worker：排程刷新 + 事件預警 + actual 即時推播。"""
    import html as _html
    print("[econ] starting loop")
    init_econ_db()
    last_ff_fetch = 0.0
    last_preview_date = ""

    while True:
        try:
            now = int(time.time())

            # === 1. 每小時刷新排程表（FF rate limit 嚴格，絕不超頻）===
            if time.monotonic() - last_ff_fetch > 3600:
                try:
                    n = await fetch_ff_schedule()
                    last_ff_fetch = time.monotonic()
                    print(f"[econ] FF schedule refreshed: {n} USD events")
                except Exception as e:
                    print(f"[econ] FF fetch error: {type(e).__name__}")
                    last_ff_fetch = time.monotonic() - 3000  # 10 分鐘後重試

            conn = _news_conn()
            try:
                # === 2. 每日預告（00:10 UTC = 08:10 台北）===
                today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
                hour_utc = dt.datetime.now(dt.timezone.utc).hour
                if today != last_preview_date and hour_utc >= 0 and (
                        dt.datetime.now(dt.timezone.utc).hour * 60 +
                        dt.datetime.now(dt.timezone.utc).minute >= 10):
                    day_start = int(dt.datetime.now(dt.timezone.utc).replace(
                        hour=0, minute=0, second=0).timestamp())
                    rows = conn.execute(
                        "SELECT title, ts_utc, impact, forecast, previous FROM econ_events "
                        "WHERE ts_utc BETWEEN ? AND ? ORDER BY ts_utc",
                        (day_start, day_start + 86400)).fetchall()
                    last_preview_date = today
                    if rows:
                        lines = ["📅 <b>今日美國經濟數據預告</b>", "━━━━━━━━━━━━━━━━"]
                        for t, ts, imp, fc, pv in rows:
                            tw = dt.datetime.fromtimestamp(ts, dt.timezone.utc) + dt.timedelta(hours=8)
                            star = "🔴" if imp == "High" else "🟡"
                            fc_str = f"　預期 <code>{_html.escape(str(fc))}</code>" if fc else ""
                            lines.append(f"{star} <code>{tw.strftime('%H:%M')}</code> "
                                         f"{_html.escape(t)}{fc_str}")
                        # Kalshi 利率機率
                        kalshi = await fetch_kalshi_fed()
                        if kalshi:
                            top = sorted(kalshi["probs"].items(), key=lambda x: -x[1])[:3]
                            lines.append("")
                            lines.append("<b>FOMC 利率預期（Kalshi 預測市場）</b>")
                            for label, pct in top:
                                lines.append(f"  • {_html.escape(str(label))}: <code>{pct}%</code>")
                        lines.append("\n<i>🔴 高影響數據發布前 30 分鐘起暫停新交易訊號</i>")
                        await tg.send_message("\n".join(lines), parse_mode="HTML")
                        print(f"[econ] daily preview sent ({len(rows)} events)")

                # === 3. T-30min 預警 ===
                rows = conn.execute(
                    "SELECT key, title, ts_utc, forecast FROM econ_events "
                    "WHERE impact='High' AND prealert_pushed=0 "
                    "AND ts_utc BETWEEN ? AND ?",
                    (now, now + BLACKOUT_BEFORE_MIN * 60)).fetchall()
                for key, title, ts, fc in rows:
                    mins = max(1, (ts - now) // 60)
                    fc_str = f"　市場預期 <code>{_html.escape(str(fc))}</code>" if fc else ""
                    await tg.send_message(
                        f"⚠️ <b>{mins} 分鐘後發布：{_html.escape(title)}</b>{fc_str}\n"
                        f"🔇 進入訊號靜默期（發布後 15 分鐘解除）— 波動將放大，"
                        f"已持倉者注意止損。", parse_mode="HTML")
                    conn.execute("UPDATE econ_events SET prealert_pushed=1 WHERE key=?", (key,))
                    print(f"[econ] prealert: {title}")

                # === 4. 發布後抓 actual（事件窗內才打 TradingView）===
                pending = conn.execute(
                    "SELECT key, title, ts_utc, forecast, previous FROM econ_events "
                    "WHERE actual_pushed=0 AND impact IN ('High','Medium') "
                    "AND ts_utc BETWEEN ? AND ?",
                    (now - 45 * 60, now)).fetchall()
                if pending:
                    try:
                        tv_events = await fetch_tv_actual(window_hours=2)
                    except Exception as e:
                        tv_events = []
                        print(f"[econ] TV fetch error: {type(e).__name__}")
                    for key, title, ts, fc, pv in pending:
                        # 模糊比對 TV 事件（標題字詞交集 + 時間 ±10min）
                        match = None
                        twords = set(title.lower().replace(",", " ").split())
                        for tv in tv_events:
                            if tv.get("actual") is None:
                                continue
                            tv_ts = tv.get("date", "")
                            try:
                                tvt = int(dt.datetime.fromisoformat(
                                    tv_ts.replace("Z", "+00:00")).timestamp())
                            except Exception:
                                continue
                            if abs(tvt - ts) > 600:
                                continue
                            vwords = set(str(tv.get("title", "")).lower().split())
                            if len(twords & vwords) >= 1:
                                match = tv
                                break
                        if not match:
                            continue
                        actual = str(match.get("actual", ""))
                        conn.execute(
                            "UPDATE econ_events SET actual=?, actual_pushed=1 WHERE key=?",
                            (actual, key))
                        # 推播 + LLM 判讀
                        analysis = await _analyze_release(title, actual, str(fc), str(pv))
                        beat = ""
                        try:
                            a, f = float(str(actual).rstrip("%KMB")), float(str(fc).rstrip("%KMB"))
                            beat = "（高於預期）" if a > f else "（低於預期）" if a < f else "（符合預期）"
                        except (ValueError, TypeError):
                            pass
                        text = (f"📊 <b>{_html.escape(title)} 公布</b>{beat}\n"
                                f"━━━━━━━━━━━━━━━━\n"
                                f"實際 <code>{_html.escape(actual)}</code>　"
                                f"預期 <code>{_html.escape(str(fc))}</code>　"
                                f"前值 <code>{_html.escape(str(pv))}</code>")
                        if analysis:
                            text += f"\n\n{analysis}"
                        await tg.send_message(text, parse_mode="HTML")
                        print(f"[econ] actual pushed: {title} = {actual}")
            finally:
                conn.close()

        except Exception as e:
            print(f"[econ] loop error: {type(e).__name__}: {e}")
        await asyncio.sleep(tick_seconds)


if __name__ == "__main__":
    async def selftest():
        n = await fetch_ff_schedule()
        print(f"FF schedule: {n} USD events upserted")
        bo, name = in_blackout()
        print(f"blackout now: {bo} {name}")
        k = await fetch_kalshi_fed()
        print(f"kalshi: {k}")
        tv = await fetch_tv_actual(12)
        with_actual = [t for t in tv if t.get("actual") is not None]
        print(f"TV events (12h): {len(tv)}, with actual: {len(with_actual)}")
        for t in with_actual[:3]:
            print(f"  {t.get('title')}: actual={t.get('actual')} fc={t.get('forecast')}")
    asyncio.run(selftest())
