"""數據來源層：BaseSource Protocol + 各家實作。

切換規則（在 settings.SETTINGS.backend）：
    mock      → MockSource（v0 預設）
    coinglass → CoinGlassSource（Task 9）
    local     → LocalSource（Task 10）

（曾規劃 "auto" 組合來源，但 CompositeSource 從未實作；單一缺料降級已由
 engine 層處理（STALE→HOLD/不計票），故移除該死路，避免選到它就 ImportError。）
"""
from __future__ import annotations

from ..settings import SETTINGS
from .base import BaseSource


def get_source() -> BaseSource:
    """根據 settings 回傳對應 source 實例。模組級單例，server 啟動時呼叫一次。"""
    backend = SETTINGS.backend
    if backend == "mock":
        from .mock import MockSource
        return MockSource()
    if backend == "coinglass":
        from .coinglass import CoinGlassSource
        return CoinGlassSource()
    if backend == "local":
        from .local import LocalSource
        return LocalSource()
    raise ValueError(f"unknown backend: {backend}")
