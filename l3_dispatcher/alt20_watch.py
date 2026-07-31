# -*- coding: utf-8 -*-
"""v180: 💎 山寨抄底·現貨Top20 主題（使用者 2026-08-01 指定）。

名單（v1,可隨時調整）：以使用者賽道論文為底（RWA/TradFi/PayFi/DeFi/AI,
memory cycle-position-layer 定案清單）＋補齊主流錨定，共 20 檔。
WLFI 有專屬主題故不在此列。

每日 10:00 台北深度卡：每檔=價格/距ATH/Mayer/價值帶(cycle_regime 框架)/
資費/OI 24h變化 → 綜合「抄底合流分」(0-4)=價值帶✚資費≤0✚OI回升✚大戶偏多。
事件推播：任一檔 24h ≤ −10% → 抄底雷達提醒（跌深不等於底,附誠實聲明）。

⛔ 鐵則：100% display_only；加密週期樣本結構上永不顯著（紅線③）——所有分數
是啟發式定位不是預測；現貨買入永遠是使用者的手。
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from botpaths import data_dir

_STATE = data_dir() / "alt20_watch_state.json"
POLL_S = 1800                     # 30 分鐘輪詢（事件偵測）
DIGEST_HOUR_TPE = 10
DROP_ALERT_PCT = -10.0
_OKX = "https://www.okx.com"

# v1 名單：使用者論文 16 檔 + 主流補齊（LINK/UNI/NEAR/TON）
UNIVERSE = ["ETH", "SOL", "XRP", "BNB", "AVAX", "AAVE", "SUI", "XLM",
            "ONDO", "TAO", "RENDER", "FET", "HYPE", "ASTER", "OKB", "DOGE",
            "LINK", "UNI", "NEAR", "TON"]
LANES = {"ETH": "L1/DeFi", "SOL": "L1/AI", "XRP": "PayFi", "BNB": "平台",
         "AVAX": "RWA/L1", "AAVE": "DeFi", "SUI": "L1", "XLM": "PayFi",
         "ONDO": "RWA", "TAO": "AI", "RENDER": "AI", "FET": "AI",
         "HYPE": "DeFi/Perp", "ASTER": "DeFi/Perp", "OKB": "平台",
         "DOGE": "支付/迷因", "LINK": "預言機/RWA", "UNI": "DeFi",
         "NEAR": "AI/L1", "TON": "支付"}


def _load() -> dict:
    try:
        return json.loads(_STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save(st: dict) -> None:
    try:
        _STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


async def _okx_pub(client: httpx.AsyncClient, path: str) -> list:
    try:
        r = await client.get(_OKX + path)
        return r.json().get("data") or []
    except Exception:  # noqa: BLE001
        return []


async def read_symbol(client: httpx.AsyncClient, sym: str) -> dict | None:
    """單檔讀數：日K 200 根算 Mayer/距ATH/200MA + 資費 + OI。全免 key。"""
    inst = f"{sym}-USDT-SWAP"
    k = await _okx_pub(client, f"/api/v5/market/candles?instId={inst}&bar=1D&limit=300")
    if len(k) < 30:
        return None
    closes = [float(c[4]) for c in reversed(k)]
    px = closes[-1]
    ma200 = sum(closes[-200:]) / min(200, len(closes))
    ath = max(float(c[2]) for c in k)
    mayer = px / ma200 if ma200 else None
    chg24 = (closes[-1] / closes[-2] - 1) * 100 if len(closes) >= 2 else 0.0
    f = await _okx_pub(client, f"/api/v5/public/funding-rate?instId={inst}")
    fr = float(f[0]["fundingRate"]) * 100 if f and f[0].get("fundingRate") else None
    oi = await _okx_pub(client, f"/api/v5/rubik/stat/contracts/open-interest-volume"
                                f"?ccy={sym}&period=1D")
    oi_chg = None
    if len(oi) >= 2:
        try:
            a, b = float(oi[-1][1]), float(oi[-2][1])
            oi_chg = (a / b - 1) * 100 if b else None
        except Exception:  # noqa: BLE001
            oi_chg = None
    zone = ("深度價值" if mayer and mayer < 0.7 else
            "價值" if mayer and mayer < 0.9 else
            "中性" if mayer and mayer < 1.4 else "過熱")
    score = sum([1 if zone in ("深度價值", "價值") else 0,
                 1 if (fr is not None and fr <= 0) else 0,
                 1 if (oi_chg is not None and oi_chg > 0) else 0,
                 1 if px < ma200 else 0])
    return {"sym": sym, "px": px, "mayer": mayer, "ath_dd": (px / ath - 1) * 100,
            "chg24": chg24, "funding": fr, "oi_chg": oi_chg,
            "zone": zone, "score": score}


def render_row(r: dict) -> str:
    z = {"深度價值": "🟢", "價值": "🟡", "中性": "⚪", "過熱": "🔴"}[r["zone"]]
    stars = "★" * r["score"] + "☆" * (4 - r["score"])
    return (f"{z} <b>{r['sym']}</b>({LANES.get(r['sym'], '?')}) "
            f"${r['px']:.4g}　距ATH {r['ath_dd']:+.0f}%　Mayer {r['mayer']:.2f}　"
            f"合流 {stars}")


_DISC = ("<i>⚠️ 誠實聲明：合流分=啟發式「定位」非預測；加密週期樣本結構上永不"
         "統計顯著；跌深可以更深、名單非投資建議；現貨買入永遠是你親手的決定。"
         "永不觸發自動下單。</i>")


async def run_alt20_loop(tg=None, poll_seconds: int = POLL_S):
    """worker：30min 大跌雷達＋每日 10:00 台北 Top20 深度卡。"""
    print("[alt20] loop online（Top20 山寨抄底追蹤;30min雷達+每日10:00深度卡）")
    st = _load()
    if not st.get("intro_sent") and tg:
        try:
            await tg.send_message(
                "💎 <b>山寨抄底·現貨 Top20 追蹤已上線</b>\n"
                f"名單(v1)：{', '.join(UNIVERSE)}\n"
                "• 每日 10:00：全名單深度卡（價值帶/Mayer/距ATH/資費/OI/抄底合流分）\n"
                "• 每 30 分：大跌雷達（單日 ≤−10% 即提醒）\n" + _DISC,
                parse_mode="HTML")
            st["intro_sent"] = True
            _save(st)
        except Exception:  # noqa: BLE001
            pass
    while True:
        try:
            now = time.time()
            tpe = time.gmtime(now + 8 * 3600)
            day_key = time.strftime("%Y-%m-%d", tpe)
            async with httpx.AsyncClient(timeout=25) as client:
                # ── 每日深度卡 ──
                if tpe.tm_hour == DIGEST_HOUR_TPE and st.get("digest_day") != day_key and tg:
                    reads = []
                    for s in UNIVERSE:
                        r = await read_symbol(client, s)
                        if r:
                            reads.append(r)
                        await asyncio.sleep(0.3)      # 溫柔限速
                    reads.sort(key=lambda r: (-r["score"], r["mayer"] or 9))
                    lines = [f"💎 <b>山寨抄底日報</b>（{len(reads)}/20 檔有數據）",
                             "<i>依抄底合流分排序；★=價值帶/負資費/OI回升/低於200日線</i>", ""]
                    lines += [render_row(r) for r in reads]
                    top = [r["sym"] for r in reads if r["score"] >= 3][:5]
                    if top:
                        lines.append(f"\n🎯 今日合流 ≥3★：{', '.join(top)}")
                    lines.append(_DISC)
                    try:
                        await tg.send_message("\n".join(lines), parse_mode="HTML")
                        st["digest_day"] = day_key
                    except Exception:  # noqa: BLE001
                        pass
                # ── 大跌雷達（30min）──
                alerted = st.get("drop_alerted", {})
                for s in UNIVERSE:
                    t = await _okx_pub(client,
                                       f"/api/v5/market/ticker?instId={s}-USDT-SWAP")
                    if not t:
                        continue
                    try:
                        last = float(t[0]["last"])
                        open24 = float(t[0]["open24h"])
                        chg = (last / open24 - 1) * 100 if open24 else 0
                    except Exception:  # noqa: BLE001
                        continue
                    if chg <= DROP_ALERT_PCT and alerted.get(s) != day_key and tg:
                        try:
                            await tg.send_message(
                                f"💎📉 <b>大跌雷達</b> {s} 24h {chg:+.1f}% → ${last:.4g}\n"
                                "跌深≠底部——查看今日深度卡的合流分再決策。\n" + _DISC,
                                parse_mode="HTML")
                            alerted[s] = day_key
                        except Exception:  # noqa: BLE001
                            pass
                    await asyncio.sleep(0.2)
                st["drop_alerted"] = {k: v for k, v in alerted.items()
                                      if v == day_key}
            _save(st)
        except Exception as e:  # noqa: BLE001
            print(f"[alt20] loop 例外（不致命）：{type(e).__name__}: {e}")
        await asyncio.sleep(max(300, int(poll_seconds)))
