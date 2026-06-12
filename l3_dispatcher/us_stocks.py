"""US Stock Pulse Worker（v16）：美股主題不再空白。

每日兩次推 OKX 美股永續行情總覽到 🇺🇸 美股主題：
    - 開盤前瞻：13:25 UTC（美股開盤 09:30 ET 前 5 分）
    - 收盤總結：20:05 UTC（收盤 16:00 ET 後 5 分）

數據源：OKX 公開 tickers API（不需 key），追蹤高流動性美股永續白名單。
未來美股 FIRE 報單功能會基於同一份數據。
"""
from __future__ import annotations

import asyncio
import datetime as dt

import httpx

# 高流動性白名單（依 2026-06 實測 24h 成交量排序）
US_STOCK_WATCHLIST = [
    # 半導體/AI（當前最熱主題）
    "MU", "SNDK", "MRVL", "SOXL", "INTC", "NVDA", "AMD", "TSM", "ARM", "AVGO",
    # 加密概念股
    "CRCL", "MSTR", "COIN", "HOOD",
    # 大盤/科技巨頭
    "QQQ", "SPY", "TSLA", "AAPL", "MSFT", "GOOGL", "META", "PLTR", "ORCL",
    # Pre-IPO 合成
    "OPENAI", "ANTHROPIC",
]

OKX_TICKERS_URL = "https://www.okx.com/api/v5/market/tickers?instType=SWAP"


async def fetch_us_stock_tickers() -> list[dict]:
    """抓 OKX 全 SWAP tickers，過濾出美股白名單。回 [{sym, last, chg_pct, vol_usd}]"""
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(OKX_TICKERS_URL)
        r.raise_for_status()
        data = r.json().get("data", [])

    watch = set(US_STOCK_WATCHLIST)
    out = []
    for t in data:
        inst = t.get("instId", "")
        base = inst.split("-")[0]
        if base not in watch or not inst.endswith("-USDT-SWAP"):
            continue
        try:
            last = float(t.get("last") or 0)
            o24 = float(t.get("open24h") or 0)
            vol_usd = float(t.get("volCcy24h") or 0) * last
            chg = (last / o24 - 1) * 100 if o24 else 0.0
            out.append({"sym": base, "last": last, "chg_pct": chg, "vol_usd": vol_usd})
        except (TypeError, ValueError):
            continue
    return out


def _render_pulse(rows: list[dict], session_label: str) -> str:
    """渲染美股永續行情總覽"""
    if not rows:
        return ""
    by_chg = sorted(rows, key=lambda r: -abs(r["chg_pct"]))
    by_vol = sorted(rows, key=lambda r: -r["vol_usd"])

    def fmt(r):
        icon = "🟢" if r["chg_pct"] >= 0 else "🔴"
        vol_m = r["vol_usd"] / 1e6
        return (f"{icon} <b>{r['sym']:6s}</b> <code>${r['last']:,.2f}</code> "
                f"<code>{r['chg_pct']:+.2f}%</code>（量 ${vol_m:,.0f}M）")

    lines = [
        f"🇺🇸 <b>美股永續行情 — {session_label}</b>",
        f"━━━━━━━━━━━━━━━━",
        f"<b>波動榜（24h 變化最大）</b>",
    ]
    lines += [f"  {fmt(r)}" for r in by_chg[:8]]
    lines.append("")
    lines.append("<b>成交量榜</b>")
    lines += [f"  {fmt(r)}" for r in by_vol[:5]]

    # 加密概念股特別欄（與 crypto 訊號相關性高）
    crypto_stocks = [r for r in rows if r["sym"] in ("MSTR", "COIN", "HOOD", "CRCL")]
    if crypto_stocks:
        lines.append("")
        lines.append("<b>加密概念股</b>")
        lines += [f"  {fmt(r)}" for r in
                  sorted(crypto_stocks, key=lambda r: -abs(r["chg_pct"]))]

    lines.append("")
    lines.append(f"<i>數據：OKX 美股永續（{len(rows)} 檔監控中）｜"
                 f"美股報單功能開發中</i>")
    return "\n".join(lines)


def _next_run_seconds(targets_utc: list[tuple[int, int]]) -> tuple[float, str]:
    """算到下一個目標 (hour, minute) UTC 的秒數。回 (秒數, 標籤)"""
    now = dt.datetime.now(tz=dt.timezone.utc)
    candidates = []
    for h, m in targets_utc:
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if t <= now:
            t += dt.timedelta(days=1)
        candidates.append(t)
    nxt = min(candidates)
    label = "開盤前瞻" if nxt.hour == 13 else "收盤總結"
    return (nxt - now).total_seconds(), label


async def run_us_stock_pulse_loop(tg, run_on_startup: bool = True):
    """Worker：每日 13:25 / 20:05 UTC 推美股行情（週末自動跳過低量日仍照推，
    因為 OKX 股票永續週末也交易 — 週末波動反而是 gap 風險訊號）。"""
    print("[us_stocks] starting loop (13:25 + 20:05 UTC daily)")

    async def _push(label: str):
        try:
            rows = await fetch_us_stock_tickers()
            if not rows:
                print("[us_stocks] no ticker data")
                return
            text = _render_pulse(rows, label)
            r = await tg.send_message(text, parse_mode="HTML")
            print(f"[us_stocks] {label} sent ok={r.get('ok')} ({len(rows)} symbols)")
        except Exception as e:
            print(f"[us_stocks] push error: {type(e).__name__}: {e}")

    if run_on_startup:
        await asyncio.sleep(120)
        await _push("即時快照")

    while True:
        wait, label = _next_run_seconds([(13, 25), (20, 5)])
        print(f"[us_stocks] next '{label}' in {wait/3600:.1f}h")
        await asyncio.sleep(wait)
        await _push(label)


if __name__ == "__main__":
    async def selftest():
        rows = await fetch_us_stock_tickers()
        print(f"got {len(rows)} symbols")
        print(_render_pulse(rows, "自測")[:800])
    asyncio.run(selftest())
