"""全市場異常掃描器（v18-A）：356+ 檔 USDT 永續每 5 分鐘異常偵測。

設計（源自 SAHARA 崩盤案例研究 + SM 浪潮競品分析）：
    - 3 次免費請求覆蓋全市場：tickers + open-interest + funding-rate（已實測 0.2s）
    - 快照入 SQLite（3 天滾動），對每檔算 1h 報酬 / OI 1h 變化 / 資費極值
    - 多條件聯合觸發 + 流動性門檻 + 每檔每類 6h 冷卻 → 目標日均 ≤5 則警報
    - 市場廣度統計（v18-E 數據基礎）：漲跌檔數、動能檔數、平均資費

定位：「事中偵測器」— 崩盤/暴動進行中告警（候選順勢單或持倉避雷），
不預測導火線（事前層 = 解鎖日曆，v18-C）。
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time

import httpx

from botpaths import db_path as _db_path

DB_PATH = _db_path("scanner.db")
OKX = "https://www.okx.com"

# 閾值（env 可調）
MIN_VOL_USD = float(os.getenv("SCAN_MIN_VOL_USD", "10000000"))      # $10M 流動性門檻
PRICE_SHOCK_1H = float(os.getenv("SCAN_PRICE_SHOCK_1H", "6.0"))     # 1h ±6%
PRICE_SHOCK_HARD = float(os.getenv("SCAN_PRICE_SHOCK_HARD", "9.0")) # 單條件即發
OI_SHOCK_UP = float(os.getenv("SCAN_OI_SHOCK_UP", "15.0"))          # OI 1h +15%
OI_SHOCK_DOWN = float(os.getenv("SCAN_OI_SHOCK_DOWN", "-12.0"))
FUNDING_NEG = float(os.getenv("SCAN_FUNDING_NEG", "-0.002"))        # -0.2%
FUNDING_HOT = float(os.getenv("SCAN_FUNDING_HOT", "0.003"))         # +0.3%
ALERT_COOLDOWN_S = int(os.getenv("SCAN_ALERT_COOLDOWN_S", "21600")) # 6h
SNAPSHOT_KEEP_DAYS = 3

# ── 來源邊界：純加密永續允許集（task#73-C 治本）─────────────────────────────
# OKX 已把「代幣化美股」(instCategory=3：AAPL/NVDA/MU/QQQ…) 與「商品」(=4：
# CL 原油/XAU 黃金/NG…) 上架為 -USDT-SWAP。掃描器只看 -USDT-SWAP 後綴，會把這
# 103/372 檔非加密標的當加密幣吸進 scanner.db → 污染廣度統計、誤發異常告警，並經
# 候選池 → 交易層 → fire_tier → 模擬下單／deepdive。它們不屬加密 SMC 模型射程
# （美股走獨立 1h 突破引擎、用真實股市數據）。instCategory=="1" ＝純加密，於此濾除。
INSTRUMENTS_URL = f"{OKX}/api/v5/public/instruments"
CRYPTO_ALLOWSET_TTL_S = int(os.getenv("SCAN_INSTRUMENTS_TTL_S", "21600"))  # 6h
# 防截斷下限：OKX /public/instruments 實測單回應回全 269 檔純加密（無分頁）。萬一未來
# API 改成分頁/截斷，回一個「非空但可疑過小」的集合，不得拿它覆蓋既有健康集合（保留舊、
# 留痕）。冷啟動（舊集合為空）不套此下限，確保首次填充永不被擋（fail-open 大原則）。
_ALLOWSET_MIN_PLAUSIBLE = int(os.getenv("SCAN_INSTRUMENTS_MIN", "50"))
_CRYPTO_INSTIDS: set[str] = set()    # {'BTC-USDT-SWAP', …}  instCategory=='1'
_CRYPTO_BASES: set[str] = set()      # {'BTC', …}（給 watchlist 的 base 級判定）
_ALLOWSET_TS: float = 0.0


def _extract_crypto_instids(rows: list[dict]) -> set[str]:
    """從 OKX instruments 回應抽出『純加密 USDT 永續』instId（instCategory=='1'）。
    純函式、零網路、可離線測試。缺 instCategory / 非 USDT 永續一律不收。"""
    return {d["instId"] for d in rows
            if d.get("instCategory") == "1"
            and str(d.get("instId", "")).endswith("-USDT-SWAP")}


def _is_crypto_perp(inst_id: str, allow: set[str]) -> bool:
    """來源邊界判定：inst_id 是否為應納入的純加密 USDT 永續。
    allow 為空（冷啟動取不到 instruments）→ fail-open（True）：寧可暫時納入也不清空
    宇宙；呼叫端會印出警告（紅線③：no silent cap）。"""
    if not inst_id.endswith("-USDT-SWAP"):
        return False
    if not allow:
        return True
    return inst_id in allow


def is_crypto_base(base: str) -> bool:
    """base 級判定（給 watchlist 候選池用）：BTC→True、MU/CL→False。
    base 允許集空 → fail-open（True，寧納勿空宇宙）。"""
    if not _CRYPTO_BASES:
        return True
    return base in _CRYPTO_BASES


async def ensure_crypto_allowset(client=None) -> None:
    """確保純加密允許集新鮮（TTL 內為 no-op）。失敗保留舊快取、絕不清空。
    instruments 變動極少，預設每 6h 才刷新一次；可傳入共用 client 省一次連線。"""
    global _CRYPTO_INSTIDS, _CRYPTO_BASES, _ALLOWSET_TS
    if _CRYPTO_INSTIDS and (time.time() - _ALLOWSET_TS) < CRYPTO_ALLOWSET_TTL_S:
        return
    own = client is None
    try:
        c = client or httpx.AsyncClient(timeout=25)
        try:
            r = await c.get(INSTRUMENTS_URL, params={"instType": "SWAP"})
            rows = r.json().get("data", [])
        finally:
            if own:
                await c.aclose()
        instids = _extract_crypto_instids(rows)
        if not instids:
            return    # 守護：壞回應/空集不得清空允許集（保留舊）
        # 防截斷：已有健康集合卻收到可疑過小的新集合 → 保留舊、留痕（冷啟動不套下限）
        if _CRYPTO_INSTIDS and len(instids) < _ALLOWSET_MIN_PLAUSIBLE:
            print(f"[scanner] instruments 回應僅 {len(instids)} 檔純加密"
                  f"（< 合理下限 {_ALLOWSET_MIN_PLAUSIBLE}），疑截斷/API 變更 → "
                  f"保留舊允許集 {len(_CRYPTO_INSTIDS)} 檔")
            return
        _CRYPTO_INSTIDS = instids
        _CRYPTO_BASES = {i.split("-")[0] for i in instids}
        _ALLOWSET_TS = time.time()
    except Exception as e:
        print(f"[scanner] instruments allowset refresh failed "
              f"(keep {len(_CRYPTO_INSTIDS)}): {type(e).__name__}: {e}")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    conn = _conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                ts INTEGER NOT NULL,            -- epoch 秒（取整到分鐘）
                inst TEXT NOT NULL,             -- base symbol（BTC）
                last REAL, vol24h_usd REAL, oi_usd REAL, funding REAL,
                chg24h_pct REAL,
                PRIMARY KEY (ts, inst)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                inst TEXT NOT NULL,
                alert_type TEXT NOT NULL,       -- 'dump' / 'pump' / 'oi_shock' / 'funding'
                last_ts INTEGER NOT NULL,
                PRIMARY KEY (inst, alert_type)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS breadth (
                ts INTEGER PRIMARY KEY,
                n_total INTEGER, n_up24h INTEGER, n_down24h INTEGER,
                n_up1h INTEGER, n_down1h INTEGER,
                n_overheat INTEGER, n_oversold INTEGER,
                avg_funding REAL,
                n_scanned INTEGER
            )
        """)
        # 遷移：舊 DB 補 n_scanned。
        # n_total = 流動性達標數（≥$10M）；n_scanned = 全市場原始掃描檔數（所有 USDT 永續）。
        # 兩者分開存，開機橫幅才不會把 87(達標) 誤標成「全市場掃描」(實際 ~372)。
        try:
            conn.execute("ALTER TABLE breadth ADD COLUMN n_scanned INTEGER")
        except sqlite3.OperationalError:
            pass  # 欄位已存在
        conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_inst ON snapshots(inst, ts)")
    finally:
        conn.close()


async def fetch_market_snapshot() -> dict[str, dict]:
    """3 次請求 → {base_symbol: {last, vol24h_usd, oi_usd, funding, chg24h_pct}}"""
    async with httpx.AsyncClient(timeout=25) as c:
        await ensure_crypto_allowset(c)
        r1, r2, r3 = await asyncio.gather(
            c.get(f"{OKX}/api/v5/market/tickers", params={"instType": "SWAP"}),
            c.get(f"{OKX}/api/v5/public/open-interest", params={"instType": "SWAP"}),
            c.get(f"{OKX}/api/v5/public/funding-rate", params={"instId": "ANY"}),
        )
    tickers = r1.json().get("data", [])
    ois = {d["instId"]: d for d in r2.json().get("data", [])}
    fundings = {d["instId"]: d for d in r3.json().get("data", [])}

    allow = _CRYPTO_INSTIDS
    n_noncrypto = 0
    out: dict[str, dict] = {}
    for t in tickers:
        inst_id = t.get("instId", "")
        if not inst_id.endswith("-USDT-SWAP"):
            continue
        if not _is_crypto_perp(inst_id, allow):    # 來源邊界：濾除代幣化美股/商品
            n_noncrypto += 1
            continue
        base = inst_id.split("-")[0]
        try:
            last = float(t.get("last") or 0)
            o24 = float(t.get("open24h") or 0)
            vol_usd = float(t.get("volCcy24h") or 0) * last
            oi = ois.get(inst_id, {})
            fr = fundings.get(inst_id, {})
            out[base] = {
                "last": last,
                "vol24h_usd": vol_usd,
                "oi_usd": float(oi.get("oiUsd") or 0) or None,
                "funding": float(fr.get("fundingRate")) if fr.get("fundingRate") else None,
                "chg24h_pct": (last / o24 - 1) * 100 if o24 else None,
            }
        except (TypeError, ValueError):
            continue
    # no silent cap（紅線③）：把過濾結果留痕，永不靜默砍宇宙
    if not allow:
        print("[scanner] instruments 允許集為空（冷啟動／取用失敗）→ "
              "fail-open 暫納全部 USDT 永續（含代幣化美股/商品），下輪刷新後收斂")
    elif n_noncrypto:
        print(f"[scanner] 來源濾除 {n_noncrypto} 檔非加密永續"
              f"（代幣化美股/商品 instCategory!=1），純加密 {len(out)} 檔")
    return out


def _save_snapshot(ts: int, snap: dict[str, dict]) -> None:
    conn = _conn()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(ts, sym, d["last"], d["vol24h_usd"], d["oi_usd"], d["funding"],
              d["chg24h_pct"]) for sym, d in snap.items()],
        )
        conn.execute("DELETE FROM snapshots WHERE ts < ?",
                     (ts - SNAPSHOT_KEEP_DAYS * 86400,))
    finally:
        conn.close()


def _get_past_snapshot(ts_now: int, minutes_ago: int,
                       tolerance_min: int = 4) -> dict[str, tuple]:
    """取約 N 分鐘前的快照 {inst: (last, oi_usd)}"""
    conn = _conn()
    try:
        target = ts_now - minutes_ago * 60
        row = conn.execute(
            "SELECT ts FROM snapshots WHERE ts BETWEEN ? AND ? "
            "ORDER BY ABS(ts - ?) LIMIT 1",
            (target - tolerance_min * 60, target + tolerance_min * 60, target),
        ).fetchone()
        if not row:
            return {}
        rows = conn.execute(
            "SELECT inst, last, oi_usd FROM snapshots WHERE ts = ?", (row[0],),
        ).fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}
    finally:
        conn.close()


def _alert_allowed(inst: str, alert_type: str, now: int) -> bool:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT last_ts FROM alerts WHERE inst=? AND alert_type=?",
            (inst, alert_type),
        ).fetchone()
        if row and now - row[0] < ALERT_COOLDOWN_S:
            return False
        conn.execute("INSERT OR REPLACE INTO alerts VALUES (?, ?, ?)",
                     (inst, alert_type, now))
        return True
    finally:
        conn.close()


def detect_anomalies(snap: dict[str, dict], past_1h: dict[str, tuple],
                     now: int) -> list[dict]:
    """多條件異常偵測。回 alert dicts（已過冷卻）。"""
    alerts = []
    for sym, d in snap.items():
        if d["vol24h_usd"] < MIN_VOL_USD:
            continue
        past = past_1h.get(sym)
        r1h = None
        oi_chg_1h = None
        if past and past[0]:
            r1h = (d["last"] / past[0] - 1) * 100
        if past and past[1] and d["oi_usd"]:
            oi_chg_1h = (d["oi_usd"] / past[1] - 1) * 100

        conds = []
        if r1h is not None and abs(r1h) >= PRICE_SHOCK_1H:
            conds.append(("price", r1h))
        if oi_chg_1h is not None and (oi_chg_1h >= OI_SHOCK_UP or oi_chg_1h <= OI_SHOCK_DOWN):
            conds.append(("oi", oi_chg_1h))
        f = d["funding"]
        if f is not None and (f <= FUNDING_NEG or f >= FUNDING_HOT):
            conds.append(("funding", f))

        # 觸發規則：≥2 條件聯合，或單一價格劇震 ≥ HARD
        hard = r1h is not None and abs(r1h) >= PRICE_SHOCK_HARD
        if len(conds) >= 2 or hard:
            direction = "dump" if (r1h or 0) < 0 else "pump"
            if not _alert_allowed(sym, direction, now):
                continue
            alerts.append({
                "symbol": sym, "direction": direction,
                "last": d["last"], "r1h": r1h, "chg24h": d["chg24h_pct"],
                "oi_chg_1h": oi_chg_1h, "funding": f,
                "vol24h_usd": d["vol24h_usd"],
                "conds": [c[0] for c in conds],
            })
    # 嚴重度排序，單輪最多 3 則（防極端行情轟炸）
    alerts.sort(key=lambda a: -abs(a["r1h"] or 0))
    return alerts[:3]


def compute_breadth(snap: dict[str, dict], past_1h: dict[str, tuple],
                    now: int) -> dict:
    """市場廣度統計（v18-E 基礎）並入庫。"""
    liquid = {s: d for s, d in snap.items() if d["vol24h_usd"] >= MIN_VOL_USD}
    up24 = sum(1 for d in liquid.values() if (d["chg24h_pct"] or 0) > 2)
    dn24 = sum(1 for d in liquid.values() if (d["chg24h_pct"] or 0) < -2)
    up1h = dn1h = 0
    for s, d in liquid.items():
        p = past_1h.get(s)
        if p and p[0]:
            r = (d["last"] / p[0] - 1) * 100
            if r > 1:
                up1h += 1
            elif r < -1:
                dn1h += 1
    overheat = sum(1 for d in liquid.values() if (d["funding"] or 0) >= 0.001)
    oversold = sum(1 for d in liquid.values() if (d["chg24h_pct"] or 0) < -8)
    fundings = [d["funding"] for d in liquid.values() if d["funding"] is not None]
    avg_f = sum(fundings) / len(fundings) if fundings else 0.0

    b = {"ts": now, "n_total": len(liquid), "n_up24h": up24, "n_down24h": dn24,
         "n_up1h": up1h, "n_down1h": dn1h, "n_overheat": overheat,
         "n_oversold": oversold, "avg_funding": round(avg_f, 6),
         "n_scanned": len(snap)}   # 全市場原始掃描檔數（n_total 是其中達 $10M 流動性的子集）
    conn = _conn()
    try:
        conn.execute("INSERT OR REPLACE INTO breadth VALUES (?,?,?,?,?,?,?,?,?,?)",
                     tuple(b.values()))
        conn.execute("DELETE FROM breadth WHERE ts < ?", (now - 7 * 86400,))
    finally:
        conn.close()
    return b


def get_latest_breadth() -> dict | None:
    """給 /status 與未來 regime 閘門用"""
    init_db()
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM breadth ORDER BY ts DESC LIMIT 1").fetchone()
        if not row:
            return None
        keys = ["ts", "n_total", "n_up24h", "n_down24h", "n_up1h", "n_down1h",
                "n_overheat", "n_oversold", "avg_funding", "n_scanned"]
        return dict(zip(keys, row[:len(keys)]))
    finally:
        conn.close()


def breadth_caution(direction: str) -> str:
    """v18-E 軟閘門：極端廣度逆風時給 FIRE 訊息加警示（提示不阻擋）。
    無數據/不極端回空字串。"""
    try:
        b = get_latest_breadth()
        if not b or b["n_total"] < 30:
            return ""
        dn_ratio = b["n_down1h"] / max(b["n_up1h"] + b["n_down1h"], 1)
        up_ratio = b["n_up1h"] / max(b["n_up1h"] + b["n_down1h"], 1)
        if direction == "bull" and dn_ratio >= 0.8 and b["n_down1h"] >= 15:
            return (f"\n🌐 <b>廣度警示</b>：全市場 1h ↓{b['n_down1h']} vs "
                    f"↑{b['n_up1h']} 檔 — 大盤逆風做多，建議降低倉位或等待")
        if direction == "bear" and up_ratio >= 0.8 and b["n_up1h"] >= 15:
            return (f"\n🌐 <b>廣度警示</b>：全市場 1h ↑{b['n_up1h']} vs "
                    f"↓{b['n_down1h']} 檔 — 大盤逆風做空，建議降低倉位或等待")
        return ""
    except Exception:
        return ""


def render_breadth_line(b: dict | None) -> str:
    if not b:
        return "🌐 市場廣度：累積數據中…"
    bias = "🟢 偏多" if b["n_up24h"] > b["n_down24h"] * 1.5 else (
           "🔴 偏空" if b["n_down24h"] > b["n_up24h"] * 1.5 else "⚪ 中性")
    return (f"🌐 市場廣度（{b['n_total']} 檔）：{bias}　"
            f"24h ↑<code>{b['n_up24h']}</code>/↓<code>{b['n_down24h']}</code>　"
            f"1h ↑<code>{b['n_up1h']}</code>/↓<code>{b['n_down1h']}</code>　"
            f"超跌 <code>{b['n_oversold']}</code>　"
            f"均資費 <code>{b['avg_funding']*100:+.3f}%</code>")


def _render_alert(a: dict) -> str:
    import html as _html
    icon = "🔻" if a["direction"] == "dump" else "🚀"
    dir_zh = "急跌" if a["direction"] == "dump" else "急漲"
    sug = ("順勢空單候選（注意反彈軋空，崩盤幣當日可反彈 20%+）"
           if a["direction"] == "dump"
           else "順勢多單候選（注意追高風險）")
    lines = [
        f"⚡ <b>{_html.escape(a['symbol'])} 全市場異常警報 — {dir_zh}</b>",
        f"━━━━━━━━━━━━━━━━",
        f"現價 <code>${a['last']:,.6g}</code>　"
        f"1h <code>{a['r1h']:+.1f}%</code>　24h <code>{(a['chg24h'] or 0):+.1f}%</code>",
        f"量 <code>${a['vol24h_usd']/1e6:,.0f}M</code>"
        + (f"　OI 1h <code>{a['oi_chg_1h']:+.1f}%</code>" if a["oi_chg_1h"] is not None else "")
        + (f"　資費 <code>{a['funding']*100:+.3f}%</code>" if a["funding"] is not None else ""),
        f"觸發：<code>{'+'.join(a['conds']) or 'hard_shock'}</code>",
        f"💡 {sug}",
    ]
    # v18-C: 解鎖風險附註（崩盤導火線常與解鎖搶跑相關）
    try:
        from news_feed.unlock_calendar import render_unlock_warning
        w = render_unlock_warning(a["symbol"])
        if w:
            lines.append(w.strip())
    except Exception:
        pass
    lines.append(f"<i>⚠️ 異常偵測非交易訊號 — 此幣不在白名單內無結構分析，僅供人工評估</i>")
    return "\n".join(lines)


async def run_market_scanner_loop(tg, interval_seconds: int = 300):
    """Worker：每 5 分鐘掃全市場。冷啟動需累積 ~1h 快照後才開始比對。"""
    print(f"[scanner] starting loop, interval={interval_seconds}s "
          f"(min_vol=${MIN_VOL_USD/1e6:.0f}M, shock={PRICE_SHOCK_1H}%/1h)")
    init_db()
    await asyncio.sleep(45)
    quadrant_pushed_day = ""   # v21-A: 每日資金流向圖推送去重

    while True:
        try:
            now = int(time.time()) // 60 * 60
            snap = await fetch_market_snapshot()
            if snap:
                _save_snapshot(now, snap)
                past_1h = _get_past_snapshot(now, 60)
                breadth = compute_breadth(snap, past_1h, now)

                # v21-A: 每日 09:00 台北（01:00 UTC）推四象限資金流向圖
                utc_now = time.gmtime(now)
                day_key = time.strftime("%Y%m%d", utc_now)
                if utc_now.tm_hour == 1 and quadrant_pushed_day != day_key:
                    quadrant_pushed_day = day_key
                    try:
                        from l3_dispatcher.scanner_viz import (
                            quadrant_summary_line, render_quadrant_chart)
                        chart = render_quadrant_chart()
                        if chart:
                            cap = ("🧭 全市場資金流向圖（4h OI × 價格）\n"
                                   + quadrant_summary_line())
                            await tg.send_photo(chart, caption=cap[:1024],
                                                parse_mode=None)
                            print(f"[scanner] 🧭 quadrant chart pushed")
                    except Exception as e:
                        print(f"[scanner] quadrant error: {type(e).__name__}: {e}")
                if past_1h:
                    alerts = detect_anomalies(snap, past_1h, now)
                    for a in alerts:
                        try:
                            await tg.send_message(_render_alert(a), parse_mode="HTML")
                            print(f"[scanner] ⚡ ALERT {a['symbol']} {a['direction']} "
                                  f"1h={a['r1h']:+.1f}% conds={a['conds']}")
                        except Exception as e:
                            print(f"[scanner] alert send error: {e}")
                    if not alerts:
                        pass  # 安靜是常態
                else:
                    print(f"[scanner] warming up ({len(snap)} symbols, "
                          f"breadth: ↑{breadth['n_up24h']}/↓{breadth['n_down24h']})")
        except Exception as e:
            print(f"[scanner] loop error: {type(e).__name__}: {e}")
        await asyncio.sleep(interval_seconds)


if __name__ == "__main__":
    async def selftest():
        init_db()
        now = int(time.time()) // 60 * 60
        snap = await fetch_market_snapshot()
        print(f"snapshot: {len(snap)} USDT swaps")
        _save_snapshot(now, snap)
        past = _get_past_snapshot(now, 60)
        print(f"past-1h snapshot: {len(past)} symbols "
              f"({'冷啟動中，1h 後才有比對基準' if not past else 'OK'})")
        b = compute_breadth(snap, past, now)
        print(render_breadth_line(b).replace('<code>', '').replace('</code>', ''))
        if past:
            alerts = detect_anomalies(snap, past, now)
            print(f"alerts: {len(alerts)}")
            for a in alerts:
                print(f"  {a['symbol']} {a['direction']} 1h={a['r1h']:+.1f}% {a['conds']}")
        # 模擬測試：人造一個 -10% 急跌驗證偵測邏輯
        fake_past = {s: (d["last"] * 1.12, (d["oi_usd"] or 0) * 0.8)
                     for s, d in list(snap.items())[:3]}
        fake_alerts = detect_anomalies(
            {s: snap[s] for s in fake_past}, fake_past, now + 99999)
        print(f"synthetic dump test: {len(fake_alerts)} alerts "
              f"({'PASS' if fake_alerts else 'FAIL'})")
    asyncio.run(selftest())
