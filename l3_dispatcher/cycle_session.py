# -*- coding: utf-8 -*-
"""cycle_session.py — 「熊底→牛頂」週期/部位層(shadow) step2：24/7 獨立觀測 Session。

每日一輪：對目標宇宙（BTC + 主流/RWA/DeFi/AI）算 cycle_regime（200週均線/Mayer/距ATH/
合流），寫 cycle_shadow.jsonl，推一張『週期觀察』卡到專屬 Telegram 主題（key="cycle"，
未provision則落 General）。回答使用者要的「現在每個標的在熊底→牛頂的哪一段」。

⛔ 影子鐵則（研究 cycle-position-layer-research 對抗審查定案，務必遵守）：
   純觀測 / display-only；永不 import strength/evaluate/fire_queue/下單；不進 symbol_gate；
   不碰真錢（紅線①）。每張卡帶誠實免責（n≈3-4 非獨立、結構上無統計顯著、非底點承諾、
   分批累積非梭哈、便宜可以更便宜）。與 4h 戰術引擎完全隔離、資金/樣本不互污染。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from typing import Optional

from botpaths import db_path, data_dir
from l3_dispatcher.cycle_regime import classify_cycle_phase, render_cycle_line, DISCLAIMER

# 點名保底宇宙：使用者指定的主流/RWA/DeFi/AI + BTC 基準（缺資料者自動略過）。
CYCLE_UNIVERSE = [
    "BTC", "ETH", "SOL", "BNB", "XRP",            # 主流/交易所
    "AAVE", "AVAX", "ONDO", "WLFI",               # DeFi / RWA
    "SUI", "APT", "XLM", "HYPE", "ASTER", "OKB",  # L1/DEX/交易所
    "TAO", "RENDER", "FET",                        # AI
]


def discover_universe(min_days: int = 200) -> list[str]:
    """v110（使用者回饋「不要只有點名那幾檔」）：自動探索快取裡所有有 ≥min_days 天
    日線的標的，與點名清單聯集。純讀快取、零網路；壞庫→退回點名清單。"""
    syms = set(CYCLE_UNIVERSE)
    try:
        conn = sqlite3.connect(f"file:{db_path('ohlc_cache.db')}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT symbol, COUNT(*) FROM ohlc WHERE tf='1d' "
                "GROUP BY symbol HAVING COUNT(*) >= ?", (min_days,)
            ).fetchall()
        finally:
            conn.close()
        for s, _n in rows:
            base = (s or "").split("/")[0].split("-")[0].strip().upper()
            if base and base.isalnum() and not base.startswith("USD"):
                syms.add(base)
    except Exception:  # noqa: BLE001 — 探索失敗不致命，用點名保底
        pass
    return sorted(syms)
SHADOW_LOG = data_dir() / "cycle_shadow.jsonl"
_MIN_DAYS = 200   # 至少 200 天才算（200日均線/Mayer 需要）


def _daily_closes(symbol: str) -> list[float]:
    """讀 ohlc_cache.db 的 1d 收盤（唯讀、純讀既有快取，零網路）。"""
    try:
        conn = sqlite3.connect(f"file:{db_path('ohlc_cache.db')}?mode=ro", uri=True)
    except Exception:
        try:
            conn = sqlite3.connect(str(db_path("ohlc_cache.db")))
        except Exception:
            return []
    try:
        rows = conn.execute(
            "SELECT close FROM ohlc WHERE symbol LIKE ? AND tf='1d' "
            "AND close IS NOT NULL ORDER BY ts", (symbol + "%",)
        ).fetchall()
        return [float(r[0]) for r in rows if r[0]]
    except Exception:
        return []
    finally:
        conn.close()


def compute_cycle_read(symbol: str) -> Optional[dict]:
    """單一標的週期讀數（缺資料→None，不臆測）。"""
    cl = _daily_closes(symbol)
    if len(cl) < _MIN_DAYS:
        return None
    price = cl[-1]
    ma200d = sum(cl[-200:]) / 200
    ma200w = sum(cl[-1400:]) / min(1400, len(cl))   # 200週≈1400天（資料不足則用全程均）
    ath = max(cl)
    r = classify_cycle_phase(price, ma200d, ma200w, ath)
    r["symbol"] = symbol
    r["price"] = round(price, 6)
    r["n_days"] = len(cl)
    return r


def build_cycle_card(reads: list[dict]) -> str:
    """把多標的讀數渲染成『週期觀察』卡（深度價值帶優先排序 + 誠實免責）。"""
    order = {"deep_value": 0, "value": 1, "neutral": 2, "elevated": 3, "euphoria": 4}
    reads = sorted(reads, key=lambda r: (order.get(r["value_zone"], 9),
                                         r.get("mayer") if r.get("mayer") is not None else 9))
    lines = [
        "🌊 <b>週期觀察 · 熊底→牛頂</b>（每日影子・純定位非進場訊號）",
        f"<i>{time.strftime('%Y-%m-%d %H:%M', time.localtime())}</i>",
        "",
    ]
    deep = [r for r in reads if r["value_zone"] == "deep_value"]
    if deep:
        lines.append("🟢 <b>深度價值帶</b>（歷史上機率較高的『分批累積』區間）")
        for r in deep:
            lines.append("　" + render_cycle_line(r["symbol"], r).replace("\n", " "))
        lines.append("")
    rest = [r for r in reads if r["value_zone"] != "deep_value"]
    if rest:
        lines.append("⚪ 其餘")
        for r in rest[:12]:   # v110：宇宙自動探索後防爆長——其餘段最多 12 檔
            lines.append("　" + render_cycle_line(r["symbol"], r).replace("\n", " "))
        if len(rest) > 12:
            lines.append(f"　…另 {len(rest) - 12} 檔（完整見 cycle_shadow.jsonl）")
        lines.append("")
    lines.append(f"⚠️ <i>{DISCLAIMER}</i>")
    return "\n".join(lines)


def _append_shadow(reads: list[dict]) -> None:
    """寫 cycle_shadow.jsonl（觀測留痕，供日後事件研究；永不回寫任何決策表）。"""
    try:
        SHADOW_LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": int(time.time() * 1000),
               "reads": [{k: r.get(k) for k in
                          ("symbol", "value_zone", "phase", "mayer",
                           "dist_200wma_pct", "drawdown_pct", "confluence_n")}
                         for r in reads]}
        with open(SHADOW_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        print(f"[cycle] shadow log 寫入失敗：{type(e).__name__}: {e}")


def _dominance_history() -> list[tuple[int, float]]:
    """從 cycle_shadow.jsonl 讀每日累積的 dominance 點（bottom_dashboard 紀錄）。"""
    out: list[tuple[int, float]] = []
    try:
        with open(SHADOW_LOG, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if rec.get("record_type") == "bottom_dashboard" and rec.get("dominance") is not None:
                    out.append((rec.get("ts", 0), float(rec["dominance"])))
    except Exception:  # noqa: BLE001
        pass
    return out


def _build_bottom_dashboard(reads: list[dict]) -> tuple[str, Optional[dict]]:
    """v111：熊底合流儀表板（BTC 層級、每日一組）。回 (卡頂區塊, shadow紀錄)。
    任一步失敗→('', None) 不影響原卡（影子鐵則：display-only、失敗不致命）。"""
    try:
        from l3_dispatcher import bottom_feeds as bf
        from l3_dispatcher.bottom_confluence import compute_bottom_score, render_dashboard_block
        btc = next((r for r in reads if r.get("symbol") == "BTC"), None)
        if not btc:
            return "", None
        inputs, background, overlay = bf.collect_bottom_inputs(
            price_now=btc.get("price"), mayer=btc.get("mayer"),
            dist_200wma_pct=btc.get("dist_200wma_pct"),
            dominance_history=_dominance_history())
        res = compute_bottom_score(inputs)
        block = render_dashboard_block(res, background, overlay)
        rec = {"record_type": "bottom_dashboard", "ts": int(time.time() * 1000),
               "score": res.get("score"), "band": res.get("band"),
               "present_mass_pct": res.get("present_mass_pct"),
               "factor_states": res.get("factor_states"),
               "inputs": {k: inputs.get(k) for k in
                          ("mvrv_z", "price", "realized_price", "mayer",
                           "dist_200wma_pct", "fng_avg30")},
               "dominance": bf.fetch_dominance_today()}
        return block, rec
    except Exception as e:  # noqa: BLE001
        print(f"[cycle] 熊底儀表板略過（不致命）：{type(e).__name__}: {e}")
        return "", None


def run_cycle_once() -> tuple[list[dict], str]:
    """跑一輪：算全宇宙讀數 + 熊底儀表板 + 組卡（純函式式，便於離線測試/CLI）。"""
    reads = []
    for sym in discover_universe():
        try:
            r = compute_cycle_read(sym)
            if r:
                reads.append(r)
        except Exception:  # noqa: BLE001
            continue
    card = build_cycle_card(reads) if reads else ""
    dash, dash_rec = _build_bottom_dashboard(reads)
    if dash and card:
        card = dash + "\n━━━━━━━━━━━━━━\n" + card
    if dash_rec:
        try:
            SHADOW_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(SHADOW_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(dash_rec, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass
    return reads, card


def _last_dashboard_age_s() -> Optional[float]:
    """cycle_shadow.jsonl 最後一筆 bottom_dashboard 的年齡（秒）。無紀錄回 None。"""
    last_ts = None
    try:
        with open(SHADOW_LOG, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if rec.get("record_type") == "bottom_dashboard" and rec.get("ts"):
                    last_ts = float(rec["ts"])
    except Exception:  # noqa: BLE001
        return None
    if last_ts is None:
        return None
    if last_ts > 1e12:                      # ms → s
        last_ts /= 1000.0
    return max(0.0, time.time() - last_ts)


async def run_cycle_loop(tg=None, interval_seconds: int = 86400):
    """24/7 週期觀測 worker（每日一輪）。tg=已綁定 cycle 主題的 client（缺則不推、僅寫log）。

    影子鐵則：純觀測；不 import strength/fire；不下單；不碰真錢。"""
    await asyncio.sleep(120)   # 啟動延後，讓快取/掃描先暖機
    # v137：啟動守衛（仿 daily_macro startup skip）——20h 內已發過儀表板就不重跑首輪，
    # 治「部署重啟日 🌊 同分卡連發 7 張＋dominance 歷史同日灌水」（2026-07-28 稽核實證）
    try:
        age_s = _last_dashboard_age_s()
        if age_s is not None and age_s < 20 * 3600:
            wait = max(600, int(interval_seconds) - int(age_s))
            print(f"[cycle] startup skip（{age_s / 3600:.1f}h 前已發過儀表板），"
                  f"{wait / 3600:.1f}h 後再跑")
            await asyncio.sleep(wait)
    except Exception:  # noqa: BLE001
        pass
    while True:
        try:
            # v111：run_cycle_once 內含外網抓取(CM CSV 可達數十秒)——丟 thread 避免阻塞事件迴圈
            reads, card = await asyncio.to_thread(run_cycle_once)
            _append_shadow(reads)
            if reads:
                print(f"[cycle] 週期觀察 {len(reads)} 標的；深度價值帶："
                      f"{[r['symbol'] for r in reads if r['value_zone']=='deep_value']}")
                if tg is not None and card:
                    try:
                        await tg.send_message(card, parse_mode="HTML")
                    except Exception as e:  # noqa: BLE001
                        print(f"[cycle] TG 推送失敗：{type(e).__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"[cycle] loop 例外（不致命）：{type(e).__name__}: {e}")
        await asyncio.sleep(max(3600, int(interval_seconds)))


if __name__ == "__main__":
    rs, card = run_cycle_once()
    print(f"算出 {len(rs)} 標的")
    import re
    print(re.sub(r"<[^>]+>", "", card))
