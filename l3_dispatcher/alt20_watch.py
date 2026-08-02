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


async def _okx_pub(client: httpx.AsyncClient, path: str) -> list | None:
    """v213：回 list＝交易所給的答案（空清單＝**確認**沒有）；回 None＝這輪讀不出來。

    舊碼任何失敗都回 `[]`，與「交易所明講沒有」共用同一個出口——呼叫端因此
    永遠分不出「未知」與「確定」。

    ⛔ 邊界線（同 v208/v210/v212）：`{"code":"0","data":[]}` 是交易所明講「沒有」
    ＝確定，不可為保險一起打成未知，否則正常空回應每輪都變告警＝慢性假警報。
    """
    try:
        r = await client.get(_OKX + path)
    except Exception:  # noqa: BLE001
        return None                       # 傳輸層炸掉＝未知
    if getattr(r, "status_code", 200) != 200:
        return None                       # HTTP 非 200（429/5xx）＝未知
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        return None                       # 解不開 JSON＝未知
    if isinstance(body, list):
        return body
    if not isinstance(body, dict):
        return None
    if str(body.get("code", "0")) != "0":
        return None                       # API 自述失敗（限流／參數錯）＝未知
    data = body.get("data")
    return data if isinstance(data, list) else None


MAYER_WINDOW = 200      # Mayer 倍數的定義窗＝200 日均；不足此數就不是 Mayer
HIST_LIMIT = 300        # OKX 單次可取的最長日 K ⇒ 我們看得到的至多是 300 日高


async def read_symbol(client: httpx.AsyncClient, sym: str) -> dict | None:
    """單檔讀數：日K 算 Mayer/近期高/200MA + 資費 + OI。全免 key。

    v213：把「這輪讀不出來／窗口不足」與「交易所確認的答案」分開——
    殘缺窗口不再掛上 Mayer（200日均）與 ATH（歷史最高）這兩個滿窗標籤，
    資費／OI 讀不出來也不再無聲折成「這顆星確實不成立」。
    """
    inst = f"{sym}-USDT-SWAP"
    k = await _okx_pub(client, f"/api/v5/market/candles?instId={inst}&bar=1D&limit=300")
    if k is None or len(k) < 30:
        return None
    kk = list(reversed(k))
    closes = [float(c[4]) for c in kk]
    highs = [float(c[2]) for c in kk]
    lows = [float(c[3]) for c in kk]
    vols = [float(c[5]) for c in kk]
    px = closes[-1]
    # v182：波動壓縮（DCA 友善窗指標）——近14日振幅 vs 近60日中位振幅
    def _rng(i0, i1):
        return (max(highs[i0:i1]) - min(lows[i0:i1])) / closes[i1 - 1] * 100
    compress = None
    if len(closes) >= 74:
        r14 = _rng(-14, len(closes))
        r14s = [_rng(i - 14, i) for i in range(len(closes) - 60, len(closes), 5)]
        med = sorted(r14s)[len(r14s) // 2] if r14s else None
        compress = bool(med and r14 < med * 0.7)
    gaps: list[str] = []
    hist_days = len(closes)
    full_window = hist_days >= MAYER_WINDOW
    if not full_window:
        gaps.append("candles_short")
    ma200 = sum(closes[-200:]) / min(200, len(closes))
    # ⛔ 這是「近 hist_days 日高」，不是歷史最高（limit=300 封頂）——標籤照實講。
    hi = max(float(c[2]) for c in k)
    # 不足 200 根時算出來的是「N 日均」，那不是 Mayer；寧可不給數字也不給錯標籤。
    mayer = (px / ma200 if ma200 else None) if full_window else None
    chg24 = (closes[-1] / closes[-2] - 1) * 100 if len(closes) >= 2 else 0.0
    f = await _okx_pub(client, f"/api/v5/public/funding-rate?instId={inst}")
    if f is None:
        gaps.append("funding")
    fr = float(f[0]["fundingRate"]) * 100 if f and f[0].get("fundingRate") else None
    oi = await _okx_pub(client, f"/api/v5/rubik/stat/contracts/open-interest-volume"
                                f"?ccy={sym}&period=1D")
    if oi is None:
        gaps.append("oi")
    oi_chg = None
    if oi and len(oi) >= 2:
        try:
            a, b = float(oi[-1][1]), float(oi[-2][1])
            oi_chg = (a / b - 1) * 100 if b else None
        except Exception:  # noqa: BLE001
            oi_chg = None
    zone = ("深度價值" if mayer and mayer < 0.7 else
            "價值" if mayer and mayer < 0.9 else
            "中性" if mayer and mayer < 1.4 else
            "過熱") if full_window else "資料不足"
    # 四項合流分：1/0＝確定成立/確定不成立，None＝這輪無法判定（⛔ 不可折成 0）。
    comps = {
        "value_zone": (1 if zone in ("深度價值", "價值") else 0) if full_window else None,
        "funding_le0": None if f is None else (1 if (fr is not None and fr <= 0) else 0),
        "oi_up": None if oi is None else (1 if (oi_chg is not None and oi_chg > 0) else 0),
        "below_ma200": (1 if px < ma200 else 0) if full_window else None,
    }
    # 滿窗且無缺料時，此加總與舊碼逐項相同（反向側守門）。
    score = sum(v for v in comps.values() if v)
    unresolved = sum(1 for v in comps.values() if v is None)
    return {"sym": sym, "px": px, "mayer": mayer, "ath_dd": (px / hi - 1) * 100,
            "hist_days": hist_days, "full_window": full_window,
            "data_gaps": gaps, "unresolved": unresolved, "comps": comps,
            "chg24": chg24, "funding": fr, "oi_chg": oi_chg,
            "zone": zone, "score": score, "compress": compress,
            "highs": highs, "lows": lows, "closes": closes, "vols": vols}


def ignition_signals(highs: list, lows: list, closes: list, vols: list,
                     oi_chg, chg24, fr_now, fr_prev, tt_now, tt_prev) -> list[str]:
    """點火偵測（純函式,v182）：八項起漲前兆,回命中清單。
    設計依據=使用者四指標(大戶比↑/OI↑/流動性獵取/放量)+四補強(資費翻正/
    收復20日高/量能翻倍/大戶比躍升幅度化)。全部啟發式,display_only。"""
    sig = []
    if len(closes) >= 25:
        prior_low = min(lows[-21:-1])
        if lows[-1] < prior_low and closes[-1] > prior_low:
            sig.append("🪤 流動性獵取完成（掃 20 日低後收回=SMC Spring 型）")
        vma20 = sum(vols[-21:-1]) / 20
        if vma20 > 0 and vols[-1] >= 2 * vma20:
            if closes[-1] > max(closes[-21:-1]):
                sig.append("📈 放量突破 20 日高（Wyckoff SOS 型,量 2 倍+）")
            elif closes[-1] > closes[-2]:
                sig.append("🔊 低位放量收漲（量 2 倍+,吸籌活動跡象）")
    if oi_chg is not None and chg24 is not None and oi_chg > 5 and chg24 > 3:
        sig.append(f"🏗 OI+價齊升（OI {oi_chg:+.0f}%/價 {chg24:+.1f}%=健康堆倉）")
    if (tt_now is not None and tt_prev is not None and tt_prev > 0
            and tt_now / tt_prev >= 1.15):
        sig.append(f"🐋 大戶多空比躍升（{tt_prev:.2f}→{tt_now:.2f},+15%+）")
    if fr_now is not None and fr_prev is not None and fr_prev < 0 <= fr_now:
        sig.append("🔁 資費由負翻正（空方擁擠解除）")
    return sig


_ZONE_ICON = {"深度價值": "🟢", "價值": "🟡", "中性": "⚪", "過熱": "🔴",
              "資料不足": "❓"}
_GAP_LABEL = {"funding": "資費", "oi": "OI"}


def render_row(r: dict) -> str:
    """v213：殘缺／未知一律在人看得到的地方講明。
    ★＝確定成立、☆＝確定不成立、?＝這輪無法判定（合流分因此是**下限**）。"""
    z = _ZONE_ICON.get(r["zone"], "⚪")
    unresolved = r.get("unresolved", 0)
    stars = ("★" * r["score"] + "☆" * (4 - r["score"] - unresolved)
             + "?" * unresolved)
    hist = r.get("hist_days")
    if r.get("mayer") is not None:
        mayer_txt = f"Mayer {r['mayer']:.2f}"
    else:
        mayer_txt = f"Mayer n/a（僅{hist}日·不足{MAYER_WINDOW}日均）"
    high_txt = f"距{hist}日高 {r['ath_dd']:+.0f}%"
    tail = "　💤壓縮" if r.get("compress") else ""
    feed_gaps = [_GAP_LABEL[g] for g in r.get("data_gaps", []) if g in _GAP_LABEL]
    if feed_gaps:
        tail += f"　⚠️{'／'.join(feed_gaps)}這輪讀不出來（合流分為下限）"
    return (f"{z} <b>{r['sym']}</b>({LANES.get(r['sym'], '?')}) "
            f"${r['px']:.4g}　{high_txt}　{mayer_txt}　"
            f"合流 {stars}" + tail)


def summarize_data_gaps(reads: list[dict]) -> str | None:
    """日報表頭那一行「這張卡有多少不確定」。全部乾淨時回 None（不多印雜訊）。"""
    n_short = sum(1 for r in reads if "candles_short" in r.get("data_gaps", []))
    n_feed = sum(1 for r in reads
                 if any(g in _GAP_LABEL for g in r.get("data_gaps", [])))
    if not n_short and not n_feed:
        return None
    parts = []
    if n_short:
        parts.append(f"{n_short} 檔日K不足 {MAYER_WINDOW} 根（算不出 Mayer／價值帶，"
                     f"不是「不在價值帶」）")
    if n_feed:
        parts.append(f"{n_feed} 檔資費或 OI 這輪讀不出來")
    # ⛔ 本行隨日報以 parse_mode="HTML" 送出 ⇒ 強調一律用 <b>，不可用 Markdown 星號。
    return ("⚠️ <i>其中 " + "、".join(parts)
            + "；這些檔的合流分是<b>下限</b>（?＝無法判定，非確定不成立）。</i>")


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
                             "<i>依抄底合流分排序；★=價值帶/負資費/OI回升/低於200日線</i>"]
                    gap_line = summarize_data_gaps(reads)
                    if gap_line:
                        lines.append(gap_line)
                    lines.append("")
                    lines += [render_row(r) for r in reads]
                    top = [r["sym"] for r in reads if r["score"] >= 3][:5]
                    if top:
                        lines.append(f"\n🎯 今日合流 ≥3★：{', '.join(top)}")
                    # v182：DCA 友善窗＝價值帶＋波動壓縮（安靜的低位=分批建倉統計上較舒服）
                    dca = [r["sym"] for r in reads
                           if r["zone"] in ("深度價值", "價值") and r.get("compress")]
                    if dca:
                        lines.append(f"🧺 DCA 友善窗（價值帶＋波動壓縮）：{', '.join(dca)}")
                    lines.append(_DISC)
                    try:
                        await tg.send_message("\n".join(lines), parse_mode="HTML")
                        st["digest_day"] = day_key
                    except Exception:  # noqa: BLE001
                        pass
                # ── 🚀 點火偵測（每 2h 全名單掃描,≥2 項命中才推=防噪）──
                if now - (st.get("ignition_ts") or 0) >= 7200 and tg:
                    ig_alerted = st.get("ignition_alerted", {})
                    prevs = st.get("ignition_prev", {})
                    try:
                        from market_intel_mcp.sources.binance_perp import get_binance_perp
                        bsrc = get_binance_perp()
                    except Exception:  # noqa: BLE001
                        bsrc = None
                    for s in UNIVERSE:
                        r = await read_symbol(client, s)
                        if not r:
                            continue
                        tt_now = None
                        if bsrc:
                            try:
                                bp = await bsrc.get_positioning(s, "1d", 3)
                                tt_now = (bp or {}).get("latest")
                            except Exception:  # noqa: BLE001
                                tt_now = None
                        pv = prevs.get(s, {})
                        sigs = ignition_signals(
                            r["highs"], r["lows"], r["closes"], r["vols"],
                            r.get("oi_chg"), r.get("chg24"),
                            r.get("funding"), pv.get("fr"),
                            tt_now, pv.get("tt"))
                        prevs[s] = {"fr": r.get("funding"), "tt": tt_now}
                        if len(sigs) >= 2 and ig_alerted.get(s) != day_key:
                            try:
                                await tg.send_message(
                                    f"🚀 <b>點火偵測</b> <b>{s}</b>"
                                    f"（{LANES.get(s, '?')}）${r['px']:.4g}\n"
                                    + "\n".join(f"• {x}" for x in sigs)
                                    + f"\n合流分 {'★' * r['score']}{'☆' * (4 - r['score'])}"
                                      f"　{r['zone']}帶\n" + _DISC,
                                    parse_mode="HTML")
                                ig_alerted[s] = day_key
                            except Exception:  # noqa: BLE001
                                pass
                        await asyncio.sleep(0.3)
                    st["ignition_prev"] = prevs
                    st["ignition_alerted"] = {k: v for k, v in ig_alerted.items()
                                              if v == day_key}
                    st["ignition_ts"] = now

                # ── 大跌雷達（30min）──
                alerted = st.get("drop_alerted", {})
                for s in UNIVERSE:
                    t = await _okx_pub(client,
                                       f"/api/v5/market/ticker?instId={s}-USDT-SWAP")
                    # v213：t is None＝這輪讀不出來、t == []＝確認沒有報價。兩者都跳過
                    # ——此處折疊方向是**漏報**（少一次雷達提醒），不會產出錯誤斷言，
                    # 故維持原行為；⛔ 不在此加告警，否則每次限流都吵一次。
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
