# -*- coding: utf-8 -*-
"""bottom_feeds.py — 熊底儀表板資料抓取層（v111，全免金鑰、每日快取）。

來源（2026-07-03 全部活線驗證可用、零註冊）：
    Coin Metrics 公開 GitHub CSV（btc.csv, 2009→今；CapMVRVCur/CapMrktCurUSD/SplyCur/PriceUSD）
        → 自算 MVRV-Z（Z=(MC−RC)/σ(MC−RC) 全史）與已實現價（RC/Sply）。
        ⚠️ 該檔更新常滯後數週 → 用「RC 短期近似不變」把 MVRV 橋接到今日價（誠實標 asof）。
    FRED 公開 fredgraph.csv（免key）：DTWEXBGS（廣義美元指數，非 ICE DXY，卡片須標）＋ SP500。
    CoinGecko /global（免key）：BTC dominance 當日值（90 日方向靠本地逐日累積後才有）。
    alternative.me F&G（免key）：30 日均。
    DefiLlama stablecoincharts（免key）：穩定幣總市值 30 日動能（落後因子，只進 overlay）。

鐵則：唯讀公網 API、無入站面；快取 %BOT_DATA%/bottom_feeds_cache.json（各源每日至多一抓）；
    任何源失敗→該欄 None（compute_bottom_score 用 present_mass 誠實處理），永不臆測、永不阻塞。
"""
from __future__ import annotations

import csv
import io
import json
import os
import time
from typing import Optional

import httpx

from botpaths import data_dir

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
_CACHE = data_dir() / "bottom_feeds_cache.json"
_TTL_SEC = 20 * 3600          # 各源每日至多一抓（20h 容錯）
_CM_CSV = "https://raw.githubusercontent.com/coinmetrics/data/master/csv/btc.csv"
_FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
_CG_GLOBAL = "https://api.coingecko.com/api/v3/global"
_FNG = "https://api.alternative.me/fng/?limit=45"
_LLAMA = "https://stablecoins.llama.fi/stablecoincharts/all"


def _load_cache() -> dict:
    try:
        return json.loads(_CACHE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_cache(c: dict) -> None:
    try:
        _CACHE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _cached(key: str):
    c = _load_cache().get(key)
    if c and (time.time() - c.get("ts", 0)) < _TTL_SEC:
        return c.get("data")
    return None


def _put(key: str, data) -> None:
    c = _load_cache()
    c[key] = {"ts": time.time(), "data": data}
    _save_cache(c)


def fetch_coinmetrics_btc(price_now: Optional[float] = None) -> dict:
    """回 {mvrv_z, realized_price, mvrv, asof}；全缺→{}。

    自算：RC=MC/MVRV；Z=(MC−RC)/σ(MC−RC 全史)。橋接：CSV 滯後時假設 RC/Sply 近似不變
    （已實現市值以週為尺度緩變），用 price_now 重算今日 MC 與 Z——誠實近似、asof 註明。"""
    cached = _cached("coinmetrics")
    rows = cached
    if rows is None:
        try:
            r = httpx.get(_CM_CSV, headers=_UA, timeout=90, follow_redirects=True)
            if r.status_code != 200:
                return {}
            raw = list(csv.DictReader(io.StringIO(r.text)))
            rows = [
                {"t": x["time"][:10], "mc": float(x["CapMrktCurUSD"]),
                 "mvrv": float(x["CapMVRVCur"]), "sply": float(x["SplyCur"])}
                for x in raw
                if x.get("CapMrktCurUSD") and x.get("CapMVRVCur") and x.get("SplyCur")
            ]
            _put("coinmetrics", rows)
        except Exception:  # noqa: BLE001
            return {}
    if not rows:
        return {}
    diffs = [x["mc"] - (x["mc"] / x["mvrv"]) for x in rows if x["mvrv"] > 0]
    n = len(diffs)
    if n < 400:
        return {}
    mean = sum(diffs) / n
    sd = (sum((d - mean) ** 2 for d in diffs) / n) ** 0.5
    last = rows[-1]
    rc = last["mc"] / last["mvrv"]
    realized_price = rc / last["sply"] if last["sply"] > 0 else None
    if price_now and last["sply"] > 0:
        mc_now = price_now * last["sply"]       # 橋接：供給/RC 以 asof 值近似
        mvrv = mc_now / rc
        z = (mc_now - rc) / sd if sd > 0 else None
    else:
        mvrv = last["mvrv"]
        z = (last["mc"] - rc) / sd if sd > 0 else None
    return {"mvrv_z": round(z, 3) if z is not None else None,
            "realized_price": round(realized_price, 2) if realized_price else None,
            "mvrv": round(mvrv, 3), "asof": last["t"]}


def _fetch_fred_series(sid: str) -> list[tuple[str, float]]:
    cached = _cached(f"fred_{sid}")
    if cached is not None:
        return [tuple(x) for x in cached]
    try:
        r = httpx.get(_FRED.format(sid=sid), headers=_UA, timeout=45, follow_redirects=True)
        out = []
        for row in csv.reader(io.StringIO(r.text)):
            if len(row) == 2 and row[1] not in (".", "") and row[0][:2] == "20":
                try:
                    out.append((row[0], float(row[1])))
                except ValueError:
                    continue
        _put(f"fred_{sid}", out)
        return out
    except Exception:  # noqa: BLE001
        return []


def fetch_macro_background() -> dict:
    """DXY 3個月動能 + SPX vs 200日線——純背景註記（n≤2 不計分，對抗審查裁定）。"""
    out: dict = {}
    try:
        dxy = _fetch_fred_series("DTWEXBGS")
        if len(dxy) > 70:
            cur, ago = dxy[-1][1], dxy[-66][1]     # ~3個月(66交易日)
            chg = (cur / ago - 1) * 100
            out["DXY(廣義,n=1)"] = f"{cur:.1f} 3月{chg:+.1f}%{'↓走弱' if chg < 0 else '↑走強'}"
    except Exception:  # noqa: BLE001
        pass
    try:
        spx = _fetch_fred_series("SP500")
        if len(spx) > 210:
            cur = spx[-1][1]
            ma200 = sum(v for _, v in spx[-200:]) / 200
            out["SPX(n=2)"] = f"{cur:,.0f} {'200日線上' if cur > ma200 else '200日線下'}"
    except Exception:  # noqa: BLE001
        pass
    return out


def fetch_fng_avg30() -> Optional[float]:
    cached = _cached("fng")
    if cached is not None:
        return cached
    try:
        r = httpx.get(_FNG, headers=_UA, timeout=30)
        vals = [int(x["value"]) for x in r.json().get("data", [])][:30]
        avg = round(sum(vals) / len(vals), 1) if vals else None
        _put("fng", avg)
        return avg
    except Exception:  # noqa: BLE001
        return None


def fetch_dominance_today() -> Optional[float]:
    cached = _cached("dominance")
    if cached is not None:
        return cached
    try:
        r = httpx.get(_CG_GLOBAL, headers=_UA, timeout=30)
        d = r.json().get("data", {}).get("market_cap_percentage", {}).get("btc")
        d = round(float(d), 2) if d is not None else None
        _put("dominance", d)
        return d
    except Exception:  # noqa: BLE001
        return None


def dominance_direction_90d(history: list[tuple[int, float]]) -> Optional[bool]:
    """90 日方向：需本地累積 ≥30 天（每日 shadow 記一點）才判——不足誠實回 None。
    history=[(ts_ms, dominance_pct)...]。"""
    if not history:
        return None
    now = time.time() * 1000
    window = [(t, v) for t, v in history if now - t <= 90 * 86400_000]
    if len(window) < 30:
        return None
    window.sort()
    first_avg = sum(v for _, v in window[:7]) / min(7, len(window))
    last_avg = sum(v for _, v in window[-7:]) / min(7, len(window))
    return last_avg > first_avg


def fetch_stablecoin_momentum_30d() -> Optional[str]:
    """穩定幣總市值 30 日動能——落後/確認因子（只進 overlay 永不計分）。"""
    cached = _cached("stablecoin")
    if cached is not None:
        return cached
    try:
        r = httpx.get(_LLAMA, headers=_UA, timeout=45)
        pts = r.json()
        if len(pts) < 35:
            return None
        def tot(p):
            t = p.get("totalCirculatingUSD") or {}
            return sum(v for v in t.values() if isinstance(v, (int, float)))
        cur, ago = tot(pts[-1]), tot(pts[-31])
        if not cur or not ago:
            return None
        chg = (cur / ago - 1) * 100
        s = f"穩定幣市值30日{chg:+.1f}%（落後因子:動能轉正=復甦確認非抄底訊號）"
        _put("stablecoin", s)
        return s
    except Exception:  # noqa: BLE001
        return None


def fetch_etf_overlay() -> Optional[str]:
    """v113（使用者指定）：多資產加密 ETF 淨流（BTC/ETH/XRP/SOL，$79 權益 2026-07-03 活線
    實證可用；SUI 尚無端點=誠實缺料）。機構/銀行/家辦動向的溫度計。

    ⚠️ 對抗審查裁定不變：ETF 2024 年才誕生、零個熊底歷史（n=0）——**只進 overlay 顯示、
    永不計入核心分數**（不能假裝有歷史校準）。每日一抓快取，與 coinglass.py 同 auth。"""
    cached = _cached("etf_multi")
    if cached is not None:
        return cached
    key = os.getenv("COINGLASS_API_KEY", "").strip()
    if not key:
        return None
    parts: list[str] = []
    try:
        with httpx.Client(base_url="https://open-api-v4.coinglass.com", timeout=30,
                          headers={"CG-API-KEY": key, "accept": "application/json"}) as cli:
            for coin, tag in (("bitcoin", "BTC"), ("ethereum", "ETH"),
                              ("xrp", "XRP"), ("solana", "SOL")):
                try:
                    r = cli.get(f"/api/etf/{coin}/flow-history")
                    data = (r.json() or {}).get("data") or []
                    if len(data) < 2:
                        continue
                    def _flow(x):
                        v = x.get("flow_usd")
                        return float(v) if v is not None else 0.0
                    d5 = sum(_flow(x) for x in data[-5:]) / 1e6
                    d30 = sum(_flow(x) for x in data[-30:]) / 1e6
                    parts.append(f"{tag} 5日{d5:+,.0f}M/30日{d30:+,.0f}M")
                except Exception:  # noqa: BLE001 — 單一資產失敗不拖累其他
                    continue
    except Exception:  # noqa: BLE001
        return None
    if not parts:
        return None
    s = "ETF淨流(機構動向,n=0無熊底校準僅參考)：" + " ｜ ".join(parts)
    _put("etf_multi", s)
    return s


def collect_bottom_inputs(price_now: Optional[float], mayer: Optional[float],
                          dist_200wma_pct: Optional[float],
                          dominance_history: Optional[list] = None) -> tuple[dict, dict, dict]:
    """彙整核心 inputs + 背景 + overlay（各源獨立失敗安全）。回 (inputs, background, overlay)。"""
    cm = fetch_coinmetrics_btc(price_now)
    inputs = {
        "mvrv_z": cm.get("mvrv_z"),
        "price": price_now,
        "realized_price": cm.get("realized_price"),
        "mayer": mayer,
        "dist_200wma_pct": dist_200wma_pct,
        "dominance_dir_90d": dominance_direction_90d(dominance_history or []),
        "altseason_idx": None,     # v1 未接（CoinGlass altseason 端點待接）——誠實缺料
        "fng_avg30": fetch_fng_avg30(),
    }
    background = fetch_macro_background()
    overlay = {}
    etf = fetch_etf_overlay()
    if etf:
        overlay["🏦"] = etf
    sc = fetch_stablecoin_momentum_30d()
    if sc:
        overlay["🪙"] = sc
    if cm.get("asof"):
        overlay["📅"] = f"鏈上資料 asof {cm['asof']}（RC 近似橋接到今日價）"
    return inputs, background, overlay
