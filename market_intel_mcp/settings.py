"""執行期設定。透過環境變數調控，預設值對開發友善。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    backend: str            # "mock" | "coinglass" | "local"
    coinglass_api_key: str | None
    db_dsn: str | None
    http_timeout_sec: float
    rate_limit_per_min: int

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            backend=os.getenv("MARKET_INTEL_BACKEND", "mock").lower(),
            coinglass_api_key=os.getenv("COINGLASS_API_KEY") or None,
            db_dsn=os.getenv("DB_DSN") or None,
            http_timeout_sec=float(os.getenv("HTTP_TIMEOUT_SEC", "10")),
            rate_limit_per_min=int(os.getenv("RATE_LIMIT_PER_MIN", "75")),
        )


SETTINGS = Settings.load()
