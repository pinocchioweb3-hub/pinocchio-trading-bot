"""跨源指標共振（task#33 純函式核心）。

問題：單一交易所的 funding / OI / 價格動能可能被該所自身的庫存、清算、刷量
扭曲。把「同一指標在多家獨立源是否同方向」做共識，能濾掉單所雜訊——三家所
funding 都偏空（負）才算「真的有空方付錢的軋空燃料」，只有一家偏空是噪音。

本模組職責：把「每源、每指標」的方向訊號彙整成「跨源共振」描述，並產出一個
**僅供 shadow 觀測**的 strength_multiplier。

════════════════════════════════════════════════════════════════════════════
鐵則（紅線禁區 strength.py 不可動，本模組不得繞道破壞之）：
    1. strength_multiplier 是 **SHADOW 專用**。永不乘進 strength_score、
       永不回寫 snapshot、永不影響 fire 決策。它只進「影子觀測欄」供日後
       A/B 回測評估共振是否真的加 alpha。
    2. 本模組（convergence.py）**不得 import 並呼叫任何會變更 strength 的東西**
       （不 import market_intel_mcp.strength，不寫任何 DB/snapshot/fire 佇列）。
    3. 全模組純函式：零 I/O、零 API、零隨機、不改輸入參數。
════════════════════════════════════════════════════════════════════════════

方向約定（direction_of 回 -1 / 0 / +1，語意統一為「對價格的看多燃料方向」）：
    price/return/oi/cvd: 數值 > band → +1（偏多）、< -band → -1（偏空）。
    funding:             **負** funding = 空方付多方 = 軋空/偏多燃料 → 負值回 +1。
                         （刻意反號，與 scanner「FUNDING_NEG 為偏多訊號」一致。）
"""
from __future__ import annotations

# funding 類指標（值的符號要反轉成「對多方的燃料方向」）
_FUNDING_LIKE = ("funding", "funding_rate", "funding_8h", "funding_hourly")

# 共振判定門檻
_MIN_AGREE = 2          # 至少 2 源同號才談「共振」
_MIN_RATIO = 0.6        # 同號比例 ≥ 0.6


def direction_of(metric: str, value, neutral_band: float = 0.0) -> int:
    """單一(源,指標)值 → 方向 -1 / 0 / +1（語意：對價格的看多燃料）。

    - value 為 None / 非數 → 0（中性，缺料不臆測方向）。
    - |value| ≤ neutral_band → 0（落在中性帶內視為無方向）。
    - funding 類：負 funding = 偏多燃料 → 回 +1；正 funding = 偏空 → -1。
    - 其餘（price/return/oi/cvd…）：正 → +1、負 → -1。

    >>> direction_of("price", 1.2)
    1
    >>> direction_of("price", -0.5)
    -1
    >>> direction_of("funding", -0.0008)   # 負 funding → 偏多燃料
    1
    >>> direction_of("funding", 0.0008)
    -1
    >>> direction_of("oi", None)
    0
    >>> direction_of("price", 0.0, neutral_band=0.1)
    0
    """
    if not isinstance(value, (int, float)):
        return 0
    band = abs(neutral_band)
    if -band <= value <= band:
        return 0
    raw = 1 if value > 0 else -1
    m = (metric or "").lower()
    is_funding = any(m == f or m.startswith(f) for f in _FUNDING_LIKE)
    if is_funding:
        return -raw   # funding 反號：負→+1（偏多燃料）
    return raw


def metric_convergence(metric: str,
                       per_exchange_signals: dict[str, int]) -> dict:
    """單一指標在多源的共振判定。**不改輸入**。

    參數
    ----
    metric: 指標名（僅標註用，方向已由 direction_of 決定後傳入此處）。
    per_exchange_signals: {source: dir∈{-1,0,+1}}。0 = 該源無方向/缺料，
        不計入「present」分母（只有明確 ±1 的源才算「有表態」）。

    回傳
    ----
    agree_dir:        主流方向（+1/-1；無共識回 0）
    n_agree:          主流方向的源數
    n_present:        有明確方向（±1）的源數
    agreement_ratio:  n_agree / n_present（n_present=0 → 0.0）
    is_convergent:    n_agree ≥ 2 且 agreement_ratio ≥ 0.6
    """
    sig = per_exchange_signals or {}
    n_bull = sum(1 for d in sig.values() if d == 1)
    n_bear = sum(1 for d in sig.values() if d == -1)
    n_present = n_bull + n_bear

    if n_bull > n_bear:
        agree_dir, n_agree = 1, n_bull
    elif n_bear > n_bull:
        agree_dir, n_agree = -1, n_bear
    else:
        # 平手（含全 0）：無主流方向
        agree_dir, n_agree = 0, max(n_bull, n_bear)

    ratio = (n_agree / n_present) if n_present > 0 else 0.0
    is_conv = (agree_dir != 0
               and n_agree >= _MIN_AGREE
               and ratio >= _MIN_RATIO)

    return {
        "metric": metric,
        "agree_dir": agree_dir,
        "n_agree": n_agree,
        "n_present": n_present,
        "agreement_ratio": round(ratio, 4),
        "is_convergent": is_conv,
    }


def aggregate_convergence(symbol: str,
                          metric_results: dict[str, dict],
                          presence: dict) -> dict:
    """把多個指標的 metric_convergence 結果彙整成單幣跨源共振摘要。**不改輸入**。

    參數
    ----
    symbol: canonical 幣名（標註用）。
    metric_results: {metric_name: metric_convergence(...) 之回傳 dict}。
    presence: compute_presence(...) 之回傳 dict（取 presence_score / triple_present）。

    回傳
    ----
    convergent_metrics:  is_convergent=True 的指標名排序列表
    n_convergent:        上者長度
    dominant_direction:  共振指標的多數方向（+1/-1；平手或無 → 0）
    convergence_score:   ∈ [0,1]，= 共振指標占比 × 平均同號比例（缺料/零指標 → 0）
    strength_multiplier: shadow 專用乘數 ∈ [0.8,1.2]（永不乘進 strength_score）
    triple_present:      直接透傳 presence['triple_present']
    """
    mr = metric_results or {}
    pres = presence or {}

    convergent = sorted(m for m, r in mr.items()
                        if isinstance(r, dict) and r.get("is_convergent"))
    n_total = sum(1 for r in mr.values() if isinstance(r, dict))
    n_conv = len(convergent)

    # 主流方向：共振指標的方向多數決
    dir_bull = sum(1 for m in convergent if mr[m].get("agree_dir") == 1)
    dir_bear = sum(1 for m in convergent if mr[m].get("agree_dir") == -1)
    if dir_bull > dir_bear:
        dominant = 1
    elif dir_bear > dir_bull:
        dominant = -1
    else:
        dominant = 0

    # convergence_score：共振覆蓋率 × 共振指標的平均同號強度
    if n_total > 0 and n_conv > 0:
        coverage = n_conv / n_total
        avg_ratio = sum(mr[m].get("agreement_ratio", 0.0)
                        for m in convergent) / n_conv
        conv_score = max(0.0, min(1.0, coverage * avg_ratio))
    else:
        conv_score = 0.0

    pres_score = pres.get("presence_score", 0.0)
    if not isinstance(pres_score, (int, float)):
        pres_score = 0.0

    return {
        "symbol": symbol,
        "convergent_metrics": convergent,
        "n_convergent": n_conv,
        "dominant_direction": dominant,
        "convergence_score": round(conv_score, 4),
        "strength_multiplier": strength_multiplier(conv_score, pres_score),
        "triple_present": bool(pres.get("triple_present", False)),
    }


def strength_multiplier(convergence_score: float,
                        presence_score: float) -> float:
    """SHADOW 專用乘數 ∈ [0.8, 1.2]。**永不乘進 strength_score、永不回寫 fire/snapshot。**

    設計：以 1.0 為中性錨點，共振愈強+存在度愈高 → 略上調（封頂 1.2）；
    共振愈弱 → 略下調（封底 0.8）。presence 當「可信度權重」：存在度低時即使
    convergence 高，也只給較小的偏移（冷門幣的共振不該被過度信任）。

    錨點校準：convergence_score=0.5 → ≈1.0（中性，不偏不倚）。
        offset = (conv - 0.5) × 0.4 × presence ；再夾 [0.8,1.2]。
        conv=0.5 → offset=0 → 1.0（與 presence 無關）。
        conv=1.0, presence=1.0 → 1.0 + 0.5×0.4 = 1.2（封頂）。
        conv=0.0, presence=1.0 → 1.0 - 0.5×0.4 = 0.8（封底）。

    >>> strength_multiplier(0.5, 1.0)
    1.0
    >>> strength_multiplier(1.0, 1.0)
    1.2
    >>> strength_multiplier(0.0, 1.0)
    0.8
    >>> 0.8 <= strength_multiplier(0.9, 0.2) <= 1.2
    True
    """
    conv = convergence_score if isinstance(convergence_score, (int, float)) else 0.0
    pres = presence_score if isinstance(presence_score, (int, float)) else 0.0
    conv = max(0.0, min(1.0, conv))
    pres = max(0.0, min(1.0, pres))
    offset = (conv - 0.5) * 0.4 * pres
    mult = 1.0 + offset
    return round(max(0.8, min(1.2, mult)), 4)
