"""新聞情報串接模組（Phase 3）。

Workers:
    truth_social_rss: 抓 trumpstruth.org RSS（Trump 第一手）
    twitter_apify:    透過 Apify Twitter Scraper 抓 X 帳號
    news_filter:      Claude 過濾 + tier 規則 + dedupe
    news_db:          SQLite 紀錄已推送過的訊息 id
"""
