"""事件脈絡敘事引擎（v25）— 把孤立新聞串成因果敘事。

使用者願景：新聞不該「推完就忘」。跨 3d/1w/2w/1m 追蹤事件、聚成主軸敘事、
推論因果鏈、關聯經濟數據，讓機器人具備「世界因果背景」再交易。

分層（慢慢堆疊，不求快）：
    第 1 層 ✅ 事件持久化檢索 — news_feed.db 已累積，這裡建跨時窗撈取
    第 2 層 ✅ 敘事聚類 — 每日 Claude session 把近期事件聚成主軸敘事
    第 3 層 ✅ 因果鏈 — 敘事內事件的前後因果（A 導致 B）
    第 4 層 ✅ 經濟數據關聯 — _econ_context_lines() 把近期數據（實際/預期/前值）餵進
            敘事 prompt，於因果鏈連結「數據意外→市場反應」（正式 econ×news 回測仍待補）
    第 5 層 ⚠️ 僅顯示卡 — narrative_alignment() 產生「訊號 vs 主導敘事順風/逆風」註記，
            但【目前僅餵 Telegram 顯示卡（telegram_bot/message_format.py:175-176），
            未進開單路徑】。消息面進訊號層的首次嘗試走 news_score.py 影子層 + 回測閘，
            在離線回測證明顯著且人工拍板前，對下單數學影響嚴格為零（紅線③：不得宣稱
            「消息面已進訊號層」）。

本檔交付第 1-4 層的敘事建構，第 5 層僅作顯示：narrative.db 儲存敘事、每日聚類
worker、敘事摘要、get_active_narratives() / narrative_alignment()（顯示卡專用）。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time

from botpaths import db_path as _db_path

DB_PATH = _db_path("narrative.db")
NEWS_DB = _db_path("news_feed.db")

WINDOWS = {"3d": 3, "1w": 7, "2w": 14, "1m": 30}   # 天


def _conn(path=DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    conn = _conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS narratives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL,            -- 'iran_geopolitics' / 'spacex_ipo' / 'fed_rate_cut'
                title_zh TEXT NOT NULL,
                summary_zh TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',  -- active / fading / closed
                impact TEXT,                  -- 'bullish' / 'bearish' / 'mixed' / 'neutral'
                assets TEXT,                  -- csv 受影響標的 'BTC,ETH,risk_assets'
                first_seen INTEGER NOT NULL,
                last_updated INTEGER NOT NULL,
                event_count INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS narrative_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                narrative_id INTEGER NOT NULL,
                event_ts INTEGER NOT NULL,
                source TEXT,
                summary_zh TEXT NOT NULL,
                causal_role TEXT,             -- 'trigger' / 'consequence' / 'escalation' / 'context'
                FOREIGN KEY (narrative_id) REFERENCES narratives(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS causal_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                narrative_id INTEGER,
                cause_zh TEXT NOT NULL,
                effect_zh TEXT NOT NULL,
                confidence INTEGER,           -- 1-10
                created_at INTEGER NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_narr_status ON narratives(status, last_updated)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nevent_narr ON narrative_events(narrative_id)")
    finally:
        conn.close()


# ── 第 1 層：跨時窗事件檢索 ────────────────────────────────────────────────
def fetch_events(days: int, pushed_only: bool = True, limit: int = 400) -> list[dict]:
    """從 news_feed.db 撈近 N 天事件（已累積的真實資料）。"""
    cutoff = int(time.time()) - days * 86400
    conn = _conn(NEWS_DB)
    try:
        sql = ("SELECT seen_at, source, handle, content_preview FROM seen_posts "
               "WHERE seen_at > ?")
        args = [cutoff]
        if pushed_only:
            sql += " AND pushed=1"
        sql += " ORDER BY seen_at DESC LIMIT ?"
        args.append(limit)
        rows = conn.execute(sql, args).fetchall()
        return [{"ts": r[0], "source": r[1], "handle": r[2],
                 "text": (r[3] or "")[:200]} for r in rows]
    finally:
        conn.close()


def fetch_econ_events(days: int = 14) -> list[dict]:
    """經濟數據事件（第 4 層關聯用）。回近期已公布實際值的事件。"""
    conn = _conn(NEWS_DB)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(econ_events)").fetchall()]
        if not cols:
            return []
        cutoff = int(time.time()) - days * 86400
        rows = conn.execute(
            "SELECT * FROM econ_events WHERE ts_utc > ? ORDER BY ts_utc DESC LIMIT 30",
            (cutoff,)).fetchall()
        return [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()


def _econ_context_lines() -> list[str]:
    """格式化近期經濟數據（含 實際 vs 預期 vs 前值）給敘事 prompt。"""
    import datetime as dt
    out = []
    for e in fetch_econ_events(14):
        if not e.get("actual"):
            continue
        when = dt.datetime.fromtimestamp(e["ts_utc"]).strftime("%m-%d")
        out.append(f"[{when}] {e.get('title','')}（{e.get('impact','')}）："
                   f"實際 {e['actual']} / 預期 {e.get('forecast') or '?'} / "
                   f"前值 {e.get('previous') or '?'}")
    return out


# ── 第 2+3 層：Claude 敘事聚類 + 因果鏈 ────────────────────────────────────
NARRATIVE_PROMPT = """你是宏觀事件分析師。下面是過去一段時間抓取的新聞事件（時間倒序）。
任務：把零散事件聚合成「主軸敘事」，並推論事件間的因果關係 —— 這要餵給交易系統當世界背景。

要求：
1. 聚成 3-6 條主軸敘事（例：中東地緣、SpaceX/Musk、Fed 降息預期、某交易所/項目事件）
2. 每條敘事判斷：對加密/風險資產的影響（bullish/bearish/mixed/neutral）、受影響標的
3. 每條敘事挑 2-4 個關鍵事件，標其因果角色（trigger 觸發/consequence 後果/escalation 升級/context 背景）
4. 推論 2-4 條明確的因果鏈（A 導致 B），給信心 1-10
5. 看似無關但實質有因果的，要點出（這是核心價值）
6. 若附有經濟數據，將「數據意外（實際 vs 預期）」納入因果鏈（例：CPI 超預期→升息預期升溫→風險資產承壓）

只輸出 JSON（繁中內容）：
{
  "narratives": [
    {"slug": "iran_geopolitics", "title_zh": "中東地緣升溫", "summary_zh": "一句話",
     "impact": "bearish", "assets": "BTC,risk_assets",
     "events": [{"summary_zh": "...", "causal_role": "trigger"}]}
  ],
  "causal_links": [
    {"cause_zh": "...", "effect_zh": "...", "confidence": 7}
  ]
}

新聞事件：
"""


async def build_narratives(timeout_sec: int = 120) -> dict | None:
    """跑一次敘事聚類：撈近 2 週事件 → Claude → 存 narrative.db。回統計或 None。"""
    init_db()
    events = fetch_events(days=14, limit=300)
    if len(events) < 5:
        print(f"[narrative] 事件不足（{len(events)}），略過")
        return None

    import datetime as dt
    lines = []
    for e in events[:300]:
        when = dt.datetime.fromtimestamp(e["ts"]).strftime("%m-%d %H:%M")
        lines.append(f"[{when}] @{e['handle']}: {e['text']}")
    # v29 第4層：附近期經濟數據（實際 vs 預期），讓敘事能連結「數據→市場反應」
    econ_lines = _econ_context_lines()
    econ_block = ("\n\n=== 近期經濟數據（實際/預期/前值）===\n" + "\n".join(econ_lines)
                  if econ_lines else "")
    prompt = NARRATIVE_PROMPT + "\n".join(lines) + econ_block

    from .llm_filter import _call_claude, _extract_json
    raw = await _call_claude(prompt, timeout_sec=timeout_sec)
    obj = _extract_json(raw) if raw else None
    if not obj or "narratives" not in obj:
        print(f"[narrative] Claude 無有效輸出")
        return None

    now = int(time.time())
    conn = _conn()
    n_narr = n_links = 0
    try:
        # 舊敘事標記為 fading（保留歷史，不刪）
        conn.execute("UPDATE narratives SET status='fading' WHERE status='active'")
        for nar in obj.get("narratives", []):
            slug = str(nar.get("slug") or "")[:60]
            if not slug:
                continue
            row = conn.execute("SELECT id, first_seen FROM narratives WHERE slug=?",
                               (slug,)).fetchone()
            evs = nar.get("events", [])
            if row:
                nid, first_seen = row
                conn.execute(
                    "UPDATE narratives SET title_zh=?, summary_zh=?, status='active', "
                    "impact=?, assets=?, last_updated=?, event_count=event_count+? WHERE id=?",
                    (str(nar.get("title_zh", ""))[:100], str(nar.get("summary_zh", ""))[:400],
                     nar.get("impact"), str(nar.get("assets", ""))[:100], now, len(evs), nid))
            else:
                cur = conn.execute(
                    "INSERT INTO narratives (slug, title_zh, summary_zh, status, impact, "
                    "assets, first_seen, last_updated, event_count) "
                    "VALUES (?,?,?,'active',?,?,?,?,?)",
                    (slug, str(nar.get("title_zh", ""))[:100],
                     str(nar.get("summary_zh", ""))[:400], nar.get("impact"),
                     str(nar.get("assets", ""))[:100], now, now, len(evs)))
                nid = cur.lastrowid
            for ev in evs:
                conn.execute(
                    "INSERT INTO narrative_events (narrative_id, event_ts, summary_zh, causal_role) "
                    "VALUES (?,?,?,?)", (nid, now, str(ev.get("summary_zh", ""))[:200],
                                         ev.get("causal_role")))
            n_narr += 1
        for lk in obj.get("causal_links", []):
            conn.execute(
                "INSERT INTO causal_links (cause_zh, effect_zh, confidence, created_at) "
                "VALUES (?,?,?,?)",
                (str(lk.get("cause_zh", ""))[:200], str(lk.get("effect_zh", ""))[:200],
                 int(lk.get("confidence") or 5), now))
            n_links += 1
    finally:
        conn.close()
    print(f"[narrative] 更新 {n_narr} 敘事 + {n_links} 因果鏈")
    return {"narratives": n_narr, "links": n_links}


def get_active_narratives() -> list[dict]:
    """供訊號層消費（第 5 層）：當前主導敘事 + 影響方向。"""
    init_db()
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT slug, title_zh, summary_zh, impact, assets, event_count "
            "FROM narratives WHERE status='active' ORDER BY event_count DESC, last_updated DESC"
        ).fetchall()
        return [{"slug": r[0], "title_zh": r[1], "summary_zh": r[2],
                 "impact": r[3], "assets": r[4], "event_count": r[5]} for r in rows]
    finally:
        conn.close()


def narrative_alignment(symbol: str, direction: str) -> str:
    """v29 第5層：訊號 vs 主導敘事一致性。回一行註記（順敘事加註/逆敘事警示）或空字串。
    direction: 'bull'/'bear'。比對 active 敘事中影響此 symbol（或 risk_assets/BTC）的方向。"""
    try:
        nars = get_active_narratives()
        if not nars:
            return ""
        sym_u = symbol.upper()
        bull_force = bear_force = 0
        hits = []
        for n in nars:
            assets = (n.get("assets") or "").upper()
            relevant = (sym_u in assets or "RISK_ASSETS" in assets or "BTC" in assets
                        or "CRYPTO" in assets)
            if not relevant:
                continue
            imp = n.get("impact")
            wt = max(1, n.get("event_count", 1))
            if imp == "bullish":
                bull_force += wt; hits.append(("🟢", n["title_zh"]))
            elif imp == "bearish":
                bear_force += wt; hits.append(("🔴", n["title_zh"]))
        if not hits:
            return ""
        net = bull_force - bear_force
        lean = "bull" if net > 0 else "bear" if net < 0 else "neutral"
        top = hits[0][1]
        if lean == "neutral":
            return f"\n🧩 <b>敘事</b>：多空敘事拉鋸（{top}…）— 與訊號方向無明顯衝突"
        aligned = (lean == direction)
        if aligned:
            return f"\n🧩 <b>敘事順風</b>：主導敘事偏{'多' if lean=='bull' else '空'}（{top}），與本訊號同向 ✅"
        return (f"\n🧩 <b>敘事逆風警示</b>：主導敘事偏{'多' if lean=='bull' else '空'}"
                f"（{top}），與本訊號相反 — 建議降低倉位或等待確認 ⚠️")
    except Exception:
        return ""


def render_narrative_digest() -> str:
    nars = get_active_narratives()
    if not nars:
        return ""
    icon = {"bullish": "🟢", "bearish": "🔴", "mixed": "🟡", "neutral": "⚪"}
    lines = ["🧩 <b>市場敘事脈絡</b>（事件因果追蹤）", "━━━━━━━━━━━━━━━━"]
    for n in nars[:6]:
        lines.append(f"{icon.get(n['impact'], '⚪')} <b>{n['title_zh']}</b>"
                     f"（{n['event_count']} 事件）")
        lines.append(f"   <i>{n['summary_zh']}</i>")
        if n["assets"]:
            lines.append(f"   影響：<code>{n['assets']}</code>")
    conn = _conn()
    try:
        links = conn.execute(
            "SELECT cause_zh, effect_zh, confidence FROM causal_links "
            "ORDER BY id DESC LIMIT 4").fetchall()
    finally:
        conn.close()
    if links:
        lines.append("\n🔗 <b>因果鏈</b>")
        for c, e, conf in links:
            lines.append(f"   {c} → {e}（信心 {conf}/10）")
    lines.append("\n<i>每日自動聚類，事件不再孤立 — 跨來源追蹤主軸敘事與因果。</i>")
    return "\n".join(lines)


async def run_narrative_loop(tg, interval_seconds: int = 86400,
                             target_hour_utc: int = 1):
    """每日敘事聚類 session（預設 09:00 台北 = 01:00 UTC）。"""
    import datetime as dt
    print(f"[narrative] loop online（每日聚類）")
    await asyncio.sleep(180)   # 開機後等其他 worker 就緒
    # 開機先跑一次（若今日尚無敘事）
    try:
        if not get_active_narratives():
            r = await build_narratives()
            if r and tg is not None:
                digest = render_narrative_digest()
                if digest:
                    await tg.send_message(digest, parse_mode="HTML")
    except Exception as e:
        print(f"[narrative] startup error: {type(e).__name__}: {e}")
    while True:
        now = dt.datetime.now(tz=dt.timezone.utc)
        nxt = now.replace(hour=target_hour_utc, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += dt.timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            r = await build_narratives()
            if r and tg is not None:
                digest = render_narrative_digest()
                if digest:
                    await tg.send_message(digest, parse_mode="HTML")
        except Exception as e:
            print(f"[narrative] loop error: {type(e).__name__}: {e}")


if __name__ == "__main__":
    import sys
    init_db()
    ev = fetch_events(days=14, limit=10)
    print(f"近 2 週事件樣本：{len(ev)} 筆")
    for e in ev[:5]:
        import datetime as dt
        print(f"  [{dt.datetime.fromtimestamp(e['ts']).strftime('%m-%d %H:%M')}] "
              f"@{e['handle']}: {e['text'][:60]}")
    if "--build" in sys.argv:
        r = asyncio.run(build_narratives())
        print(f"build: {r}")
        print(render_narrative_digest().replace("<b>","").replace("</b>","")
              .replace("<i>","").replace("</i>","").replace("<code>","").replace("</code>",""))
    sys.exit(0)
