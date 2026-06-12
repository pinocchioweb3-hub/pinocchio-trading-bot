"""33 帳號清單 + 每個帳號的 tier 過濾規則（v17 重整：38→33）。

Tier:
    T0 = 全部推（政府/央行，極低頻怕漏訊）
    Tm = 宏觀權威全推（低頻、每帖有資訊量，標 macro tag）
    Ts = 美股/總經快訊（盤中寬鬆關鍵字、盤外嚴格關鍵字、每帳號每小時限流）v17 新增
    T1 = 交易所公告（list/delist/launch/halt 才推）
    T2 = 鯨魚地址（$ amount >= MIN_WHALE_USD 才推）
    T3 = 創辦人/巨頭（ticker mention 或政策關鍵字）
    T4 = 交易員（ticker mention）
    T5 = 新聞媒體（ticker mention 或關鍵字）

v17 移除（11）：realDonaldTrump（Truth Social RSS 已全量覆蓋）、CathieDWood、
    HTX_Global、krakenfx、glassnode、nansen_ai、CryptoQuant_com、CryptoCapo_、
    TheKingfisher_、DocumentingBTC、Cointelegraph
v17 新增（6）：federalreserve(T0)、NickTimiraos(Tm)、DeItaone/FirstSquawk/
    unusual_whales/KobeissiLetter(Ts)
v17 降級：elonmusk Tm→T3（日量 50-100+，改關鍵字過濾）
"""
from __future__ import annotations

import datetime as _dt

# ===========================================================================
# 帳號 → tier 對照表
# ===========================================================================
TWITTER_ACCOUNTS: dict[str, dict] = {
    # === T0 全部推（政府/央行） ===
    "WhiteHouse":      {"tier": "T0", "category": "macro_gov",      "label": "White House"},
    "SECGov":          {"tier": "T0", "category": "macro_sec",      "label": "SEC"},
    "CFTC":            {"tier": "T0", "category": "macro_cftc",     "label": "CFTC"},
    "USTreasury":      {"tier": "T0", "category": "macro_treasury", "label": "US Treasury"},
    "federalreserve":  {"tier": "T0", "category": "macro_fed",      "label": "Federal Reserve"},

    # === Tm 宏觀權威全推 ===
    "NickTimiraos":    {"tier": "Tm", "category": "macro_fedwire",  "label": "Nick Timiraos (Fed傳聲筒)"},

    # === Ts 美股/總經快訊（v17 新 tier）===
    "DeItaone":        {"tier": "Ts", "category": "squawk",         "label": "Walter Bloomberg"},
    "FirstSquawk":     {"tier": "Ts", "category": "squawk",         "label": "First Squawk"},
    "unusual_whales":  {"tier": "Ts", "category": "options_flow",   "label": "Unusual Whales"},
    "KobeissiLetter":  {"tier": "Ts", "category": "macro_analysis", "label": "Kobeissi Letter"},

    # === T1 交易所公告 ===
    "binance":         {"tier": "T1", "category": "exchange",       "label": "Binance"},
    "coinbase":        {"tier": "T1", "category": "exchange",       "label": "Coinbase"},
    "OKX":             {"tier": "T1", "category": "exchange",       "label": "OKX"},
    "bybit_official":  {"tier": "T1", "category": "exchange",       "label": "Bybit"},

    # === T2 鏈上鯨魚 ===
    "whale_alert":     {"tier": "T2", "category": "onchain_whale",  "label": "Whale Alert"},
    "lookonchain":     {"tier": "T2", "category": "onchain_whale",  "label": "Lookonchain"},
    "SpotOnChain":     {"tier": "T2", "category": "onchain",        "label": "Spot On Chain"},
    "arkham":          {"tier": "T2", "category": "onchain",        "label": "Arkham"},

    # === T3 創辦人/巨頭 ===
    "cz_binance":      {"tier": "T3", "category": "founder",        "label": "CZ"},
    "VitalikButerin":  {"tier": "T3", "category": "founder",        "label": "Vitalik"},
    "saylor":          {"tier": "T3", "category": "founder",        "label": "Saylor (MSTR)"},
    "aeyakovenko":     {"tier": "T3", "category": "founder",        "label": "Anatoly (SOL)"},
    "StaniKulechov":   {"tier": "T3", "category": "founder",        "label": "Stani (Aave)"},
    "elonmusk":        {"tier": "T3", "category": "macro_elon",     "label": "Elon Musk"},

    # === T4 交易員 ===
    "Pentosh1":        {"tier": "T4", "category": "trader",         "label": "Pentoshi"},
    "CryptoCred":      {"tier": "T4", "category": "trader",         "label": "Crypto Cred"},
    "HsakaTrades":     {"tier": "T4", "category": "trader",         "label": "Hsaka"},
    "AltcoinSherpa":   {"tier": "T4", "category": "trader",         "label": "Altcoin Sherpa"},
    "DonAlt":          {"tier": "T4", "category": "trader",         "label": "DonAlt"},

    # === T5 新聞媒體 ===
    "TheBlock__":      {"tier": "T5", "category": "news",           "label": "The Block"},
    "CoinDesk":        {"tier": "T5", "category": "news",           "label": "CoinDesk"},
    "WatcherGuru":     {"tier": "T5", "category": "news",           "label": "Watcher Guru"},
    "CoinbaseAssets":  {"tier": "T5", "category": "news_listings",  "label": "Coinbase Assets"},
}


# ===========================================================================
# 過濾規則
# ===========================================================================
# 加密 watchlist tickers
WATCHED_TICKERS = {
    "BTC", "ETH", "SOL", "SUI", "WLFI", "XRP", "DOGE", "BNB", "ADA", "AVAX",
    "MATIC", "LINK", "DOT", "ATOM", "NEAR", "ARB", "OP", "LDO", "AAVE", "UNI",
    "APT", "SEI", "TIA", "INJ", "FIL", "ETC", "BCH", "PEPE", "WIF", "FET",
    "RNDR", "MSTR", "COIN", "MARA", "RIOT",
}

# v17: OKX 美股永續 tickers（否則 Ts/T4 的 ticker 過濾對美股永不命中）
US_STOCK_TICKERS = {
    "TSLA", "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "AMD",
    "MU", "SNDK", "SOXL", "QQQ", "SPY", "PLTR", "HOOD",
    "OPENAI", "ANTHROPIC",
}
WATCHED_TICKERS |= US_STOCK_TICKERS

# T1 交易所「上下架/launch」關鍵字
EXCHANGE_KEYWORDS = {
    "list", "listing", "delist", "delisting", "launch", "launches", "launched",
    "halt", "halts", "halted", "suspend", "suspends", "suspended", "resume",
    "perpetual", "spot trading", "margin trading", "new asset", "new token",
    "futures launch", "available for trading",
}

# T3/T5 政策 / 大事件關鍵字
POLICY_KEYWORDS = {
    "etf", "sec", "approve", "approval", "approved", "reject", "denied",
    "regulation", "rule", "policy", "executive order", "ban", "legal",
    "lawsuit", "settlement", "guidance", "framework", "stablecoin", "cbdc",
    "halving", "hard fork", "fork", "upgrade", "mainnet",
    "hack", "exploit", "drain", "stolen", "rug", "exit scam",
    "airdrop", "tokenomics", "burn", "buyback",
    "rate cut", "rate hike", "fed", "fomc", "cpi", "fomc minutes",
}

# v17: Ts 高影響關鍵字（盤外唯一放行條件之一）
HIGH_IMPACT_KEYWORDS = {
    "fed", "fomc", "powell", "rate cut", "rate hike", "rate decision",
    "cpi", "ppi", "pce", "nfp", "payrolls", "jobless claims", "gdp", "ism",
    "tariff", "trade deal", "sanctions", "export controls",
    "war", "strike", "ceasefire", "nuclear", "missile",
    "halt", "halted", "circuit breaker", "bankruptcy", "chapter 11",
    "downgrade", "default", "shutdown", "debt ceiling", "treasury auction",
    "sec investigation", "antitrust", "emergency", "opec",
}

TS_HOURLY_CAP = 15  # Ts 單帳號每小時最多通過件數（DeItaone 日量可達 300+）

US_CASH_SESSION_UTC = ((13, 30), (20, 0))   # 夏令；冬令 ((14, 30), (21, 0))

# T2 鯨魚最小金額（USD）
MIN_WHALE_USD = 30_000_000  # $30M

# Ts 每帳號每小時計數器（module state）
_ts_hourly: dict[tuple[str, str], int] = {}


def is_us_cash_session(now_utc: _dt.datetime | None = None) -> bool:
    now_utc = now_utc or _dt.datetime.now(_dt.timezone.utc)
    (h1, m1), (h2, m2) = US_CASH_SESSION_UTC
    t = now_utc.hour * 60 + now_utc.minute
    return h1 * 60 + m1 <= t < h2 * 60 + m2


def ts_should_forward(handle: str, text: str,
                      now_utc: _dt.datetime | None = None) -> tuple[bool, str]:
    """Ts tier：盤中寬鬆、盤外嚴格、每帳號每小時限流。
    通過後仍走既有 LLM 過濾。回 (pass, reason)。"""
    now_utc = now_utc or _dt.datetime.now(_dt.timezone.utc)
    hour_key = (handle.lower(), now_utc.strftime("%Y%m%d%H"))
    if _ts_hourly.get(hour_key, 0) >= TS_HOURLY_CAP:
        return False, "Ts_hourly_cap"

    low = text.lower()
    hit_ticker = any(t.lower() in low for t in WATCHED_TICKERS)
    hit_impact = any(k in low for k in HIGH_IMPACT_KEYWORDS)
    if is_us_cash_session(now_utc):
        hit_policy = any(k in low for k in POLICY_KEYWORDS)
        ok = hit_ticker or hit_impact or hit_policy
    else:
        ok = hit_ticker or hit_impact  # 盤外門檻提高

    if ok:
        _ts_hourly[hour_key] = _ts_hourly.get(hour_key, 0) + 1
        # 清掉舊小時 key（防 dict 無限長大）
        if len(_ts_hourly) > 200:
            cur = now_utc.strftime("%Y%m%d%H")
            for k in [k for k in _ts_hourly if k[1] != cur]:
                del _ts_hourly[k]
        return True, "Ts_pass"
    return False, "Ts_no_match"


def get_filter_tier(handle: str) -> str:
    norm = handle.lower().lstrip("@")
    for k, v in TWITTER_ACCOUNTS.items():
        if k.lower() == norm:
            return v["tier"]
    return "T5"


def get_account_meta(handle: str) -> dict:
    norm = handle.lower().lstrip("@")
    for k, v in TWITTER_ACCOUNTS.items():
        if k.lower() == norm:
            return {**v, "handle": k}
    return {"tier": "T5", "category": "unknown", "label": handle, "handle": handle}


def get_all_handles() -> list[str]:
    return list(TWITTER_ACCOUNTS.keys())


_WATCHED_LOWER = {k.lower() for k in TWITTER_ACCOUNTS}


def is_watched(handle: str) -> bool:
    """白名單檢查 — Apify 搜尋會夾帶非清單帳號（廣告/提及），必須擋掉"""
    return handle.lower().lstrip("@") in _WATCHED_LOWER


if __name__ == "__main__":
    from collections import Counter
    c = Counter(v["tier"] for v in TWITTER_ACCOUNTS.values())
    print(f"Total: {len(TWITTER_ACCOUNTS)} accounts")
    for tier in sorted(c.keys()):
        print(f"  {tier}: {c[tier]}")
    print(f"Watched tickers: {len(WATCHED_TICKERS)} (含美股 {len(US_STOCK_TICKERS)})")
    import datetime
    ok, r = ts_should_forward("DeItaone", "Fed's Powell signals rate cut in September")
    print(f"Ts test (fed news): {ok} {r}")
    ok2, r2 = ts_should_forward("DeItaone", "Good morning everyone!")
    print(f"Ts test (noise): {ok2} {r2}")