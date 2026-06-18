"""CoinGlass 加密快訊 worker（news_feed/coinglass_news.py）離線測試 — v52。

全離線：FakeSource 假裝 CoinGlass API、FakeTG 收推送、stub 掉 LLM 過濾，
news_db 指向臨時檔，_recent_pushed_titles 替換成可控清單 → 不碰網路/不碰正式 DB。

執行（任一）：
    pytest tests/test_coinglass_news.py
    python tests/test_coinglass_news.py

驗證重點：
    1. _post_id 穩定且為 16 碼 hex；不同輸入不同 id。
    2. _strip_html 去標籤 + 還原 entity。
    3. _esc 轉義 < & >（防 Telegram HTML 破版）。
    4. 過舊新聞（>MAX_AGE_S）→ 不推。
    5. 已 seen → 跳過（不重推）。
    6. 新鮮且相關高重要度 → 推 1 則且 DB 標記 pushed=1、content_preview 為英文標題。
    7. 重要度低於門檻 → 不推、但仍 mark_seen（避免下輪重進 AI）。
    8. 每輪上限 MAX_PUSH_PER_CYCLE → 多則只推上限數。
    9. low_content 標題 → 跳過。
   10. 跨來源同事件（_is_dup_story 命中 recent_titles）→ 不推。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import news_feed.news_db as news_db

# news_db 全域 DB_PATH 指到臨時檔（_conn 每次讀全域 → import 後改即生效）
_TEST_DB = Path(tempfile.mkdtemp(prefix="cgnews_test_")) / "news_feed_test.db"
news_db.DB_PATH = _TEST_DB

import news_feed.coinglass_news as cn


def _fresh():
    """清空測試 DB；把跨來源去重的 DB 讀取替換為固定空清單（隔離正式庫）。"""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(_TEST_DB) + suffix)
        if p.exists():
            p.unlink()
    news_db.init_db()
    cn._recent_pushed_titles = lambda hours=24, sources=None: []


# --------------------------------------------------------------------------
# 假件
# --------------------------------------------------------------------------
class FakeSource:
    def __init__(self, articles):
        self._articles = articles
        self.calls = 0

    async def get_article_list(self, limit: int = 50):
        self.calls += 1
        return {"source": "coinglass", "articles": list(self._articles)[:limit]}


class FakeTG:
    def __init__(self):
        self.sent = []

    async def send_message(self, text, parse_mode=None):
        self.sent.append((text, parse_mode))


def _article(title, body="<p>some body</p>", age_s=60, source_name="Coindesk"):
    rel_ms = int((time.time() - age_s) * 1000)
    return {
        "article_title": title,
        "article_content": body,
        "article_release_time": rel_ms,
        "source_name": source_name,
        "article_picture": "",
    }


def _stub_llm(relevant=True, importance=8, summary="這是繁中摘要", translation="繁中全文"):
    async def _fake(handle, label, content, timeout_sec=90):
        return {
            "relevant": relevant, "category": "crypto", "importance": importance,
            "summary_zh": summary, "translation_zh": translation, "reason": "stub",
        }
    return _fake


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) \
        if False else asyncio.run(coro)


# --------------------------------------------------------------------------
# 純函式
# --------------------------------------------------------------------------
def test_post_id_stable_and_hex():
    a = cn._post_id("BTC breaks 100k", 1781746320000)
    b = cn._post_id("BTC breaks 100k", 1781746320000)
    c = cn._post_id("BTC breaks 100k", 1781746320001)  # 時間差一毫秒
    assert a == b, "同輸入應產生同 id"
    assert a != c, "不同發布時間應不同 id"
    assert len(a) == 16 and all(ch in "0123456789abcdef" for ch in a), "應為 16 碼 hex"
    print("  ok test_post_id_stable_and_hex")


def test_strip_html():
    out = cn._strip_html("<p>Hello&nbsp;<b>World</b> &amp; more</p>")
    assert "<" not in out and ">" not in out, "不應殘留標籤"
    assert out == "Hello World & more", f"entity 還原錯: {out!r}"
    print("  ok test_strip_html")


def test_esc():
    assert cn._esc("a < b & c > d") == "a &lt; b &amp; c &gt; d"
    assert cn._esc(None) == ""
    print("  ok test_esc")


# --------------------------------------------------------------------------
# process_once 整合（離線）
# --------------------------------------------------------------------------
def test_old_news_not_pushed():
    _fresh()
    cn.classify_and_translate = _stub_llm()
    src = FakeSource([_article("Old crypto headline alpha", age_s=cn.MAX_AGE_S + 600)])
    tg = FakeTG()
    n = _run(cn.process_once(tg, src))
    assert n == 0 and not tg.sent, "過舊新聞不應推送"
    print("  ok test_old_news_not_pushed")


def test_already_seen_skipped():
    _fresh()
    cn.classify_and_translate = _stub_llm()
    art = _article("Fresh story bravo unique words here")
    src = FakeSource([art])
    tg = FakeTG()
    n1 = _run(cn.process_once(tg, src))
    assert n1 == 1, "首次應推送"
    tg2 = FakeTG()
    n2 = _run(cn.process_once(tg2, src))
    assert n2 == 0 and not tg2.sent, "已 seen 應跳過"
    print("  ok test_already_seen_skipped")


def test_relevant_pushes_and_marks():
    _fresh()
    cn.classify_and_translate = _stub_llm(importance=9, summary="比特幣突破十萬")
    title = "Bitcoin breaks one hundred thousand charlie"
    src = FakeSource([_article(title)])
    tg = FakeTG()
    n = _run(cn.process_once(tg, src))
    assert n == 1 and len(tg.sent) == 1, "相關高重要度應推 1 則"
    text, mode = tg.sent[0]
    assert mode == "HTML" and "加密快訊" in text and "比特幣突破十萬" in text
    # DB 標記 pushed=1，content_preview = 英文原標題（供跨來源去重）
    pushed = news_db.get_recent_pushed(24)
    assert len(pushed) == 1 and pushed[0]["content_preview"].startswith("Bitcoin breaks")
    print("  ok test_relevant_pushes_and_marks")


def test_low_importance_filtered_but_marked():
    _fresh()
    cn.classify_and_translate = _stub_llm(importance=3)  # < MIN_IMPORTANCE(7)
    art = _article("Some minor delta crypto news item")
    src = FakeSource([art])
    tg = FakeTG()
    n = _run(cn.process_once(tg, src))
    assert n == 0 and not tg.sent, "低重要度不推"
    # 仍應 mark_seen（pushed=0）→ 下輪不重進 AI
    assert news_db.already_seen(cn.SOURCE, "Coindesk",
                                cn._post_id(art["article_title"],
                                            art["article_release_time"]))
    print("  ok test_low_importance_filtered_but_marked")


def test_cycle_cap():
    _fresh()
    cn.classify_and_translate = _stub_llm(importance=9)
    # 四則彼此字詞重疊 <50%（否則會被 _is_dup_story 當同事件去重，無法純測上限）
    arts = [
        _article("Bitcoin surges past major resistance zone"),
        _article("Ethereum staking withdrawals hit yearly record"),
        _article("Solana network outage fully resolved overnight"),
        _article("Ripple wins partial appeal court ruling"),
    ]
    src = FakeSource(arts)
    tg = FakeTG()
    n = _run(cn.process_once(tg, src))
    assert n == cn.MAX_PUSH_PER_CYCLE, f"應只推上限 {cn.MAX_PUSH_PER_CYCLE} 則，實得 {n}"
    print("  ok test_cycle_cap")


def test_low_content_title_skipped():
    _fresh()
    cn.classify_and_translate = _stub_llm()
    src = FakeSource([_article("hi")])  # is_low_content → True
    tg = FakeTG()
    n = _run(cn.process_once(tg, src))
    assert n == 0 and not tg.sent, "過短標題應跳過"
    print("  ok test_low_content_title_skipped")


def test_cross_source_dup_skipped():
    _fresh()
    cn.classify_and_translate = _stub_llm(importance=9)
    # 模擬 us_news 已推過同事件英文標題
    cn._recent_pushed_titles = lambda hours=24, sources=None: [
        "SEC approves spot Ethereum ETF for trading"]
    src = FakeSource([_article("SEC approves spot Ethereum ETF trading today")])
    tg = FakeTG()
    n = _run(cn.process_once(tg, src))
    assert n == 0 and not tg.sent, "跨來源同事件應去重不推"
    print("  ok test_cross_source_dup_skipped")


# --------------------------------------------------------------------------
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    print(f"running {len(tests)} tests...")
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
