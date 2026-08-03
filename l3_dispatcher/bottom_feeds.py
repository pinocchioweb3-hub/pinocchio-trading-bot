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
        raw = r.json().get("data")
        if not isinstance(raw, list):     # 限流／錯誤 body：未知≠沒有，不可續算
            return None
        vals = [int(x["value"]) for x in raw][:30]
        # v212：窗口殘缺不可折成「這就是 30 日均」——只抓到 N(<30) 天卻回一個數字，
        # 會被 compute_bottom_score 當成滿窗的 fng_avg30 計分。缺料就誠實回 None
        # （present_mass 會重正規化，這是本檔既有的誠實路徑）。
        if len(vals) < 30:
            return None
        avg = round(sum(vals) / 30, 1)
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
    """90 日方向：需本地累積 ≥30 個「不重複日」才判——不足誠實回 None。
    history=[(ts_ms, dominance_pct)...]。
    v137：daemon 重啟補跑會造成同日多點——按 UTC 日去重取末筆再數天數與算首尾
    7 日均，否則點數灌水提早過閘、7 日平滑塌縮成同日重複值（稽核實證 51 點僅 25 日）。"""
    if not history:
        return None
    now = time.time() * 1000
    window = [(t, v) for t, v in history if now - t <= 90 * 86400_000]
    by_day: dict[int, float] = {}
    for t, v in sorted(window):
        by_day[int(t // 86400_000)] = v          # 同日取末筆
    days = [by_day[k] for k in sorted(by_day)]
    if len(days) < 30:
        return None
    first_avg = sum(days[:7]) / 7
    last_avg = sum(days[-7:]) / 7
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
        # ⛔「沒接這個源」不是「量不到」（v238 邊界）：沒金鑰就不該在報告上多一列
        #    假警訊，否則 ⚠️ 這個符號會貶值，等於用另一種方式製造失明。
        return None
    parts: list[str] = []
    missing: list[str] = []            # v243：死掉的資產要具名，不得靜默 continue
    try:
        with httpx.Client(base_url="https://open-api-v4.coinglass.com", timeout=30,
                          headers={"CG-API-KEY": key, "accept": "application/json"}) as cli:
            for coin, tag in (("bitcoin", "BTC"), ("ethereum", "ETH"),
                              ("xrp", "XRP"), ("solana", "SOL")):
                try:
                    r = cli.get(f"/api/etf/{coin}/flow-history")
                    body = r.json() or {}
                    data = body.get("data")
                    if not isinstance(data, list) or len(data) < 5:
                        # 連 5 日窗口都湊不齊＝該資產不報（未知≠零淨流）——但要說為什麼。
                        missing.append(f"{tag}({_etf_miss_reason(r, body, data)})")
                        continue
                    def _flow(x):
                        v = x.get("flow_usd")
                        return float(v) if v is not None else 0.0
                    d5 = sum(_flow(x) for x in data[-5:]) / 1e6
                    # v212：窗口殘缺不可折成完整的「30日」數字。舊碼在只有 3 天時會印
                    # 「5日+30M/30日+30M」——同一個數字掛兩個窗口標籤（紅線③相鄰：對外數字）。
                    if len(data) >= 30:
                        d30 = sum(_flow(x) for x in data[-30:]) / 1e6
                        parts.append(f"{tag} 5日{d5:+,.0f}M/30日{d30:+,.0f}M")
                    else:
                        parts.append(f"{tag} 5日{d5:+,.0f}M/30日資料不足(僅{len(data)}日)")
                except Exception as e:  # noqa: BLE001 — 單一資產失敗不拖累其他
                    missing.append(f"{tag}(例外 {type(e).__name__})")
                    continue
    except Exception as e:  # noqa: BLE001
        return f"ETF淨流：讀不到（連線層 {type(e).__name__}: {e}）"[:200]
    if not parts:
        # v243：⛔ 不再回 None。消費端是 `if etf: overlay["🏦"] = etf`，None 等於
        # 整行從報告上消失——於是「這源沒資料」和「這源死了」長得一模一樣。
        # ⛔ 這一行不得帶任何看似淨流的金額（v212 鐵則不倒退）。
        # ⛔ 不寫進 _put：失敗若進了 20 小時快取，源恢復了報告還在說它死著。
        return "ETF淨流：讀不到（" + "；".join(missing or ["無成因可回報"]) + "）"
    s = "ETF淨流(機構動向,n=0無熊底校準僅參考)：" + " ｜ ".join(parts)
    if missing:
        s += "　⚠️未取得：" + "；".join(missing)
    _put("etf_multi", s)
    return s


def _etf_miss_reason(resp, body: dict, data) -> str:
    """這一個資產為什麼沒有結果（v243）。

    ⛔ 三種成因要能分辨，因為處置完全不同：
        窗口不足 → 等資料累積（正常，新上市 ETF 就是這樣）
        API 錯誤 → 續訂／換源
        回應無 data → 查端點契約
    """
    if isinstance(data, list):
        return f"僅{len(data)}日<5日窗口"
    # ⚠️ 這個端點在方案到期時是「HTTP 200 + 錯誤 body」（2026-08-03 線上實證）——
    #    只看 raise_for_status() 永遠抓不到。所以狀態碼與 body code 兩個都要報。
    status = getattr(resp, "status_code", None)
    bits = [f"HTTP{status}" if status else None, body.get("code")]
    code = "/".join(str(b) for b in bits if b) or "無狀態碼"
    msg = str(body.get("msg") or body.get("message") or "").strip()
    return f"{code}: {msg}"[:60] if msg else f"回應無 data（{code}）"


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
