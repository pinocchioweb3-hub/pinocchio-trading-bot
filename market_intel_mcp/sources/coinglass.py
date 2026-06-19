"""CoinGlass v4 真實 API client。

端點（v4 open-api）：
    /api/futures/top-long-short-position-ratio/history   大戶持倉比
    /api/futures/top-long-short-account-ratio/history    大戶帳戶比
    /api/futures/global-long-short-account-ratio/history 散戶帳戶比
    /api/futures/open-interest/aggregated-history        聚合 OI
    /api/futures/funding-rate/history                    funding (OHLC per interval)
    /api/futures/liquidation/aggregated-history          聚合清算
    /api/futures/price-ohlc-history                      價格 OHLC

Auth：header CG-API-KEY
Envelope：{code: "0", msg, data}
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx

from ..errors import make_error
from ..settings import SETTINGS
from ..symbol_mapping import to_coinglass
from .base import RatioType


# ===========================================================================
# Sliding-window rate limiter (per process)
# ===========================================================================
class _RateLimiter:
    def __init__(self, max_per_minute: int):
        self.max = max_per_minute
        self.timestamps: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.time()
            self.timestamps = [t for t in self.timestamps if now - t < 60]
            if len(self.timestamps) >= self.max:
                wait = 60 - (now - self.timestamps[0]) + 0.1
                await asyncio.sleep(max(0.0, wait))
                now = time.time()
                self.timestamps = [t for t in self.timestamps if now - t < 60]
            self.timestamps.append(now)


# ===========================================================================
# Short-TTL 成功快取（v34-2）：跨幣/跨 worker 去重 + 吸收瞬時節流
# ---------------------------------------------------------------------------
# 真因（實測 _snap_burst）：每幣快照 ~13 個 CoinGlass 呼叫，其中 btc_gate /
# strength_universe 為「全域」(每幣完全相同卻每幣重打)、structure 又重抓
# price/cvd/liq/funding（與頂層重複）；一輪掃描 ~150+ 呼叫撞 75/80-per-min
# 上限，且 _get 零重試零快取 → 瞬時 429/timeout 直接標 stale。
# 對策：同 (path,params) 在 TTL 內回上次「成功」值，自動吃掉全域重複與快照內
# 重複；TTL（90s，<< 掃描間隔 900s）確保跨輪仍是新鮮抓取。錯誤永不入快取。
_CACHE_TTL = float(os.getenv("CG_CACHE_TTL_SEC", "90"))
_MAX_RETRIES = int(os.getenv("CG_MAX_RETRIES", "3"))
_RETRY_BASE = float(os.getenv("CG_RETRY_BASE_SEC", "0.5"))


class _TTLCache:
    def __init__(self, ttl: float):
        self.ttl = ttl
        self._store: dict[str, tuple[float, dict]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(path: str, params: dict | None) -> str:
        items = sorted((params or {}).items())
        return path + "?" + "&".join(f"{k}={v}" for k, v in items)

    async def get(self, path: str, params: dict | None) -> dict | None:
        if self.ttl <= 0:
            return None
        k = self._key(path, params)
        async with self._lock:
            hit = self._store.get(k)
            if hit and (time.time() - hit[0]) < self.ttl:
                return hit[1]
            if hit:
                del self._store[k]
        return None

    async def put(self, path: str, params: dict | None, value: dict) -> None:
        if self.ttl <= 0:
            return
        k = self._key(path, params)
        async with self._lock:
            self._store[k] = (time.time(), value)
            if len(self._store) > 2000:          # 輕量修剪，防無限長
                now = time.time()
                self._store = {kk: vv for kk, vv in self._store.items()
                               if now - vv[0] < self.ttl}


# ===========================================================================
# Client
# ===========================================================================
class CoinGlassSource:
    name = "coinglass"
    BASE_URL = "https://open-api-v4.coinglass.com"

    # positioning 是 per-exchange；預設 Binance（流動性最大、top-trader 樣本最廣）
    DEFAULT_EXCHANGE = "Binance"
    DEFAULT_OI_EXCHANGES = "Binance,OKX,Bybit"   # 聚合 OI 看這幾家

    # CoinGlass 雙命名：per-exchange 用 BTCUSDT、aggregated 用 BTC
    @staticmethod
    def _agg_symbol(symbol: str) -> str:
        """Aggregated 端點吃 base symbol，e.g. 'BTC' / 'SUI'"""
        from ..symbol_mapping import normalize
        return normalize(symbol)

    # 各 ratio_type 在 CG 回應裡的欄位名
    _RATIO_FIELDS = {
        "top_trader_position": "top_position_long_short_ratio",
        "top_trader_account":  "top_account_long_short_ratio",
        "account":             "global_account_long_short_ratio",
        "position":            "global_account_long_short_ratio",
    }

    def __init__(self):
        self.api_key = SETTINGS.coinglass_api_key
        self.timeout = SETTINGS.http_timeout_sec
        self.limiter = _RateLimiter(SETTINGS.rate_limit_per_min)
        self._cache = _TTLCache(_CACHE_TTL)
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.BASE_URL,
                headers={
                    "CG-API-KEY": self.api_key or "",
                    "Accept": "application/json",
                },
                timeout=self.timeout,
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ---------------------------------------------------------------------
    # Core HTTP wrapper：吞所有可預期錯誤、絕不 raise
    # ---------------------------------------------------------------------
    async def _get(self, path: str, params: dict | None, *,
                   tool: str, symbol: str | None = None) -> dict:
        if not self.api_key:
            return make_error(
                tool=tool, symbol=symbol, source="coinglass",
                code="BACKEND_NOT_READY",
                message="COINGLASS_API_KEY env not set",
                suggestion="Put your key in .env then restart",
            )

        # 短 TTL 成功快取：同 (path,params) 在 TTL 內直接回上次成功值
        # → 吃掉全域重複(btc_gate/strength_universe) 與快照內重複(structure)
        cached = await self._cache.get(path, params)
        if cached is not None:
            return cached

        # 只對「瞬時」錯誤（timeout / network / 429 / 5xx）退避重試；
        # 終局錯誤（401/403/4xx/業務碼/解析）立刻回，不浪費 rate budget。
        last_err: dict = make_error(
            tool=tool, symbol=symbol, source="coinglass",
            code="UNKNOWN", message="no attempt made")
        for attempt in range(_MAX_RETRIES + 1):
            await self.limiter.acquire()
            transient = False
            try:
                r = await self.client.get(path, params=params or {})
            except httpx.TimeoutException as e:
                last_err = make_error(
                    tool=tool, symbol=symbol, source="coinglass", code="TIMEOUT",
                    message=f"HTTP timeout after {self.timeout}s: {e}",
                    suggestion="Retry; if persistent increase HTTP_TIMEOUT_SEC")
                transient = True
            except httpx.HTTPError as e:
                last_err = make_error(
                    tool=tool, symbol=symbol, source="coinglass",
                    code="NETWORK_ERROR", message=str(e))
                transient = True
            else:
                if r.status_code == 429:
                    last_err = make_error(
                        tool=tool, symbol=symbol, source="coinglass",
                        code="RATE_LIMITED", message="CoinGlass rate limit hit",
                        suggestion=f"Reduce RATE_LIMIT_PER_MIN (now {SETTINGS.rate_limit_per_min})",
                        upstream_status=r.status_code, upstream_body=r.text)
                    transient = True
                elif r.status_code in (401, 403):
                    return make_error(
                        tool=tool, symbol=symbol, source="coinglass",
                        code="AUTH_FAILED", message="API key invalid or expired",
                        suggestion="Verify .env COINGLASS_API_KEY; rotate in CG dashboard if leaked",
                        upstream_status=r.status_code, upstream_body=r.text)
                elif r.status_code >= 500:
                    last_err = make_error(
                        tool=tool, symbol=symbol, source="coinglass",
                        code="HTTP_ERROR", message=f"HTTP {r.status_code}",
                        upstream_status=r.status_code, upstream_body=r.text)
                    transient = True
                elif r.status_code >= 400:
                    return make_error(
                        tool=tool, symbol=symbol, source="coinglass",
                        code="HTTP_ERROR", message=f"HTTP {r.status_code}",
                        upstream_status=r.status_code, upstream_body=r.text)
                else:
                    try:
                        body = r.json()
                    except Exception as e:
                        return make_error(
                            tool=tool, symbol=symbol, source="coinglass",
                            code="PARSE_ERROR",
                            message=f"non-JSON: {e}", upstream_body=r.text)
                    code = body.get("code")
                    if code not in ("0", 0, "success", "SUCCESS"):
                        return make_error(
                            tool=tool, symbol=symbol, source="coinglass",
                            code="API_ERROR",
                            message=str(body.get("msg") or "unknown"),
                            upstream_body=str(body)[:600])
                    out = {"data": body.get("data"), "source": "coinglass"}
                    await self._cache.put(path, params, out)
                    return out

            if transient and attempt < _MAX_RETRIES:
                await asyncio.sleep(min(_RETRY_BASE * (2 ** attempt), 8.0))
            else:
                break
        return last_err

    # ---------------------------------------------------------------------
    # 解析 helper
    # ---------------------------------------------------------------------
    @staticmethod
    def _to_float(v) -> float | None:
        """CoinGlass 數值常為 string，需轉 float。"""
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_ts(d: dict) -> int:
        for k in ("time", "ts", "timestamp", "t"):
            if k in d and d[k] is not None:
                try:
                    return int(d[k])
                except (TypeError, ValueError):
                    pass
        return 0

    @staticmethod
    def _delta_pct(series: list[dict]) -> float:
        if len(series) < 2:
            return 0.0
        first = series[0]["value"]
        last = series[-1]["value"]
        return ((last - first) / first * 100) if first else 0.0

    # =====================================================================
    # Positioning（per-exchange, BTCUSDT 命名）
    # =====================================================================
    async def get_positioning(self, symbol, ratio_type, window, limit) -> dict:
        cg_symbol = to_coinglass(symbol)
        if ratio_type == "top_trader_position":
            path = "/api/futures/top-long-short-position-ratio/history"
        elif ratio_type == "top_trader_account":
            path = "/api/futures/top-long-short-account-ratio/history"
        elif ratio_type in ("account", "position"):
            path = "/api/futures/global-long-short-account-ratio/history"
        else:
            return make_error(
                tool="mi_get_positioning", symbol=symbol, source="coinglass",
                code="VALIDATION", message=f"unknown ratio_type: {ratio_type}",
            )

        ratio_field = self._RATIO_FIELDS[ratio_type]
        result = await self._get(
            path,
            {"exchange": self.DEFAULT_EXCHANGE, "symbol": cg_symbol,
             "interval": window, "limit": min(max(limit, 1), 500)},
            tool="mi_get_positioning", symbol=symbol,
        )
        if result.get("error"):
            return result

        data = result["data"] or []
        if not data:
            return make_error(
                tool="mi_get_positioning", symbol=symbol, source="coinglass",
                code="EMPTY_DATA", message="no data returned",
            )

        series = []
        for d in data:
            v = self._to_float(d.get(ratio_field))
            if v is not None:
                series.append({"ts": self._extract_ts(d), "value": round(v, 4)})

        if not series:
            return make_error(
                tool="mi_get_positioning", symbol=symbol, source="coinglass",
                code="PARSE_ERROR",
                message=f"field '{ratio_field}' not found in any data point",
                upstream_body=str(data[:2]),
            )

        return {
            "symbol": symbol, "source": "coinglass", "ratio_type": ratio_type,
            "latest": series[-1]["value"], "series": series,
            "delta_pct": round(self._delta_pct(series), 3),
        }

    # =====================================================================
    # OI（聚合；symbol 用 base 例如 BTC，exchange_list 為參數）
    # =====================================================================
    async def get_oi(self, symbol, window, limit) -> dict:
        agg_sym = self._agg_symbol(symbol)
        result = await self._get(
            "/api/futures/open-interest/aggregated-history",
            {"symbol": agg_sym, "interval": window,
             "limit": min(max(limit, 1), 500),
             "exchange_list": self.DEFAULT_OI_EXCHANGES},
            tool="mi_get_oi", symbol=symbol,
        )
        if result.get("error"):
            return result

        data = result["data"] or []
        if not data:
            return make_error(tool="mi_get_oi", symbol=symbol, source="coinglass",
                              code="EMPTY_DATA", message="no data")

        # close 欄位（string→float）為當期 OI USD 值
        series = []
        for d in data:
            v = self._to_float(d.get("close"))
            if v is not None:
                series.append({"ts": self._extract_ts(d), "value": v})

        if not series:
            return make_error(tool="mi_get_oi", symbol=symbol, source="coinglass",
                              code="PARSE_ERROR", message="no 'close' OI field",
                              upstream_body=str(data[:2]))

        latest = series[-1]["value"]
        first = series[0]["value"]
        delta_pct_24h = ((latest - first) / first * 100) if first else 0.0
        return {
            "symbol": symbol, "source": "coinglass",
            "latest": round(latest, 2),
            "delta_pct_24h": round(delta_pct_24h, 3),
            "series": series,
        }

    # =====================================================================
    # Funding（OHLC：close = 當期 funding rate）
    # =====================================================================
    async def get_funding(self, symbol) -> dict:
        cg_symbol = to_coinglass(symbol)
        result = await self._get(
            "/api/futures/funding-rate/history",
            {"exchange": self.DEFAULT_EXCHANGE, "symbol": cg_symbol,
             "interval": "8h", "limit": 2},
            tool="mi_get_funding", symbol=symbol,
        )
        if result.get("error"):
            return result

        data = result["data"] or []
        if not data:
            return make_error(tool="mi_get_funding", symbol=symbol,
                              source="coinglass", code="EMPTY_DATA",
                              message="no funding data")

        latest = data[-1]
        current = float(latest.get("close", 0))
        # 預估：取最近兩根的趨勢延伸（CG v4 沒專屬 predicted 端點，用近期均值近似）
        predicted = current
        if len(data) >= 2:
            prev = float(data[-2].get("close", current))
            predicted = current + (current - prev) * 0.5
        return {
            "symbol": symbol, "source": "coinglass",
            "funding": round(current, 6),
            "funding_predicted": round(predicted, 6),
            "ts": self._extract_ts(latest),
        }

    # =====================================================================
    # Liquidations（聚合；base symbol + exchange_list）
    # =====================================================================
    async def get_liquidations(self, symbol, window) -> dict:
        agg_sym = self._agg_symbol(symbol)
        result = await self._get(
            "/api/futures/liquidation/aggregated-history",
            {"symbol": agg_sym, "interval": "1h",
             "exchange_list": self.DEFAULT_OI_EXCHANGES,
             "limit": 24},
            tool="mi_get_liquidations", symbol=symbol,
        )
        if result.get("error"):
            return result

        data = result["data"] or []
        if not data:
            return make_error(tool="mi_get_liquidations", symbol=symbol,
                              source="coinglass", code="EMPTY_DATA",
                              message="no liquidation data")

        total_long = 0.0
        total_short = 0.0
        for d in data:
            vl = self._to_float(d.get("aggregated_long_liquidation_usd"))
            vs = self._to_float(d.get("aggregated_short_liquidation_usd"))
            if vl is not None: total_long += vl
            if vs is not None: total_short += vs
        return {
            "symbol": symbol, "source": "coinglass",
            "window": window,
            "liq_long": round(total_long, 2),
            "liq_short": round(total_short, 2),
            "ts": self._extract_ts(data[-1]),
        }

    async def get_funding_series(self, symbol, window: str = "4h",
                                 limit: int = 60) -> dict:
        """資金費率歷史序列（v33 圖表用）。value = 當期 funding rate（小數，0.0009=0.09%）。"""
        result = await self._get(
            "/api/futures/funding-rate/history",
            {"exchange": self.DEFAULT_EXCHANGE, "symbol": to_coinglass(symbol),
             "interval": window, "limit": min(max(limit, 1), 500)},
            tool="mi_get_funding_series", symbol=symbol,
        )
        if result.get("error"):
            return result
        data = result["data"] or []
        series = []
        for d in data:
            v = self._to_float(d.get("close"))
            if v is not None:
                series.append({"ts": self._extract_ts(d), "value": v})
        if not series:
            return make_error(tool="mi_get_funding_series", symbol=symbol,
                              source="coinglass", code="EMPTY_DATA",
                              message="no funding series")
        return {"symbol": symbol, "source": "coinglass",
                "latest": series[-1]["value"], "series": series}

    async def get_liquidation_series(self, symbol, window: str = "4h",
                                     limit: int = 60) -> dict:
        """清算歷史序列（v33 圖表用）。每點含多單清算 long_usd / 空單清算 short_usd。"""
        agg_sym = self._agg_symbol(symbol)
        result = await self._get(
            "/api/futures/liquidation/aggregated-history",
            {"symbol": agg_sym, "interval": window,
             "exchange_list": self.DEFAULT_OI_EXCHANGES,
             "limit": min(max(limit, 1), 500)},
            tool="mi_get_liquidation_series", symbol=symbol,
        )
        if result.get("error"):
            return result
        data = result["data"] or []
        series = []
        for d in data:
            vl = self._to_float(d.get("aggregated_long_liquidation_usd"))
            vs = self._to_float(d.get("aggregated_short_liquidation_usd"))
            series.append({"ts": self._extract_ts(d),
                           "long_usd": vl or 0.0, "short_usd": vs or 0.0})
        if not series:
            return make_error(tool="mi_get_liquidation_series", symbol=symbol,
                              source="coinglass", code="EMPTY_DATA",
                              message="no liquidation series")
        return {"symbol": symbol, "source": "coinglass", "series": series}

    # =====================================================================
    # Price series（per-exchange OHLC + volume）
    # =====================================================================
    async def get_price_series(self, symbol, tf, limit) -> dict:
        cg_symbol = to_coinglass(symbol)
        result = await self._get(
            "/api/futures/price/history",
            {"exchange": self.DEFAULT_EXCHANGE, "symbol": cg_symbol,
             "interval": tf, "limit": min(max(limit, 1), 500)},
            tool="mi_get_price_series", symbol=symbol,
        )
        if result.get("error"):
            return result

        data = result["data"] or []
        if not data:
            return make_error(tool="mi_get_price_series", symbol=symbol,
                              source="coinglass", code="EMPTY_DATA",
                              message="no price data")

        # 同時取 OHLC + volume_usd（給 ATR / 量比 derivation 用）
        series = []
        for d in data:
            close = self._to_float(d.get("close"))
            if close is None:
                continue
            series.append({
                "ts": self._extract_ts(d),
                "value": close,
                "open": self._to_float(d.get("open")),
                "high": self._to_float(d.get("high")),
                "low": self._to_float(d.get("low")),
                "volume_usd": self._to_float(d.get("volume_usd")),
            })

        if not series:
            return make_error(tool="mi_get_price_series", symbol=symbol,
                              source="coinglass", code="PARSE_ERROR",
                              message="no 'close' price field",
                              upstream_body=str(data[:2]))

        return {
            "symbol": symbol, "source": "coinglass", "tf": tf,
            "price": series[-1]["value"],
            "series": series,
        }

    # =====================================================================
    # CVD（從 aggregated-taker-buy-sell-volume 推導：CVD = cumsum(buy-sell)）
    # =====================================================================
    async def get_cvd_series(self, symbol, window, limit) -> dict:
        agg_sym = self._agg_symbol(symbol)
        # 至少取 7d（給 cvd_slope_7d 用）。1h 視窗 → 168 bars。
        requested = max(limit, 168)
        result = await self._get(
            "/api/futures/aggregated-taker-buy-sell-volume/history",
            {"symbol": agg_sym, "interval": window,
             "exchange_list": self.DEFAULT_OI_EXCHANGES,
             "limit": min(requested, 500)},
            tool="mi_get_cvd", symbol=symbol,
        )
        if result.get("error"):
            return result

        data = result["data"] or []
        if not data:
            return make_error(tool="mi_get_cvd", symbol=symbol, source="coinglass",
                              code="EMPTY_DATA", message="no taker volume data")

        # CVD = 累積 (buy - sell)；delta_pct = 每根 (buy-sell)/(buy+sell)*100
        cumsum = 0.0
        series: list[dict] = []
        delta_pcts: list[float] = []
        for d in data:
            ts = self._extract_ts(d)
            buy = self._to_float(d.get("aggregated_buy_volume_usd")) or 0.0
            sell = self._to_float(d.get("aggregated_sell_volume_usd")) or 0.0
            delta = buy - sell
            total = buy + sell
            cumsum += delta
            series.append({"ts": ts, "value": cumsum})
            if total > 0:
                delta_pcts.append(delta / total * 100)

        if not series:
            return make_error(tool="mi_get_cvd", symbol=symbol, source="coinglass",
                              code="PARSE_ERROR",
                              message="no usable taker volume rows")

        # 短期斜率（最後 12 根 = 12h on 1h）= 近期主動買賣淨力
        n_recent = min(12, len(delta_pcts))
        cvd_slope = (sum(delta_pcts[-n_recent:]) / n_recent) if n_recent > 0 else 0.0

        # 7d 斜率（最後 168 根 on 1h）
        n_7d = min(168, len(delta_pcts))
        cvd_slope_7d = (sum(delta_pcts[-n_7d:]) / n_7d) if n_7d > 0 else 0.0

        return {
            "symbol": symbol, "source": "coinglass",
            "cvd": round(series[-1]["value"], 2),
            "cvd_slope": round(cvd_slope, 4),
            "cvd_slope_7d": round(cvd_slope_7d, 4),
            "cvd_price_divergence": "none",  # 在 snapshot 層比對 price 後填
            "series": series,
        }

    async def get_btc_gate(self) -> dict:
        # 簡易版：取 BTC 4h 100 根 → 算 200MA（CoinGlass v4 沒給 MA，本地算）
        from statistics import mean
        result = await self.get_price_series("BTC", "4h", 250)
        if isinstance(result, dict) and result.get("error"):
            return result
        prices = [p["value"] for p in result["series"]]
        if len(prices) < 200:
            return make_error(
                tool="mi_get_btc_gate", symbol="BTC", source="coinglass",
                code="INSUFFICIENT_DATA",
                message=f"need 200 bars, got {len(prices)}",
            )
        ma200 = mean(prices[-200:])
        last = prices[-1]
        regime = "trend_up" if last > ma200 * 1.02 else (
                 "trend_down" if last < ma200 * 0.98 else "range")
        gate_open = last > ma200 and regime != "trend_down"
        return {
            "source": "coinglass",
            "btc_gate_open": gate_open,
            "btc_regime": regime,
            "rule": "btc_close_4h > 4h_200ma AND regime != trend_down",
            "evidence": {"btc_close_4h": round(last, 2),
                         "btc_4h_200ma": round(ma200, 2)},
            "ts": result["series"][-1]["ts"],
        }

    # =====================================================================
    # 強勢分數候選池：用 /pairs-markets?symbol=X 對每個 Core symbol 拉
    # （避開 /coins-markets 需 Standard $299 升級）
    # =====================================================================
    async def get_strength_universe(self, limit: int,
                                    candidate_symbols: list[str] | None = None) -> dict:
        if candidate_symbols is not None:
            candidates = list(candidate_symbols)[:limit]
        else:
            from ..symbol_mapping import CORE_SYMBOLS, TRADING_CANDIDATES
            candidates = list(TRADING_CANDIDATES)[:limit]

        items: list[dict] = []
        for sym in candidates:
            r = await self._get(
                "/api/futures/pairs-markets",
                {"symbol": sym},
                tool="mi_get_strength_rank", symbol=sym,
            )
            if r.get("error"):
                continue
            rows = r.get("data") or []
            if not rows:
                continue
            # 聚合所有交易所的數字（sum / mean）→ 一個 symbol 一行
            total_vol = sum(self._to_float(x.get("volume_usd")) or 0 for x in rows)
            total_oi = sum(self._to_float(x.get("open_interest_usd")) or 0 for x in rows)
            ret_24h = sum(self._to_float(x.get("price_change_percent_24h")) or 0 for x in rows) / max(1, len(rows))
            vol_change = sum(self._to_float(x.get("volume_usd_change_percent_24h")) or 0 for x in rows) / max(1, len(rows))
            oi_change = sum(self._to_float(x.get("open_interest_change_percent_24h")) or 0 for x in rows) / max(1, len(rows))
            funding = sum(self._to_float(x.get("funding_rate")) or 0 for x in rows) / max(1, len(rows))

            # 24h vol vs 30d 估算：CG 給 24h 變化%，用「現在 vol / (1 - vol_change_pct/100)」反推昨天 vol；
            # 30d 均量近似為 24h × 0.8（粗估，等之後接 historical-volume 改進）
            est_yesterday_vol = total_vol / max(0.01, 1 - vol_change / 100) if vol_change != 0 else total_vol
            est_30d_avg = est_yesterday_vol * 0.85
            vol_24h_vs_30d = total_vol / est_30d_avg if est_30d_avg > 0 else 1.0

            items.append({
                "symbol": sym,
                "return_7d_pct": ret_24h * 5,           # 24h 變化 ×5 近似 7d（粗估）
                "vol_24h_usd": total_vol,
                "vol_24h_vs_30d": round(vol_24h_vs_30d, 3),
                "oi_delta_7d_pct": oi_change * 3,        # 24h 變化 ×3 近似 7d
                "cvd_slope_7d": 0.0,                     # 需另一筆 CVD 呼叫，這裡 skip
                "top_trader_dev": 0.05,                  # 預估，待 positioning 補
                "btc_corr_30d": 0.70 if sym != "BTC" else 1.0,
                "funding": funding,
            })

        return {"source": "coinglass", "ts": 0, "items": items}

    # =====================================================================
    # Setup B 7d 結構：從 price/OI/positioning/CVD 時序推導
    # =====================================================================
    # =====================================================================
    # ETF 機構流向（BTC + ETH 都有）
    # =====================================================================
    async def get_etf_flows(self, symbol: str = "BTC", lookback_days: int = 7) -> dict:
        """近 N 天 ETF 流向（淨流入/流出）。symbol = BTC | ETH。"""
        sym = symbol.upper()
        if sym not in ("BTC", "ETH"):
            return make_error(tool="mi_get_etf_flows", symbol=symbol,
                              source="coinglass", code="VALIDATION",
                              message="ETF flows only available for BTC and ETH")

        path = f"/api/etf/{'bitcoin' if sym == 'BTC' else 'ethereum'}/flow-history"
        r = await self._get(path, {}, tool="mi_get_etf_flows", symbol=symbol)
        if r.get("error"):
            return r

        data = r.get("data") or []
        if not data:
            return make_error(tool="mi_get_etf_flows", symbol=symbol,
                              source="coinglass", code="EMPTY_DATA",
                              message="no ETF flow data")

        # data 從舊到新；取最後 lookback_days
        recent = data[-lookback_days:]
        flows = []
        cumulative_7d = 0.0
        for d in recent:
            flow = self._to_float(d.get("flow_usd")) or 0.0
            ts = self._extract_ts(d) if "time" in d or "ts" in d else int(self._to_float(d.get("timestamp")) or 0)
            cumulative_7d += flow
            flows.append({"ts": ts, "flow_usd": flow,
                          "price_usd": self._to_float(d.get("price_usd"))})

        latest_flow = flows[-1]["flow_usd"] if flows else 0
        return {
            "symbol": sym, "source": "coinglass",
            "latest_24h_flow_usd": round(latest_flow, 2),
            "cumulative_7d_flow_usd": round(cumulative_7d, 2),
            "n_days": len(flows),
            "series": flows,
        }

    # =====================================================================
    # 情緒：Fear-Greed + AHR999
    # =====================================================================
    async def get_sentiment(self) -> dict:
        fg, ah = await asyncio.gather(
            self._get("/api/index/fear-greed-history", {"limit": 7},
                      tool="mi_get_sentiment"),
            self._get("/api/index/ahr999", {"limit": 7},
                      tool="mi_get_sentiment"),
            return_exceptions=True,
        )

        out: dict = {"source": "coinglass"}

        # Fear-Greed
        if isinstance(fg, dict) and not fg.get("error"):
            d = fg.get("data") or {}
            values = d.get("data_list", [])
            if values:
                latest_fg = self._to_float(values[-1])
                out["fear_greed_now"] = latest_fg
                out["fear_greed_label"] = self._fear_greed_label(latest_fg)
                if len(values) >= 7:
                    week_avg = sum(self._to_float(v) or 0 for v in values[-7:]) / 7
                    out["fear_greed_7d_avg"] = round(week_avg, 1)

        # AHR999（< 0.45 適合定投、> 1.2 偏高估）
        if isinstance(ah, dict) and not ah.get("error"):
            data = ah.get("data") or []
            if data:
                latest_ah = self._to_float(data[-1].get("ahr999_value"))
                out["ahr999_now"] = round(latest_ah, 3) if latest_ah else None
                out["ahr999_label"] = self._ahr999_label(latest_ah)

        return out

    @staticmethod
    def _fear_greed_label(v):
        if v is None: return "—"
        if v <= 20: return "極度恐懼"
        if v <= 40: return "恐懼"
        if v <= 60: return "中性"
        if v <= 80: return "貪婪"
        return "極度貪婪"

    @staticmethod
    def _ahr999_label(v):
        if v is None: return "—"
        if v < 0.45: return "底部區（適合定投）"
        if v < 1.2: return "上漲區"
        return "高估區（過熱）"

    # =====================================================================
    # Funding 多維度（單一 funding 比較器、OI 加權、Vol 加權、跨所套利）
    # =====================================================================
    async def get_funding_outliers(self, top_n: int = 15) -> dict:
        """掃 1233 幣 funding 找極端值（過熱 + 過冷）。
        過熱 → 多殺多風險；過冷（負）→ 空方付錢、軋空燃料。
        """
        r = await self._get("/api/futures/funding-rate/exchange-list", {},
                            tool="mi_get_funding_outliers")
        if r.get("error"):
            return r
        data = r.get("data") or []
        items = []
        for d in data:
            sym = d.get("symbol")
            # 用 stablecoin_margin (主流 USDT/USDC) 找最大值
            stable = d.get("stablecoin_margin_list", []) or []
            best_funding = None
            best_ex = None
            for row in stable:
                f = self._to_float(row.get("funding_rate"))
                if f is None: continue
                if best_funding is None or abs(f) > abs(best_funding):
                    best_funding = f
                    best_ex = row.get("exchange")
            if best_funding is None: continue
            items.append({
                "symbol": sym, "funding": best_funding,
                "funding_pct_8h": round(best_funding * 100, 4),
                "exchange": best_ex,
            })
        # 排序：最熱（>0）+ 最冷（<0）各 top_n
        items.sort(key=lambda x: x["funding"], reverse=True)
        hottest = items[:top_n]
        items.sort(key=lambda x: x["funding"])
        coldest = items[:top_n]
        return {"source": "coinglass", "total_scanned": len(data),
                "hottest": hottest, "coldest": coldest}

    async def get_funding_weighted(self, symbol: str, weight: str = "oi",
                                   interval: str = "1h", limit: int = 24) -> dict:
        """加權 funding rate 時序（OI 加權更準確反映真實多頭壓力）。
        weight: oi | vol
        """
        path = f"/api/futures/funding-rate/{weight}-weight-history"
        agg_sym = self._agg_symbol(symbol)
        r = await self._get(path, {"symbol": agg_sym, "interval": interval, "limit": limit},
                            tool="mi_get_funding_weighted", symbol=symbol)
        if r.get("error"): return r
        data = r.get("data") or []
        series = []
        for d in data:
            close = self._to_float(d.get("close"))
            if close is not None:
                series.append({"ts": self._extract_ts(d), "value": close})
        if not series:
            return make_error(tool="mi_get_funding_weighted", symbol=symbol, source="coinglass",
                              code="EMPTY_DATA", message="no weighted funding data")
        return {"symbol": symbol, "source": "coinglass", "weight": weight,
                "latest": series[-1]["value"], "series": series}

    async def get_funding_arbitrage(self, top_n: int = 10) -> dict:
        """484 個跨交易所 funding 套利機會（買低 funding 所、賣高 funding 所）"""
        r = await self._get("/api/futures/funding-rate/arbitrage", {},
                            tool="mi_get_funding_arbitrage")
        if r.get("error"): return r
        data = r.get("data") or []
        items = []
        for d in data:
            apr = self._to_float(d.get("apr"))
            if apr is None or apr < 5: continue   # 過濾 APR < 5% 的不值得
            items.append({
                "symbol": d.get("symbol"),
                "buy_at": d.get("buy"),
                "sell_at": d.get("sell"),
                "apr": round(apr, 2),
                "funding_diff": self._to_float(d.get("funding")),
            })
        items.sort(key=lambda x: x["apr"], reverse=True)
        return {"source": "coinglass", "total": len(data), "items": items[:top_n]}

    # =====================================================================
    # 現貨 vs 期貨 基差（spot 與 futures 價格差距 → 期現失衡）
    # =====================================================================
    async def get_spot_futures_basis(self, symbol: str) -> dict:
        """計算 spot 與 futures 的最新基差%。
        負基差（現貨 > 期貨）= 期貨折價 = 看跌；正基差 = 期貨溢價 = 看多
        """
        cg_sym = to_coinglass(symbol)
        spot_r, fut_r = await asyncio.gather(
            self._get("/api/spot/price/history",
                      {"exchange": "Binance", "symbol": cg_sym, "interval": "1h", "limit": 1},
                      tool="mi_get_spot_futures_basis", symbol=symbol),
            self._get("/api/futures/price/history",
                      {"exchange": "Binance", "symbol": cg_sym, "interval": "1h", "limit": 1},
                      tool="mi_get_spot_futures_basis", symbol=symbol),
            return_exceptions=True,
        )
        if not (isinstance(spot_r, dict) and isinstance(fut_r, dict)):
            return make_error(tool="mi_get_spot_futures_basis", symbol=symbol,
                              source="coinglass", code="FETCH_FAILED", message="parallel fetch failed")
        if spot_r.get("error") or fut_r.get("error"):
            return make_error(tool="mi_get_spot_futures_basis", symbol=symbol,
                              source="coinglass", code="PARTIAL_DATA",
                              message=f"spot:{spot_r.get('error')} fut:{fut_r.get('error')}")
        spot_data = spot_r.get("data") or []
        fut_data = fut_r.get("data") or []
        if not spot_data or not fut_data:
            return make_error(tool="mi_get_spot_futures_basis", symbol=symbol,
                              source="coinglass", code="EMPTY_DATA", message="no price data")
        spot_close = self._to_float(spot_data[-1].get("close"))
        fut_close = self._to_float(fut_data[-1].get("close"))
        if not (spot_close and fut_close):
            return make_error(tool="mi_get_spot_futures_basis", symbol=symbol,
                              source="coinglass", code="PARSE_ERROR", message="no close prices")
        basis_pct = (fut_close - spot_close) / spot_close * 100
        return {
            "symbol": symbol, "source": "coinglass",
            "spot_price": spot_close, "futures_price": fut_close,
            "basis_pct": round(basis_pct, 4),
            "interpretation": "expensive_futures" if basis_pct > 0.1 else (
                              "discount_futures" if basis_pct < -0.1 else "near_par"),
        }

    # =====================================================================
    # 期權市場（機構部位、OI 分佈、波動率指標）
    # =====================================================================
    async def get_options_market(self, symbol: str = "BTC") -> dict:
        """期權市場分佈 + 跨交易所 OI 變化。
        institutional 透露：BTC/ETH 期權多在 Deribit、CME；
        OI 24h 增加 = 機構建倉、減少 = 出清。
        """
        sym = self._agg_symbol(symbol)
        r = await self._get("/api/option/info", {"symbol": sym},
                            tool="mi_get_options_market", symbol=symbol)
        if r.get("error"): return r
        data = r.get("data") or []
        if not data:
            return make_error(tool="mi_get_options_market", symbol=symbol,
                              source="coinglass", code="EMPTY_DATA", message="no options data")
        total_oi = sum(self._to_float(d.get("open_interest_usd")) or 0 for d in data)
        items = []
        for d in data:
            items.append({
                "exchange": d.get("exchange_name"),
                "oi_usd": round(self._to_float(d.get("open_interest_usd")) or 0, 2),
                "oi_market_share": round(self._to_float(d.get("oi_market_share")) or 0, 3),
                "oi_change_24h_pct": round(self._to_float(d.get("open_interest_change_24h")) or 0, 3),
            })
        items.sort(key=lambda x: x["oi_usd"], reverse=True)
        agg_24h_change = sum(it.get("oi_change_24h_pct", 0) * it.get("oi_market_share", 0)/100 for it in items)
        return {
            "symbol": symbol, "source": "coinglass",
            "total_oi_usd": round(total_oi, 2),
            "weighted_24h_change_pct": round(agg_24h_change, 3),
            "by_exchange": items,
        }

    # =====================================================================
    # 完整週期指標組（Pi Cycle + Puell + S2F + Golden Ratio + 2-year MA）
    # =====================================================================
    async def get_market_cycle_full(self) -> dict:
        """全套 BTC 週期定位指標：5 個一次回。
        Pi Cycle / Puell / S2F + Golden Ratio Multiplier + 2-year MA Multiplier
        """
        pi, pu, sf, gr, two = await asyncio.gather(
            self._get("/api/index/pi-cycle-indicator", {"limit": 1}, tool="mi_get_market_cycle"),
            self._get("/api/index/puell-multiple", {"limit": 1}, tool="mi_get_market_cycle"),
            self._get("/api/index/stock-flow", {"limit": 1}, tool="mi_get_market_cycle"),
            self._get("/api/index/golden-ratio-multiplier", {"limit": 1}, tool="mi_get_market_cycle"),
            self._get("/api/index/2-year-ma-multiplier", {"limit": 1}, tool="mi_get_market_cycle"),
            return_exceptions=True,
        )
        out: dict = {"source": "coinglass"}

        if isinstance(pi, dict) and not pi.get("error"):
            data = pi.get("data") or []
            if data:
                last = data[-1]
                ma110 = self._to_float(last.get("ma_110"))
                ma350x2 = self._to_float(last.get("ma_350_mu_2"))
                price = self._to_float(last.get("price"))
                if ma110 and ma350x2:
                    distance_pct = (ma110 - ma350x2) / ma350x2 * 100
                    out["pi_cycle"] = {
                        "ma_110": round(ma110, 2), "ma_350x2": round(ma350x2, 2),
                        "price": round(price, 2) if price else None,
                        "distance_pct": round(distance_pct, 2),
                        "signal": "top_warning" if distance_pct > -5 else "neutral",
                    }

        if isinstance(pu, dict) and not pu.get("error"):
            data = pu.get("data") or []
            if data:
                p = self._to_float(data[-1].get("puell_multiple"))
                if p is not None:
                    label = "頂部區（高估）" if p > 4 else ("底部區（低估）" if p < 0.5
                            else ("偏低估" if p < 1.0 else "中性"))
                    out["puell"] = {"value": round(p, 3), "label": label}

        if isinstance(sf, dict) and not sf.get("error"):
            data = sf.get("data") or []
            if data:
                price = self._to_float(data[-1].get("price"))
                halving = data[-1].get("next_halving", "")
                out["stock_flow"] = {"price": round(price, 2) if price else None,
                                     "next_halving": halving}

        if isinstance(gr, dict) and not gr.get("error"):
            data = gr.get("data") or []
            if data:
                last = data[-1]
                price = self._to_float(last.get("price"))
                ma350 = self._to_float(last.get("ma_350"))
                bull_high_2 = self._to_float(last.get("low_bull_high_2"))
                acc_high_1_6 = self._to_float(last.get("accumulation_high_1_6"))
                if price and ma350:
                    multiplier = price / ma350
                    label = ("黃金比例頂部訊號" if multiplier > 2.0
                             else ("接近頂部" if multiplier > 1.6
                             else ("中性區" if multiplier > 1.0
                             else "底部累積區")))
                    out["golden_ratio"] = {
                        "price": round(price, 2), "ma_350": round(ma350, 2),
                        "multiplier": round(multiplier, 3),
                        "low_bull_high_2": round(bull_high_2, 2) if bull_high_2 else None,
                        "accumulation_high_1_6": round(acc_high_1_6, 2) if acc_high_1_6 else None,
                        "label": label,
                    }

        if isinstance(two, dict) and not two.get("error"):
            data = two.get("data") or []
            if data:
                last = data[-1]
                price = self._to_float(last.get("price"))
                ma730 = self._to_float(last.get("moving_average_730"))
                ma730_x5 = self._to_float(last.get("moving_average_730_multiplier_5"))
                if price and ma730:
                    mult = price / ma730
                    label = ("頂部風險（>5x 730 MA）" if price > (ma730_x5 or ma730*5)
                             else ("頂部接近" if mult > 3.5
                             else ("正常" if mult > 1.0
                             else "底部買入區")))
                    out["two_year_ma"] = {
                        "price": round(price, 2), "ma_730": round(ma730, 2),
                        "multiplier": round(mult, 3), "label": label,
                    }
        return out

    # =====================================================================
    # Hyperliquid 鯨魚倉位（真實 whale 數據，無需 Whale Alert $49/月）
    # =====================================================================
    async def get_hyperliquid_whales(self, top_n: int = 30) -> dict:
        """回前 N 個鯨魚倉位（按 notional 大小），同時聚合多空淨倉位。"""
        r = await self._get("/api/hyperliquid/whale-alert", {},
                            tool="mi_get_hyperliquid_whales")
        if r.get("error"):
            return r
        data = r.get("data") or []
        if not data:
            return make_error(tool="mi_get_hyperliquid_whales", symbol=None,
                              source="coinglass", code="EMPTY_DATA",
                              message="no whale positions")

        items = []
        per_symbol_long = {}
        per_symbol_short = {}
        for d in data:
            size = self._to_float(d.get("position_size")) or 0
            value = self._to_float(d.get("position_value_usd")) or 0
            entry = self._to_float(d.get("entry_price")) or 0
            liq = self._to_float(d.get("liq_price")) or 0
            sym = d.get("symbol", "?")
            user = d.get("user", "")[:8]   # 截短匿名 wallet
            direction = "long" if size > 0 else ("short" if size < 0 else "flat")
            items.append({
                "user": user, "symbol": sym, "direction": direction,
                "size": abs(size), "value_usd": abs(value),
                "entry": entry, "liq_price": liq,
            })
            if direction == "long":
                per_symbol_long[sym] = per_symbol_long.get(sym, 0) + abs(value)
            elif direction == "short":
                per_symbol_short[sym] = per_symbol_short.get(sym, 0) + abs(value)

        items.sort(key=lambda x: x["value_usd"], reverse=True)

        # 聚合：每幣多空淨倉
        per_symbol = []
        for sym in set(list(per_symbol_long.keys()) + list(per_symbol_short.keys())):
            long_v = per_symbol_long.get(sym, 0)
            short_v = per_symbol_short.get(sym, 0)
            total = long_v + short_v
            net_long_pct = ((long_v - short_v) / total * 100) if total > 0 else 0
            per_symbol.append({
                "symbol": sym, "long_usd": round(long_v, 2),
                "short_usd": round(short_v, 2),
                "net_long_pct": round(net_long_pct, 2),  # +100=全多 -100=全空
                "total_usd": round(total, 2),
            })
        per_symbol.sort(key=lambda x: x["total_usd"], reverse=True)

        return {
            "source": "coinglass-hyperliquid",
            "top_positions": items[:top_n],
            "per_symbol_aggregate": per_symbol[:15],
            "total_whales": len(data),
        }

    # =====================================================================
    # BTC 市場週期指標（Pi Cycle / Puell / Stock-to-Flow）
    # =====================================================================
    async def get_market_cycle(self) -> dict:
        """三大 BTC 週期定位指標：
        - Pi Cycle Top: BTC 110MA vs 350MA×2 交叉預測週期頂
        - Puell Multiple: 礦工日營收 vs 365d MA，>4 頂部、<0.5 底部
        - Stock-to-Flow: 基於減半的稀缺性模型
        """
        pi, pu, sf = await asyncio.gather(
            self._get("/api/index/pi-cycle-indicator", {}, tool="mi_get_market_cycle"),
            self._get("/api/index/puell-multiple", {"limit": 1}, tool="mi_get_market_cycle"),
            self._get("/api/index/stock-flow", {"limit": 1}, tool="mi_get_market_cycle"),
            return_exceptions=True,
        )

        out: dict = {"source": "coinglass"}

        # Pi Cycle
        if isinstance(pi, dict) and not pi.get("error"):
            data = pi.get("data") or []
            if data:
                last = data[-1]
                ma110 = self._to_float(last.get("ma_110"))
                ma350x2 = self._to_float(last.get("ma_350_mu_2"))
                price = self._to_float(last.get("price"))
                if ma110 and ma350x2:
                    distance_pct = (ma110 - ma350x2) / ma350x2 * 100
                    out["pi_cycle"] = {
                        "ma_110": round(ma110, 2),
                        "ma_350x2": round(ma350x2, 2),
                        "price": round(price, 2) if price else None,
                        "distance_pct": round(distance_pct, 2),  # >0=超越=頂部訊號
                        "signal": "top_warning" if distance_pct > -5 else "neutral",
                    }

        # Puell Multiple
        if isinstance(pu, dict) and not pu.get("error"):
            data = pu.get("data") or []
            if data:
                last = data[-1]
                p_val = self._to_float(last.get("puell_multiple"))
                if p_val is not None:
                    if p_val > 4:
                        label = "頂部區（高估）"
                    elif p_val < 0.5:
                        label = "底部區（低估）"
                    elif p_val < 1.0:
                        label = "偏低估"
                    else:
                        label = "中性"
                    out["puell"] = {"value": round(p_val, 3), "label": label}

        # Stock-to-Flow
        if isinstance(sf, dict) and not sf.get("error"):
            data = sf.get("data") or []
            if data:
                last = data[-1]
                price = self._to_float(last.get("price"))
                halving = last.get("next_halving", "")
                out["stock_flow"] = {
                    "price": round(price, 2) if price else None,
                    "next_halving": halving,
                }

        return out

    # =====================================================================
    # 全市場清算掃描（替代 whale alert 的 proxy）
    # =====================================================================
    async def get_liquidation_scan(self, top_n: int = 20) -> dict:
        """掃 1220 幣，找近 24h 清算最大的 Top N（=擠壓燃料候選）。"""
        r = await self._get("/api/futures/liquidation/coin-list", {},
                            tool="mi_get_liquidation_scan")
        if r.get("error"):
            return r

        data = r.get("data") or []
        items = []
        for d in data:
            total = self._to_float(d.get("liquidation_usd_24h")) or 0
            longs = self._to_float(d.get("long_liquidation_usd_24h")) or 0
            shorts = self._to_float(d.get("short_liquidation_usd_24h")) or 0
            if total < 100_000:    # 過濾低噪音
                continue
            # 失衡分數：空清算遠大於多清算 = bullish squeeze fuel
            imbalance = (shorts - longs) / total if total > 0 else 0
            items.append({
                "symbol": d.get("symbol"),
                "total_24h": total,
                "long_liq_24h": longs,
                "short_liq_24h": shorts,
                "imbalance": round(imbalance, 3),  # +1=全空 -1=全多
                "liq_12h": self._to_float(d.get("liquidation_usd_12h")) or 0,
            })

        items.sort(key=lambda x: x["total_24h"], reverse=True)
        return {
            "source": "coinglass",
            "total_scanned": len(data),
            "items": items[:top_n],
        }

    async def get_structure(self, symbol) -> dict:
        # 並行拉 4 條時序
        results = await asyncio.gather(
            self.get_price_series(symbol, "4h", 200),       # 7d × 4h = 42 bars，多拉一些做 ATR
            self.get_oi(symbol, "1h", 168),                  # 7d 整 168 小時
            self.get_positioning(symbol, "top_trader_position", "4h", 42),
            self.get_cvd_series(symbol, "1h", 168),
            return_exceptions=True,
        )
        price_r, oi_r, pos_r, cvd_r = results

        out: dict = {"symbol": symbol, "source": "coinglass", "ts": 0,
                     "atr_pct_7d": None, "vol_24h_vs_30d": None,
                     "cvd_slope_7d": None, "top_trader_slope_7d": None,
                     "oi_delta_7d_pct": None, "higher_lows_7d": None,
                     "above_4h_200ma": None}

        # ---- ATR% (7d) + higher_lows_7d + vol_24h_vs_30d + above_4h_200ma ----
        if isinstance(price_r, dict) and not price_r.get("error"):
            pseries = price_r.get("series", [])

            # above_4h_200ma：有 200 根 4h 才算
            if len(pseries) >= 200:
                ma200 = sum(p["value"] for p in pseries[-200:]) / 200
                out["above_4h_200ma"] = pseries[-1]["value"] > ma200

            recent_7d = pseries[-42:] if len(pseries) >= 42 else pseries
            if recent_7d:
                highs = [p["high"] for p in recent_7d if p.get("high") is not None]
                lows = [p["low"] for p in recent_7d if p.get("low") is not None]
                cur = recent_7d[-1]["value"]
                if highs and lows and cur:
                    out["atr_pct_7d"] = round((max(highs) - min(lows)) / cur * 100, 2)

                # higher_lows: 7 天中至少 4 天的 low 比前一天高
                daily_lows = []
                for i in range(0, len(recent_7d), 6):  # 6 × 4h = 24h
                    day_bars = recent_7d[i:i + 6]
                    day_lo = [b["low"] for b in day_bars if b.get("low") is not None]
                    if day_lo:
                        daily_lows.append(min(day_lo))
                if len(daily_lows) >= 4:
                    ascending = sum(1 for i in range(1, len(daily_lows))
                                    if daily_lows[i] > daily_lows[i - 1])
                    out["higher_lows_7d"] = ascending >= max(2, (len(daily_lows) - 1) // 2)

                # vol_24h_vs_30d 近似：用 4h 視窗，7d 內最後 6 根 vs 全期均
                vols = [p["volume_usd"] for p in pseries[-180:] if p.get("volume_usd") is not None]
                if len(vols) >= 30:
                    recent_24h_vol = sum(vols[-6:])  # 6 × 4h
                    avg_24h_vol = sum(vols) / len(vols) * 6
                    if avg_24h_vol > 0:
                        out["vol_24h_vs_30d"] = round(recent_24h_vol / avg_24h_vol, 3)

        # ---- OI 7d delta ----
        if isinstance(oi_r, dict) and not oi_r.get("error"):
            oseries = oi_r.get("series", [])
            if len(oseries) >= 24:
                first = oseries[0]["value"]
                last = oseries[-1]["value"]
                if first > 0:
                    out["oi_delta_7d_pct"] = round((last - first) / first * 100, 3)

        # ---- top_trader 7d slope（per-bar 變化平均）----
        if isinstance(pos_r, dict) and not pos_r.get("error"):
            tseries = pos_r.get("series", [])
            if len(tseries) >= 6:
                first = tseries[0]["value"]
                last = tseries[-1]["value"]
                out["top_trader_slope_7d"] = round((last - first) / len(tseries), 5)

        # ---- cvd_slope_7d 從 CVD 拿 ----
        if isinstance(cvd_r, dict) and not cvd_r.get("error"):
            out["cvd_slope_7d"] = cvd_r.get("cvd_slope_7d")

        return out

    # =====================================================================
    # 綜合宏觀端點（已付費 $79 Startup）：已正式接入 macro_confluence 影子分數。
    # ✅ 誠實註記：macro_confluence._collect_components 已呼叫以下 5 個方法
    # （coinbase premium / coin netflow / btc dominance / altcoin season /
    # btc vs m2），各以低權重計入 _WEIGHTS（合計 0.22），純讀觀測。
    # ⚠️ 影子鐵則：這 5 個分量永不乘進/加進 strength_score、永不進 fire/
    # symbol_gate/下單、不發 Telegram；只供 confluence 影子合成 + 儀表板顯示。
    # ---------------------------------------------------------------------
    # 設計鐵則（與本檔既有方法一致）：
    #   * 全部走 self._get（共用限流器 + TTL 快取 + 退避重試），不另開額度。
    #   * 任何失敗一律回 make_error dict（不 raise）；上層對缺料分量一律
    #     「中性化」（不臆測方向），故路徑/欄位日後微調也優雅降級。
    #   * 純讀，零下單路徑（紅線①）。
    #   * ⚠️ 不得改動上方任何既有方法/簽名（純附加）。
    # 端點路徑依 CoinGlass v4 文件 + 權益實測（trading-bot-coinglass-entitlements）。
    # =====================================================================
    async def get_coinbase_premium_index(self, interval: str = "1h",
                                         limit: int = 24) -> dict:
        """Coinbase 溢價指數歷史：美國現貨買壓代理（>0＝美國買盤強＝偏多）。

        回 {"source","latest","series":[{ts,value}]}；失敗回 make_error dict。
        """
        r = await self._get(
            "/api/coinbase-premium-index/history",
            {"interval": interval, "limit": min(max(limit, 1), 500)},
            tool="mi_get_coinbase_premium",
        )
        if r.get("error"):
            return r
        data = r.get("data") or []
        series = []
        for d in data:
            # 文件常見欄位 premium / premium_rate / close 任一存在即取
            v = self._to_float(
                d.get("premium_rate") if d.get("premium_rate") is not None
                else (d.get("premium") if d.get("premium") is not None
                      else d.get("close")))
            if v is not None:
                series.append({"ts": self._extract_ts(d), "value": v})
        if not series:
            return make_error(tool="mi_get_coinbase_premium", symbol=None,
                              source="coinglass", code="EMPTY_DATA",
                              message="no coinbase premium data")
        return {"source": "coinglass", "latest": series[-1]["value"],
                "series": series}

    async def get_coin_netflow(self, symbol: str = "BTC",
                               interval: str = "1h", limit: int = 24) -> dict:
        """交易所淨流（futures/coin/netflow）：>0＝資金流入交易所（潛在賣壓）、
        <0＝流出（提幣冷錢包＝偏多）。注意文件 overview 曾有 typo `furures`，
        實際路徑為 /api/futures/coin/netflow。

        回 {"symbol","source","latest","series":[{ts,value}]}；失敗回 make_error。
        """
        agg_sym = self._agg_symbol(symbol)
        r = await self._get(
            "/api/futures/coin/netflow",
            {"symbol": agg_sym, "interval": interval,
             "limit": min(max(limit, 1), 500)},
            tool="mi_get_coin_netflow", symbol=symbol,
        )
        if r.get("error"):
            return r
        data = r.get("data") or []
        series = []
        for d in data:
            v = self._to_float(
                d.get("netflow_usd") if d.get("netflow_usd") is not None
                else (d.get("netflow") if d.get("netflow") is not None
                      else d.get("close")))
            if v is not None:
                series.append({"ts": self._extract_ts(d), "value": v})
        if not series:
            return make_error(tool="mi_get_coin_netflow", symbol=symbol,
                              source="coinglass", code="EMPTY_DATA",
                              message="no netflow data")
        return {"symbol": symbol, "source": "coinglass",
                "latest": series[-1]["value"], "series": series}

    async def get_bitcoin_dominance(self, limit: int = 30) -> dict:
        """BTC.D（比特幣市占率）歷史：上升＝資金回流 BTC（山寨偏弱）。

        回 {"source","latest","series":[{ts,value}]}；失敗回 make_error dict。
        """
        r = await self._get(
            "/api/index/bitcoin-dominance",
            {"limit": min(max(limit, 1), 500)},
            tool="mi_get_btc_dominance",
        )
        if r.get("error"):
            return r
        data = r.get("data") or []
        series = []
        for d in data:
            v = self._to_float(
                d.get("bitcoin_dominance") if d.get("bitcoin_dominance") is not None
                else (d.get("dominance") if d.get("dominance") is not None
                      else d.get("close")))
            if v is not None:
                series.append({"ts": self._extract_ts(d), "value": v})
        if not series:
            return make_error(tool="mi_get_btc_dominance", symbol=None,
                              source="coinglass", code="EMPTY_DATA",
                              message="no dominance data")
        return {"source": "coinglass", "latest": series[-1]["value"],
                "series": series}

    async def get_altcoin_season(self, limit: int = 30) -> dict:
        """Altcoin Season Index（0-100）：>75＝山寨季、<25＝比特幣季。

        回 {"source","latest","label","series":[{ts,value}]}；失敗回 make_error。
        """
        r = await self._get(
            "/api/index/altcoin-season",
            {"limit": min(max(limit, 1), 500)},
            tool="mi_get_altcoin_season",
        )
        if r.get("error"):
            return r
        data = r.get("data") or []
        series = []
        for d in data:
            v = self._to_float(
                d.get("altcoin_index") if d.get("altcoin_index") is not None
                else (d.get("altcoin_season") if d.get("altcoin_season") is not None
                      else (d.get("value") if d.get("value") is not None
                            else d.get("close"))))
            if v is not None:
                series.append({"ts": self._extract_ts(d), "value": v})
        if not series:
            return make_error(tool="mi_get_altcoin_season", symbol=None,
                              source="coinglass", code="EMPTY_DATA",
                              message="no altcoin season data")
        latest = series[-1]["value"]
        label = ("山寨季" if latest >= 75 else
                 ("比特幣季" if latest <= 25 else "中性"))
        return {"source": "coinglass", "latest": latest, "label": label,
                "series": series}

    async def get_bitcoin_vs_m2(self, region: str = "global",
                                limit: int = 60) -> dict:
        """BTC 價格 vs 全球/美國 M2 貨幣供給（流動性週期對照）。
        region: global | us。M2 擴張通常為風險資產順風。

        回 {"source","region","series":[{ts,price,m2}]}；失敗回 make_error dict。
        """
        reg = "us" if str(region).lower() == "us" else "global"
        path = ("/api/index/bitcoin-vs-us-m2" if reg == "us"
                else "/api/index/bitcoin-vs-global-m2")
        r = await self._get(path, {"limit": min(max(limit, 1), 500)},
                            tool="mi_get_btc_vs_m2")
        if r.get("error"):
            return r
        data = r.get("data") or []
        series = []
        for d in data:
            price = self._to_float(d.get("price"))
            m2 = self._to_float(
                d.get("m2") if d.get("m2") is not None else d.get("m2_supply"))
            if price is None and m2 is None:
                continue
            series.append({"ts": self._extract_ts(d), "price": price, "m2": m2})
        if not series:
            return make_error(tool="mi_get_btc_vs_m2", symbol=None,
                              source="coinglass", code="EMPTY_DATA",
                              message="no btc-vs-m2 data")
        return {"source": "coinglass", "region": reg, "series": series}

    # =====================================================================
    async def health(self) -> dict:
        if not self.api_key:
            return {"ok": False, "source": "coinglass", "details": "API key missing"}
        # 真打 funding endpoint 確認 key 通
        r = await self._get(
            "/api/futures/funding-rate/history",
            {"exchange": "Binance", "symbol": "BTCUSDT", "interval": "4h", "limit": 1},
            tool="mi_health",
        )
        if r.get("error"):
            return {"ok": False, "source": "coinglass", "details": r.get("message"),
                    "code": r.get("code")}
        return {"ok": True, "source": "coinglass",
                "details": f"rate limit {SETTINGS.rate_limit_per_min}/min"}

    # =====================================================================
    async def get_article_list(self, limit: int = 50) -> dict:
        """v52: CoinGlass 新聞/快訊列表（GET /api/article/list；Startup $79 即有）。

        回 {"source":"coinglass","articles":[...]}；底層失敗時回 make_error dict
        （無 "articles" 鍵，上層以 .get("articles") or [] 取用即優雅降級）。

        實測欄位：article_title / article_content(HTML body) / article_release_time(毫秒)
        / article_picture / source_name / source_website_logo。
        無 id / url / language 欄、內容恆英文 → 上層需 hash 去重 + LLM 翻繁中。
        """
        r = await self._get("/api/article/list", {}, tool="mi_get_news")
        if r.get("error"):
            return r
        data = r.get("data")
        # data 可能是 list，或被包成 {list/items/data: [...]}（防 API 包裝日後變動）
        if isinstance(data, dict):
            data = data.get("list") or data.get("items") or data.get("data") or []
        articles = data if isinstance(data, list) else []
        if limit and len(articles) > limit:
            articles = articles[:limit]
        return {"source": "coinglass", "articles": articles}
