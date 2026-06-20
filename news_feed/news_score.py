"""消息面進開單邏輯 · Phase 0「影子捕捉」（純觀測、零訊號數學變更）。

task#66（Q2）紅隊整合方案的第一步。設計總綱＝「二元否決旗標優先、連續 alpha 推遲」；
本檔只做**最底層的影子捕捉**：每次採樣把「當下的新聞原子事實」寫進獨立 sink
data_dir()/news_factor.jsonl，**完全不參與任何下單計算**。不攢資料就永遠沒法回測，
故先攢——但攢的同時對真實/模擬/demo 下單的數學影響嚴格為零。

Phase 0 只記「原子分量」，**不做任何聚合/衰減/加權**（採納紅隊缺陷 6.3）。聚合公式、
τ 半衰期、source 權重、去偏校準全部留到閘②（離線回測 backtest/news_factor_eventstudy.py）
在同一批原子資料上探索——先驗不污染歷史值。

兩個觀測來源：
    1. **新聞原子（NewsAtom）**：讀 news_feed.db `seen_posts`（既有去重表）近窗貼文，
       每筆×命中 ticker 攤成一個 atom。客觀戳記只用系統落庫時間 `seen_at`（ingestion_ts）
       與自增主鍵 `id`（ingestion_seq，單調遞增）——**不用任何文字推斷的「事件發生時間」**
       （採納缺陷 1.1：回測一律用 ingestion 順序排序，杜絕自欺）。
    2. **敘事傾向（免費探針）**：把既有 narrative_engine.narrative_alignment() 內部算的
       bull_force/bear_force/net/lean 量化中間值抽出來一起記（採納缺陷 6.2）。零新模組成本，
       順便驗「現有敘事對齊」到底有沒有 EV——若連這個既存函式都不顯著，連續因子版不必建。

刻意**未實作**的（誠實標明，避免日後誤以為已涵蓋）：
    * `ess_raw`（LLM 每則情緒 ∈ [-1,+1]）：需逐則 LLM 呼叫，Phase 0 不在採集路徑加 LLM
      成本/依賴；先記 None（紅線③：缺料誠實 None 不捏造）。去偏校準集 + 連續 ESS 是
      用法二（連續因子），整段推遲到閘②證明連續因子顯著才建。
    * `liquidity_tag`（小幣硬排除用）：需 per-symbol 市場數據查詢，Phase 0 先記 None；
      閘③否決旗標上線前再以離線數據補（屬 Phase 3 防操縱層，非 Phase 0 骨架）。

════════════════════════════════════════════════════════════════════════════
影子鐵則（與 cvd_shadow / convergence_shadow 同一套，絕不可違反）：
    * 本模組**永不**把任何欄位乘進 strength_score / 任何 check 的 score；
      **永不**寫 fire_queue / snapshot 的決策欄 / symbol_gate / 任何下單路徑；
      **永不**給方向票、**永不**單獨擋單、**永不**import market_intel_mcp.strength
      或 l2_trigger.*（消息面進訊號層須過閘②回測 + 閘③人工拍板，屬另案）。
    * 唯一輸出 = 自己的 news_factor.jsonl（觀測用，給日後閘② event-study 離線檢定）。
    * 整個迴圈體包 try/except；任何源失敗都吞掉續跑，絕不拖垮 daemon。
    * 不發 Telegram（純背景觀測，不打擾使用者）。
    * capture_entry_news_context() 供 plan_snapshot 進場快照「純觀測欄」用——回傳只進
      snapshot 的觀測區、**不進**任何 rr/expected_r/score/方向判定。
════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time

# JSONL sink 軟上限（位元組）；超過就先改名 .1 再重開，避免無限長（與 cvd_shadow 同）
_SINK_MAX_BYTES = 5_000_000
# 每輪回看窗（秒）。預設取 interval × 1.5，保證即使某輪遲到也不留缺口；重疊由離線端
# 依 ingestion_seq（單調主鍵）去重，故重疊安全、缺口才致命，刻意偏大。
_LOOKBACK_MULT = 1.5
# 一輪最多攤幾筆貼文（保守上限，避免冷啟動回看一大批時 sink 單行爆大）
_MAX_POSTS_PER_CYCLE = 400


def _sink_path():
    from botpaths import data_dir
    return data_dir() / "news_factor.jsonl"


def _append_jsonl(record: dict) -> None:
    """把一輪觀測寫一行 JSONL（純本地檔；超過軟上限就輪替一次）。與 cvd_shadow 同樣板。"""
    path = _sink_path()
    try:
        if path.exists() and path.stat().st_size > _SINK_MAX_BYTES:
            backup = path.with_suffix(".jsonl.1")
            try:
                if backup.exists():
                    backup.unlink()
                path.rename(backup)
            except OSError:
                pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 觀測 sink 寫失敗不致命，吞掉續跑


# ════════════════════════════════════════════════════════════════════════
#  純函式：建 NewsAtom / 敘事傾向（無 I/O，離線可測）
# ════════════════════════════════════════════════════════════════════════
def build_news_atom(*, source: str, handle: str, symbol: str,
                    ingestion_ts: int, ingestion_seq: int,
                    relevance: int, pushed: bool,
                    push_reason: str | None) -> dict:
    """把「一則貼文 × 一個命中標的」組成一個原子觀測（純函式，無聚合）。

    symbol＝命中的 watchlist ticker（大寫）；未命中任何 ticker 的貼文以 "MARKET" 記一個
    市場層級 atom（relevance=0）。客觀戳記只用 ingestion_ts/seq（不用任何文字推斷時間）。
    ess_raw / liquidity_tag 刻意 None（見檔頭：Phase 0 不在採集加 LLM/市場查詢，誠實 None）。
    """
    return {
        "source": source,
        "handle": handle,
        "symbol": symbol,
        "relevance": int(relevance),       # 0 或 100（命中 ticker=100）；細緻評分推遲到閘②
        "ess_raw": None,                   # 逐則 LLM 情緒：Phase 0 不採（誠實 None，紅線③）
        "liquidity_tag": None,             # 小幣排除用：Phase 0 不採（誠實 None，紅線③）
        "ingestion_ts": int(ingestion_ts),  # 系統落庫 UTC 秒（唯一可信戳記，缺陷 1.1）
        "ingestion_seq": int(ingestion_seq),  # 自增主鍵＝單調 ingestion 序（回測排序鍵）
        "pushed": bool(pushed),            # 是否通過既有過濾被推到 TG（既有過濾的判定，純記錄）
        "push_reason": push_reason,        # 既有過濾原因標籤（純記錄）
    }


def atoms_from_post(*, post: dict, tickers, now: int | None = None) -> list[dict]:
    """把一則 seen_posts 列 + 抽出的 ticker 集合攤成 atom 清單（純函式）。

    post＝{id, source, handle, seen_at, pushed, push_reason, content_preview}；
    tickers＝extract_tickers(content_preview) 的結果（set[str]）。命中多個 ticker → 多個
    atom（每個 relevance=100）；未命中 → 一個 "MARKET" atom（relevance=0）。
    """
    seq = post.get("id")
    ts = post.get("seen_at") or now or 0
    src = post.get("source") or "unknown"
    handle = post.get("handle") or ""
    pushed = bool(post.get("pushed"))
    reason = post.get("push_reason")
    syms = sorted({t.upper() for t in (tickers or [])})
    if not syms:
        return [build_news_atom(source=src, handle=handle, symbol="MARKET",
                                ingestion_ts=ts, ingestion_seq=seq, relevance=0,
                                pushed=pushed, push_reason=reason)]
    return [build_news_atom(source=src, handle=handle, symbol=sym,
                            ingestion_ts=ts, ingestion_seq=seq, relevance=100,
                            pushed=pushed, push_reason=reason)
            for sym in syms]


def narrative_lean_for(symbol: str, active_narratives) -> dict:
    """免費探針：抽出 narrative_alignment() 內部的量化中間值（純函式，無 I/O）。

    完全鏡射 narrative_engine.narrative_alignment() 的 force 計算邏輯，但回**數字**而非
    顯示字串，供閘② event-study 離線檢定「敘事對齊」有沒有 EV。**不**做任何方向決策。
    回 {bull_force, bear_force, net, lean, n_hits}；無相關敘事 → 全 0 / lean="neutral"。
    """
    sym_u = (symbol or "").upper()
    bull_force = bear_force = 0
    n_hits = 0
    for n in active_narratives or []:
        assets = (n.get("assets") or "").upper()
        relevant = (sym_u in assets or "RISK_ASSETS" in assets or "BTC" in assets
                    or "CRYPTO" in assets)
        if not relevant:
            continue
        imp = n.get("impact")
        wt = max(1, n.get("event_count", 1) or 1)
        if imp == "bullish":
            bull_force += wt
            n_hits += 1
        elif imp == "bearish":
            bear_force += wt
            n_hits += 1
    net = bull_force - bear_force
    lean = "bull" if net > 0 else "bear" if net < 0 else "neutral"
    return {"bull_force": bull_force, "bear_force": bear_force,
            "net": net, "lean": lean, "n_hits": n_hits}


def build_cycle_record(*, posts, active_narratives, watchlist_symbols,
                       now: int, lookback_seconds: int,
                       extract_tickers_fn) -> dict:
    """組裝一輪觀測記錄（純函式，無 I/O；便於離線單測）。

    posts＝近窗 seen_posts 列清單（每列 dict）；active_narratives＝get_active_narratives()；
    watchlist_symbols＝要記敘事傾向的標的清單；extract_tickers_fn＝注入的純函式
    （正式跑傳 news_filter.extract_tickers，測試可注入假的）。
    刻意把「新聞原子」與「敘事傾向」並排為兩個獨立區塊，互不混算（無聚合）。
    """
    atoms: list[dict] = []
    for p in posts or []:
        try:
            tks = extract_tickers_fn(p.get("content_preview") or "")
        except Exception:
            tks = set()
        atoms.extend(atoms_from_post(post=p, tickers=tks, now=now))

    seqs = [a["ingestion_seq"] for a in atoms if a.get("ingestion_seq") is not None]
    narr_lean = {sym: narrative_lean_for(sym, active_narratives)
                 for sym in (watchlist_symbols or [])}
    # 只保留「有命中敘事」的標的，避免 sink 塞一堆全 0 的中性列
    narr_lean = {s: v for s, v in narr_lean.items() if v["n_hits"] > 0}

    return {
        "ts": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "now_unix_s": int(now),
        "lookback_seconds": int(lookback_seconds),
        "seq_min": min(seqs) if seqs else None,   # 本輪 ingestion 序範圍（離線去重/排序用）
        "seq_max": max(seqs) if seqs else None,
        "n_posts": len(posts or []),
        "n_atoms": len(atoms),
        "n_ticker_atoms": sum(1 for a in atoms if a["symbol"] != "MARKET"),
        "active_narratives": [
            {"slug": n.get("slug"), "impact": n.get("impact"),
             "assets": n.get("assets"), "event_count": n.get("event_count")}
            for n in (active_narratives or [])
        ],
        "narrative_lean": narr_lean,          # 免費探針：per-symbol 量化傾向
        "atoms": atoms,                       # 原子分量（無聚合）
        "note": "shadow-only Phase0(task#66): 任何欄位從不寫回 strength/check score/"
                "fire/snapshot 決策欄/方向票；客觀戳記只用 ingestion_ts/seq（不用文字推斷"
                "事件時間）；ess_raw/liquidity_tag 刻意 None（誠實，紅線③）；聚合/去偏推遲到閘②",
    }


# ════════════════════════════════════════════════════════════════════════
#  Live 讀取（唯讀；只讀既有 news_feed.db + narrative.db，零寫入既有表）
# ════════════════════════════════════════════════════════════════════════
def _recent_posts(lookback_seconds: int, now: int,
                  limit: int = _MAX_POSTS_PER_CYCLE) -> list[dict]:
    """唯讀讀 news_feed.db `seen_posts` 近窗貼文（pushed 與被過濾的都要，回完整觀測）。

    不改 news_db.py：用其 DB_PATH 開唯讀連線。表不存在（全新環境）→ 先 init_db 建空表。
    回每列 dict（依 id 升序＝ingestion 序）。任何例外吞掉回 []（影子鐵則：不拖垮呼叫端）。
    """
    try:
        from . import news_db
        news_db.init_db()
        since = int(now) - int(lookback_seconds)
        conn = sqlite3.connect(news_db.DB_PATH)
        try:
            rows = conn.execute(
                """SELECT id, source, handle, seen_at, pushed, push_reason, content_preview
                   FROM seen_posts WHERE seen_at >= ?
                   ORDER BY id ASC LIMIT ?""",
                (since, int(limit)),
            ).fetchall()
        finally:
            conn.close()
        return [{"id": r[0], "source": r[1], "handle": r[2], "seen_at": r[3],
                 "pushed": bool(r[4]), "push_reason": r[5], "content_preview": r[6]}
                for r in rows]
    except Exception:
        return []


def _active_narratives_safe() -> list[dict]:
    """唯讀取 active 敘事；任何例外吞掉回 []（narrative.db 可能尚未建）。"""
    try:
        from .narrative_engine import get_active_narratives
        return get_active_narratives() or []
    except Exception:
        return []


def _watchlist_symbols() -> list[str]:
    """要記敘事傾向的標的＝既有 news 過濾的 watchlist ticker 集合（唯讀）。失敗回 []。"""
    try:
        from .twitter_accounts import WATCHED_TICKERS
        return sorted(WATCHED_TICKERS)
    except Exception:
        return []


def _run_cycle(now: int | None = None,
               lookback_seconds: int | None = None) -> dict:
    """跑一輪消息面影子觀測，回可序列化摘要 dict（唯讀；組裝走純函式 build_cycle_record）。"""
    from .news_filter import extract_tickers
    if now is None:
        now = int(time.time())
    if lookback_seconds is None:
        lookback_seconds = int(3600 * _LOOKBACK_MULT)
    posts = _recent_posts(lookback_seconds, now)
    narratives = _active_narratives_safe()
    watchlist = _watchlist_symbols()
    return build_cycle_record(
        posts=posts, active_narratives=narratives, watchlist_symbols=watchlist,
        now=now, lookback_seconds=lookback_seconds, extract_tickers_fn=extract_tickers)


def capture_entry_news_context(symbol: str, direction: str,
                               now: int | None = None,
                               lookback_seconds: int = 21600) -> dict | None:
    """進場快照用：回「此標的當下的消息面觀測」（純觀測欄，供 plan_snapshot 之後接線）。

    回 {symbol, narrative_lean, recent_ticker_atoms, captured_at_ms} 或 None（失敗）。
    **絕不**回傳任何方向決策/分數——只是把「進場那一刻有哪些相關新聞、敘事偏哪邊」釘進
    snapshot 觀測區，供復盤引擎事後歸因。direction 只用於記錄（不據以擋單/改向）。
    """
    try:
        if now is None:
            now = int(time.time())
        sym_u = (symbol or "").upper()
        narratives = _active_narratives_safe()
        lean = narrative_lean_for(sym_u, narratives)
        from .news_filter import extract_tickers
        posts = _recent_posts(lookback_seconds, now)
        hits = []
        for p in posts:
            try:
                tks = extract_tickers(p.get("content_preview") or "")
            except Exception:
                tks = set()
            if sym_u in {t.upper() for t in tks}:
                hits.append({"source": p.get("source"), "handle": p.get("handle"),
                             "ingestion_ts": p.get("seen_at"), "ingestion_seq": p.get("id"),
                             "pushed": bool(p.get("pushed"))})
        return {
            "symbol": sym_u,
            "direction_observed": direction,    # 純記錄，不據以決策
            "narrative_lean": lean,
            "recent_ticker_atoms": hits,
            "n_ticker_atoms": len(hits),
            "lookback_seconds": int(lookback_seconds),
            "captured_at_ms": int(now * 1000),
            "note": "observation-only: 不進 rr/expected_r/score/方向判定（影子鐵則）",
        }
    except Exception:
        return None


async def run_news_shadow_loop(interval_seconds: int = 3600):
    """消息面影子觀測常駐迴圈（每 interval 跑一輪，純觀測寫 news_factor.jsonl）。

    啟動延遲 60s 避開開機尖峰（讓 news worker 先補幾筆貼文進 db）。任何意外吞掉續跑。
    """
    import asyncio
    await asyncio.sleep(60)
    lookback = int(interval_seconds * _LOOKBACK_MULT)
    while True:
        try:
            summary = _run_cycle(lookback_seconds=lookback)
            _append_jsonl(summary)
            print(f"[news_shadow] posts={summary['n_posts']} atoms={summary['n_atoms']} "
                  f"ticker_atoms={summary['n_ticker_atoms']} "
                  f"narr_lean_syms={len(summary['narrative_lean'])} "
                  f"seq=[{summary['seq_min']},{summary['seq_max']}]")
        except Exception as e:  # 整輪保護：任何意外吞掉續跑，不拖垮 daemon
            print(f"[news_shadow] cycle error: {e}")
        await asyncio.sleep(max(60, int(interval_seconds)))


# ════════════════════════════════════════════════════════════════════════
#  純函式自測（離線、零網路、零 DB）
# ════════════════════════════════════════════════════════════════════════
def _selftest() -> int:
    # 1) build_news_atom：客觀戳記帶入、ess_raw/liquidity_tag 誠實 None
    a = build_news_atom(source="twitter", handle="cz_binance", symbol="BNB",
                        ingestion_ts=1000, ingestion_seq=42, relevance=100,
                        pushed=True, push_reason="T3_founder_ticker")
    assert a["symbol"] == "BNB" and a["relevance"] == 100
    assert a["ingestion_ts"] == 1000 and a["ingestion_seq"] == 42
    assert a["ess_raw"] is None and a["liquidity_tag"] is None   # 紅線③誠實 None
    assert a["pushed"] is True
    print("  ✅ build_news_atom：戳記帶入、ess_raw/liquidity_tag 誠實 None")

    # 2) atoms_from_post：命中多 ticker → 多 atom；未命中 → MARKET atom relevance=0
    post = {"id": 7, "source": "tvnews", "handle": "TheBlock__", "seen_at": 2000,
            "pushed": True, "push_reason": "T5_news_match"}
    multi = atoms_from_post(post=post, tickers={"BTC", "ETH"})
    assert {x["symbol"] for x in multi} == {"BTC", "ETH"}
    assert all(x["relevance"] == 100 and x["ingestion_seq"] == 7 for x in multi)
    market = atoms_from_post(post=post, tickers=set())
    assert len(market) == 1 and market[0]["symbol"] == "MARKET"
    assert market[0]["relevance"] == 0
    print("  ✅ atoms_from_post：多 ticker 攤多 atom / 無 ticker → MARKET(relevance=0)")

    # 3) narrative_lean_for：鏡射 narrative_alignment force 計算、回數字、無相關→中性
    nars = [
        {"slug": "etf_inflow", "impact": "bullish", "assets": "BTC,ETH", "event_count": 3},
        {"slug": "geopolitics", "impact": "bearish", "assets": "RISK_ASSETS", "event_count": 1},
        {"slug": "doge_meme", "impact": "bullish", "assets": "DOGE", "event_count": 5},
    ]
    lean_btc = narrative_lean_for("BTC", nars)
    # BTC 命中 etf(+3 bull) + geopolitics(RISK_ASSETS, +1 bear) → net=+2 lean=bull
    assert lean_btc["bull_force"] == 3 and lean_btc["bear_force"] == 1
    assert lean_btc["net"] == 2 and lean_btc["lean"] == "bull" and lean_btc["n_hits"] == 2
    # 鏡射真實 narrative_alignment：assets 含 "BTC"/"RISK_ASSETS"/"CRYPTO" 視為全市場相關。
    # XRP 雖未具名，仍因 etf_inflow(assets含"BTC") + geopolitics(RISK_ASSETS) 命中兩條 →
    # 這是刻意鎖住的既有行為（BTC/大盤敘事波及全市場），不是 bug。
    lean_xrp = narrative_lean_for("XRP", nars)
    assert lean_xrp["bull_force"] == 3 and lean_xrp["bear_force"] == 1
    assert lean_xrp["net"] == 2 and lean_xrp["lean"] == "bull" and lean_xrp["n_hits"] == 2
    # 具名命中疊加市場級：DOGE 命中 doge_meme(+5) + etf(+3,含BTC) + geopolitics(-1) → bull
    lean_doge = narrative_lean_for("DOGE", nars)
    assert lean_doge["bull_force"] == 8 and lean_doge["bear_force"] == 1
    assert lean_doge["lean"] == "bull" and lean_doge["n_hits"] == 3
    # 純中性：唯一敘事 assets=SOL（不含市場關鍵字）→ LTC 無命中
    lean_none = narrative_lean_for("LTC", [{"slug": "x", "impact": "bullish",
                                            "assets": "SOL", "event_count": 2}])
    assert lean_none["lean"] == "neutral" and lean_none["n_hits"] == 0
    # 純 bear 單條：assets=ETH（不含市場關鍵字）→ 只 ETH 具名命中那條 bear
    lean_eth_bear = narrative_lean_for("ETH", [{"slug": "hack", "impact": "bearish",
                                                "assets": "ETH", "event_count": 2}])
    assert lean_eth_bear["lean"] == "bear" and lean_eth_bear["n_hits"] == 1
    print("  ✅ narrative_lean_for：force 計算正確、市場級關鍵字波及全市場、無相關→中性 0")

    # 4) build_cycle_record：原子與敘事傾向並排、seq 範圍、只留命中敘事的標的
    posts = [
        {"id": 10, "source": "twitter", "handle": "a", "seen_at": 5000,
         "pushed": True, "push_reason": "r", "content_preview": "pump $BTC now"},
        {"id": 11, "source": "twitter", "handle": "b", "seen_at": 5050,
         "pushed": False, "push_reason": "filtered", "content_preview": "no ticker here"},
    ]
    fake_extract = lambda c: {"BTC"} if "BTC" in c else set()
    # 用「具名、不含市場級關鍵字」的敘事，乾淨示範「無命中標的被剔除」
    nars_named = [{"slug": "sol_upgrade", "impact": "bullish",
                   "assets": "SOL", "event_count": 2}]
    rec = build_cycle_record(posts=posts, active_narratives=nars_named,
                             watchlist_symbols=["SOL", "LTC"],
                             now=6000, lookback_seconds=5400,
                             extract_tickers_fn=fake_extract)
    assert rec["n_posts"] == 2 and rec["n_atoms"] == 2     # BTC atom + MARKET atom
    assert rec["n_ticker_atoms"] == 1
    assert rec["seq_min"] == 10 and rec["seq_max"] == 11
    assert "SOL" in rec["narrative_lean"] and "LTC" not in rec["narrative_lean"]  # LTC 無命中被剔
    print("  ✅ build_cycle_record：原子/敘事並排、seq 範圍、無命中標的剔除")

    # 5) 記錄可 JSON 序列化（sink 寫得出去）
    s = json.dumps(rec, ensure_ascii=False)
    assert "BTC" in s and "shadow-only" in s
    print("  ✅ 記錄可 JSON 序列化")

    # 6) extract 例外不致命：壞 extractor → 該貼文當無 ticker（MARKET atom），不丟例外
    def _boom(_c):
        raise RuntimeError("boom")
    rec2 = build_cycle_record(posts=posts[:1], active_narratives=[],
                              watchlist_symbols=[], now=6000, lookback_seconds=100,
                              extract_tickers_fn=_boom)
    assert rec2["n_atoms"] == 1 and rec2["atoms"][0]["symbol"] == "MARKET"
    print("  ✅ extract 例外不致命 → 退為 MARKET atom")

    print("自測通過：NewsAtom 原子捕捉 + 敘事傾向免費探針 + 純函式組裝 + 誠實 None ✅")
    return 0


if __name__ == "__main__":
    import sys
    from pathlib import Path
    _root = str(Path(__file__).resolve().parent.parent)
    if _root not in sys.path:
        sys.path.insert(0, _root)
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        raise SystemExit(_selftest())
    # 一次性 live 試跑（唯讀；讀 news_feed.db + narrative.db 現況）
    rec = _run_cycle()
    print(json.dumps(rec, ensure_ascii=False, indent=2)[:2500])
