# -*- coding: utf-8 -*-
"""neff_cluster_correction.py — 大盤方向濾網 gap 的叢聚穩健推論（v116，離線唯讀 CLI）。

背景（Fable5 稽核 do_now#2，對抗審查裁定「決定一切下游」）：
    順勢 vs 逆勢的 gap（旗艦讀數 +0.88R、名目 t≈2.18）假設樣本 i.i.d.——但加密單同日
    同向叢集嚴重（同一 BTC beta、風控日開倉上限），名目 t 是樂觀上界（task#27 已踩過坑）。
    本工具用兩法校正：①day-cluster bootstrap（按 UTC 日整叢重抽，10k 次）算 gap 的
    穩健 CI/p；②設計效應 n_eff = n / (1 + (m̄−1)ρ)（ρ=組內相關, m̄=平均叢大小）。

口徑鎖定（預註冊，紅線③）：
    主口徑＝與每日 digest 的 trend_alignment_impact 完全同規則：
        aligned = (bull ∧ btc_above_200ma_4h) ∨ (bear ∧ ¬btc_above_200ma_4h)
        樣本＝已平倉、排除 entry_expired、snapshot 有 btc_above_200ma_4h 者。
    敏感度口徑（僅報告不裁決）＝btc_regime 代理版（稽核發現只剩 +0.26R 的那版）。

判準：cluster-robust 後 gap 的 95% CI 不含 0 且等效 t≥2 → 才准開大盤濾網 challenger。
純唯讀；不寫任何 DB/config；結論不宣稱「已證實 edge」，只裁決「可否開 challenger」。
"""
from __future__ import annotations

import math
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.review_attribution import load_closed, _find_in_snap  # noqa: E402


def _aligned_groups(rows: list[dict]) -> tuple[list[tuple], list[tuple]]:
    """回 (aligned, counter)，元素=(realized_r, utc_day)。主口徑同 trend_alignment_impact。"""
    aligned, counter = [], []
    for r in rows:
        er = (r.get("exit_reason") or "").lower()
        rr = r.get("realized_r")
        snap = r.get("_snap")
        if rr is None or not snap or er == "entry_expired":
            continue
        btc = _find_in_snap(snap, "btc_above_200ma_4h")
        if btc is None:
            continue
        day = time.strftime("%Y-%m-%d", time.gmtime((r.get("entry_at") or 0) / 1000))
        is_aligned = ((r.get("direction") == "bull" and btc)
                      or (r.get("direction") == "bear" and not btc))
        (aligned if is_aligned else counter).append((float(rr), day))
    return aligned, counter


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _nominal_t(xs: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    m = _mean(xs)
    sd = (sum((x - m) ** 2 for x in xs) / (n - 1)) ** 0.5
    return m / (sd / math.sqrt(n)) if sd > 0 else None


def _icc_neff(vals_days: list[tuple]) -> tuple[float, float]:
    """組內相關 ρ（單因子 ANOVA 估計）與設計效應 n_eff。叢=UTC 日。"""
    by_day: dict[str, list[float]] = {}
    for v, d in vals_days:
        by_day.setdefault(d, []).append(v)
    k = len(by_day)
    n = len(vals_days)
    if k < 2 or n <= k:
        return 0.0, float(n)
    grand = _mean([v for v, _ in vals_days])
    ssb = sum(len(g) * (_mean(g) - grand) ** 2 for g in by_day.values())
    ssw = sum(sum((x - _mean(g)) ** 2 for x in g) for g in by_day.values())
    msb = ssb / (k - 1)
    msw = ssw / (n - k) if n > k else 0.0
    m_bar = n / k
    if msw <= 0:
        rho = 1.0
    else:
        rho = max(0.0, (msb - msw) / (msb + (m_bar - 1) * msw))
    neff = n / (1 + (m_bar - 1) * rho)
    return round(rho, 3), round(neff, 1)


def _cluster_bootstrap_gap(aligned, counter, n_boot: int = 10_000, seed: int = 42):
    """按 UTC 日整叢重抽（兩組聯合以日為單位），回 (gap觀測, CI95, p雙尾, 等效z)。"""
    rng = random.Random(seed)
    days = sorted({d for _, d in aligned} | {d for _, d in counter})
    a_by, c_by = {}, {}
    for v, d in aligned:
        a_by.setdefault(d, []).append(v)
    for v, d in counter:
        c_by.setdefault(d, []).append(v)
    obs_gap = _mean([v for v, _ in aligned]) - _mean([v for v, _ in counter])
    gaps = []
    for _ in range(n_boot):
        sel = [rng.choice(days) for _ in days]        # 整日重抽
        av = [v for d in sel for v in a_by.get(d, [])]
        cv = [v for d in sel for v in c_by.get(d, [])]
        if not av or not cv:
            continue
        gaps.append(_mean(av) - _mean(cv))
    gaps.sort()
    if len(gaps) < 100:
        return obs_gap, (None, None), None, None
    lo = gaps[int(0.025 * len(gaps))]
    hi = gaps[int(0.975 * len(gaps))]
    # 雙尾 p：bootstrap 分佈中「號誌翻轉」比例的兩倍（centered percentile 法）
    p_neg = sum(1 for g in gaps if g <= 0) / len(gaps)
    p = 2 * min(p_neg, 1 - p_neg)
    mg = _mean(gaps)
    se = (sum((g - mg) ** 2 for g in gaps) / (len(gaps) - 1)) ** 0.5
    z = (obs_gap / se) if se > 1e-12 else None
    return obs_gap, (lo, hi), p, z


def main() -> None:
    rows = load_closed()
    aligned, counter = _aligned_groups(rows)
    na, nc = len(aligned), len(counter)
    ev_a, ev_c = _mean([v for v, _ in aligned]), _mean([v for v, _ in counter])
    print("═" * 62)
    print("大盤方向濾網 gap — 叢聚穩健推論（主口徑=digest trend_alignment）")
    print("═" * 62)
    print(f"順勢: n={na}  EV={ev_a:+.3f}R   逆勢: n={nc}  EV={ev_c:+.3f}R")
    print(f"名目 gap = {ev_a - ev_c:+.3f}R")
    ta = _nominal_t([v for v, _ in aligned])
    print(f"名目 t(順勢單獨) = {ta:.2f}" if ta else "名目 t 無法計算")

    rho_a, neff_a = _icc_neff(aligned)
    rho_c, neff_c = _icc_neff(counter)
    print(f"\n組內相關/設計效應:  順勢 ρ={rho_a} n_eff={neff_a}/{na}"
          f"   逆勢 ρ={rho_c} n_eff={neff_c}/{nc}")

    gap, (lo, hi), p, z = _cluster_bootstrap_gap(aligned, counter)
    print(f"\nday-cluster bootstrap (10k):  gap={gap:+.3f}R")
    if lo is not None:
        z_s = f"{z:.2f}" if z is not None else "n/a"
        print(f"  95% CI = [{lo:+.3f}, {hi:+.3f}]   雙尾 p={p:.4f}   等效 z={z_s}")
        passed = (lo > 0) and (z is not None and z >= 2.0)
        print("\n裁決：" + ("✅ 過閘——可開大盤濾網 forward challenger（仍須 L2 前向樣本才晉升）"
                          if passed else
                          "❌ 未過閘——叢聚校正後不足 t≥2，繼續累積樣本，不開 challenger"))
        print("（此裁決只管『可否開試驗』；『已證實 edge』永遠要 L2 前向樣本，紅線③）")
    else:
        print("  bootstrap 樣本不足，無法裁決")


if __name__ == "__main__":
    main()
