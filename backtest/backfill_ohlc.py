"""年級 OHLC 回填工具（Session B，純價格層）。

問題：ohlc_cache.db 目前只有 BTC/ETH/SOL 有年級 K（3 年 4h / 2 年 1h / 3.2 年 1d，
Binance 免費），其餘 9 檔（ADA/AVAX/BNB/DOGE/DOT/LINK/LTC/SUI/XRP）只有 ~120 天。
這讓「跨年純價格層回測」與「跨年歷史類比」對這 9 檔物理上做不到。

本工具用既有的 data_loader（Binance USDⓈ-M fapi 公開端點、免 API key、startTime/endTime
分頁）把這 9 檔的「年級 4h + 1d」補進同一個 ohlc_cache.db。1h 級因為一年≈8760 根、佔空間
大且年級類比/回測多用 4h/1d，預設不補 1h（可用 --tf 顯式指定）。

⚠️ 純價格層而已：這裡只回填 OHLCV。CoinGlass 綜合指標（OI/CVD/funding/多空比）每個
history 端點硬卡 500 根且 present-anchored、無時間分頁 → 跨年「綜合指標」歷史物理上做不到，
本工具不碰、也不假裝有。跨年只有純價格層誠實可行。

安全紅線：
    - 純讀外部公開端點 + 寫本機 SQLite 快取；不碰任何下單/交易所寫入 API。
    - 不放 API key（公開端點不需要）。
    - 走既有 data_loader.get_ohlc(force_refresh) → 缺口自動分頁補、去重、可重跑。

用法：
    python -m backtest.backfill_ohlc                 # 預設：9 檔 × {4h,1d} × 730 天
    python -m backtest.backfill_ohlc --days 1095     # 拉 3 年
    python -m backtest.backfill_ohlc --symbols ADA XRP --tf 4h
    python -m backtest.backfill_ohlc --all --tf 4h 1d   # 含 BTC/ETH/SOL 一起刷新
    python -m backtest.backfill_ohlc --stats         # 只看現有快取覆蓋，不抓
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt

from .data_loader import cache_stats, get_ohlc

# 目前只有 ~120 天歷史、需要補到年級的 9 檔（BTC/ETH/SOL 已有年級，預設不重抓）。
SHORT_HISTORY_SYMBOLS = ["ADA", "AVAX", "BNB", "DOGE", "DOT", "LINK", "LTC", "SUI", "XRP"]
# 年級回測/類比常用時框（1h 量大、預設不補；要可用 --tf 1h 顯式加）。
DEFAULT_TFS = ["4h", "1d"]
DEFAULT_DAYS = 730   # 2 年；多數年級類比/回測夠用，也避免一次抓太久


def _fmt_ts(ms: int | None) -> str:
    if not ms:
        return "—"
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d")


async def backfill_one(symbol: str, tf: str, days: int) -> dict:
    """回填單一 symbol×tf 的年級 K。回 {symbol, tf, bars, from, to, ok}。

    走 data_loader.get_ohlc(force_refresh=True)：強制向 Binance 分頁補齊整個
    [now-days, now] 視窗（缺口去重、可重跑）。失敗時 data_loader 內部會 fallback
    OKX 短歷史並回傳，最終以實際快取覆蓋為準。"""
    try:
        bars = await get_ohlc(symbol, tf, days, force_refresh=True)
    except Exception as e:
        return {"symbol": symbol, "tf": tf, "bars": 0, "ok": False,
                "error": f"{type(e).__name__}: {e}"}
    first = bars[0]["ts"] if bars else None
    last = bars[-1]["ts"] if bars else None
    return {"symbol": symbol, "tf": tf, "bars": len(bars),
            "from": first, "to": last, "ok": bool(bars)}


async def backfill(symbols: list[str] | None = None,
                   tfs: list[str] | None = None,
                   days: int = DEFAULT_DAYS,
                   throttle_sec: float = 1.0) -> list[dict]:
    """回填多個 symbol×tf 的年級 K 進 ohlc_cache.db。回每組結果清單。

    參數預設＝SHORT_HISTORY_SYMBOLS × {4h,1d} × 730 天（補齊只有 120 天的 9 檔）。
    每組之間禮貌節流，避免 Binance weight 爆掉（data_loader 內部本身也有分頁節流）。"""
    syms = symbols or SHORT_HISTORY_SYMBOLS
    use_tfs = tfs or DEFAULT_TFS
    results: list[dict] = []
    for sym in syms:
        for tf in use_tfs:
            r = await backfill_one(sym, tf, days)
            results.append(r)
            status = "OK" if r["ok"] else "FAIL"
            print(f"  [{status}] {sym:6} {tf:3} → {r['bars']:6d} 根 "
                  f"({_fmt_ts(r.get('from'))} → {_fmt_ts(r.get('to'))})"
                  + (f"  {r.get('error')}" if r.get("error") else ""))
            await asyncio.sleep(throttle_sec)
    return results


def print_cache_coverage() -> None:
    """印出目前 ohlc_cache.db 各 symbol×tf 的覆蓋（根數 + 起訖日）。"""
    stats = cache_stats()
    print("─" * 60)
    print(f"{'symbol':8}{'tf':5}{'bars':>8}  {'from':12}{'to':12}")
    print("─" * 60)
    for s in stats.get("series", []):
        print(f"{s['symbol']:8}{s['tf']:5}{s['bars']:>8}  "
              f"{_fmt_ts(s['from']):12}{_fmt_ts(s['to']):12}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="年級 OHLC 回填（純價格層；不碰 CoinGlass 綜合指標）")
    p.add_argument("--symbols", nargs="*", default=None,
                   help="要補的 canonical symbol（預設＝只有 120 天的 9 檔）")
    p.add_argument("--all", action="store_true",
                   help="含 BTC/ETH/SOL 一起刷新（預設只補短歷史 9 檔）")
    p.add_argument("--tf", nargs="*", dest="tfs", default=None,
                   help="時框（預設 4h 1d；可加 1h，量大慎用）")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS,
                   help=f"回填天數（預設 {DEFAULT_DAYS}）")
    p.add_argument("--stats", action="store_true",
                   help="只印現有快取覆蓋，不抓資料")
    return p.parse_args(argv)


async def _amain(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.stats:
        print_cache_coverage()
        return
    syms = args.symbols
    if syms is None:
        syms = (["BTC", "ETH", "SOL"] + SHORT_HISTORY_SYMBOLS) if args.all \
            else SHORT_HISTORY_SYMBOLS
    print("=" * 60)
    print(f"  年級 OHLC 回填（純價格層）｜{len(syms)} 檔 × {args.tfs or DEFAULT_TFS}"
          f" × {args.days} 天")
    print("  ⚠️ 只補 OHLCV；CoinGlass 綜合指標跨年做不到，不在此回填範圍")
    print("=" * 60)
    results = await backfill(syms, args.tfs, args.days)
    ok = sum(1 for r in results if r["ok"])
    print("=" * 60)
    print(f"  完成：{ok}/{len(results)} 組成功")
    print()
    print_cache_coverage()


if __name__ == "__main__":
    asyncio.run(_amain())
