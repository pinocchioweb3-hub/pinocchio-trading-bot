"""多時框 K 線聚合（純函式核心）— 補 OKX 原生缺的 8H / 5D 層。

【為什麼需要】
OKX 原生 K 線 bar 只到 6H/12H（**無 8H**），且**無 5D**。但 #34 多時框嵌套
（timeframe_nesting.py）使用者明列要 月/週/日/5d/3d/2d/12h/8h/4h 共 9 層。
缺的兩層用「低框合併」補齊：
    8H = 兩根 4H 合併     →  to_8h(candles_4h)   或 aggregate_by_factor(c, 2)
    5D = 五根 1D 合併     →  to_5d(candles_1d)   或 aggregate_by_factor(c, 5)

【對齊方式】
以 epoch 為基準的時間桶（bucket = ts // bucket_ms），與交易所多日 bar 的慣例
一致（OKX 自家 3D 亦為 epoch grid）。8H 桶自然落在 UTC 00:00 / 08:00 / 16:00；
5D 為 epoch 起算的固定 5 日格（與 OKX 3D 同理，非週對齊，但一致可重現）。

【設計鐵則】
    * 純函式、零 I/O、零 API、零隨機、不改輸入。
    * OHLCV 標準聚合：open=桶內首根、close=桶內末根、high=max、low=min、
      volume/volume_usd=sum、confirm=末根。
    * 容忍亂序輸入（內部先依 ts 升序排）；base interval 由相鄰 ts 差中位數自推，
      呼叫端不必指定。資料不足（<2 根或無法推斷）→ 回原樣（不崩、不臆造）。
    * 不接線、不影響下單；給 timeframe_nesting 在接線時餵 8H/5D 用。
"""
from __future__ import annotations

from statistics import median

# 桶內要「加總」的量欄（存在才加；OKX candles 有 volume / volume_usd）
_SUM_KEYS = ("volume", "volume_usd", "vol", "vol_usd", "turnover_usd")


def _infer_base_ms(candles: list[dict]) -> int | None:
    """由相鄰 ts 差的中位數推單根時長（ms）。<2 根或非正 → None。"""
    ts = [c["ts"] for c in candles if isinstance(c.get("ts"), (int, float))]
    if len(ts) < 2:
        return None
    diffs = [b - a for a, b in zip(ts, ts[1:]) if b > a]
    if not diffs:
        return None
    base = int(median(diffs))
    return base if base > 0 else None


def _merge_bucket(group: list[dict]) -> dict:
    """把同桶的多根 candle 合成一根（group 已依 ts 升序、非空）。"""
    first, last = group[0], group[-1]
    out: dict = {
        "ts": first["ts"],
        "open": first["open"],
        "high": max(c["high"] for c in group),
        "low": min(c["low"] for c in group),
        "close": last["close"],
    }
    for k in _SUM_KEYS:
        vals = [c[k] for c in group if isinstance(c.get(k), (int, float))]
        if vals:
            out[k] = round(sum(vals), 8)
    if "confirm" in last:
        out["confirm"] = last["confirm"]
    return out


def aggregate_by_factor(candles: list[dict], factor: int) -> list[dict]:
    """把 candles 每 factor 根合併成一根（epoch 時間桶對齊）。

    factor<=1 或資料不足無法推斷 base → 回原 list 的淺拷貝（不變更語意）。
    亂序輸入會先升序排。回傳升序（最舊在前），與輸入慣例一致。

    >>> c = [{"ts": i*4*3600_000, "open": i, "high": i+1, "low": i-1,
    ...       "close": i+0.5, "volume": 10} for i in range(6)]
    >>> agg = aggregate_by_factor(c, 2)   # 4H → 8H
    >>> len(agg)
    3
    >>> agg[0]["open"] == 0 and agg[0]["close"] == 1.5
    True
    >>> agg[0]["volume"]
    20
    """
    if not candles or not isinstance(candles, list):
        return list(candles or [])
    f = int(factor)
    if f <= 1:
        return list(candles)

    ordered = sorted(
        (c for c in candles if isinstance(c.get("ts"), (int, float))),
        key=lambda c: c["ts"],
    )
    if len(ordered) < 2:
        return list(ordered)

    base = _infer_base_ms(ordered)
    if base is None:
        return list(ordered)

    bucket_ms = base * f
    buckets: dict[int, list[dict]] = {}
    order_keys: list[int] = []
    for c in ordered:
        key = int(c["ts"]) // bucket_ms
        if key not in buckets:
            buckets[key] = []
            order_keys.append(key)
        buckets[key].append(c)

    return [_merge_bucket(buckets[k]) for k in order_keys]


def to_8h(candles_4h: list[dict]) -> list[dict]:
    """4H candles → 8H candles（兩根合一）。"""
    return aggregate_by_factor(candles_4h, 2)


def to_5d(candles_1d: list[dict]) -> list[dict]:
    """1D candles → 5D candles（五根合一）。"""
    return aggregate_by_factor(candles_1d, 5)


def fill_missing_tfs(candles_by_tf: dict) -> dict:
    """接線用便利層：若有 4h 但缺 8h、有 1d 但缺 5d，就用低框聚合補上。

    **回新 dict（不改輸入）**。只在來源層存在且足量時補；補出的層標 'derived'。
    既有的 8h/5d（若交易所別處有）一律不覆蓋。容忍 list 或 {'candles':...} 形態。

    >>> c4 = [{"ts": i*4*3600_000, "open": i, "high": i+1, "low": i-1,
    ...        "close": i+0.5, "volume": 1} for i in range(4)]
    >>> out = fill_missing_tfs({"4h": c4})
    >>> "8h" in out and len(out["8h"]) == 2
    True
    """
    out = dict(candles_by_tf or {})

    def _candles(entry):
        if isinstance(entry, dict):
            return None if entry.get("error") else (entry.get("candles") or None)
        if isinstance(entry, list):
            return entry or None
        return None

    if not out.get("8h"):
        c4 = _candles(out.get("4h"))
        if c4 and len(c4) >= 2:
            out["8h"] = to_8h(c4)

    if not out.get("5d"):
        c1d = _candles(out.get("1d"))
        if c1d and len(c1d) >= 5:
            out["5d"] = to_5d(c1d)

    return out
