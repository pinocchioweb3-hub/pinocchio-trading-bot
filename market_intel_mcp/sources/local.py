"""LocalSource：TimescaleDB client（v0 為 stub，Task 10 啟用）。

負責的領域：
    CVD（從 trade 明細 agg）
    BTC 閘（從 1m K 線算 4h 200MA）
    白名單 view（mi_query_view 的後端）
"""
from __future__ import annotations

from ..errors import make_error
from ..settings import SETTINGS
from .base import RatioType


class LocalSource:
    name = "local"

    def __init__(self):
        self.dsn = SETTINGS.db_dsn

    def _not_ready(self, tool: str, symbol: str | None = None) -> dict:
        return make_error(
            tool=tool, symbol=symbol, source="local",
            code="BACKEND_NOT_READY",
            message="TimescaleDB not configured (DB_DSN env missing or stub).",
            suggestion="Complete Task 10: docker compose up timescaledb, run db_schema.sql.",
        )

    async def get_positioning(self, symbol, ratio_type, window, limit) -> dict:
        # local 不提供 positioning（這是 CoinGlass/OKX 的）
        return make_error(
            tool="mi_get_positioning", symbol=symbol, source="local",
            code="WRONG_BACKEND",
            message="positioning is exchange-side data, not local.",
            suggestion="Use backend=coinglass.",
        )

    async def get_oi(self, symbol, window, limit) -> dict:
        return self._not_ready("mi_get_oi", symbol)

    async def get_funding(self, symbol) -> dict:
        return self._not_ready("mi_get_funding", symbol)

    async def get_liquidations(self, symbol, window) -> dict:
        return self._not_ready("mi_get_liquidations", symbol)

    async def get_cvd_series(self, symbol, window, limit) -> dict:
        return self._not_ready("mi_get_cvd", symbol)

    async def get_price_series(self, symbol, tf, limit) -> dict:
        return self._not_ready("mi_get_price_series", symbol)

    async def get_btc_gate(self) -> dict:
        return self._not_ready("mi_get_btc_gate")

    async def get_strength_universe(self, limit) -> dict:
        return self._not_ready("mi_get_strength_rank")

    async def get_structure(self, symbol) -> dict:
        return self._not_ready("mi_get_structure", symbol)

    async def health(self) -> dict:
        if not self.dsn:
            return {"ok": False, "source": "local", "details": "DB_DSN missing"}
        return {"ok": True, "source": "local", "details": "stub; real check in Task 10"}
