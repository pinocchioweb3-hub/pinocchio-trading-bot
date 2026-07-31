# -*- coding: utf-8 -*-
"""v175 新聞源測試：OKX news 子指令硬鎖 / 解析容錯 / CoinDesk RSS 解析。零網路。"""
import pytest

from news_feed import okx_news, coindesk_rss


def test_okx_news_subcommand_hard_lock():
    # ⛔ 安全鐵則：本模組唯一持有 live profile 呼叫能力,非 news 子指令必須 raise
    with pytest.raises(ValueError):
        okx_news._okx_news(["swap", "place"])
    with pytest.raises(ValueError):
        okx_news._okx_news(["account", "balance"])
    with pytest.raises(ValueError):
        okx_news._okx_news([])


def test_okx_parse_items_tolerates_both_shapes():
    assert okx_news.parse_items('[{"id":1,"title":"a"}]') == [{"id": 1, "title": "a"}]
    assert okx_news.parse_items('{"data":[{"id":2}]}') == [{"id": 2}]
    assert okx_news.parse_items("not json") == []
    assert okx_news.parse_items("{}") == []


def test_okx_fail_class():
    assert okx_news._fail_class("Your IP [REDACTED] is not included in your API key's whitelist") == "auth_ip_whitelist"
    assert okx_news._fail_class("HTTP 401 credential") == "auth"
    assert okx_news._fail_class("timeout") == "timeout"
    assert okx_news._fail_class("weird") == "other"


def test_okx_item_id_stable():
    a = okx_news._item_id({"id": 123})
    assert a == "okx123"
    b = okx_news._item_id({"title": "t", "publishTime": 111})
    assert b == okx_news._item_id({"title": "t", "publishTime": 111})


def test_coindesk_parse_rss():
    xml = """<?xml version="1.0"?><rss><channel>
      <item><title>Bitcoin Rises</title><link>https://x/a</link>
        <guid>g1</guid><pubDate>Fri, 01 Aug 2026 01:00:00 +0000</pubDate>
        <description>&lt;p&gt;Body text&lt;/p&gt;</description></item>
      <item><title></title></item>
    </channel></rss>"""
    items = coindesk_rss.parse_rss(xml)
    assert len(items) == 1
    assert items[0]["title"] == "Bitcoin Rises"
    assert items[0]["guid"] == "g1"
    assert items[0]["pub_ts"] > 0
    assert items[0]["desc"] == "Body text"
    assert coindesk_rss.parse_rss("<broken") == []
