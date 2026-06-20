"""消息面 Phase 0「影子捕捉」測試（task#66 Q2・news_feed/news_score.py）。

鎖住影子鐵則與純函式正確性，避免日後改動把缺口/捏造埋回：
  * NewsAtom 只記客觀戳記（ingestion_ts/seq），ess_raw/liquidity_tag 誠實 None（紅線③）。
  * narrative_lean_for 忠實鏡射 narrative_alignment 的 force 計算（含「市場級關鍵字波及全市場」）。
  * build_cycle_record 純組裝、無聚合、無命中標的剔除。
  * _append_jsonl 寫獨立 sink、超軟上限輪替；絕不碰任何既有表/下單路徑。
  * capture_entry_news_context 只回觀測欄、永不回方向決策/分數。

全離線：monkeypatch sink 路徑與讀取函式到假資料；零網路、零真錢、零訊號數學、零既有表寫入。
"""
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import news_feed.news_score as ns


# ── 純函式：build_news_atom ────────────────────────────────────────────────
def test_build_news_atom_objective_stamps_and_honest_none():
    a = ns.build_news_atom(source="twitter", handle="cz_binance", symbol="BNB",
                           ingestion_ts=1000, ingestion_seq=42, relevance=100,
                           pushed=True, push_reason="T3_founder_ticker")
    assert a["symbol"] == "BNB" and a["relevance"] == 100
    assert a["ingestion_ts"] == 1000 and a["ingestion_seq"] == 42
    assert a["pushed"] is True and a["push_reason"] == "T3_founder_ticker"
    # 紅線③：Phase 0 不在採集路徑加 LLM/市場查詢 → 誠實 None，不捏造
    assert a["ess_raw"] is None
    assert a["liquidity_tag"] is None


# ── 純函式：atoms_from_post ────────────────────────────────────────────────
def test_atoms_from_post_multi_ticker():
    post = {"id": 7, "source": "tvnews", "handle": "TheBlock__", "seen_at": 2000,
            "pushed": True, "push_reason": "T5_news_match"}
    atoms = ns.atoms_from_post(post=post, tickers={"BTC", "ETH"})
    assert {x["symbol"] for x in atoms} == {"BTC", "ETH"}
    assert all(x["relevance"] == 100 for x in atoms)
    assert all(x["ingestion_seq"] == 7 and x["ingestion_ts"] == 2000 for x in atoms)


def test_atoms_from_post_no_ticker_becomes_market():
    post = {"id": 9, "source": "x", "handle": "h", "seen_at": 3000,
            "pushed": False, "push_reason": "filtered"}
    atoms = ns.atoms_from_post(post=post, tickers=set())
    assert len(atoms) == 1
    assert atoms[0]["symbol"] == "MARKET" and atoms[0]["relevance"] == 0


def test_atoms_from_post_seen_at_fallback_to_now():
    # seen_at 缺 → 退回傳入 now（不可無聲變 0 造成排序錯亂）
    post = {"id": 3, "source": "x", "handle": "h"}
    atoms = ns.atoms_from_post(post=post, tickers={"SOL"}, now=8888)
    assert atoms[0]["ingestion_ts"] == 8888


# ── 純函式：narrative_lean_for（鏡射 narrative_alignment）────────────────────
_NARS = [
    {"slug": "etf_inflow", "impact": "bullish", "assets": "BTC,ETH", "event_count": 3},
    {"slug": "geopolitics", "impact": "bearish", "assets": "RISK_ASSETS", "event_count": 1},
    {"slug": "doge_meme", "impact": "bullish", "assets": "DOGE", "event_count": 5},
]


def test_narrative_lean_named_hit():
    lean = ns.narrative_lean_for("BTC", _NARS)
    assert lean["bull_force"] == 3 and lean["bear_force"] == 1
    assert lean["net"] == 2 and lean["lean"] == "bull" and lean["n_hits"] == 2


def test_narrative_lean_market_wide_keyword_reaches_all():
    # assets 含 "BTC"/"RISK_ASSETS"/"CRYPTO" → 視為全市場相關（刻意鎖住的既有行為）
    lean = ns.narrative_lean_for("XRP", _NARS)
    assert lean["bull_force"] == 3 and lean["bear_force"] == 1
    assert lean["net"] == 2 and lean["lean"] == "bull" and lean["n_hits"] == 2


def test_narrative_lean_named_plus_market():
    lean = ns.narrative_lean_for("DOGE", _NARS)
    assert lean["bull_force"] == 8 and lean["bear_force"] == 1   # etf3 + doge5
    assert lean["lean"] == "bull" and lean["n_hits"] == 3


def test_narrative_lean_neutral_when_no_market_keyword():
    lean = ns.narrative_lean_for("LTC", [{"slug": "x", "impact": "bullish",
                                          "assets": "SOL", "event_count": 2}])
    assert lean["lean"] == "neutral" and lean["n_hits"] == 0
    assert lean["bull_force"] == 0 and lean["bear_force"] == 0


def test_narrative_lean_pure_bear_single():
    lean = ns.narrative_lean_for("ETH", [{"slug": "hack", "impact": "bearish",
                                          "assets": "ETH", "event_count": 2}])
    assert lean["lean"] == "bear" and lean["n_hits"] == 1 and lean["bear_force"] == 2


def test_narrative_lean_empty_and_none_safe():
    assert ns.narrative_lean_for("BTC", []) == {
        "bull_force": 0, "bear_force": 0, "net": 0, "lean": "neutral", "n_hits": 0}
    assert ns.narrative_lean_for("BTC", None)["lean"] == "neutral"


# ── 純函式：build_cycle_record ──────────────────────────────────────────────
_POSTS = [
    {"id": 10, "source": "twitter", "handle": "a", "seen_at": 5000,
     "pushed": True, "push_reason": "r", "content_preview": "pump $BTC now"},
    {"id": 11, "source": "twitter", "handle": "b", "seen_at": 5050,
     "pushed": False, "push_reason": "filtered", "content_preview": "no ticker here"},
]


def test_build_cycle_record_assembly_and_seq_range():
    fake_extract = lambda c: {"BTC"} if "BTC" in c else set()
    nars_named = [{"slug": "sol_upgrade", "impact": "bullish",
                   "assets": "SOL", "event_count": 2}]
    rec = ns.build_cycle_record(posts=_POSTS, active_narratives=nars_named,
                                watchlist_symbols=["SOL", "LTC"],
                                now=6000, lookback_seconds=5400,
                                extract_tickers_fn=fake_extract)
    assert rec["n_posts"] == 2 and rec["n_atoms"] == 2      # BTC atom + MARKET atom
    assert rec["n_ticker_atoms"] == 1
    assert rec["seq_min"] == 10 and rec["seq_max"] == 11
    # 只留有命中敘事的標的，避免 sink 塞一堆全 0 中性列
    assert "SOL" in rec["narrative_lean"] and "LTC" not in rec["narrative_lean"]
    # 記錄可序列化（sink 寫得出去）
    assert "BTC" in json.dumps(rec, ensure_ascii=False)


def test_build_cycle_record_extract_exception_not_fatal():
    def _boom(_c):
        raise RuntimeError("boom")
    rec = ns.build_cycle_record(posts=_POSTS[:1], active_narratives=[],
                                watchlist_symbols=[], now=6000, lookback_seconds=100,
                                extract_tickers_fn=_boom)
    # 壞 extractor → 該貼文退為 MARKET atom，整輪不丟例外
    assert rec["n_atoms"] == 1 and rec["atoms"][0]["symbol"] == "MARKET"


def test_build_cycle_record_empty_posts():
    rec = ns.build_cycle_record(posts=[], active_narratives=[], watchlist_symbols=[],
                                now=1, lookback_seconds=1, extract_tickers_fn=lambda c: set())
    assert rec["n_posts"] == 0 and rec["n_atoms"] == 0
    assert rec["seq_min"] is None and rec["seq_max"] is None


# ── I/O：_append_jsonl 寫獨立 sink + 輪替（monkeypatch 到暫存）────────────────
def test_append_jsonl_writes_to_sink(tmp_path, monkeypatch):
    sink = tmp_path / "news_factor.jsonl"
    monkeypatch.setattr(ns, "_sink_path", lambda: sink)
    ns._append_jsonl({"hello": "world", "n": 1})
    ns._append_jsonl({"hello": "again", "n": 2})
    lines = sink.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["n"] == 1 and json.loads(lines[1])["n"] == 2


def test_append_jsonl_rotates_over_soft_limit(tmp_path, monkeypatch):
    sink = tmp_path / "news_factor.jsonl"
    monkeypatch.setattr(ns, "_sink_path", lambda: sink)
    monkeypatch.setattr(ns, "_SINK_MAX_BYTES", 50)   # 極小上限以觸發輪替
    ns._append_jsonl({"x": "A" * 100})               # 先寫一筆超上限
    ns._append_jsonl({"x": "B"})                      # 下一筆觸發輪替
    assert (tmp_path / "news_factor.jsonl.1").exists()  # 舊檔被改名保存
    # 新檔只剩最新一筆，不會無限長
    new_lines = sink.read_text(encoding="utf-8").strip().splitlines()
    assert len(new_lines) == 1 and json.loads(new_lines[0])["x"] == "B"


# ── capture_entry_news_context：純觀測欄、永不回方向決策 ─────────────────────
def test_capture_entry_news_context_observation_only(monkeypatch):
    monkeypatch.setattr(ns, "_active_narratives_safe", lambda: _NARS)
    monkeypatch.setattr(ns, "_recent_posts", lambda lb, now, **kw: [
        {"id": 21, "source": "twitter", "handle": "whale", "seen_at": 9000,
         "pushed": True, "content_preview": "$BTC breakout"},
        {"id": 22, "source": "twitter", "handle": "noise", "seen_at": 9100,
         "pushed": False, "content_preview": "unrelated chatter"},
    ])
    # extract_tickers 走真實純函式即可；BTC 必在 watchlist
    out = ns.capture_entry_news_context("BTC", "bull", now=10000)
    assert out is not None
    assert out["symbol"] == "BTC"
    assert out["direction_observed"] == "bull"     # 純記錄
    assert out["narrative_lean"]["lean"] == "bull"
    # 命中此標的的近窗 atom 被釘進 snapshot 觀測區
    assert out["n_ticker_atoms"] == 1
    assert out["recent_ticker_atoms"][0]["ingestion_seq"] == 21
    # 鐵則：回值絕不含 rr / expected_r / score / 方向判定欄
    forbidden = {"rr", "expected_r", "score", "strength_score", "decision",
                 "direction", "vote", "fire"}
    assert not (forbidden & set(out.keys()))


def test_capture_entry_news_context_swallows_errors(monkeypatch):
    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(ns, "_active_narratives_safe", _boom)
    # 任何源失敗 → 回 None（影子鐵則：不拖垮呼叫端）
    assert ns.capture_entry_news_context("BTC", "bull", now=1) is None
