"""代幣解鎖日曆（v18-C）：事前數週的崩盤預警層。

源自 SAHARA 案例：6/26 解鎖 30.1% 流通量是提前數週公開的資訊，
搶跑拋售是崩盤主因之一 — 這層在事發前數週就能標記風險。

數據源（全免費）：
    - DefiLlama emissionsIndex（21MB，每日抓 1 次）→ 未來解鎖事件
    - CoinGecko /coins/list（id→ticker 對映，每日 1 次）

輸出：
    - get_unlock_risk(symbol)：給 dispatcher/scanner 查「此幣近期有大解鎖嗎」
    - 每日解鎖預告（7 天內 ≥5% 流通量）推 📅 經濟數據主題
"""
from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3
import time

import httpx

from botpaths import db_path as _db_path

DB_PATH = _db_path("scanner.db")  # 與掃描器同檔（同屬市場結構數據）

EMISSIONS_URL = "https://defillama-datasets.llama.fi/emissionsIndex"
GECKO_LIST_URL = "https://api.coingecko.com/api/v3/coins/list"

RISK_PCT_MIN = 5.0      # 解鎖 ≥5% 流通量才列入風險表
DIGEST_PCT_MIN = 5.0    # 每日預告門檻（7 天內）
LOOKAHEAD_DAYS = 35


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    conn = _conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS unlock_risks (
                symbol TEXT PRIMARY KEY,        -- ticker 大寫（SPK）
                name TEXT,
                unlock_ts INTEGER,              -- 最近一次大解鎖 epoch 秒
                unlock_pct REAL,                -- 佔流通量 %
                category TEXT,
                updated_at INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS unlock_meta (
                k TEXT PRIMARY KEY, v TEXT
            )
        """)
    finally:
        conn.close()


async def _fetch_gecko_map() -> dict[str, str]:
    """gecko_id → TICKER 大寫"""
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(GECKO_LIST_URL)
        r.raise_for_status()
        return {d["id"]: (d.get("symbol") or "").upper()
                for d in r.json() if d.get("id")}


async def refresh_unlock_calendar() -> int:
    """每日刷新：抓 emissions + gecko 對映 → 寫 unlock_risks。回筆數。"""
    init_db()
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.get(EMISSIONS_URL)
        r.raise_for_status()
        data = r.json().get("data", [])

    try:
        gecko_map = await _fetch_gecko_map()
    except Exception as e:
        print(f"[unlock] gecko map failed ({type(e).__name__}), name-only fallback")
        gecko_map = {}

    now = time.time()
    rows = []
    for d in data:
        circ = _f(d.get("circSupply"))
        if circ <= 0:
            continue
        sym = gecko_map.get(d.get("gecko_id") or "", "")
        if not sym:
            continue
        best = None
        for e in (d.get("events") or []):
            ts = _f(e.get("timestamp"))
            if not (now < ts < now + LOOKAHEAD_DAYS * 86400):
                continue
            amt = max((_f(t) for t in (e.get("noOfTokens") or [])), default=0)
            pct = amt / circ * 100
            if pct >= RISK_PCT_MIN and (best is None or pct > best[1]):
                best = (int(ts), pct, e.get("category") or "")
        if best:
            rows.append((sym, d.get("name") or sym, best[0], round(best[1], 1),
                         best[2], int(now)))

    conn = _conn()
    try:
        conn.execute("DELETE FROM unlock_risks")
        conn.executemany(
            "INSERT OR REPLACE INTO unlock_risks VALUES (?, ?, ?, ?, ?, ?)", rows)
        conn.execute("INSERT OR REPLACE INTO unlock_meta VALUES ('last_refresh', ?)",
                     (str(int(now)),))
    finally:
        conn.close()
    return len(rows)


def get_unlock_risk(symbol: str, within_days: int = 30) -> dict | None:
    """查某 ticker 近期是否有大解鎖。回 {unlock_ts, unlock_pct, days_away} 或 None。
    任何錯誤回 None（絕不阻塞交易管線）。"""
    try:
        init_db()
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT unlock_ts, unlock_pct, name FROM unlock_risks WHERE symbol=?",
                (symbol.upper(),),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        days_away = (row[0] - time.time()) / 86400
        if not (0 <= days_away <= within_days):
            return None
        return {"unlock_ts": row[0], "unlock_pct": row[1],
                "days_away": round(days_away, 1), "name": row[2]}
    except Exception:
        return None


def render_unlock_warning(symbol: str) -> str:
    """給 FIRE/警報訊息附註用。無風險回空字串。"""
    r = get_unlock_risk(symbol, within_days=14)
    if not r:
        return ""
    return (f"\n🔓 <b>解鎖警告</b>：{r['days_away']:.0f} 天後解鎖 "
            f"<b>{r['unlock_pct']}%</b> 流通量（搶跑拋壓風險，做多需謹慎）")


def get_week_digest() -> list[dict]:
    """未來 7 天 ≥5% 的解鎖清單"""
    init_db()
    conn = _conn()
    try:
        now = int(time.time())
        rows = conn.execute(
            "SELECT symbol, name, unlock_ts, unlock_pct FROM unlock_risks "
            "WHERE unlock_ts BETWEEN ? AND ? AND unlock_pct >= ? "
            "ORDER BY unlock_ts",
            (now, now + 7 * 86400, DIGEST_PCT_MIN),
        ).fetchall()
        return [{"symbol": r[0], "name": r[1], "ts": r[2], "pct": r[3]} for r in rows]
    finally:
        conn.close()


def _unlock_state_path():
    from botpaths import data_dir
    return data_dir() / "unlock_state.json"


def _load_unlock_state() -> dict:
    import json
    try:
        return json.loads(_unlock_state_path().read_text(encoding="utf-8"))
    except Exception:
        return {"last_push_date": "", "pinned_msg_id": None}


def _save_unlock_state(st: dict) -> None:
    import json
    try:
        _unlock_state_path().write_text(json.dumps(st), encoding="utf-8")
    except Exception:
        pass


async def run_unlock_calendar_loop(tg, refresh_hour_utc: int = 0):
    """Worker：每日 00:20 UTC 刷新 + 解鎖預告（v33：改『置頂留言』取代重複推播）。

    使用者回饋：解鎖是已知資訊，每日重推佔版面。改為：當日產一則預告→置頂；
    隔日刷新時先取消舊置頂、再發新預告並置頂；無料則取消置頂不發。
    狀態持久化（last_push_date + pinned_msg_id），重啟不會重推。"""
    print("[unlock] starting loop")
    await asyncio.sleep(180)
    state = _load_unlock_state()

    while True:
        try:
            n = await refresh_unlock_calendar()
            print(f"[unlock] calendar refreshed: {n} tokens with ≥{RISK_PCT_MIN}% "
                  f"unlocks in {LOOKAHEAD_DAYS}d")

            today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
            if today != state.get("last_push_date") and tg is not None:
                digest = get_week_digest()
                # 先取消舊置頂（時效已過 / 即將被新的取代）
                old_pin = state.get("pinned_msg_id")
                if old_pin:
                    try:
                        await tg.unpin_chat_message(old_pin)
                    except Exception as e:
                        print(f"[unlock] unpin error: {e}")
                    state["pinned_msg_id"] = None
                if digest:
                    import html as _html
                    lines = ["📌🔓 <b>未來 7 天大額解鎖預告</b>（≥5% 流通量・每日更新）",
                             "━━━━━━━━━━━━━━━━"]
                    for d in digest[:12]:
                        date_str = dt.datetime.fromtimestamp(
                            d["ts"], dt.timezone.utc).strftime("%m/%d")
                        lines.append(
                            f"  <code>{date_str}</code> "
                            f"<b>{_html.escape(d['symbol'])}</b>"
                            f"（{_html.escape(d['name'] or '')[:18]}）"
                            f" 解鎖 <code>{d['pct']}%</code>")
                    lines.append("\n<i>解鎖前 1-2 週常見搶跑拋售（SAHARA 6/9 案例）"
                                 "— 持有/做多名單內幣種請留意。此則為置頂、每日刷新，不重複洗版。</i>")
                    try:
                        resp = await tg.send_message("\n".join(lines), parse_mode="HTML")
                        mid = (resp or {}).get("result", {}).get("message_id")
                        if mid:
                            try:
                                await tg.pin_chat_message(mid)
                                state["pinned_msg_id"] = mid
                            except Exception as e:
                                print(f"[unlock] pin error: {e}")
                        print(f"[unlock] digest pinned ({len(digest)} tokens, msg={mid})")
                    except Exception as e:
                        print(f"[unlock] push error: {e}")
                else:
                    print("[unlock] no digest today, kept unpinned")
                state["last_push_date"] = today
                _save_unlock_state(state)
        except Exception as e:
            print(f"[unlock] loop error: {type(e).__name__}: {e}")

        # 睡到明日 00:20 UTC
        now = dt.datetime.now(dt.timezone.utc)
        nxt = now.replace(hour=refresh_hour_utc, minute=20, second=0) + dt.timedelta(days=1)
        await asyncio.sleep(max(3600, (nxt - now).total_seconds()))


if __name__ == "__main__":
    async def selftest():
        n = await refresh_unlock_calendar()
        print(f"refreshed: {n} risk tokens")
        digest = get_week_digest()
        print(f"7d digest: {len(digest)}")
        for d in digest[:8]:
            print(f"  {d['symbol']:8s} {dt.datetime.fromtimestamp(d['ts']).strftime('%m-%d')} {d['pct']}%")
        # 測 OKX 白名單交集
        from l3_dispatcher.market_scanner import fetch_market_snapshot
        snap = await fetch_market_snapshot()
        hits = [d for d in digest if d["symbol"] in snap]
        print(f"OKX 可交易且 7 天內解鎖: {[d['symbol'] for d in hits]}")
        for sym in list(snap.keys())[:0]:
            pass
        w = render_unlock_warning(digest[0]["symbol"]) if digest else ""
        print("warning render:", w.replace("<b>", "").replace("</b>", "")[:100] or "(無)")
    asyncio.run(selftest())
