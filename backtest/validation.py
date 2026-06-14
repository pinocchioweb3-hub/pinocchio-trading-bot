"""回測統計顯著性檢定（v33，路線 D #6）。

防「漂亮夏普是運氣/過擬合」自欺。公式為 Bailey & López de Prado 的閉式解
（PSR / Deflated Sharpe / minTRL），純 Python 自實作（無 mlfinlab 等外部相依，
該庫已停更轉閉源）。對照真理：mlfinlab / esvhd-pypbo 文件。

用在「逐筆 R 報酬序列」上：
    sharpe()        每筆 Sharpe = mean(R)/std(R)（未年化）
    psr()           機率：真實 Sharpe > 基準(預設0) 的信心；>0.95 才算顯著有效
    min_trl()       還需多少筆才能在指定信心下證明 SR>基準
    deflated_sharpe 用 n_trials 修「試了 N 組參數挑最佳」的多重檢定偏差
"""
from __future__ import annotations

from math import sqrt, e, log
from statistics import mean, pstdev, NormalDist

_N = NormalDist()


def _moments(xs: list[float]):
    n = len(xs)
    m = mean(xs)
    sd = pstdev(xs)
    if sd == 0 or n < 2:
        return m, sd, 0.0, 3.0
    skew = sum(((x - m) / sd) ** 3 for x in xs) / n
    kurt = sum(((x - m) / sd) ** 4 for x in xs) / n   # 非超額(常態=3)
    return m, sd, skew, kurt


def sharpe(returns: list[float]) -> float:
    """每筆(未年化) Sharpe = mean/std。"""
    if len(returns) < 2:
        return 0.0
    sd = pstdev(returns)
    return (mean(returns) / sd) if sd > 0 else 0.0


def psr(returns: list[float], sr_benchmark: float = 0.0) -> float:
    """Probabilistic Sharpe Ratio：P(真實 SR > sr_benchmark)。回 0..1。"""
    n = len(returns)
    if n < 3:
        return 0.0
    _, sd, skew, kurt = _moments(returns)
    if sd == 0:
        return 0.0
    sr = mean(returns) / sd
    denom = sqrt(max(1e-12, 1 - skew * sr + ((kurt - 1) / 4) * sr * sr))
    z = (sr - sr_benchmark) * sqrt(n - 1) / denom
    return _N.cdf(z)


def min_trl(returns: list[float], sr_benchmark: float = 0.0,
            confidence: float = 0.95) -> float:
    """minimum Track Record Length：要證明 SR>基準(在 confidence 信心)所需最少筆數。"""
    n = len(returns)
    if n < 3:
        return float("inf")
    _, sd, skew, kurt = _moments(returns)
    if sd == 0:
        return float("inf")
    sr = mean(returns) / sd
    if sr <= sr_benchmark:
        return float("inf")
    za = _N.inv_cdf(confidence)
    return 1 + (1 - skew * sr + ((kurt - 1) / 4) * sr * sr) * (za / (sr - sr_benchmark)) ** 2


def deflated_sharpe(returns: list[float], n_trials: int = 1,
                    sr_variance: float | None = None) -> float:
    """Deflated Sharpe Ratio：以「N 組試驗的期望最大 SR」當基準的 PSR。
    n_trials<=1 時無多重檢定偏差 → 退化為 psr(0)。
    sr_variance=各試驗 SR 的變異數（未提供時用 1/(n-1) 近似單一序列 SR 抽樣變異）。"""
    n = len(returns)
    if n < 3:
        return 0.0
    if n_trials <= 1:
        return psr(returns, 0.0)
    var_sr = sr_variance if sr_variance is not None else 1.0 / (n - 1)
    gamma = 0.5772156649  # Euler–Mascheroni
    # 期望最大 SR（across N trials）
    sr_star = sqrt(max(0.0, var_sr)) * (
        (1 - gamma) * _N.inv_cdf(1 - 1.0 / n_trials)
        + gamma * _N.inv_cdf(1 - 1.0 / (n_trials * e)))
    return psr(returns, sr_star)


def assess(returns: list[float], n_trials: int = 1) -> dict:
    """一站式：回 sharpe/psr/dsr/min_trl + 判讀。"""
    if len(returns) < 3:
        return {"n": len(returns), "verdict": "樣本不足"}
    sr = sharpe(returns)
    p = psr(returns, 0.0)
    d = deflated_sharpe(returns, n_trials)
    mtrl = min_trl(returns, 0.0, 0.95)
    if p >= 0.95 and d >= 0.95:
        verdict = "顯著有效（PSR/DSR≥95%）"
    elif p >= 0.95:
        verdict = "PSR 顯著，但多重檢定後 DSR 不足（小心過擬合）" if n_trials > 1 else "PSR 顯著"
    else:
        verdict = "未達顯著（可能是運氣，需更多樣本）"
    return {"n": len(returns), "sharpe_per_trade": round(sr, 4),
            "psr": round(p, 4), "dsr": round(d, 4),
            "min_trl": (round(mtrl, 0) if mtrl != float("inf") else None),
            "n_trials": n_trials, "verdict": verdict}


if __name__ == "__main__":
    import random
    random.seed(1)
    # 自測：正期望序列應 PSR 高
    pos = [random.gauss(0.07, 0.9) for _ in range(1000)]
    print("positive edge:", assess(pos))
    flat = [random.gauss(0.0, 1.0) for _ in range(1000)]
    print("no edge:", assess(flat))
