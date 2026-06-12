"""BaseSource Protocol：所有後端必須實作的介面。

所有方法 async；缺料用回傳 dict 內的 `error` 欄位表示，禁止 raise。
單位約定：
    ts: epoch ms (int)
    price/oi/cvd: float USD
    funding: float (小數，0.0001 = 0.01%)
    ls_ratio/top_trader_ratio: float (>1 偏多)
"""
from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

RatioType = Literal["account", "position", "top_trader_account", "top_trader_position"]


@runtime_checkable
class BaseSource(Protocol):
    """所有 source 實作此介面。Mock / CoinGlass / Local / Composite 都符合。"""

    name: str  # "mock" | "coinglass" | "local" | "composite"

    async def get_positioning(
        self, symbol: str, ratio_type: RatioType, window: str, limit: int,
    ) -> dict: ...

    async def get_oi(self, symbol: str, window: str, limit: int) -> dict: ...

    async def get_funding(self, symbol: str) -> dict: ...

    async def get_liquidations(self, symbol: str, window: str) -> dict: ...

    async def get_cvd_series(self, symbol: str, window: str, limit: int) -> dict: ...

    async def get_price_series(self, symbol: str, tf: str, limit: int) -> dict: ...

    async def get_btc_gate(self) -> dict: ...

    async def get_strength_universe(self, limit: int,
                                    candidate_symbols: list[str] | None = None) -> dict:
        """回傳候選池 + 每幣的強度排行原始指標。
        candidate_symbols 為 None 時，source 自選預設名單。
        """
        ...

    async def get_structure(self, symbol: str) -> dict:
        """Setup B 用的 7d 結構：atr_pct_7d, vol_24h_vs_30d, cvd_slope_7d,
        top_trader_slope_7d, oi_delta_7d_pct, higher_lows_7d。
        Mock 用預設；real 實作會由其他時序方法 derive。
        """
        ...

    async def health(self) -> dict:
        """回 {ok: bool, details: ...}，給 mi_get_snapshot 看誰活著"""
        ...
