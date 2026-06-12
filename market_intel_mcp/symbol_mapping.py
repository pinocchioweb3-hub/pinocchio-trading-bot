"""Symbol 命名空間對照：OKX 永續 ↔ CoinGlass ↔ 規範形式。

OKX:        SUI-USDT-SWAP / BTC-USDT-SWAP
CoinGlass:  SUIUSDT / BTCUSDT
規範形式:    SUI / BTC（內部用）

支援 watchlist + Hot 新進幣動態加入。
"""
from __future__ import annotations

from typing import Literal

Namespace = Literal["okx", "coinglass", "canonical"]


# === 分層 watchlist ===
# 指標層：規範市場走勢、推導 BTC regime / market regime
TIER_INDICATOR = ("BTC", "ETH", "SOL")

# 現貨層：使用者現貨倉位，純監控不交易
TIER_SPOT = ("SUI", "WLFI")

# 交易層候選池（30+ 個常見高流動性永續），動態挑出 Top 7-10
# v10: BNB 從候選池排除（30d 真實回測勝率 33% / -$339）
TRADING_CANDIDATES = (
    "BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "AVAX",
    "DOT", "LINK", "ARB", "OP", "TRX", "LTC", "ATOM", "NEAR",
    "AAVE", "UNI", "SEI", "APT", "INJ", "TIA", "SUI", "RNDR",
    "MATIC", "FIL", "FTM", "BCH", "ETC", "XLM",
)

# 向後相容
CORE_SYMBOLS = TIER_INDICATOR + TIER_SPOT

# Hot 動態名單 — 由 watchlist refresh 寫入
HOT_SYMBOLS: set[str] = set()


def to_canonical(symbol: str, ns: Namespace = "canonical") -> str:
    """正規化為內部 canonical（純大寫 base symbol）。

    >>> to_canonical("SUI-USDT-SWAP", "okx")
    'SUI'
    >>> to_canonical("SUIUSDT", "coinglass")
    'SUI'
    >>> to_canonical("sui", "canonical")
    'SUI'
    """
    s = symbol.upper().strip()
    if ns == "okx":
        return s.replace("-USDT-SWAP", "").replace("-USDT", "")
    if ns == "coinglass":
        return s.removesuffix("USDT").removesuffix("USD")
    return s


def to_okx(canonical: str) -> str:
    """canonical → OKX 永續格式"""
    return f"{canonical.upper()}-USDT-SWAP"


def to_coinglass(canonical: str) -> str:
    """canonical → CoinGlass 格式"""
    return f"{canonical.upper()}USDT"


def detect_namespace(symbol: str) -> Namespace:
    """智慧判斷 symbol 是哪種命名空間"""
    s = symbol.upper()
    if "-SWAP" in s or "-USDT" in s:
        return "okx"
    if s.endswith("USDT") or s.endswith("USD"):
        return "coinglass"
    return "canonical"


def normalize(symbol: str) -> str:
    """任何來源的 symbol → canonical（自動偵測）"""
    return to_canonical(symbol, detect_namespace(symbol))


def is_watched(canonical: str) -> bool:
    """是否在當前 watchlist（Core ∪ Hot）"""
    return canonical in CORE_SYMBOLS or canonical in HOT_SYMBOLS
