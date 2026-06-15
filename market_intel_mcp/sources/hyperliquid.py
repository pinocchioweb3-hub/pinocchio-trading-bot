"""Hyperliquid 公開 API client（永續 DEX 原生交易數據）。

為什麼接：Hyperliquid 是目前最大的鏈上永續合約交易所之一，其 funding / OI /
成交量是「鏈上真實掛單撮合」的第一手數據，與 CEX（Binance/OKX）獨立，可作為
confluence 的另一個獨立確認來源。本來只透過 CoinGlass 拿到 HL「鯨魚倉位」，
這支補上 HL 自家的盤面數據（資金費率、未平倉、標記價、24h 量、K 線）。

紅線：本所數據端點為「公開、免金鑰、唯讀」。本檔不下任何單、不碰任何金鑰。
端點：POST https://api.hyperliquid.xyz/info  body={"type": ...}
    metaAndAssetCtxs → [universe_meta, [asset_ctx, ...]]（一次回全部永續）
    allMids          → {coin: mid_px}
    candleSnapshot   → [{t,T,s,i,o,c,h,l,v,n}, ...]
單位約定（與 base.py 一致）：
    funding: 小數（0.0000125 = 每小時 0.00125%）。HL 每「小時」收取一次。
    oi/vol/price: float USD。ts: epoch ms。缺料回 dict.error，禁止 raise。
"""
from __future__ import annotations

import asyncio
import os
import time

import httpx

from ..errors import make_error
from ..symbol_mapping import normalize

# ---------------------------------------------------------------------------
# 設定（環境變數可調；預設保守，HL info 端點額度寬鬆但禮貌節流）
# ---------------------------------------------------------------------------
_CACHE_TTL = float(os.getenv("HL_CACHE_TTL_SEC", "30"))      # metaAndAssetCtxs 一次回全幣，快取 30s 共用
_MAX_RETRIES = int(os.getenv("HL_MAX_RETRIES", "3"))
_RETRY_BASE = float(os.getenv("HL_RETRY_BASE_SEC", "0.5"))
_RATE_LIMIT = int(os.getenv("HL_RATE_LIMIT_PER_MIN", "60"))
_TIMEOUT = float(os.getenv("HTTP_TIMEOUT_SEC", "10"))

# HL 幣名與 canonical 多數一致；少數有別名（HL 用 k 前綴表千倍）。
# 只列實際會用到/已知有別的；其餘以 canonical 直送。
_HL_ALIAS = {
    "PEPE": "kPEPE",
    "SHIB": "kSHIB",
    "BONK": "kBONK",
    "FLOKI": "kFLOKI",
    "1000PEPE": "kPEPE",
    "1000SHIB": "kSHIB",
    "1000BONK": "kBONK",
    "1000FLOKI": "kFLOKI",
}

# 候選的 K 線區間 → 毫秒（用於由 limit 反推 startTime）
_INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
    "3d": 259_200_000, "1w": 604_800_000,
}


class _RateLimiter:
    """Sliding-window，每分鐘上限（與 coinglass 同模式）。"""

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


class _TTLCache:
    """短 TTL 成功快取：metaAndAssetCtxs 一呼叫回全幣，跨 get_market 共用。"""

    def __init__(self, ttl: float):
        self.ttl = ttl
        self._store: dict[str, tuple[float, object]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str):
        if self.ttl <= 0:
            return None
        async with self._lock:
            hit = self._store.get(key)
            if hit and (time.time() - hit[0]) < self.ttl:
                return hit[1]
            if hit:
                del self._store[key]
        return None

    async def put(self, key: str, value) -> None:
        if self.ttl <= 0:
            return
        async with self._lock:
            self._store[key] = (time.time(), value)
            if len(self._store) > 500:
                now = time.time()
                self._store = {k: v for k, v in self._store.items()
                               if now - v[0] < self.ttl}


def _to_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _hl_coin(symbol: str) -> str:
    """canonical/任意命名 → HL 幣名。"""
    c = normalize(symbol)
    return _HL_ALIAS.get(c, c)


class HyperliquidSource:
    """Hyperliquid 公開 info API。唯讀、免金鑰。

    非 BaseSource 全實作；為 CoinGlass 主後端之外的「獨立確認來源」，
    以模組單例方式在 server.py 直接掛 mi_get_hl_* 工具（與 whale 同模式）。
    """

    name = "hyperliquid"
    BASE_URL = "https://api.hyperliquid.xyz"

    def __init__(self):
        self.timeout = _TIMEOUT
        self.limiter = _RateLimiter(_RATE_LIMIT)
        self._cache = _TTLCache(_CACHE_TTL)
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.BASE_URL,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=self.timeout,
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ---------------------------------------------------------------------
    # Core HTTP：POST /info，吞所有可預期錯誤、絕不 raise
    # ---------------------------------------------------------------------
    async def _post(self, body: dict, *, tool: str, symbol: str | None = None,
                    cache_key: str | None = None) -> dict:
        if cache_key is not None:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                return {"data": cached, "source": "hyperliquid", "cached": True}

        last_err: dict = make_error(
            tool=tool, symbol=symbol, source="hyperliquid",
            code="UNKNOWN", message="no attempt made")
        for attempt in range(_MAX_RETRIES + 1):
            await self.limiter.acquire()
            transient = False
            try:
                r = await self.client.post("/info", json=body)
            except httpx.TimeoutException as e:
                last_err = make_error(
                    tool=tool, symbol=symbol, source="hyperliquid", code="TIMEOUT",
                    message=f"HTTP timeout after {self.timeout}s: {e}",
                    suggestion="Retry; if persistent increase HTTP_TIMEOUT_SEC")
                transient = True
            except httpx.HTTPError as e:
                last_err = make_error(
                    tool=tool, symbol=symbol, source="hyperliquid",
                    code="NETWORK_ERROR", message=str(e))
                transient = True
            else:
                if r.status_code == 429:
                    last_err = make_error(
                        tool=tool, symbol=symbol, source="hyperliquid",
                        code="RATE_LIMITED", message="Hyperliquid rate limit hit",
                        suggestion=f"Reduce HL_RATE_LIMIT_PER_MIN (now {_RATE_LIMIT})",
                        upstream_status=r.status_code, upstream_body=r.text)
                    transient = True
                elif r.status_code >= 500:
                    last_err = make_error(
                        tool=tool, symbol=symbol, source="hyperliquid",
                        code="HTTP_ERROR", message=f"HTTP {r.status_code}",
                        upstream_status=r.status_code, upstream_body=r.text)
                    transient = True
                elif r.status_code >= 400:
                    return make_error(
                        tool=tool, symbol=symbol, source="hyperliquid",
                        code="HTTP_ERROR", message=f"HTTP {r.status_code}",
                        upstream_status=r.status_code, upstream_body=r.text)
                else:
                    try:
                        data = r.json()
                    except Exception as e:  # noqa: BLE001
                        return make_error(
                            tool=tool, symbol=symbol, source="hyperliquid",
                            code="PARSE_ERROR", message=f"non-JSON: {e}",
                            upstream_body=r.text)
                    if cache_key is not None:
                        await self._cache.put(cache_key, data)
                    return {"data": data, "source": "hyperliquid"}

            if transient and attempt < _MAX_RETRIES:
                await asyncio.sleep(min(_RETRY_BASE * (2 ** attempt), 8.0))
            else:
                break
        return last_err

    # ---------------------------------------------------------------------
    # 內部：取 metaAndAssetCtxs，整理成 {coin: ctx_dict}（含 meta）
    # ---------------------------------------------------------------------
    async def _meta_ctxs(self, *, tool: str) -> dict:
        r = await self._post({"type": "metaAndAssetCtxs"}, tool=tool,
                             cache_key="metaAndAssetCtxs")
        if r.get("error"):
            return r
        payload = r.get("data") or []
        if not isinstance(payload, list) or len(payload) < 2:
            return make_error(tool=tool, symbol=None, source="hyperliquid",
                              code="PARSE_ERROR", message="unexpected metaAndAssetCtxs shape")
        meta = payload[0] or {}
        ctxs = payload[1] or []
        universe = meta.get("universe") or []
        by_coin: dict[str, dict] = {}
        for i, u in enumerate(universe):
            if i >= len(ctxs):
                break
            name = (u or {}).get("name")
            if not name:
                continue
            ctx = ctxs[i] or {}
            by_coin[name] = {"meta": u, "ctx": ctx}
        return {"by_coin": by_coin, "count": len(by_coin)}

    @staticmethod
    def _shape_market(coin: str, meta: dict, ctx: dict) -> dict:
        """把 HL ctx 整理成統一盤面 dict。"""
        funding = _to_float(ctx.get("funding"))           # 每小時資金費率（小數）
        mark = _to_float(ctx.get("markPx"))
        oracle = _to_float(ctx.get("oraclePx"))
        mid = _to_float(ctx.get("midPx"))
        prev = _to_float(ctx.get("prevDayPx"))
        oi_coin = _to_float(ctx.get("openInterest"))      # 單位：幣的數量
        day_vol_usd = _to_float(ctx.get("dayNtlVlm"))     # 24h 名目成交額（USD）
        premium = _to_float(ctx.get("premium"))
        px = mark or oracle or mid
        oi_usd = (oi_coin * px) if (oi_coin is not None and px) else None
        chg_24h = (((px - prev) / prev) * 100) if (px and prev) else None
        return {
            "coin": coin,
            "mark_px": mark,
            "oracle_px": oracle,
            "mid_px": mid,
            "prev_day_px": prev,
            "change_24h_pct": round(chg_24h, 2) if chg_24h is not None else None,
            "funding_hourly": funding,                                            # HL 每小時收
            "funding_8h_pct": round(funding * 8 * 100, 4) if funding is not None else None,    # 換算成 CEX 慣用的 8h%
            "funding_annual_pct": round(funding * 24 * 365 * 100, 2) if funding is not None else None,
            "open_interest_coin": oi_coin,
            "open_interest_usd": round(oi_usd, 2) if oi_usd is not None else None,
            "day_notional_volume_usd": round(day_vol_usd, 2) if day_vol_usd is not None else None,
            "premium": premium,
            "max_leverage": (meta or {}).get("maxLeverage"),
            "ts": int(time.time() * 1000),
        }

    # ---------------------------------------------------------------------
    # 對外：單幣完整盤面
    # ---------------------------------------------------------------------
    async def get_market(self, symbol: str) -> dict:
        """單幣 HL 盤面：funding / OI(USD) / 標記/預言/中價 / 24h 量 / 24h 漲跌。"""
        tool = "mi_get_hl_market"
        coin = _hl_coin(symbol)
        mc = await self._meta_ctxs(tool=tool)
        if mc.get("error"):
            return mc
        node = mc["by_coin"].get(coin)
        if node is None:
            return make_error(tool=tool, symbol=symbol, source="hyperliquid",
                              code="SYMBOL_UNKNOWN",
                              message=f"{coin} not listed on Hyperliquid perps",
                              suggestion="Check coin name; HL uses k-prefix for 1000x meme coins")
        out = self._shape_market(coin, node["meta"], node["ctx"])
        out["source"] = "hyperliquid"
        return out

    async def get_funding(self, symbol: str) -> dict:
        """HL 資金費率（每小時收）。回 hourly / 8h% / 年化%，便於與 CEX 對齊。"""
        m = await self.get_market(symbol)
        if m.get("error"):
            return m
        return {
            "source": "hyperliquid",
            "coin": m["coin"],
            "funding_hourly": m["funding_hourly"],
            "funding_8h_pct": m["funding_8h_pct"],
            "funding_annual_pct": m["funding_annual_pct"],
            "premium": m["premium"],
            "ts": m["ts"],
            "note": "HL 每小時收 funding；8h%＝hourly×8 以對齊 CEX 慣例",
        }

    async def get_candles(self, symbol: str, interval: str = "1h", limit: int = 100) -> dict:
        """HL K 線（OHLCV）。interval 例：1m/5m/15m/1h/4h/1d。"""
        tool = "mi_get_hl_candles"
        coin = _hl_coin(symbol)
        iv = interval if interval in _INTERVAL_MS else "1h"
        limit = max(1, min(int(limit), 1000))
        end = int(time.time() * 1000)
        start = end - _INTERVAL_MS[iv] * limit
        r = await self._post(
            {"type": "candleSnapshot",
             "req": {"coin": coin, "interval": iv, "startTime": start, "endTime": end}},
            tool=tool, symbol=symbol)
        if r.get("error"):
            return r
        rows = r.get("data") or []
        candles = []
        for d in rows:
            candles.append({
                "ts": d.get("t"),
                "open": _to_float(d.get("o")),
                "high": _to_float(d.get("h")),
                "low": _to_float(d.get("l")),
                "close": _to_float(d.get("c")),
                "volume": _to_float(d.get("v")),
                "trades": d.get("n"),
            })
        if not candles:
            return make_error(tool=tool, symbol=symbol, source="hyperliquid",
                              code="EMPTY_DATA", message=f"no candles for {coin} {iv}")
        return {"source": "hyperliquid", "coin": coin, "interval": iv,
                "candles": candles[-limit:], "count": len(candles)}

    async def get_overview(self, top_n: int = 20) -> dict:
        """全 HL 永續市場一覽：依 OI(USD) 排序的前 N 幣，外加 funding 極端值。
        一次 metaAndAssetCtxs 涵蓋全部永續，零額外呼叫。
        """
        tool = "mi_get_hl_overview"
        top_n = max(1, min(int(top_n), 50))
        mc = await self._meta_ctxs(tool=tool)
        if mc.get("error"):
            return mc
        rows = []
        for coin, node in mc["by_coin"].items():
            s = self._shape_market(coin, node["meta"], node["ctx"])
            if s["open_interest_usd"] is None:
                continue
            rows.append(s)
        if not rows:
            return make_error(tool=tool, symbol=None, source="hyperliquid",
                              code="EMPTY_DATA", message="no perp contexts")
        by_oi = sorted(rows, key=lambda x: x["open_interest_usd"], reverse=True)
        funded = [r for r in rows if r["funding_8h_pct"] is not None]
        hottest = sorted(funded, key=lambda x: x["funding_8h_pct"], reverse=True)[:5]
        coldest = sorted(funded, key=lambda x: x["funding_8h_pct"])[:5]
        total_oi = round(sum(r["open_interest_usd"] for r in rows), 2)
        total_vol = round(sum((r["day_notional_volume_usd"] or 0) for r in rows), 2)
        return {
            "source": "hyperliquid",
            "perp_count": len(rows),
            "total_open_interest_usd": total_oi,
            "total_day_volume_usd": total_vol,
            "top_by_oi": by_oi[:top_n],
            "funding_hottest": hottest,   # 多頭過熱（多殺多風險）
            "funding_coldest": coldest,   # 空方付錢（軋空燃料）
            "ts": int(time.time() * 1000),
        }

    async def health(self) -> dict:
        mc = await self._meta_ctxs(tool="mi_get_hl_overview")
        if mc.get("error"):
            return {"ok": False, "source": "hyperliquid", "details": mc}
        return {"ok": True, "source": "hyperliquid", "perp_count": mc.get("count", 0)}
