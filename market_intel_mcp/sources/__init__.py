"""數據來源層：BaseSource Protocol + 各家實作。

切換規則（在 settings.SETTINGS.backend）：
    mock      → MockSource（v0 預設）
    coinglass → CoinGlassSource（Task 9）
    local     → LocalSource（Task 10）
    auto      → 組合來源；單一缺料降級而不整包失敗
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
    if backend == "auto":
        from .composite import CompositeSource
        return CompositeSource()
    raise ValueError(f"unknown backend: {backend}")
