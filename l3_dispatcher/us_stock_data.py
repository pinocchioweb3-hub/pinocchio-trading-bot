"""美股永續數據層（v17）：OKX 公開端點 → MarketSnapshot（us_breakout 欄位）。

實測事實（2026-06-11）：
    - K 線 /market/candles 24/7 有數據，confirm=1 = 已收盤
    - funding /public/funding-rate 8h 結算（00/08/16 UTC）
    - OI 歷史 /rubik/stat/contracts/open-interest-history [ts, oi, oiCcy, oiUsd]
    - taker /rubik/stat/contracts/taker-volume-contract [ts, sellVol, buyVol]
"""
from __future__ import annotations

import asyncio
import datetime as dt
from statistics import mean
from zoneinfo import ZoneInfo

import httpx

from l2_trigger.types import MarketSnapshot

OKX = "https://www.okx.com"

# FIRE 白名單：高流動性 + 與 BTC 低相關（排除加密概念股與 Pre-IPO 合成）
US_FIRE_WHITELIST = ["MU", "SNDK", "SOXL", "MRVL", "NVDA", "INTC", "ORCL", "QQQ"]

_NY = ZoneInfo("America/New_York")
_sem = asyncio.Semaphore(4)


def us_session_now(now: dt.datetime | None = None) -> str:
    """美股時段：rth（現金盤）/ ext（延長）/ wkd（週末）/ off（平日夜間）。自動處理夏令。
    v32：週末獨立為 wkd（OKX 美股永續 24/7 有真實波動，與平日深夜 off 區分，便於分時段分析）。"""
    now_ny = (now or dt.datetime.now(dt.timezone.utc)).astimezone(_NY)
    if now_ny.weekday() >= 5:  # 週六日
        return "wkd"
    t = now_ny.hour * 60 + now_ny.minute
    if 9 * 60 + 30 <= t < 16 * 60:
        return "rth"
    if 4 * 60 <= t < 9 * 60 + 30 or 16 * 60 <= t < 20 * 60:
        return "ext"
    return "off"


def compute_atr_pct(bars: list[dict], period: int = 14) -> float | None:
    """標準 ATR(period) / 最新 close × 100"""
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = mean(trs[-period:])
    close = bars[-1]["close"]
    return atr / close * 100 if close else None


def detect_breakout(bars: list[dict]) -> tuple[str, float | None, float | None]:
    """最後一根已收盤 1h K 是否突破前 24 根高/低。回 (dir, level, vol_mult)。"""
    confirmed = [b for b in bars if b.get("confirm", True)]
    if len(confirmed) < 25:
        return "none", None, None
    cur = confirmed[-1]
    window = confirmed[-25:-1]  # 前 24 根
    vols = [w.get("volume_usd") or 0 for w in window]
    avg_vol = mean(vols) if vols else 0
    vol_mult = (cur.get("volume_usd") or 0) / avg_vol if avg_vol > 0 else None

    hi = max(w["high"] for w in window)
    lo = min(w["low"] for w in window)
    if cur["close"] > hi:
        return "bull", hi, vol_mult
    if cur["close"] < lo:
        return "bear", lo, vol_mult
    return "none", None, vol_mult


async def _get(client: httpx.AsyncClient, path: str, params: dict) -> list:
    async with _sem:
        r = await client.get(f"{OKX}{path}", params=params)
        r.raise_for_status()
        body = r.json()
        if body.get("code") != "0":
            raise RuntimeError(f"OKX {path}: {body.get('msg')}")
        return body.get("data", [])


async def fetch_funding(client: httpx.AsyncClient, sym: str) -> float | None:
    try:
        data = await _get(client, "/api/v5/public/funding-rate",
                          {"instId": f"{sym}-USDT-SWAP"})
        return float(data[0]["fundingRate"]) if data else None
    except Exception:
        return None


async def fetch_oi_delta_24h(client: httpx.AsyncClient, sym: str) -> float | None:
    try:
        data = await _get(client, "/api/v5/rubik/stat/contracts/open-interest-history",
                          {"instId": f"{sym}-USDT-SWAP", "period": "1H", "limit": "25"})
        if len(data) < 2:
            return None
        # 回傳新→舊；oiUsd 是第 4 欄
        newest, oldest = float(data[0][3]), float(data[-1][3])
        return (newest / oldest - 1) * 100 if oldest > 0 else None
    except Exception:
        return None


async def fetch_taker_ratio_4h(client: httpx.AsyncClient, sym: str) -> float | None:
    try:
        data = await _get(client, "/api/v5/rubik/stat/contracts/taker-volume-contract",
                          {"instId": f"{sym}-USDT-SWAP", "period": "1H", "limit": "4"})
        if not data:
            return None
        sell = sum(float(r[1]) for r in data)
        buy = sum(float(r[2]) for r in data)
        return buy / sell if sell > 0 else None
    except Exception:
        return None


async def fetch_qqq_chg_24h(okx_candles) -> float | None:
    """QQQ 永續 24h 變化%（大盤閘輸入）"""
    try:
        d = await okx_candles.get_candles("QQQ", "1h", 25)
        bars = d.get("candles") if isinstance(d, dict) else None
        if not bars or len(bars) < 25:
            return None
        return (bars[-1]["close"] / bars[0]["close"] - 1) * 100
    except Exception:
        return None


async def build_us_snapshot(sym: str, qqq_chg: float | None,
                            okx_candles, client: httpx.AsyncClient) -> MarketSnapshot | None:
    """組一檔美股永續的 snapshot。K 線失敗回 None；其他欄位失敗 = None（STALE）。"""
    d = await okx_candles.get_candles(sym, "1h", 26)
    bars = d.get("candles") if isinstance(d, dict) else None
    if not bars:
        return None

    funding, oi_delta, taker = await asyncio.gather(
        fetch_funding(client, sym),
        fetch_oi_delta_24h(client, sym),
        fetch_taker_ratio_4h(client, sym),
    )
    direction, level, vol_mult = detect_breakout(bars)
    confirmed = [b for b in bars if b.get("confirm", True)]
    cur = confirmed[-1] if confirmed else bars[-1]

    return MarketSnapshot(
        symbol=sym, ts=cur["ts"], price=cur["close"], tf="1h",
        funding=funding, oi_delta_pct=oi_delta,
        us_breakout_dir=direction, us_break_level=level,
        us_vol_mult=vol_mult, us_taker_ratio=taker,
        us_session=us_session_now(),
        qqq_chg_24h_pct=qqq_chg,
        atr_1h_pct=compute_atr_pct(bars),
        sources_used=("okx",),
    )


if __name__ == "__main__":
    async def selftest():
        from market_intel_mcp.sources.okx_candles import OkxCandlesSource
        okx = OkxCandlesSource()
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                qqq = await fetch_qqq_chg_24h(okx)
                print(f"session={us_session_now()}  QQQ 24h: {qqq:+.2f}%" if qqq else f"session={us_session_now()}  QQQ: N/A")
                for sym in US_FIRE_WHITELIST:
                    s = await build_us_snapshot(sym, qqq, okx, client)
                    if not s:
                        print(f"  {sym}: no data")
                        continue
                    print(f"  {sym:5s} ${s.price:>9,.2f} breakout={s.us_breakout_dir:4s} "
                          f"vol_mult={s.us_vol_mult and round(s.us_vol_mult,2)} "
                          f"funding={s.funding} taker={s.us_taker_ratio and round(s.us_taker_ratio,2)} "
                          f"oi24h={s.oi_delta_pct and round(s.oi_delta_pct,1)}% atr={s.atr_1h_pct and round(s.atr_1h_pct,2)}%")
        finally:
            await okx.close()
    asyncio.run(selftest())
