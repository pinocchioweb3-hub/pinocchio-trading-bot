"""Mock 歷史時序生成器。

設計：
- 基礎走 Geometric Brownian Motion (隨機漂移)
- 在固定間隔注入「事件」：
    squeeze:      4-8h CVD bull div + OI 飆 + funding 微負 + 大戶轉多
                  → 事件結束後 12-24h 拉漲 5-12%
    accumulation: 48-72h ATR 收斂 + 量枯竭 + CVD 緩升 + 大戶緩進
                  → 事件結束後 48h 突破 10-25%
- 其餘時間為「雜訊」：偶爾誤觸發 1-2 個假訊號

可重現：seed 控制。預設 30 天 1h 解析度 = 720 點。
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass


@dataclass
class HistoryPoint:
    """單一時間點完整快照（給 backtest 用）。"""
    ts: int
    symbol: str
    price: float

    oi: float
    oi_delta_pct: float
    funding: float
    funding_predicted: float
    cvd: float
    cvd_slope: float
    cvd_price_divergence: str
    top_trader_ratio: float
    ls_ratio: float
    liq_long: float
    liq_short: float

    btc_gate_open: bool
    btc_regime: str
    above_4h_200ma: bool
    is_hot: bool
    strength_score: float

    atr_pct_7d: float
    vol_24h_vs_30d: float
    cvd_slope_7d: float
    top_trader_slope_7d: float
    oi_delta_7d_pct: float
    higher_lows_7d: bool

    # 標記（給 backtest 報告用，不影響 L2 評估）
    event_tag: str = ""    # "squeeze" | "accumulation" | "noise" | ""


# 每幣的基準值（與 mock.py 一致）
_BASE = {
    "BTC":  {"price": 70_000.0, "oi": 80_000_000_000.0, "atr_pct_7d": 3.0},
    "ETH":  {"price":  3_800.0, "oi": 30_000_000_000.0, "atr_pct_7d": 4.0},
    "SOL":  {"price":    180.0, "oi":  5_000_000_000.0, "atr_pct_7d": 6.0},
    "SUI":  {"price":      3.4, "oi":    200_000_000.0, "atr_pct_7d": 7.0},
    "WLFI": {"price":     0.25, "oi":     30_000_000.0, "atr_pct_7d": 15.0},
    "ARB":  {"price":     0.81, "oi":     50_000_000.0, "atr_pct_7d": 3.5},
}


def _base(symbol: str) -> dict:
    return _BASE.get(symbol, {"price": 1.0, "oi": 10_000_000.0, "atr_pct_7d": 5.0})


def _plan_events(n_points: int, rng: random.Random) -> tuple[list, list, list]:
    """規劃 squeeze / accumulation / 假訊號 的時間索引。

    重要：每個事件帶 outcome_type 標記，決定事件後價格行為：
        squeeze:
            "real"    60% → 事件後拉漲 5-12%
            "fakeout" 25% → 事件後盤整，timeout 出場
            "trap"    15% → 事件後反向跌 4-7%（吃 stop）
        accumulation:
            "real"    50% → 突破上漲 10-25%
            "stall"   35% → 持續橫盤，TP1 觸及後 timeout
            "false"   15% → 假突破往下（吃 stop）
    """
    squeezes: list[tuple[int, int, float, str]] = []      # (start, end, magnitude, type)
    accumulations: list[tuple[int, int, float, str]] = []
    noise: list[int] = []

    def _pick(weights: dict[str, float]) -> str:
        r = rng.random()
        cum = 0.0
        for k, w in weights.items():
            cum += w
            if r <= cum:
                return k
        return list(weights.keys())[-1]

    SQ_W = {"real": 0.60, "fakeout": 0.25, "trap": 0.15}
    AC_W = {"real": 0.50, "stall": 0.35, "false": 0.15}

    # squeeze events
    i = rng.randint(48, 120)
    while i < n_points - 36:
        duration = rng.randint(4, 8)
        kind = _pick(SQ_W)
        if kind == "real":
            mag = rng.uniform(0.05, 0.12)
        elif kind == "trap":
            mag = -rng.uniform(0.04, 0.07)
        else:
            mag = 0.0
        squeezes.append((i, i + duration, mag, kind))
        i += rng.randint(120, 240)

    # accumulation events
    j = rng.randint(72, 168)
    while j < n_points - 72:
        duration = rng.randint(48, 72)
        kind = _pick(AC_W)
        if kind == "real":
            mag = rng.uniform(0.10, 0.25)
        elif kind == "false":
            mag = -rng.uniform(0.06, 0.10)
        else:
            mag = 0.0
        accumulations.append((j, j + duration, mag, kind))
        j += rng.randint(168, 240)

    # 雜訊誤觸發（少量）
    for _ in range(rng.randint(2, 5)):
        noise.append(rng.randint(24, n_points - 24))

    return squeezes, accumulations, noise


def generate(
    symbol: str = "SUI",
    days: int = 30,
    interval_hours: int = 1,
    seed: int = 42,
    end_ts: int | None = None,
) -> list[HistoryPoint]:
    """產生 mock 歷史時序。"""
    rng = random.Random(seed)
    base = _base(symbol)

    interval_ms = interval_hours * 3600_000
    n_points = days * 24 // interval_hours
    end_ts = end_ts if end_ts is not None else int(time.time() * 1000)
    start_ts = end_ts - n_points * interval_ms

    # === Step 1: 規劃事件位置 ===
    squeezes, accumulations, noise = _plan_events(n_points, rng)
    squeeze_idx = {i: kind for s_start, s_end, _, kind in squeezes
                   for i in range(s_start, s_end)}
    accum_idx = {i: kind for a_start, a_end, _, kind in accumulations
                 for i in range(a_start, a_end)}
    noise_idx = set(noise)

    # === Step 2: 走價格 GBM，事件後依 outcome_type 走不同方向 ===
    log_prices = [math.log(base["price"])]
    sigma_hourly = (base["atr_pct_7d"] / 100) / math.sqrt(24 * 7)
    for i in range(1, n_points):
        drift = 0.0
        for s_start, s_end, mag, kind in squeezes:
            rally_window = 24
            offset = i - s_end
            if 0 <= offset < rally_window:
                if kind == "real":
                    drift += mag / rally_window * (1 + (rally_window - offset) / rally_window)
                elif kind == "trap":
                    # 反向（mag 已是負值）
                    drift += mag / rally_window * 1.5
                # fakeout 不加 drift，自然 timeout
        for a_start, a_end, mag, kind in accumulations:
            breakout_window = 48
            offset = i - a_end
            if 0 <= offset < breakout_window:
                if kind == "real":
                    drift += mag / breakout_window * (offset / breakout_window) * 2
                elif kind == "false":
                    drift += mag / breakout_window * 1.5
                # stall 不加 drift
        log_prices.append(log_prices[-1] + drift + rng.normalvariate(0, sigma_hourly))
    prices = [math.exp(lp) for lp in log_prices]

    # === Step 3: 組裝每點完整快照 ===
    cvd_cum = 0.0
    history: list[HistoryPoint] = []

    for idx in range(n_points):
        ts = start_ts + idx * interval_ms
        price = prices[idx]
        sq_kind = squeeze_idx.get(idx)
        ac_kind = accum_idx.get(idx)
        in_squeeze = sq_kind is not None
        in_accum = ac_kind is not None
        in_noise = idx in noise_idx
        if in_squeeze:
            tag = f"squeeze_{sq_kind}"
        elif in_accum:
            tag = f"accumulation_{ac_kind}"
        elif in_noise:
            tag = "noise"
        else:
            tag = ""

        # 預設「雜訊」狀態
        oi = base["oi"] * (1 + rng.uniform(-0.05, 0.05))
        oi_delta_pct = rng.uniform(-2.5, 2.5)
        funding = rng.uniform(-0.00005, 0.00010)
        funding_pred = funding + rng.uniform(-0.00002, 0.00002)
        cvd_slope = rng.uniform(-0.05, 0.05)
        cvd_div = "none"
        top_trader = rng.uniform(0.92, 1.08)
        ls_ratio = rng.uniform(0.92, 1.08)
        atr_pct_7d = base["atr_pct_7d"] + rng.uniform(-1.0, 1.0)
        vol_24h_vs_30d = 1.0 + rng.uniform(-0.3, 0.3)
        cvd_slope_7d = rng.uniform(-0.03, 0.03)
        top_trader_slope_7d = rng.uniform(-0.005, 0.005)
        oi_delta_7d_pct = rng.uniform(-3.0, 3.0)
        higher_lows_7d = False

        # squeeze overlay
        if in_squeeze:
            cvd_div = "bull"
            cvd_slope = 0.20 + rng.uniform(-0.03, 0.03)
            funding = -0.0001 + rng.uniform(-0.00002, 0.00002)
            top_trader = 1.18 + rng.uniform(-0.03, 0.03)
            ls_ratio = 0.87 + rng.uniform(-0.02, 0.02)
            oi_delta_pct = 6.0 + rng.uniform(-1, 1)

        # accumulation overlay
        if in_accum:
            atr_pct_7d = 2.5 + rng.uniform(-0.3, 0.3)
            vol_24h_vs_30d = 0.62 + rng.uniform(-0.05, 0.05)
            cvd_slope_7d = 0.10 + rng.uniform(-0.02, 0.02)
            top_trader_slope_7d = 0.010 + rng.uniform(-0.002, 0.002)
            oi_delta_7d_pct = 3.0 + rng.uniform(-1, 1)
            higher_lows_7d = True

        # noise overlay：故意製造 1-2 個方向訊號但其他不齊（測投票政策）
        if in_noise and not (in_squeeze or in_accum):
            cvd_div = "bull"
            cvd_slope = 0.15

        cvd_cum += cvd_slope * 3600 + rng.uniform(-30_000, 30_000)

        # BTC 閘隨機關 ~5% 時間
        gate_open = rng.random() > 0.05
        regime = "trend_up" if gate_open else "trend_down"

        history.append(HistoryPoint(
            ts=ts, symbol=symbol, price=price,
            oi=oi, oi_delta_pct=oi_delta_pct,
            funding=funding, funding_predicted=funding_pred,
            cvd=cvd_cum, cvd_slope=cvd_slope, cvd_price_divergence=cvd_div,
            top_trader_ratio=top_trader, ls_ratio=ls_ratio,
            liq_long=base["oi"] * 0.00001, liq_short=base["oi"] * 0.00002,
            btc_gate_open=gate_open, btc_regime=regime,
            above_4h_200ma=gate_open,
            is_hot=True,
            strength_score=72.0 + rng.uniform(-5, 5),
            atr_pct_7d=atr_pct_7d, vol_24h_vs_30d=vol_24h_vs_30d,
            cvd_slope_7d=cvd_slope_7d, top_trader_slope_7d=top_trader_slope_7d,
            oi_delta_7d_pct=oi_delta_7d_pct, higher_lows_7d=higher_lows_7d,
            event_tag=tag,
        ))

    return history
