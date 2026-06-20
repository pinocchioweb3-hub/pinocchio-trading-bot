"""獨立性校正原語（n_eff / 叢聚 ICC）— 共用葉模組（task#80）。

逐筆 R 報酬若按同一 UTC 日叢聚（day-shock 共振），就不是 i.i.d.；名目
n=len(returns) 會高估有效獨立樣本數，使 PSR/DSR 過度自信、minTRL 低估
→ 偏向「假晉升」。本模組把這套校正抽成單一真相源，給兩個消費者共用：

    backtest/crypto_ev_significance.py   離線唯讀顯著性工具（task#27）
    backtest/l2_stat_gates.py            LIVE 晉升閘（auto_tuner 過閘即寫活鍵）

提供：
    _utc_day_key       epoch 毫秒 → 'YYYY-MM-DD'（UTC）叢聚鍵
    _icc_oneway        單因子隨機效果 ICC(1)（量「同群相關」，非僅共用標籤）
    effective_n        Kish design effect → n_eff = n / deff（n_eff ≤ n 恆成立）
    psr_with_n         Bailey-LdP PSR 但以 n_eff 取代 len（sqrt(n_eff-1)）
    deflated_sharpe_n  Deflated Sharpe 的 n_eff 版（去膨脹基準 axis 不變）

設計守則：純離線、零網路、零 I/O；只相依 stdlib 與 backtest.validation._moments
（validation 為凍結正準葉，本模組 → validation，無循環）。

⚠️ ICC 量的是「同群值的相關」非僅共用日期標籤：同日但 i.i.d. 抽樣 → ICC≈0、
deff≈1、不罰；要有真實同日共振才 n_eff<n。fail-closed：無法可靠估計時
effective_n 回 None，呼叫端據此退回名目 n（只會更嚴或不變，絕不放寬）。
"""
from __future__ import annotations

import datetime as _dt
from math import e as _E, sqrt
from statistics import NormalDist, mean

from backtest.validation import _moments

_NORM = NormalDist()


def _utc_day_key(entry_ms: int | None) -> str | None:
    """epoch 毫秒 → 'YYYY-MM-DD'（UTC）；None/異常回 None。"""
    if entry_ms is None:
        return None
    try:
        d = _dt.datetime.fromtimestamp(entry_ms / 1000.0, tz=_dt.timezone.utc)
        return d.strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _icc_oneway(groups: list[list[float]]):
    """單因子隨機效果 ICC(1)。groups＝各群的 R 值清單。
    回 (icc∈[0,1], 平均群大小 m̄, 群數 k)。k<2 或退化 → icc=0。"""
    groups = [g for g in groups if g]
    k = len(groups)
    total = sum(len(g) for g in groups)
    if k < 2 or total <= k:
        return 0.0, (total / k if k else 0.0), k
    grand = sum(sum(g) for g in groups) / total
    means = [sum(g) / len(g) for g in groups]
    ssb = sum(len(g) * (means[i] - grand) ** 2 for i, g in enumerate(groups))
    ssw = sum(sum((x - means[i]) ** 2 for x in g) for i, g in enumerate(groups))
    msb = ssb / (k - 1)
    msw = ssw / (total - k)
    # n0＝不等群大小的有效平均（標準 one-way ANOVA 修正）
    n0 = (total - sum(len(g) ** 2 for g in groups) / total) / (k - 1)
    denom = msb + (n0 - 1) * msw
    if denom <= 0:
        return 0.0, total / k, k
    icc = (msb - msw) / denom
    return max(0.0, min(1.0, icc)), total / k, k


def effective_n(returns: list[float], day_keys: list[str | None]):
    """叢聚（按 UTC 日）校正後的有效獨立樣本數 n_eff。
    回 (n_eff, icc, design_effect, coverage)；無法可靠估計時 n_eff=None。"""
    n = len(returns)
    paired = [(r, d) for r, d in zip(returns, day_keys) if d is not None]
    coverage = (len(paired) / n) if n else 0.0
    if n < 3 or coverage < 0.5:
        return None, None, None, round(coverage, 3)
    by_day: dict[str, list[float]] = {}
    for r, d in paired:
        by_day.setdefault(d, []).append(r)
    icc, mbar, k = _icc_oneway(list(by_day.values()))
    if k < 2:
        return None, None, None, round(coverage, 3)
    deff = 1.0 + (mbar - 1.0) * icc            # Kish design effect
    deff = max(1.0, deff)
    n_eff = len(paired) / deff
    return round(n_eff, 1), round(icc, 3), round(deff, 2), round(coverage, 3)


def psr_with_n(returns: list[float], n_eff: float, sr_benchmark: float = 0.0):
    """Bailey-LdP PSR 但以 n_eff 取代 len（叢聚校正版）。閉式同 validation.psr，
    僅 sqrt(n-1)→sqrt(n_eff-1)。n_eff<2 回 None。"""
    if len(returns) < 3 or n_eff is None or n_eff < 2:
        return None
    _, sd, skew, kurt = _moments(returns)
    if sd == 0:
        return None
    sr = mean(returns) / sd
    denom = sqrt(max(1e-12, 1 - skew * sr + ((kurt - 1) / 4) * sr * sr))
    z = (sr - sr_benchmark) * sqrt(n_eff - 1) / denom
    return round(_NORM.cdf(z), 4)


def deflated_sharpe_n(returns: list[float], n_trials: int = 1,
                      sr_variance: float | None = None, *,
                      n_eff: float | None = None):
    """Deflated Sharpe 的 n_eff 版（叢聚校正）。沿用 validation.deflated_sharpe 的
    去膨脹基準 sr_star（「N 組試驗的期望最大 SR」，**axis 完全不變**），僅把最終
    PSR 評估改以 psr_with_n（sqrt(n_eff-1)）。n_eff None → 回 None（呼叫端退名目）。
    n_trials<=1 → 無多重檢定偏差，退化為 psr_with_n(returns, n_eff, 0.0)。

    ⚠️ sr_star 公式複刻 backtest.validation.deflated_sharpe（該檔為凍結正準）；若
    validation 的去膨脹公式變更，必須同步此處。
    """
    n = len(returns)
    if n < 3 or n_eff is None:
        return None
    if n_trials <= 1:
        return psr_with_n(returns, n_eff, 0.0)
    var_sr = sr_variance if sr_variance is not None else 1.0 / (n - 1)
    gamma = 0.5772156649  # Euler–Mascheroni
    sr_star = sqrt(max(0.0, var_sr)) * (
        (1 - gamma) * _NORM.inv_cdf(1 - 1.0 / n_trials)
        + gamma * _NORM.inv_cdf(1 - 1.0 / (n_trials * _E)))
    return psr_with_n(returns, n_eff, sr_star)
