"""新聞過濾規則（按 tier）。

純函式設計：給 (handle, content) 回 (should_push, push_reason, extracted_tickers)。
"""
from __future__ import annotations

import re
from .twitter_accounts import (
    EXCHANGE_KEYWORDS, MIN_WHALE_USD, POLICY_KEYWORDS,
    WATCHED_TICKERS, get_account_meta, get_filter_tier,
)


# Ticker patterns
_TICKER_RE = re.compile(r"\$?\b([A-Z]{2,6})\b")
# Whale tx 金額 pattern：1,234,567 USD / $1.2M / 1.2 million dollars
_DOLLAR_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s?(million|billion|M|B|k)?", re.IGNORECASE)


def extract_tickers(content: str) -> set[str]:
    """從文字抽出 ticker 提及（與我們 watchlist 取交集）"""
    found = set()
    for m in _TICKER_RE.finditer(content):
        t = m.group(1).upper()
        if t in WATCHED_TICKERS:
            found.add(t)
    return found


def extract_dollar_amount(content: str) -> float:
    """從文字抽最大美元金額（whale alert 用）。回 USD 數值；找不到回 0。"""
    max_usd = 0.0
    for m in _DOLLAR_RE.finditer(content):
        try:
            num = float(m.group(1).replace(",", ""))
            unit = (m.group(2) or "").lower()
            if unit in ("million", "m"):
                num *= 1_000_000
            elif unit in ("billion", "b"):
                num *= 1_000_000_000
            elif unit == "k":
                num *= 1_000
            max_usd = max(max_usd, num)
        except (ValueError, AttributeError):
            continue
    return max_usd


def has_keyword(content: str, keywords: set[str]) -> set[str]:
    """偵測命中的關鍵字（不分大小寫）"""
    lower = content.lower()
    return {k for k in keywords if k in lower}


def should_push(handle: str, content: str) -> tuple[bool, str, dict]:
    """主過濾函式。

    Returns:
        (should_push: bool, reason: str, meta: dict)
        meta 包含 tickers / keywords / dollar_amount 等過濾依據
    """
    meta = get_account_meta(handle)
    tier = meta["tier"]
    tickers = extract_tickers(content)
    info: dict = {"tier": tier, "category": meta["category"], "label": meta["label"],
                  "tickers": sorted(tickers), "handle": handle}

    # === T0: 全部推（政府 / Trump）===
    if tier == "T0":
        info["push_reason"] = "T0 全部推（怕漏訊）"
        return True, "T0_must_push", info

    # === Tm: 宏觀權威全推 ===
    if tier == "Tm":
        info["push_reason"] = f"Tm Macro（{meta['label']}）"
        return True, "Tm_macro_always", info

    # === Ts: 美股/總經快訊（v17：盤中寬鬆、盤外嚴格、每帳號限流）===
    if tier == "Ts":
        from .twitter_accounts import ts_should_forward
        ok, ts_reason = ts_should_forward(handle, content)
        if ok:
            info["push_reason"] = f"Ts 快訊（{meta['label']}）"
            return True, "Ts_squawk_pass", info
        return False, ts_reason, info

    # === T1: 交易所公告 ===
    if tier == "T1":
        hits = has_keyword(content, EXCHANGE_KEYWORDS)
        info["matched_keywords"] = sorted(hits)
        if hits:
            info["push_reason"] = f"T1 交易所關鍵字 {hits}"
            return True, "T1_exchange_keyword", info
        # 加碼：若提到我們 watchlist ticker，也推（可能是行情評論）
        if tickers:
            info["push_reason"] = f"T1 交易所 + ticker {tickers}"
            return True, "T1_ticker_mention", info
        return False, "T1_no_match", info

    # === T2: 鯨魚（amount 門檻 + ticker 提及）===
    if tier == "T2":
        amount = extract_dollar_amount(content)
        info["dollar_amount_usd"] = amount
        if amount >= MIN_WHALE_USD:
            if tickers:
                info["push_reason"] = f"T2 鯨魚 ${amount/1e6:.1f}M + {tickers}"
                return True, "T2_whale_with_ticker", info
            info["push_reason"] = f"T2 鯨魚 ${amount/1e6:.1f}M"
            return True, "T2_whale_amount", info
        return False, "T2_below_threshold", info

    # === T3: 創辦人 ===
    if tier == "T3":
        policy_hits = has_keyword(content, POLICY_KEYWORDS)
        info["matched_keywords"] = sorted(policy_hits)
        if tickers:
            info["push_reason"] = f"T3 創辦人 + ticker {tickers}"
            return True, "T3_founder_ticker", info
        if policy_hits:
            info["push_reason"] = f"T3 創辦人 + 政策 {policy_hits}"
            return True, "T3_founder_policy", info
        return False, "T3_no_signal", info

    # === T4: 交易員（必須有 ticker 才推）===
    if tier == "T4":
        if tickers:
            info["push_reason"] = f"T4 交易員 + ticker {tickers}"
            return True, "T4_trader_ticker", info
        return False, "T4_no_ticker", info

    # === T5: 新聞媒體 ===
    if tier == "T5":
        policy_hits = has_keyword(content, POLICY_KEYWORDS)
        info["matched_keywords"] = sorted(policy_hits)
        if tickers or policy_hits:
            info["push_reason"] = f"T5 新聞 ticker={tickers or '-'} keys={policy_hits or '-'}"
            return True, "T5_news_match", info
        return False, "T5_no_match", info

    # Fallback
    return False, "unknown_tier", info


# ===========================================================================
# 自測
# ===========================================================================
if __name__ == "__main__":
    test_cases = [
        ("realDonaldTrump", "Crypto is the future of America!"),
        ("SECGov", "Routine market structure announcement"),
        ("binance", "Binance lists $XYZ for futures trading"),
        ("binance", "Maintenance scheduled for 2026-06-15"),
        ("whale_alert", "🚨 1,234 BTC ($82M) transferred from unknown wallet to Binance"),
        ("whale_alert", "🚨 100 BTC ($6M) transferred from unknown wallet"),
        ("cz_binance", "Building BNB Chain ecosystem"),
        ("cz_binance", "Big update on $BNB tokenomics coming"),
        ("Pentosh1", "BTC looks ready for a move higher"),
        ("Pentosh1", "Loving this market vibe"),
        ("TheBlock__", "SEC approves spot $ETH ETF"),
        ("elonmusk", "Just had coffee"),
    ]
    for handle, content in test_cases:
        ok, reason, meta = should_push(handle, content)
        flag = "✅" if ok else "❌"
        print(f"{flag} @{handle:18s} | {reason:25s} | tickers={meta.get('tickers')}")
        print(f"   '{content[:70]}'")
