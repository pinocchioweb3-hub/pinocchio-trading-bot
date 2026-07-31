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
                    for lg in (logs or []):
                        tr = decode_transfer(lg)
                        if not tr or not price:
                            continue
                        usd = tr["amount"] * price
                        if usd >= WHALE_USD and tr["tx"] not in st.get("seen_tx", []):
                            if tg:
                                try:
                                    await tg.send_message(render_whale_card(tr, price),
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
