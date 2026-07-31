# -*- coding: utf-8 -*-
"""v179: WLFI 專屬追蹤（使用者 2026-08-01 指定：🦅 獨立主題）。

三層追蹤：
    ①鏈上（15min）：ETH 主網 Transfer 事件掃描（公共 RPC eth_getLogs,免key）
      → 大額轉帳(≥WHALE_USD)即推,標注已知地址（交易所=賣壓/提幣=囤積方向）。
    ②行情（15min）：OKX 公開 REST 價格/OI/資費 → 劇烈變動(|1h|≥4%)即推。
    ③長週期（每日 09:30 台北）：日報卡=價格位置(距ATH/ATL)/OI/資費/持有人數
      (Ethplorer 免key)/24h 鯨魚彙總/解鎖倒數/WLFI 相關新聞(OKX news by-coin)。

⛔ 鐵則：100% display_only——本模組永不進任何開單數學、永不下單；
    每張日報帶固定誠實聲明（top10 集中 87%/專案方有凍結權(Justin Sun 案)/
    2028-05 解鎖牆 171% 流通量/政治連結雙向風險——2026-07-29 深度研究定案）。
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from botpaths import data_dir

_STATE = data_dir() / "wlfi_watch_state.json"
POLL_S = 900                        # 15 分鐘
WHALE_USD = 200_000                 # 大額轉帳門檻（美元）
PRICE_MOVE_PCT = 4.0                # 1h 價格劇變門檻
CONTRACT = "0xda5e1988097297dcdc1f90d4dfe7909e847cbef6"   # WLFI (Ethereum)
_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_RPCS = ("https://eth.llamarpc.com", "https://ethereum-rpc.publicnode.com",
         "https://cloudflare-eth.com")
_OKX = "https://www.okx.com"
INST = "WLFI-USDT-SWAP"
DIGEST_HOUR_TPE = 9                 # 每日 09:30 台北（tick 內判 9:30±poll 窗）

# 已知地址標籤（2026-07-29 深度研究＋公開標籤;小寫比對）
LABELS = {
    "0xf977814e90da44bfa03b6295a0616a897441acec": "幣安冷錢包",
    "0x28c6c06298d514db089934071355e5743bf21d60": "幣安熱錢包14",
    "0x0d0707963952f2fba59dd06f2b425ace40b492fe": "Gate.io",
    "0x5041ed759dd4afc3a72b8192c143f72f4724081a": "OKX熱錢包",
    "0x1b46970cfe6a271e884f636663c257a5a571fb2c": "WLFI鎖倉/團隊關聯",
}
_EXCHANGES = {a for a, l in LABELS.items()
              if any(x in l for x in ("幣安", "Gate", "OKX"))}


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


def label_of(addr: str) -> str:
    a = (addr or "").lower()
    return LABELS.get(a, a[:8] + "…" + a[-4:])


def decode_transfer(log: dict) -> dict | None:
    """eth_getLogs 單筆 → {from,to,amount}。壞資料回 None。"""
    try:
        topics = log.get("topics") or []
        if len(topics) < 3 or topics[0].lower() != _TRANSFER:
            return None
        amt = int(log.get("data", "0x0"), 16) / 1e18
        return {"from": "0x" + topics[1][-40:].lower(),
                "to": "0x" + topics[2][-40:].lower(),
                "amount": amt, "tx": log.get("transactionHash", "")}
    except Exception:  # noqa: BLE001
        return None


def classify_flow(tr: dict) -> str:
    """轉帳方向語意：→交易所=潛在賣壓 / 交易所→=提幣囤積 / 其他=錢包間移轉。"""
    if tr["to"] in _EXCHANGES:
        return "🔻 轉入交易所（潛在賣壓）"
    if tr["from"] in _EXCHANGES:
        return "🟢 自交易所提出（囤積傾向）"
    return "↔️ 錢包間移轉"


async def _rpc(method: str, params: list) -> dict | list | None:
    for url in _RPCS:
        try:
            async with httpx.AsyncClient(timeout=25) as c:
                r = await c.post(url, json={"jsonrpc": "2.0", "id": 1,
                                            "method": method, "params": params})
            body = r.json()
            if "result" in body:
                return body["result"]
        except Exception:  # noqa: BLE001
            continue
    return None


async def _okx_pub(path: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(_OKX + path)
        d = r.json()
        return (d.get("data") or [None])[0]
    except Exception:  # noqa: BLE001
        return None


async def _market() -> dict:
    """OKX 公開行情三件套（免認證）。缺料欄位=None 誠實留空。"""
    t = await _okx_pub(f"/api/v5/market/ticker?instId={INST}")
    oi = await _okx_pub(f"/api/v5/public/open-interest?instId={INST}&instType=SWAP")
    f = await _okx_pub(f"/api/v5/public/funding-rate?instId={INST}")
    return {"price": float(t["last"]) if t and t.get("last") else None,
            "vol24h_usd": (float(t["volCcy24h"]) * float(t["last"])
                           if t and t.get("volCcy24h") and t.get("last") else None),
            "oi_usd": (float(oi["oiCcy"]) * float(t["last"])
                       if oi and oi.get("oiCcy") and t and t.get("last") else None),
            "funding_pct8h": (float(f["fundingRate"]) * 100
                              if f and f.get("fundingRate") else None)}


async def _holders() -> int | None:
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.get("https://api.ethplorer.io/getTokenInfo/"
                            f"{CONTRACT}?apiKey=freekey")
        return int(r.json().get("holdersCount"))
    except Exception:  # noqa: BLE001
        return None


async def _top_holders(n: int = 20) -> dict[str, float]:
    """v185：Top N 持倉地址→餘額(枚)。失敗回空 dict。Ethplorer freekey。"""
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.get("https://api.ethplorer.io/getTopTokenHolders/"
                            f"{CONTRACT}?apiKey=freekey&limit={n}")
        # balance 為 wei 原始值（2026-08-01 活測實證）→ /1e18 轉枚
        return {(h.get("address") or "").lower(): float(h.get("balance") or 0) / 1e18
                for h in (r.json().get("holders") or [])}
    except Exception:  # noqa: BLE001
        return {}


# v185/v186-2：Uniswap V3 工廠自動發現 WLFI 資金池——池子流出=DEX 買入。
# 報價幣涵蓋 USDT/USDC/USD1（使用者情報：機構多用 USDC 買;USD1=生態原生對）。
# 三地址皆經雙源驗證（Ethplorer name/symbol + CoinGecko platforms）。
_UNI_FACTORY = "0x1f98431c8ad98523631ae4a59f267346ea31f984"
_QUOTES = {
    "USDT": "0xdac17f958d2ee523a2206206994597c13d831ec7",
    "USDC": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    "USD1": "0x8d0d000ee44948fc98c9b98a4fa4921476f08b0d",
}


async def _discover_pools() -> set[str]:
    pools: set[str] = set()
    sel = "0x1698ee82"  # getPool(address,address,uint24)
    for quote in _QUOTES.values():
        for fee in (500, 3000, 10000):
            data = (sel + CONTRACT[2:].rjust(64, "0") + quote[2:].rjust(64, "0")
                    + hex(fee)[2:].rjust(64, "0"))
            res = await _rpc("eth_call", [{"to": _UNI_FACTORY, "data": data},
                                          "latest"])
            if res and int(res, 16) != 0:
                pools.add("0x" + res[-40:].lower())
    return pools


async def _usd1_supply() -> float | None:
    """v186：USD1 穩定幣跨鏈總供給（≈市值,$1 錨定）。CoinGecko usd1-wlfi,
    合約 0x8d0d...8b0d 已於 2026-08-01 經 CoinGecko+Ethplorer 雙源驗證。
    生態命脈指標：供給成長=採用擴張,通常領先幣價。失敗回 None。"""
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.get("https://api.coingecko.com/api/v3/simple/price"
                            "?ids=usd1-wlfi&vs_currencies=usd&include_market_cap=true")
        return float(r.json()["usd1-wlfi"]["usd_market_cap"])
    except Exception:  # noqa: BLE001
        return None


_DISCLAIMER = ("<i>⚠️ 固定誠實聲明：top10 地址持 87% 供給｜專案方具凍結任意錢包能力"
               "（Justin Sun 訴訟在案）｜2028-05 起 543 億枚解鎖（流通量 171%）｜"
               "政治連結=題材與打擊面一體兩面。以上為觀察數據，非投資建議、"
               "永不觸發自動下單。</i>")


def render_whale_card(tr: dict, price: float | None) -> str:
    usd = tr["amount"] * price if price else None
    usd_s = f"≈${usd / 1e6:.2f}M" if usd and usd >= 1e6 else \
            (f"≈${usd / 1e3:.0f}K" if usd else "")
    return ("🦅🐋 <b>WLFI 大額轉帳</b>\n"
            f"{tr['amount']:,.0f} 枚 {usd_s}\n"
            f"從 <code>{label_of(tr['from'])}</code> → <code>{label_of(tr['to'])}</code>\n"
            f"{classify_flow(tr)}\n"
            f"<a href=\"https://etherscan.io/tx/{tr['tx']}\">tx</a>　"
            "<i>鏈上觀察·非訊號</i>")


async def run_wlfi_watch_loop(tg=None, poll_seconds: int = POLL_S):
    """worker：15min 鏈上+行情掃描,事件才推；每日 09:30 台北日報。"""
    print(f"[wlfi] loop online（15min 鏈上+行情;日報 {DIGEST_HOUR_TPE}:30 台北;"
          "display_only）")
    st = _load()
    if not st.get("intro_sent") and tg:
        try:
            await tg.send_message(
                "🦅 <b>WLFI 專屬追蹤已上線</b>\n"
                "• 每 15 分鐘：鏈上大額轉帳（≥$200K,標注交易所流向）＋價格劇變（|1h|≥4%）\n"
                f"• 每日 {DIGEST_HOUR_TPE}:30：深度日報（價格位置/OI/資費/持有人數/"
                "鯨魚彙總/解鎖倒數/相關新聞）\n" + _DISCLAIMER, parse_mode="HTML")
            st["intro_sent"] = True
            _save(st)
        except Exception:  # noqa: BLE001
            pass
    while True:
        try:
            mkt = await _market()
            price = mkt.get("price")
            now = time.time()

            # ── ① 鏈上：新區塊 Transfer 掃描 ──
            head_hex = await _rpc("eth_blockNumber", [])
            if head_hex:
                head = int(head_hex, 16)
                frm = st.get("last_block") or (head - 80)
                frm = max(frm + 1, head - 1800)          # 補掃上限 ~6h,防冷啟動灌爆
                if head >= frm:
                    logs = await _rpc("eth_getLogs", [{
                        "address": CONTRACT, "topics": [_TRANSFER],
                        "fromBlock": hex(frm), "toBlock": hex(head)}])
                    whales_24h = st.get("whales_24h", [])
                    top_set = set((st.get("top_holders") or {}).keys())
                    tracked = set(st.get("tracked_wallets") or [])
                    pools = set(st.get("dex_pools") or [])
                    for lg in (logs or []):
                        tr = decode_transfer(lg)
                        if not tr or not price:
                            continue
                        usd = tr["amount"] * price
                        # v185：DEX 池流出=買入（≥$100K 即推,獨立門檻）
                        if (tr["from"] in pools and usd >= 100_000
                                and tr["tx"] not in st.get("seen_tx", [])):
                            if tg:
                                try:
                                    await tg.send_message(
                                        f"🦅💱 <b>DEX 大額買入</b> {tr['amount']:,.0f} 枚"
                                        f"≈${usd / 1e3:,.0f}K 自資金池流出 → "
                                        f"<code>{label_of(tr['to'])}</code>\n"
                                        "<i>鏈上觀察·非訊號</i>", parse_mode="HTML")
                                except Exception:  # noqa: BLE001
                                    pass
                            st.setdefault("seen_tx", []).append(tr["tx"])
                        # v185：交易所提幣收件戶→自動入追蹤名冊（空投提幣近似追蹤）
                        if tr["from"] in _EXCHANGES and usd >= 50_000:
                            tw = st.setdefault("tracked_wallets", [])
                            if tr["to"] not in tw and tr["to"] not in _EXCHANGES:
                                tw.append(tr["to"])
                                st["tracked_wallets"] = tw[-100:]
                        # v185：Top20/追蹤名冊地址動作用低門檻（$50K）
                        eff_thresh = (50_000 if (tr["from"] in top_set | tracked
                                                 or tr["to"] in top_set | tracked)
                                      else WHALE_USD)
                        if usd >= eff_thresh and tr["tx"] not in st.get("seen_tx", []):
                            card = render_whale_card(tr, price)
                            if tr["from"] in top_set or tr["to"] in top_set:
                                card = card.replace("🦅🐋", "🦅👑 <b>Top20 地址動作</b>·🐋")
                            elif tr["from"] in tracked or tr["to"] in tracked:
                                card = card.replace("🦅🐋", "🦅📇 <b>追蹤名冊動作</b>·🐋")
                            if tg:
                                try:
                                    await tg.send_message(card,
                                                          parse_mode="HTML",
                                                          disable_web_page_preview=True)
                                except Exception:  # noqa: BLE001
                                    pass
                            st.setdefault("seen_tx", []).append(tr["tx"])
                            st["seen_tx"] = st["seen_tx"][-300:]
                            whales_24h.append({"ts": now, "usd": usd,
                                               "flow": classify_flow(tr)})
                    st["whales_24h"] = [w for w in whales_24h
                                        if now - w["ts"] < 86400][-100:]
                    st["last_block"] = head

            # ── ② 行情劇變 ──
            if price:
                ref = st.get("px_1h_ref")
                if ref and ref.get("ts") and now - ref["ts"] >= 3600:
                    chg = (price / ref["px"] - 1) * 100
                    if abs(chg) >= PRICE_MOVE_PCT and tg:
                        try:
                            await tg.send_message(
                                f"🦅⚡ <b>WLFI 價格劇變</b> 1h {chg:+.1f}% → "
                                f"${price:.5f}\n資費 {mkt.get('funding_pct8h')}%/8h　"
                                f"OI ${(mkt.get('oi_usd') or 0) / 1e6:.1f}M\n"
                                "<i>觀察·非訊號</i>", parse_mode="HTML")
                        except Exception:  # noqa: BLE001
                            pass
                    st["px_1h_ref"] = {"ts": now, "px": price}
                elif not ref:
                    st["px_1h_ref"] = {"ts": now, "px": price}

            # ── ②b 小時級深度卡（v180 使用者指定：幣安=WLFI最深池,接OI/大戶/taker）──
            # 事件驅動（|ΔOI 1h|≥2% 或 |Δ價 1h|≥2% 或資費翻號）＋最少每 4h 一張心跳卡
            hr_ref = st.get("hourly_ref") or {}
            if now - (hr_ref.get("ts") or 0) >= 3600:
                # ── v185：DEX 池發現（一次性,快取）──
                if not st.get("dex_pools"):
                    try:
                        st["dex_pools"] = sorted(await _discover_pools())
                        if st["dex_pools"]:
                            print(f"[wlfi] DEX 池發現 {len(st['dex_pools'])} 個")
                    except Exception:  # noqa: BLE001
                        pass
                # ── v185：Top20 持倉每小時差分（增減 ≥0.05% 供給即推）──
                try:
                    cur_top = await _top_holders(20)
                    prev_top = st.get("top_holders") or {}
                    if cur_top and prev_top and tg:
                        supply = 1e11          # 總供給 100B（名目,比例用）
                        moves = []
                        for a, bal in cur_top.items():
                            d = bal - prev_top.get(a, bal)
                            if abs(d) >= supply * 0.0005:
                                moves.append((a, d))
                        for a, d in moves[:3]:
                            try:
                                await tg.send_message(
                                    f"🦅👑 <b>Top20 持倉變動</b> "
                                    f"<code>{label_of(a)}</code> "
                                    f"{'增持' if d > 0 else '減持'} {abs(d):,.0f} 枚"
                                    f"（{abs(d) / supply * 100:.2f}% 供給）\n"
                                    "<i>鏈上觀察·非訊號</i>", parse_mode="HTML")
                            except Exception:  # noqa: BLE001
                                pass
                    if cur_top:
                        st["top_holders"] = cur_top
                        top10_share = sum(sorted(cur_top.values())[-10:]) / 1e11 * 100
                        st["top10_share"] = round(top10_share, 2)
                except Exception:  # noqa: BLE001
                    pass
                # ── v185：每小時 WLFI 新聞掃描（新條目才推,最多 1 則/時）──
                try:
                    from news_feed.okx_news import _okx_news, parse_items
                    code, out = await asyncio.to_thread(
                        _okx_news, ["news", "by-coin", "--coins", "WLFI",
                                    "--lang", "zh-CN", "--limit", "5"])
                    if code == 0:
                        seen_news = st.get("seen_news", [])
                        for it in parse_items(out)[:5]:
                            nid = str(it.get("id") or it.get("title"))[:60]
                            if nid and nid not in seen_news:
                                t = (it.get("title") or "").strip()
                                if t and tg:
                                    await tg.send_message(
                                        f"🦅📰 <b>WLFI 動態</b>\n{t[:200]}\n"
                                        "<i>OKX News·僅供參考</i>", parse_mode="HTML")
                                seen_news.append(nid)
                                break
                        st["seen_news"] = seen_news[-50:]
                except Exception:  # noqa: BLE001
                    pass
                try:
                    from market_intel_mcp.sources.binance_perp import get_binance_perp
                    bsrc = get_binance_perp()
                    boi = await bsrc.get_oi("WLFI", "1h", 26)
                    bpos = await bsrc.get_positioning("WLFI", "1h", 3)
                    btak = await bsrc.get_taker_ratio("WLFI", "1h", 7)
                    oi_now = (boi or {}).get("latest")
                    oi_1h = None
                    srs = (boi or {}).get("series") or []
                    if len(srs) >= 2 and srs[-2].get("value"):
                        oi_1h = (srs[-1]["value"] / srs[-2]["value"] - 1) * 100
                    tt = (bpos or {}).get("latest")
                    tvols = [(s.get("buy_vol"), s.get("sell_vol"))
                             for s in ((btak or {}).get("series") or [])
                             if s.get("buy_vol") is not None]
                    net1h = (tvols[-1][0] - tvols[-1][1]) if tvols else None
                    px_chg = ((price / hr_ref["px"] - 1) * 100
                              if price and hr_ref.get("px") else None)
                    fr = mkt.get("funding_pct8h")
                    fr_flip = (hr_ref.get("fr") is not None and fr is not None
                               and (fr > 0) != (hr_ref["fr"] > 0))
                    event = ((oi_1h is not None and abs(oi_1h) >= 2.0)
                             or (px_chg is not None and abs(px_chg) >= 2.0)
                             or fr_flip)
                    heartbeat = now - (st.get("hourly_card_ts") or 0) >= 4 * 3600
                    if (event or heartbeat) and tg:
                        # 軋空燃料啟發式：OI升+價未漲+資費負=空方擁擠
                        squeeze = (oi_1h is not None and oi_1h > 2
                                   and (px_chg or 0) < 1 and (fr or 0) < 0)
                        lines = ["🦅📊 <b>WLFI 小時深度（幣安池）</b>"
                                 + ("　⚡事件" if event else "　♥心跳")]
                        if price:
                            lines.append(f"價 ${price:.5f}"
                                         + (f"（1h {px_chg:+.1f}%）" if px_chg is not None else ""))
                        if oi_now:
                            lines.append(f"幣安OI {oi_now:,.0f} 枚"
                                         + (f"（1h {oi_1h:+.1f}%）" if oi_1h is not None else ""))
                        if tt:
                            lines.append(f"大戶多空比 {tt:.2f}"
                                         + ("（偏多）" if tt > 1.1 else "（偏空）" if tt < 0.9 else ""))
                        if net1h is not None:
                            lines.append(f"taker 1h 淨流 {net1h:+,.0f}"
                                         + ("（買方主動）" if net1h > 0 else "（賣方主動）"))
                        if fr is not None:
                            lines.append(f"OKX 資費 {fr:+.4f}%/8h" + ("　🔁翻號" if fr_flip else ""))
                        if squeeze:
                            lines.append("🧨 軋空燃料觀察：OI升+價滯+負資費=空方擁擠（觀察非預測）")
                        lines.append("<i>幣安+OKX 雙池觀察·非訊號</i>")
                        try:
                            await tg.send_message("\n".join(lines), parse_mode="HTML")
                            st["hourly_card_ts"] = now
                        except Exception:  # noqa: BLE001
                            pass
                    st["hourly_ref"] = {"ts": now, "px": price, "fr": fr}
                except Exception as e:  # noqa: BLE001
                    print(f"[wlfi] hourly 深度失敗（下小時再試）：{type(e).__name__}: {e}")

            # ── ③ 每日日報（09:30 台北 = 01:30 UTC；poll 窗內只發一次）──
            tpe_now = time.gmtime(now + 8 * 3600)
            day_key = time.strftime("%Y-%m-%d", tpe_now)
            if (tpe_now.tm_hour == DIGEST_HOUR_TPE and tpe_now.tm_min >= 30
                    and st.get("digest_day") != day_key and tg):
                holders = await _holders()
                ws = st.get("whales_24h", [])
                in_ex = sum(w["usd"] for w in ws if "賣壓" in w["flow"])
                out_ex = sum(w["usd"] for w in ws if "囤積" in w["flow"])
                ath, atl = 0.3313, 0.05144       # 現貨口徑（研究定案,人工更新）
                news_lines = []
                try:
                    from news_feed.okx_news import _okx_news, parse_items
                    code, out = await asyncio.to_thread(
                        _okx_news, ["news", "by-coin", "--coins", "WLFI",
                                    "--lang", "zh-CN", "--limit", "5"])
                    if code == 0:
                        for it in parse_items(out)[:2]:
                            t = (it.get("title") or "").strip()
                            if t:
                                news_lines.append(f"• {t[:80]}")
                except Exception:  # noqa: BLE001
                    pass
                card = ["🦅 <b>WLFI 每日追蹤日報</b>"]
                if price:
                    card.append(f"價格 <b>${price:.5f}</b>　距歷史低 "
                                f"{(price / atl - 1) * 100:+.1f}%　距歷史高 "
                                f"{(price / ath - 1) * 100:+.1f}%")
                card.append(f"24h量 ${(mkt.get('vol24h_usd') or 0) / 1e6:.1f}M　"
                            f"OI ${(mkt.get('oi_usd') or 0) / 1e6:.1f}M　"
                            f"資費 {mkt.get('funding_pct8h')}%/8h")
                if holders:
                    prev = st.get("holders_prev")
                    d = f"（{holders - prev:+,} vs 昨日）" if prev else ""
                    card.append(f"持有地址 {holders:,}{d}")
                    st["holders_prev"] = holders
                card.append(f"24h 鯨魚：入交易所 ${in_ex / 1e6:.2f}M｜"
                            f"提出 ${out_ex / 1e6:.2f}M｜共 {len(ws)} 筆≥$200K")
                # v186：USD1 生態命脈——供給成長=採用擴張,領先指標
                u1 = await _usd1_supply()
                if u1:
                    prev_u1 = st.get("usd1_prev")
                    d1 = (f"（vs 昨日 {(u1 - prev_u1) / 1e6:+,.0f}M）"
                          if prev_u1 else "")
                    card.append(f"💵 USD1 供給 ${u1 / 1e9:.2f}B{d1}"
                                f"　Top10 集中度 {st.get('top10_share', '—')}%")
                    st["usd1_prev"] = u1
                _days = max(0, int((1841500800 - now) / 86400))   # 2028-05-06
                card.append(f"⏳ 解鎖牆倒數 {_days} 天（2028-05,543億枚=流通171%）")
                if news_lines:
                    card.append("📰 相關動態：\n" + "\n".join(news_lines))
                card.append(_DISCLAIMER)
                try:
                    await tg.send_message("\n".join(card), parse_mode="HTML")
                    st["digest_day"] = day_key
                except Exception:  # noqa: BLE001
                    pass
            _save(st)
        except Exception as e:  # noqa: BLE001
            print(f"[wlfi] loop 例外（不致命）：{type(e).__name__}: {e}")
        await asyncio.sleep(max(120, int(poll_seconds)))
