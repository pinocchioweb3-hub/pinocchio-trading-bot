"""長週期歷史 OHLC 資料載入器（v33，路線 D #3）。

解鎖長回測：OKX 公開 K 線只給 300 根（1h≈12.5 天），不足以驗證真實期望值。
Binance USDⓈ-M 永續 fapi /fapi/v1/klines 支援 startTime/endTime 分頁（limit 1500、
免 API key、2400 weight/min），可拉年級歷史（含加密與美股永續 NVDAUSDT 等）。

設計：
    - 本地 SQLite 快取（ohlc_cache.db），unique(symbol,tf,ts) 去重，重跑不重抓。
    - get_ohlc(symbol, tf, days) → 升序 OHLC dict 清單（與 backtest 既有格式相容）。
    - 缺口自動向 Binance 補；Binance 失敗 fallback OKX 公開端點（短歷史）。
    - 絕不放 API key（公開端點不需要，守安全紅線）。

symbol 用 canonical（BTC / NVDA…）；內部轉 BTCUSDT。
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import httpx

from botpaths import db_path as _db_path
from market_intel_mcp.symbol_mapping import normalize

DB_PATH = _db_path("ohlc_cache.db")
FAPI = "https://fapi.binance.com"

# 我們的時框 → Binance interval（毫秒）
_TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
          "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
          "12h": 43_200_000, "1d": 86_400_000}
_TF_BN = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h",
          "2h": "2h", "4h": "4h", "12h": "12h", "1d": "1d"}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=8000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_schema() -> None:
    conn = _conn()
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS ohlc (
                symbol TEXT NOT NULL, tf TEXT NOT NULL, ts INTEGER NOT NULL,
                open REAL, high REAL, low REAL, close REAL, volume REAL,
                volume_usd REAL,
                PRIMARY KEY (symbol, tf, ts))""")
        conn.commit()
    finally:
        conn.close()


async def _fetch_klines_page(client: httpx.AsyncClient, sym: str, interval: str,
                             start_ms: int, end_ms: int) -> list[list]:
    r = await client.get(f"{FAPI}/fapi/v1/klines", params={
        "symbol": sym, "interval": interval, "startTime": start_ms,
        "endTime": end_ms, "limit": 1500})
    if r.status_code != 200:
        raise RuntimeError(f"binance klines HTTP {r.status_code}: {r.text[:120]}")
    return r.json()


async def _backfill_binance(symbol: str, tf: str, start_ms: int, end_ms: int) -> int:
    """從 Binance fapi 分頁拉 [start,end] 並寫快取。回新增筆數。"""
    sym = f"{normalize(symbol)}USDT"
    bn = _TF_BN.get(tf)
    step = _TF_MS.get(tf)
    if not bn or not step:
        raise ValueError(f"unsupported tf {tf}")
    _ensure_schema()
    rows_total = 0
    cur = start_ms
    async with httpx.AsyncClient(timeout=30) as client:
        while cur < end_ms:
            data = await _fetch_klines_page(client, sym, bn, cur, end_ms)
            if not data:
                break
            conn = _conn()
            try:
                conn.executemany(
                    "INSERT OR IGNORE INTO ohlc(symbol,tf,ts,open,high,low,close,volume,volume_usd) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    [(symbol, tf, int(k[0]), float(k[1]), float(k[2]), float(k[3]),
                      float(k[4]), float(k[5]), float(k[7])) for k in data])
                conn.commit()
                rows_total += conn.total_changes
            finally:
                conn.close()
            last_ts = int(data[-1][0])
            if last_ts <= cur:
                break
            cur = last_ts + step
            await asyncio.sleep(0.25)   # 禮貌節流，遠在 2400 weight/min 內
    return rows_total


def _read_cache(symbol: str, tf: str, start_ms: int, end_ms: int) -> list[dict]:
    _ensure_schema()
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT ts,open,high,low,close,volume,volume_usd FROM ohlc "
            "WHERE symbol=? AND tf=? AND ts>=? AND ts<=? ORDER BY ts",
            (symbol, tf, start_ms, end_ms)).fetchall()
    finally:
        conn.close()
    return [{"ts": r[0], "open": r[1], "high": r[2], "low": r[3],
             "close": r[4], "volume": r[5], "volume_usd": r[6], "confirm": True}
            for r in rows]


async def get_ohlc(symbol: str, tf: str = "1h", days: int = 365,
                   end_ms: int | None = None, force_refresh: bool = False) -> list[dict]:
    """取 [now-days, now] 的 OHLC（升序）。先讀快取，缺口向 Binance 補。
    Binance 失敗時 fallback OKX 公開端點（僅短歷史）。"""
    step = _TF_MS.get(tf)
    if not step:
        raise ValueError(f"unsupported tf {tf}")
    end_ms = end_ms or int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000
    expected = (end_ms - start_ms) // step

    cached = _read_cache(symbol, tf, start_ms, end_ms)
    # 快取覆蓋率夠（>95%）就直接用，否則向 Binance 補
    if not force_refresh and expected and len(cached) >= expected * 0.95:
        return cached
    try:
        await _backfill_binance(symbol, tf, start_ms, end_ms)
        return _read_cache(symbol, tf, start_ms, end_ms)
    except Exception as e:
        print(f"[data_loader] binance backfill failed for {symbol} {tf}: "
              f"{type(e).__name__}: {e}; fallback OKX(短歷史)")
        if cached:
            return cached
        try:
            from market_intel_mcp.sources.okx_candles import get_okx_candles
            okx = get_okx_candles()
            try:
                d = await okx.get_candles(symbol, tf, 300)
            finally:
                await okx.close()
            return d.get("candles", []) if isinstance(d, dict) else []
        except Exception:
            return []


def cache_stats() -> dict:
    _ensure_schema()
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT symbol, tf, COUNT(*), MIN(ts), MAX(ts) FROM ohlc "
            "GROUP BY symbol, tf ORDER BY symbol, tf").fetchall()
    finally:
        conn.close()
    return {"series": [{"symbol": r[0], "tf": r[1], "bars": r[2],
                        "from": r[3], "to": r[4]} for r in rows]}


if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "BTC"
    tf = sys.argv[2] if len(sys.argv) > 2 else "1h"
    days = int(sys.argv[3]) if len(sys.argv) > 3 else 60

    async def t():
        import datetime as dt
        c = await get_ohlc(sym, tf, days)
        if c:
            f = dt.datetime.fromtimestamp(c[0]["ts"] / 1000, dt.timezone.utc)
            l = dt.datetime.fromtimestamp(c[-1]["ts"] / 1000, dt.timezone.utc)
            print(f"{sym} {tf} {days}d: {len(c)} bars, {f:%Y-%m-%d} → {l:%Y-%m-%d}, "
                  f"last close={c[-1]['close']}")
        else:
            print(f"{sym} {tf}: no data")
        print("cache:", cache_stats())
    asyncio.run(t())
