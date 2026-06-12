"""MockSource：deterministic 假資料後端，v0 預設。

設計：
- 用 (symbol, hour) 種子隨機，同 hour 內結果穩定，跨 hour 自然變動
- 每幣有合理價/OI/funding 區間（依 2026 大略真實值）
- 預設情境讓 SUI 觸發 Setup A FIRE BULL、ARB 觸發 Setup B FIRE BULL，
  其他幣保持中性，方便 demo / 開發
"""
from __future__ import annotations

import random
import time
from typing import Literal

from ..symbol_mapping import normalize
from .base import RatioType


# --- 每幣的真實感「典型值」（2026 大略）------------------------------------
_BASE_VALUES = {
    "BTC":  {"price": 70_000.0, "oi": 80_000_000_000.0, "atr_pct_7d": 3.0},
    "ETH":  {"price":  3_800.0, "oi": 30_000_000_000.0, "atr_pct_7d": 4.0},
    "SOL":  {"price":    180.0, "oi":  5_000_000_000.0, "atr_pct_7d": 6.0},
    "SUI":  {"price":      3.4, "oi":    200_000_000.0, "atr_pct_7d": 7.0},
    "WLFI": {"price":      0.25, "oi":    30_000_000.0, "atr_pct_7d": 15.0},
    "ARB":  {"price":      0.81, "oi":    50_000_000.0, "atr_pct_7d": 3.5},
    # default for unknown symbols
    "_DEFAULT": {"price": 1.0, "oi": 10_000_000.0, "atr_pct_7d": 5.0},
}


def _base(symbol: str) -> dict:
    return _BASE_VALUES.get(symbol, _BASE_VALUES["_DEFAULT"])


def _rng(symbol: str, ts_ms: int) -> random.Random:
    hour = ts_ms // (3600 * 1000)
    return random.Random(hash((symbol, hour)) & 0xFFFFFFFF)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _window_to_ms(window: str) -> int:
    """1h/4h/1d/7d 轉成 ms。"""
    unit = window[-1]
    n = int(window[:-1])
    return {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}[unit] * n


# ===========================================================================
class MockSource:
    name = "mock"

    # ------------------------------------------------------------------
    # 多空比 / 大戶多空比
    # ------------------------------------------------------------------
    async def get_positioning(
        self, symbol: str, ratio_type: RatioType, window: str, limit: int,
    ) -> dict:
        sym = normalize(symbol)
        now = _now_ms()
        step = _window_to_ms(window)
        rng = _rng(sym + ratio_type, now)

        # 預設情境：SUI 大戶轉多 1.18、散戶偏空 0.88（Setup A 條件）
        # 其他幣中性 0.95-1.05
        is_top = ratio_type.startswith("top_trader")
        if sym == "SUI":
            latest = 1.18 if is_top else 0.88
        elif sym == "ARB":  # Setup B 緩升中
            latest = 1.07 if is_top else 1.02
        else:
            latest = rng.uniform(0.92, 1.08)

        # 產生 limit 個歷史點，圍繞 latest 走來走去
        series = []
        v = latest * 0.95
        for i in range(limit):
            ts = now - (limit - 1 - i) * step
            v = v * 0.98 + latest * 0.02 + rng.uniform(-0.02, 0.02)
            series.append({"ts": ts, "value": round(v, 4)})

        first = series[0]["value"]
        delta_pct = ((latest - first) / first) * 100 if first else 0.0
        return {
            "symbol": sym,
            "source": "mock",
            "ratio_type": ratio_type,
            "latest": round(latest, 4),
            "series": series,
            "delta_pct": round(delta_pct, 3),
        }

    # ------------------------------------------------------------------
    # OI
    # ------------------------------------------------------------------
    async def get_oi(self, symbol: str, window: str, limit: int) -> dict:
        sym = normalize(symbol)
        now = _now_ms()
        step = _window_to_ms(window)
        rng = _rng(sym + "oi", now)
        base = _base(sym)["oi"]

        # SUI 預設 OI 上升 +6%、ARB 平穩 +3%、其他隨機
        if sym == "SUI":
            delta_pct_24h = 6.0
        elif sym == "ARB":
            delta_pct_24h = 3.1
        else:
            delta_pct_24h = rng.uniform(-2.0, 2.0)

        latest = base * (1 + delta_pct_24h / 100)
        # 回看 limit 根
        series = []
        v = base
        for i in range(limit):
            ts = now - (limit - 1 - i) * step
            v = v + (latest - v) * 0.05 + rng.uniform(-base * 0.005, base * 0.005)
            series.append({"ts": ts, "value": round(v, 2)})

        return {
            "symbol": sym,
            "source": "mock",
            "latest": round(latest, 2),
            "delta_pct_24h": round(delta_pct_24h, 3),
            "series": series,
        }

    # ------------------------------------------------------------------
    # Funding
    # ------------------------------------------------------------------
    async def get_funding(self, symbol: str) -> dict:
        sym = normalize(symbol)
        # SUI 預設微負 -0.0001（空方付錢），其他中性
        rng = _rng(sym + "funding", _now_ms())
        if sym == "SUI":
            current = -0.0001
            predicted = -0.00012
        elif sym == "ARB":
            current = 0.00002
            predicted = 0.00005
        else:
            current = rng.uniform(-0.00005, 0.00015)
            predicted = current + rng.uniform(-0.00002, 0.00002)

        return {
            "symbol": sym,
            "source": "mock",
            "funding": round(current, 6),
            "funding_predicted": round(predicted, 6),
            "ts": _now_ms(),
        }

    # ------------------------------------------------------------------
    # Liquidations
    # ------------------------------------------------------------------
    async def get_liquidations(self, symbol: str, window: str) -> dict:
        sym = normalize(symbol)
        rng = _rng(sym + "liq", _now_ms())
        base = _base(sym)
        # SUI 軋空情境：空單清算 > 多單清算（多殺空翻倍）
        if sym == "SUI":
            liq_long = base["oi"] * 0.000008
            liq_short = base["oi"] * 0.000020
        else:
            scale = rng.uniform(0.00001, 0.00005)
            liq_long = base["oi"] * scale
            liq_short = base["oi"] * scale * rng.uniform(0.8, 1.2)

        return {
            "symbol": sym,
            "source": "mock",
            "window": window,
            "liq_long": round(liq_long, 2),
            "liq_short": round(liq_short, 2),
            "ts": _now_ms(),
        }

    # ------------------------------------------------------------------
    # CVD 時序
    # ------------------------------------------------------------------
    async def get_cvd_series(self, symbol: str, window: str, limit: int) -> dict:
        sym = normalize(symbol)
        now = _now_ms()
        step = _window_to_ms(window)
        rng = _rng(sym + "cvd", now)

        # SUI 預設背離（短期斜率正、價格走平 → bull divergence）
        # ARB 預設 7d 緩升累積
        if sym == "SUI":
            slope = 0.22
            slope_7d = 0.10
            divergence = "bull"
        elif sym == "ARB":
            slope = 0.03
            slope_7d = 0.18
            divergence = "none"
        else:
            slope = rng.uniform(-0.05, 0.05)
            slope_7d = rng.uniform(-0.03, 0.03)
            divergence = "none"

        v = 0.0
        series = []
        for i in range(limit):
            ts = now - (limit - 1 - i) * step
            v += slope * step / 3_600_000 + rng.uniform(-50_000, 50_000)
            series.append({"ts": ts, "value": round(v, 2)})

        return {
            "symbol": sym,
            "source": "mock",
            "cvd": series[-1]["value"],
            "cvd_slope": slope,
            "cvd_slope_7d": slope_7d,
            "cvd_price_divergence": divergence,
            "series": series,
        }

    # ------------------------------------------------------------------
    # 價格時序
    # ------------------------------------------------------------------
    async def get_price_series(self, symbol: str, tf: str, limit: int) -> dict:
        sym = normalize(symbol)
        now = _now_ms()
        step = _window_to_ms(tf)
        rng = _rng(sym + "px" + tf, now)
        base = _base(sym)["price"]
        atr = _base(sym)["atr_pct_7d"] / 100

        series = []
        v = base * 0.95
        for i in range(limit):
            ts = now - (limit - 1 - i) * step
            v = v * 0.99 + base * 0.01 + rng.uniform(-base * atr * 0.3, base * atr * 0.3)
            series.append({"ts": ts, "value": round(v, 6)})

        return {
            "symbol": sym,
            "source": "mock",
            "tf": tf,
            "price": series[-1]["value"],
            "series": series,
        }

    # ------------------------------------------------------------------
    # BTC 閘
    # ------------------------------------------------------------------
    async def get_btc_gate(self) -> dict:
        # 預設開（讓 demo 能 FIRE）
        return {
            "source": "mock",
            "btc_gate_open": True,
            "btc_regime": "trend_up",
            "rule": "btc_close_4h > btc_4h_200ma AND regime != trend_down",
            "evidence": {"btc_close_4h": 71_200.0, "btc_4h_200ma": 65_300.0},
            "ts": _now_ms(),
        }

    # ------------------------------------------------------------------
    # 強勢候選池
    # ------------------------------------------------------------------
    async def get_strength_universe(self, limit: int,
                                    candidate_symbols: list[str] | None = None) -> dict:
        """回傳一份預設候選池的原始強度指標。
        實作參考 spec Part D：7d 報酬 / 24h 量 / OI 7d / CVD 7d / 大戶偏離 / BTC 相關性。
        """
        if candidate_symbols is not None:
            candidates = list(candidate_symbols)
        else:
            candidates = list(_BASE_VALUES.keys())
        candidates = [c for c in candidates if c != "_DEFAULT"]

        items = []
        for sym in candidates:
            rng = _rng(sym + "rank", _now_ms())
            # 預設情境：SUI 強、ARB 中、BTC 穩、WLFI 弱
            if sym == "SUI":
                items.append({"symbol": sym, "return_7d_pct": 18.5, "vol_24h_usd": 380_000_000,
                              "vol_24h_vs_30d": 1.4, "oi_delta_7d_pct": 22.0,
                              "cvd_slope_7d": 0.10, "top_trader_dev": 0.18,
                              "btc_corr_30d": 0.72})
            elif sym == "BTC":
                items.append({"symbol": sym, "return_7d_pct": 5.2, "vol_24h_usd": 35_000_000_000,
                              "vol_24h_vs_30d": 0.95, "oi_delta_7d_pct": 3.1,
                              "cvd_slope_7d": 0.04, "top_trader_dev": 0.05,
                              "btc_corr_30d": 1.0})
            elif sym == "ARB":
                items.append({"symbol": sym, "return_7d_pct": 8.4, "vol_24h_usd": 120_000_000,
                              "vol_24h_vs_30d": 0.62, "oi_delta_7d_pct": 3.1,
                              "cvd_slope_7d": 0.18, "top_trader_dev": 0.07,
                              "btc_corr_30d": 0.65})
            else:
                items.append({"symbol": sym,
                              "return_7d_pct": rng.uniform(-5, 10),
                              "vol_24h_usd": rng.uniform(10_000_000, 1_000_000_000),
                              "vol_24h_vs_30d": rng.uniform(0.7, 1.3),
                              "oi_delta_7d_pct": rng.uniform(-3, 8),
                              "cvd_slope_7d": rng.uniform(-0.1, 0.1),
                              "top_trader_dev": rng.uniform(0.0, 0.1),
                              "btc_corr_30d": rng.uniform(0.4, 0.9)})

        return {"source": "mock", "ts": _now_ms(), "items": items[:limit]}

    # ------------------------------------------------------------------
    # 7d 結構（Setup B 用）
    # ------------------------------------------------------------------
    async def get_structure(self, symbol: str) -> dict:
        sym = normalize(symbol)
        base = _base(sym)
        # 預設情境：ARB 處於打底完成（適合 Setup B FIRE），其他不適合
        if sym == "ARB":
            return {
                "symbol": sym, "source": "mock", "ts": _now_ms(),
                "atr_pct_7d": 2.8,           # coiling
                "vol_24h_vs_30d": 0.62,      # drying
                "cvd_slope_7d": 0.18,
                "top_trader_slope_7d": 0.012,
                "oi_delta_7d_pct": 3.1,
                "higher_lows_7d": True,
            }
        if sym == "SUI":
            # SUI 強勢中：ATR 較大、量上升、結構非埋伏型
            return {
                "symbol": sym, "source": "mock", "ts": _now_ms(),
                "atr_pct_7d": base["atr_pct_7d"],
                "vol_24h_vs_30d": 1.4,
                "cvd_slope_7d": 0.10,
                "top_trader_slope_7d": 0.020,
                "oi_delta_7d_pct": 22.0,
                "higher_lows_7d": True,
            }
        # 其他幣中性
        return {
            "symbol": sym, "source": "mock", "ts": _now_ms(),
            "atr_pct_7d": base["atr_pct_7d"],
            "vol_24h_vs_30d": 1.0,
            "cvd_slope_7d": 0.0,
            "top_trader_slope_7d": 0.0,
            "oi_delta_7d_pct": 0.0,
            "higher_lows_7d": False,
        }

    # ------------------------------------------------------------------
    async def health(self) -> dict:
        return {"ok": True, "source": "mock", "details": "deterministic fixture"}
