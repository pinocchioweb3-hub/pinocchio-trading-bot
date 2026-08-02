# -*- coding: utf-8 -*-
"""OKX 新聞源 v220 ── 「呼叫成功但一則都沒推」必須出聲，且錯誤處方不得寫死。

治的是什麼（線上實證，2026-08-02）：
    okx_news 自 v175（8/01）上線以來，news_feed.db 裡 source='okxnews' **一列都沒有**。
    但日誌從頭到尾沒有任何一行說它壞了——因為它 exit=0（源其實是通的），
    而 parse_items 認 "data"/"list"，OKX 實際回的頂層 key 是 **"details"** ⇒ 恆得 0 則
    ⇒ 不進 mark_seen、不印 pushed ⇒ 「解析不出來」被折成「今天沒有重要新聞」。
    同一物種第 25 次。

    第二段：就算解析對了，_ts_ms 認 publishTime/time，OKX 給的是 **cTime** ⇒ 回 0
    ⇒ now - 0 > 3h ⇒ 每一則都被判 too_old ⇒ 還是一則都不推。
    兩段各自獨立，任一段沒修都是同樣的靜默零。

    第三段：故障行把「補 IP 白名單後自癒」**寫死**，不管 _fail_class 回什麼類別都照貼。
    使用者 8/01 就把 IP 白名單取消了；這句話正是昨天讓他跑去 OKX 改一個
    早已不存在的設定的來源。處方必須由類別推導，未知就講未知。
"""
from __future__ import annotations

import json

from news_feed import okx_news as m


# OKX ATK CLI `news important --json` 的真實回應形狀（2026-08-02 線上抓取、內容截短）
_REAL = json.dumps({
    "details": [
        {"cTime": "1785662942000", "ccyList": ["BTC", "ETH"], "ccySentiments": [],
         "content": "", "id": "83554106131744", "importance": "high",
         "platformList": ["blockbeats"], "summary": "摘要內文", "title": "標題一"},
        {"cTime": "1785662000000", "ccyList": [], "ccySentiments": [],
         "content": "", "id": "83554106131745", "importance": "high",
         "platformList": ["blockbeats"], "summary": "摘要二", "title": "標題二"},
    ],
    "nextCursor": "MTc4NTY2MDM3NjAwMDo4MzU1MTY0ODg0NTA4OA==",
}, ensure_ascii=False)


# ── ① 解析：真實形狀必須解得出來 ────────────────────────────────────────


def test_parses_the_real_details_envelope():
    """線上實際回的是 {"details": [...]}。這條在修好前必紅。"""
    items = m.parse_items(_REAL)
    assert len(items) == 2
    assert items[0]["title"] == "標題一"


def test_still_parses_legacy_envelopes():
    """⛔ 不得為了修 details 而弄壞既有的 data/list 相容。"""
    assert len(m.parse_items('{"data": [{"id": 1}]}')) == 1
    assert len(m.parse_items('{"list": [{"id": 1}]}')) == 1
    assert len(m.parse_items('[{"id": 1}]')) == 1


def test_timestamp_reads_ctime():
    """OKX 給 cTime（毫秒字串）。讀不到會回 0 ⇒ 每則都被判 too_old。"""
    it = json.loads(_REAL)["details"][0]
    assert m._ts_ms(it) == 1785662942000.0


def test_timestamp_unknown_is_not_silently_zero():
    """⛔ 讀不到時間 ≠ 1970 年的舊聞。回 None 讓呼叫端自己決定，不可折成 0。"""
    assert m._ts_ms({"title": "無時間"}) is None


# ── ② 卡片：欄位名要對得上真實回應 ──────────────────────────────────────


def test_card_uses_ccylist_for_coins():
    it = json.loads(_REAL)["details"][0]
    card = m.render_card(it)
    assert "BTC" in card and "標題一" in card


def test_card_survives_unknown_sentiment_shape():
    """ccySentiments 線上樣本恆為空；⛔ 不得為了顯示而臆造形狀 → 不認識就不顯示。"""
    card = m.render_card({"title": "T", "ccySentiments": [{"weird": 1}]})
    assert "T" in card


# ── ③ 故障處方：由類別推導，未知講未知 ──────────────────────────────────


def test_ip_whitelist_hint_only_for_that_class():
    """⛔ 這是本次核心：auth ≠ auth_ip_whitelist，處方不可寫死。"""
    assert "白名單" in m.fail_message(1, "error: ip not in whitelist")
    assert "白名單" not in m.fail_message(1, "HTTP 401 invalid credential")


def test_unknown_class_says_unknown_and_shows_evidence():
    msg = m.fail_message(1, "ECONNRESET socket hang up")
    assert "原因未知" in msg
    assert "ECONNRESET" in msg, "未知就必須附原文，否則沒人查得下去"


def test_fail_message_redacts_long_tokens():
    """⛔ repo 是 public，故障原文可能夾金鑰 id。"""
    msg = m.fail_message(1, "bad key aBcD1234567890EfGhIjKl99 rejected")
    assert "aBcD1234567890EfGhIjKl99" not in msg


# ── ④ 靜默零：成功但解析不出東西，必須出聲 ──────────────────────────────


def test_success_with_zero_parsed_is_an_anomaly():
    """exit=0 卻解析出 0 則＝本次的元兇狀態。它以前完全不出聲。"""
    msg = m.parse_anomaly('{"details": [], "nextCursor": "x"}')
    assert msg is not None
    assert "不等於" in msg, "必須明講：解析不出來 ≠ 今天沒有重要新聞"


def test_unrecognised_envelope_reports_its_keys():
    msg = m.parse_anomaly('{"brandNewKey": [{"id": 1}]}')
    assert msg is not None and "brandNewKey" in msg, "換了外殼要看得出來是換了外殼"


def test_broken_json_is_distinct_from_empty():
    broken = m.parse_anomaly("<html>502</html>")
    empty = m.parse_anomaly('{"details": []}')
    assert broken is not None and broken != empty


def test_no_anomaly_when_items_parse():
    assert m.parse_anomaly(_REAL) is None


# ── ⑤ 推 0 則要有理由 ───────────────────────────────────────────────────


def test_zero_pushed_with_items_explains_itself():
    line = m.cycle_summary(parsed=20, dup=18, too_old=2, pushed=0)
    assert line is not None
    assert "20" in line and "18" in line and "2" in line


def test_quiet_when_something_was_pushed():
    assert m.cycle_summary(parsed=20, dup=18, too_old=0, pushed=2) is None


# ── ⑥ 真因（線上實證）：空字串佔位被 CLI 讀成「有提供憑證」──────────────
#
# daemon 實跑回的是：
#   "Partial API credentials detected. Hint: Set OKX_API_KEY, OKX_SECRET_KEY
#    and OKX_PASSPHRASE together (env vars or config.toml profile)."
# .env 帶了 OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE 三個**空字串**佔位,
# CLI 只看「變數在不在」⇒ 判 partial ⇒ 整個拒絕,且不回退到 config.toml 的
# [profiles.live]（那裡三個欄位都填好了）。空字串不是憑證——同物種在環境變數層。
# 手動測會通是因為沒載 .env,這正是「我的殼跟 daemon 的殼不同」型的假陰性。

_PARTIAL = ("error: Partial API credentials detected. Hint: Set OKX_API_KEY, "
            "OKX_SECRET_KEY and OKX_PASSPHRASE together (env vars or config.toml "
            "profile). Version: @okx_ai/okx-trade-cli@1.4.2")


def test_partial_credentials_is_its_own_class():
    """⛔ 舊分類因為字串含 credential 就歸成 auth ⇒ 看起來像金鑰壞了,
    實際上是設定被空值污染,方向完全相反。"""
    assert m._fail_class(_PARTIAL) == "credentials_partial"


def test_partial_credentials_hint_points_at_env_not_at_keys():
    msg = m.fail_message(1, _PARTIAL)
    assert "白名單" not in msg
    assert "空" in msg and "config.toml" in msg


def test_child_env_drops_empty_credential_placeholders(monkeypatch):
    monkeypatch.setenv("OKX_API_KEY", "")
    monkeypatch.setenv("OKX_API_SECRET", "   ")
    monkeypatch.setenv("OKX_TRADE_API_KEY", "realvalue")
    monkeypatch.setenv("OKX_DEMO_TRADING_ENABLED", "")
    env = m._child_env()
    assert "OKX_API_KEY" not in env, "空字串佔位不移除 ⇒ CLI 永遠判 partial"
    assert "OKX_API_SECRET" not in env
    assert env["OKX_TRADE_API_KEY"] == "realvalue", "⛔ 有值的一律不得動"
    assert "OKX_DEMO_TRADING_ENABLED" in env, "⛔ 只清憑證形狀的空變數,不掃全場"


def test_child_env_never_injects_values(monkeypatch):
    """⛔ 這個函式只准刪空值,永遠不准填任何憑證進去。"""
    monkeypatch.delenv("OKX_API_KEY", raising=False)
    env = m._child_env()
    assert "OKX_API_KEY" not in env
