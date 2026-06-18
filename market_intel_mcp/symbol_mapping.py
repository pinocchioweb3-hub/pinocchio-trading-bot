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


# ===========================================================================
# task#33 additive：跨源別名收斂表（ADDITIVE — 不改既有任何函式行為）
# ---------------------------------------------------------------------------
# 動機：同一資產在不同交易所/數據源用不同 ticker，跨源共現/共振前須先收斂到
# 同一 canonical，否則「OKX 給 RNDR、CoinGlass 給 RENDER、HL 給 RENDER」會被
# 當成三個不同幣，triple_present 永遠落空。
#
# 口徑（與 sources/hyperliquid._HL_ALIAS 一致）：
#   canonical 一律用「無前綴、無千倍標記」的主名（如 PEPE，不是 kPEPE/1000PEPE）。
#   別名 → canonical 為「多對一」收斂。本表只收斂到 canonical，不負責產生各源格式
#   （各源格式仍由 to_okx / to_coinglass / hyperliquid._hl_coin 負責）。
#
# 收錄原則：只列「已知會在候選池/熱點出現、且跨源 ticker 確實有別」者。
#   - 改名/遷移：RNDR→RENDER（Render 改名）、MATIC→POL（Polygon 改名）、
#                FTM→S（Fantom 遷移為 Sonic）。
#   - 千倍合約 ticker：1000PEPE/kPEPE→PEPE 等（CEX 用 1000x、HL 用 k 前綴，
#     底層同一資產）。
#   WBTC/XBT→BTC 為「人工決定項」：WBTC 是包裝資產、XBT 是 BTC 別名，是否視為
#   同一資產涉及流動性/風險判斷，預設「不併」（保留註解，需人工拍板才開）。
# ===========================================================================
SYMBOL_ALIASES: dict[str, str] = {
    # --- 改名 / 鏈遷移（多對一收斂到新主名）---
    "RNDR": "RENDER",          # Render Network 改名 RNDR→RENDER
    "MATIC": "POL",            # Polygon 代幣 MATIC→POL
    "FTM": "S",                # Fantom 遷移為 Sonic（FTM→S）
    # --- 千倍合約 ticker → 底層資產 canonical ---
    "1000PEPE": "PEPE",
    "KPEPE": "PEPE",           # HL k 前綴（.upper() 後為 KPEPE）
    "1000SHIB": "SHIB",
    "KSHIB": "SHIB",
    "1000BONK": "BONK",
    "KBONK": "BONK",
    "1000FLOKI": "FLOKI",
    "KFLOKI": "FLOKI",
    "1000SATS": "SATS",
    # --- 人工決定項：預設「不併」（需人工拍板才取消註解）---
    # "WBTC": "BTC",   # 包裝 BTC，流動性/風險不同，預設視為獨立資產
    # "XBT": "BTC",    # BTC 的另一代碼（部分交易所），預設不自動併
}


def to_canonical_aliased(symbol: str, ns: Namespace = "canonical") -> str:
    """先去交易所後綴（沿用既有 to_canonical），再過別名表收斂到主 canonical。

    與 to_canonical 的差異：多加一步 SYMBOL_ALIASES 收斂，讓跨源 ticker（RNDR /
    RENDER、1000PEPE / kPEPE / PEPE）落到同一 canonical。**不改 to_canonical 本身**。

    >>> to_canonical_aliased("RNDR-USDT-SWAP", "okx")
    'RENDER'
    >>> to_canonical_aliased("RENDERUSDT", "coinglass")
    'RENDER'
    >>> to_canonical_aliased("1000PEPE")
    'PEPE'
    >>> to_canonical_aliased("kPEPE")
    'PEPE'
    >>> to_canonical_aliased("MATIC")
    'POL'
    """
    base = to_canonical(symbol, ns).upper()
    return SYMBOL_ALIASES.get(base, base)
