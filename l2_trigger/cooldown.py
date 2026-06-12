"""FIRE 訊號去重 / 冷卻 (dispatcher 端使用)。

L2 引擎本身純函式、stateless。冷卻邏輯放這個獨立模組由 dispatcher 用，
這樣回放工具直接吃 evaluate() 結果不被去重干擾。

用法（在 L3 dispatcher 中）：
    store = CooldownStore(cooldown_seconds=3600)
    if store.should_emit(decision):
        push_to_l3(decision)
        store.mark_fired(decision)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .types import TriggerAction, TriggerDecision


@dataclass
class CooldownStore:
    cooldown_seconds: int = 3600          # 同 (symbol, direction, setup) 至少間隔
    _last_fired: dict[tuple[str, str, str], float] = field(default_factory=dict)

    def _key(self, d: TriggerDecision) -> tuple[str, str, str]:
        return (d.snapshot.symbol, d.direction.value, d.setup_name)

    def should_emit(self, d: TriggerDecision, now: float | None = None) -> bool:
        if d.action != TriggerAction.FIRE:
            return False
        ts = now if now is not None else time.time()
        last = self._last_fired.get(self._key(d))
        if last is None:
            return True  # 從沒 fire 過 → 直接允許
        return (ts - last) >= self.cooldown_seconds

    def mark_fired(self, d: TriggerDecision, now: float | None = None) -> None:
        if d.action != TriggerAction.FIRE:
            return
        ts = now if now is not None else time.time()
        self._last_fired[self._key(d)] = ts

    def reset(self, symbol: str | None = None) -> None:
        if symbol is None:
            self._last_fired.clear()
            return
        for k in [k for k in self._last_fired if k[0] == symbol]:
            del self._last_fired[k]

    def seconds_until_next(self, d: TriggerDecision, now: float | None = None) -> float:
        """回傳離下次可發還剩多少秒（已可發 → 0）。"""
        ts = now if now is not None else time.time()
        last = self._last_fired.get(self._key(d))
        if last is None:
            return 0.0
        return max(0.0, self.cooldown_seconds - (ts - last))
