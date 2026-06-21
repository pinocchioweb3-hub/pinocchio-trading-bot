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


# ── v83 task#5：依止損距離推導「資金效率最高、但清算價仍在止損之外」的槓桿 ──
#   研究 w7r04t691（106 代理、高信心）：固定名目下槓桿只是保證金效率旋鈕、不改風險，
#   但清算逆向緩衝 ≈ 100/leverage(%)。故正確法＝先依止損定倉位，再取「清算距離 ≥ 止損
#   距離 × LIQ_BUFFER_MULT」前提下的最高槓桿（最省保證金）。止損越緊→可用越高槓桿仍安全；
#   止損越寬→自動降槓桿，避免「清算先於止損」失控虧損。解 demo 51008（低槓桿鎖太多保證金）。
LEV_CEILING = 20       # 模擬盤資金效率天花板（使用者：15–20x）
LEV_FLOOR = 2
LIQ_BUFFER_MULT = 1.5  # 清算須在止損的 1.5 倍距離之外（緩衝維持保證金/費用/滑點）


def leverage_for_stop(sl_distance_pct: float, *, ceiling: int = LEV_CEILING,
                      floor: int = LEV_FLOOR,
                      liq_buffer_mult: float = LIQ_BUFFER_MULT) -> int:
    """依止損距離(%)取最高安全槓桿：lev ≤ 100 / (sl% × buffer)，夾在 [floor, ceiling]。
    保證 100/lev ≥ sl% × buffer（清算在止損之外）；對過寬止損退到 floor（倉位本就極小）。"""
    if not sl_distance_pct or sl_distance_pct <= 0:
        return floor
    import math
    safe_max = int(math.floor(100.0 / (sl_distance_pct * liq_buffer_mult)))
    return max(floor, min(ceiling, safe_max))


def compute_position(entry: float, stop: float, risk_usd: float,
                     leverage: int, *, max_notional_usd: float | None = None) -> dict:
    """從進場/止損/風險/槓桿算出名目與保證金。

    sl_distance_pct 給 L3 訊息「清算距離」對照用。
    max_notional_usd：Jesse 陷阱防呆——止損過緊會讓名目爆量超過資金；給上限則封頂名目，
      實際風險隨之下降（誠實回報 realized_risk_usd 與 capped）。預設 None＝不封頂（相容）。
    """
    sl_distance = abs(entry - stop)
    if sl_distance == 0:
        raise ValueError("entry == stop: cannot compute position")
    if leverage <= 0:
        raise ValueError(f"leverage must be > 0, got {leverage}")
    if risk_usd <= 0:
        raise ValueError(f"risk_usd must be > 0, got {risk_usd}")

    notional_usd = (risk_usd / sl_distance) * entry
    realized_risk_usd = risk_usd
    capped = False
    if max_notional_usd is not None and notional_usd > max_notional_usd > 0:
        notional_usd = max_notional_usd
        realized_risk_usd = (notional_usd / entry) * sl_distance  # 封頂後反推真實風險
        capped = True
    margin_usd = notional_usd / leverage
    contracts = notional_usd / entry
    sl_distance_pct = (sl_distance / entry) * 100

    return {
        "notional_usd": round(notional_usd, 2),
        "margin_usd": round(margin_usd, 2),
        "contracts": round(contracts, 6),
        "sl_distance_pct": round(sl_distance_pct, 3),
        "realized_risk_usd": round(realized_risk_usd, 2),
        "capped": capped,
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
