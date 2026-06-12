"""強勢分數計算（6 因子 z-score 加權，套 sigmoid 到 0–100）。

權重（依使用者偏好：「趨勢先行於熱點」→ OI 拉到 25%）：
    7d 報酬           25%
    24h 量 / 30d 均量  15%
    OI 7d 變化%       25%
    CVD 7d 斜率        20%
    大戶比偏離 1       10%
    BTC 相關性         5%  (sweet spot 0.5–0.85；過低/過高扣分)
"""
from __future__ import annotations

import math
from statistics import mean, pstdev


WEIGHTS = {
    "return_7d_pct":     0.25,
    "vol_24h_vs_30d":    0.15,
    "oi_delta_7d_pct":   0.25,
    "cvd_slope_7d":      0.20,
    "top_trader_dev":    0.10,
    "btc_corr_30d":      0.05,
}


def _zscore(values: list[float], v: float) -> float:
    if len(values) < 2:
        return 0.0
    m = mean(values)
    sd = pstdev(values)
    if sd == 0:
        return 0.0
    return (v - m) / sd


def _btc_corr_score(corr: float) -> float:
    """BTC 相關性在 [0.5, 0.85] 給 +1；過低/過高扣分（避免脫節或純跟風）"""
    if 0.5 <= corr <= 0.85:
        return 1.0
    if corr < 0.3 or corr > 0.95:
        return -1.0
    return -0.3


def _sigmoid_100(x: float) -> float:
    """壓到 0-100"""
    return 100.0 / (1.0 + math.exp(-x))


def compute_strength_scores(universe: list[dict]) -> list[dict]:
    """每個 item 加上 strength_score 欄位；回傳按分數降序排序。"""
    if not universe:
        return []

    # 預備每個因子的 universe 值池（給 z-score 用）
    pools: dict[str, list[float]] = {
        f: [u.get(f, 0.0) for u in universe] for f in WEIGHTS
    }

    scored = []
    for item in universe:
        composite = 0.0
        contrib: dict[str, float] = {}
        for factor, w in WEIGHTS.items():
            v = item.get(factor, 0.0)
            if factor == "btc_corr_30d":
                z = _btc_corr_score(v)
            else:
                z = _zscore(pools[factor], v)
                z = max(-3.0, min(3.0, z))   # clip 避免單因子主導
            c = z * w
            contrib[factor] = round(c, 3)
            composite += c

        score = _sigmoid_100(composite * 2.0)
        scored.append({
            **item,
            "strength_score": round(score, 1),
            "strength_contrib": contrib,
        })

    return sorted(scored, key=lambda x: x["strength_score"], reverse=True)
