"""弱勢分數計算（3 因子 z-score 加權，套 sigmoid 到 0–100）— 做空候選排名。

【誠實聲明：刻意不做 6 因子對稱】
本模組只用「3 個真因子」，刻意不鏡像 strength.py 的 6 因子對稱結構，原因如下：
strength.py 的 6 因子裡有 3 個（cvd_slope_7d / top_trader_dev / btc_corr_30d）
在排名路徑上是「常數 stub 死值」——它們並非每幣即時計算，而是 coinglass.py:666-668
回填的固定佔位值。對「相對排名」而言，常數欄位的 universe 值池標準差為 0，
z-score 一律回 0.0（見 _zscore 的 sd==0 分支），對排序毫無貢獻，純屬裝飾。
因此弱勢評分只取「真正有跨幣變異、能驅動排名」的 3 個因子：
    return_7d_pct      45%  （跌越多 → 弱勢分越高，取負號）
    oi_delta_7d_pct    35%  （價跌且 OI 增 = 新空進場 → 加分，用象限邏輯）
    vol_24h_vs_30d     20%  （越放量 → 弱勢分越高，取負號）

設計守則：
    * 純函式，零 API、零 await、零跨模組 import（不 import strength.py）。
    * z-score clip ±3（避免單一離群幣主導排序）。
    * 因子值池標準差為 0 → z-score 回 0.0（與 strength.py 一致）。
    * weakness_contrib 只含上述 3 個 key，絕不出現三個 stub 欄位。
"""
from __future__ import annotations

import math
from statistics import mean, pstdev


# 弱勢權重：報酬為主（趨勢崩壞先行），OI 象限次之，量能佐證。
WEAKNESS_WEIGHTS = {
    "return_7d_pct":   0.45,
    "oi_delta_7d_pct": 0.35,
    "vol_24h_vs_30d":  0.20,
}


def _zscore(values: list[float], v: float) -> float:
    if len(values) < 2:
        return 0.0
    m = mean(values)
    sd = pstdev(values)
    if sd == 0:
        return 0.0
    return (v - m) / sd


def _sigmoid_100(x: float) -> float:
    """壓到 0-100"""
    return 100.0 / (1.0 + math.exp(-x))


def passes_short_liquidity(item: dict, min_vol_usd: float) -> bool:
    """做空流動性閘：24h 成交額（USD）須 >= 門檻，否則不可作為做空候選。

    純函式（無副作用）。低流動性幣做空成本高、易被軋、滑點大，故先過濾。
    讀 'vol_24h_usd'，缺值視為 0.0（保守 → 不通過）。
    """
    return float(item.get("vol_24h_usd", 0.0) or 0.0) >= float(min_vol_usd)


def compute_weakness_scores(universe: list[dict]) -> list[dict]:
    """每個 item 加上 weakness_score 欄位；回傳按分數降序排序（最弱在前）。

    弱勢方向邏輯：
        * return_7d_pct：取「負」z（跌越多 → 越弱 → 分越高）。
        * vol_24h_vs_30d：取「負」z（越放量 → 出貨/恐慌 → 越弱 → 分越高）。
        * oi_delta_7d_pct：象限邏輯 z(oi_delta) * sign(-return_7d_pct)
          —— 價跌（return<0 → sign(-return)=+1）且 OI 增（z>0）= 新空進場 → 加分；
             價漲時 OI 增反而扣分（多頭續命，不利做空）。
    """
    if not universe:
        return []

    # 預備每個因子的 universe 值池（給 z-score 用）
    pools: dict[str, list[float]] = {
        f: [u.get(f, 0.0) for u in universe] for f in WEAKNESS_WEIGHTS
    }

    scored = []
    for item in universe:
        composite = 0.0
        contrib: dict[str, float] = {}
        for factor, w in WEAKNESS_WEIGHTS.items():
            v = item.get(factor, 0.0)
            z = _zscore(pools[factor], v)
            z = max(-3.0, min(3.0, z))   # clip 避免單因子主導

            if factor == "return_7d_pct":
                z = -z                                  # 跌越多分越高
            elif factor == "vol_24h_vs_30d":
                z = -z                                  # 越放量分越高
            elif factor == "oi_delta_7d_pct":
                ret = item.get("return_7d_pct", 0.0)
                sign = 1.0 if ret < 0 else (-1.0 if ret > 0 else 0.0)
                z = z * sign                            # 價跌 OI 增才加分（新空）

            c = z * w
            contrib[factor] = round(c, 3)
            composite += c

        score = _sigmoid_100(composite * 2.0)
        scored.append({
            **item,
            "weakness_score": round(score, 1),
            "weakness_contrib": contrib,
        })

    return sorted(scored, key=lambda x: x["weakness_score"], reverse=True)
