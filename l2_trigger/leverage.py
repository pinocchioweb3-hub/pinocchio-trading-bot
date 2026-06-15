"""自動槓桿選擇 + 倉位/TP 計算。

槓桿規則（依使用者偏好）：
    LEVERAGE_OVERRIDES 字典硬寫優先（WLFI=5 永遠 5x）
    否則依 7d ATR/price (%)：
        <5%  → default（低波動用預設槓桿；default 未指定時讀 botconfig
               的 DEFAULT_LEVERAGE，目前明確為 15x，使用者未拍板前不動）
        5–8% → 10x
        ≥8%  → 5x
    缺料 → 保守用 5x

倉位算法（R = 風險單位，金額由交易員自設，不綁死固定 U 數）：
    notional_usd = risk_usd / |entry - stop| × entry
    margin_usd   = notional_usd / leverage
"""
from __future__ import annotations

from typing import Optional


# 硬性 override（不論 ATR 都用這個）
LEVERAGE_OVERRIDES: dict[str, int] = {
    "WLFI": 5,
}

# 自動分層門檻（7d ATR/price，%）
ATR_TIER_MED = 5.0
ATR_TIER_HIGH = 8.0


def choose_leverage(symbol: str, atr_pct_7d: Optional[float],
                    default: int | None = None) -> int:
    """回傳該標的合理槓桿。
    None → 保守 5x（不冒險）。
    v23-2: default 未指定時讀 botconfig（讓 .env 的 DEFAULT_LEVERAGE 活過來）。
    """
    if default is None:
        from botconfig import CONFIG
        default = CONFIG.default_leverage
    if symbol in LEVERAGE_OVERRIDES:
        return LEVERAGE_OVERRIDES[symbol]
    if atr_pct_7d is None:
        return 5
    if atr_pct_7d >= ATR_TIER_HIGH:
        return 5
    if atr_pct_7d >= ATR_TIER_MED:
        return 10
    return default


def compute_position(entry: float, stop: float, risk_usd: float,
                     leverage: int) -> dict:
    """從進場/止損/風險/槓桿算出名目與保證金。

    sl_distance_pct 給 L3 訊息「清算距離」對照用。
    """
    sl_distance = abs(entry - stop)
    if sl_distance == 0:
        raise ValueError("entry == stop: cannot compute position")
    if leverage <= 0:
        raise ValueError(f"leverage must be > 0, got {leverage}")
    if risk_usd <= 0:
        raise ValueError(f"risk_usd must be > 0, got {risk_usd}")

    notional_usd = (risk_usd / sl_distance) * entry
    margin_usd = notional_usd / leverage
    contracts = notional_usd / entry
    sl_distance_pct = (sl_distance / entry) * 100

    return {
        "notional_usd": round(notional_usd, 2),
        "margin_usd": round(margin_usd, 2),
        "contracts": round(contracts, 6),
        "sl_distance_pct": round(sl_distance_pct, 3),
    }


def compute_tp_prices(entry: float, stop: float, direction: str,
                      r_multiples: tuple[float, ...]) -> dict[str, float]:
    """從 R 倍數算 TP1/TP2/TP3 價位。

    direction: "bull"（多）/"bear"（空）
    r_multiples: 例如 (1.0, 1.5, 2.0)
    """
    if direction not in ("bull", "bear"):
        raise ValueError(f"direction must be bull|bear, got {direction}")
    sl_distance = abs(entry - stop)
    out: dict[str, float] = {}
    for i, r in enumerate(r_multiples, start=1):
        tp = entry + sl_distance * r if direction == "bull" else entry - sl_distance * r
        out[f"tp{i}"] = round(tp, 6)
        out[f"tp{i}_r"] = r
    return out
