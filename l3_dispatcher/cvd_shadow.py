"""task#20 cvd_slope 治本 · 第二步「影子接線」（純觀測、零訊號數學變更）。

第一步（v61，已 SHIP）＝忠實度回測閘：證明免 key Binance 自算 CVD 是 CoinGlass
聚合 CVD 的忠實代理（每根 delta 時序 r 中位 0.928、cvd_slope_7d 同號率 100%）。

本檔＝第二步：開始「回補缺的數據」。資料缺口在 universe 排名路徑——
coinglass.py:666 對每個 universe 幣硬塞 `cvd_slope_7d = 0.0`（z-score 恆 0 →
strength.py 的 20% CVD 權重在排名路徑等於死掉）。本影子 worker 每小時：
    1. 取 universe 因子快照（source.get_strength_universe，bounded N，重用 daemon
       共用限流器 + TTL 快取）——這是 strength 排名器「實際吃到」的因子（含 0.0 stub）。
    2. 對同一批幣用免 key Binance klines 自算 cvd_slope_7d / cvd_slope（零額度）。
    3. 把「實際因子（含 0.0 缺口）＋ 該補的 Binance CVD 值」併成一筆 JSONL 寫進
       獨立 sink：data_dir()/cvd_shadow.jsonl。

日後的「EV 閘」（離線、另案）可讀這份 JSONL 做**反事實重排序**：
「若 cvd_slope_7d 用 Binance 值取代 0.0，universe 排名/EV 是否改善？」過閘才晉升
去真填 coinglass.py:666（走 RUNBOOK #26）。離線 EV 閘可以 import strength.py；
但本 worker 不行（見影子鐵則）。

════════════════════════════════════════════════════════════════════════════
影子鐵則（與 convergence_shadow 同一套，絕不可違反）：
    * 本 worker **永不** 把 binance_cvd_slope_7d 寫回 universe items / strength_score；
      **永不** 寫 fire_queue / snapshot / symbol_gate / 任何下單路徑；
      **永不** import market_intel_mcp.strength 或 l2_trigger.signals。
    * 唯一輸出 = 自己的 cvd_shadow.jsonl（觀測用，給日後 EV 閘離線反事實重排序）。
    * CVD 數學唯一來源 = backtest.binance_cvd_validate.cvd_slopes_from_klines
      （v61 回測閘已認證的純函式；本檔只重用、不另寫一份數學）。
    * 整個迴圈體包 try/except；任何源失敗都吞掉續跑，絕不拖垮 daemon
      （外層另有 supervise() 崩潰隔離 + 退避重啟）。
    * 不發 Telegram（純背景觀測，不打擾使用者）。
════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json

# universe 因子快照取樣數（對它們逐幣免費自算 Binance CVD；上限保守省 CoinGlass 額度）
_UNIVERSE_N = 20
# Binance CVD 窗口（與回測閘一致：1h × 168 根 = 7d）
_CVD_INTERVAL = "1h"
_CVD_LIMIT = 168
# JSONL sink 軟上限（位元組）；超過就先改名 .1 再重開，避免無限長
_SINK_MAX_BYTES = 5_000_000


def _sink_path():
    from botpaths import data_dir
    return data_dir() / "cvd_shadow.jsonl"


def _append_jsonl(record: dict) -> None:
    """把一輪觀測寫一行 JSONL（純本地檔；超過軟上限就輪替一次）。"""
    path = _sink_path()
    try:
        if path.exists() and path.stat().st_size > _SINK_MAX_BYTES:
            backup = path.with_suffix(".jsonl.1")
            try:
                if backup.exists():
                    backup.unlink()
                path.rename(backup)
            except OSError:
                pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 觀測 sink 寫失敗不致命，吞掉續跑


# ════════════════════════════════════════════════════════════════════════
#  純函式：把「universe 因子 + Binance CVD」併成一筆觀測記錄（離線可測）
# ════════════════════════════════════════════════════════════════════════
# universe item 裡要保留的因子鍵（strength 排名器實際吃到的；cvd_slope_7d 是 0.0 缺口）
_FACTOR_KEYS = (
    "return_7d_pct", "vol_24h_usd", "vol_24h_vs_30d",
    "oi_delta_7d_pct", "cvd_slope_7d", "top_trader_dev",
    "btc_corr_30d", "funding",
)


def _build_item(symbol: str, factors: dict, cvd: dict | None) -> dict:
    """把一個 universe 因子 dict + 一份 cvd_slopes_from_klines 輸出併成觀測 item。

    純函式（無 I/O）：factors=get_strength_universe 的單筆 item；cvd=Binance 自算
    （None＝Binance 取數失敗，誠實記 None 不捏造，紅線③）。
    刻意把「實際因子（含 0.0 stub 缺口）」與「該補的 Binance 值」並排，供離線反事實重排。
    """
    factors_live = {k: factors.get(k) for k in _FACTOR_KEYS}
    stub_cvd = factors_live.get("cvd_slope_7d")
    item = {
        "symbol": symbol,
        "factors_live": factors_live,        # strength 排名器實際吃到的（cvd_slope_7d=0.0 缺口）
        "stub_cvd_slope_7d": stub_cvd,       # 明確標出 universe 路徑現用的 stub（通常 0.0）
        "binance_cvd_slope_7d": None,
        "binance_cvd_slope": None,
        "binance_cvd": None,
        "binance_bars": 0,
        "binance_ok": False,
    }
    if isinstance(cvd, dict):
        item["binance_cvd_slope_7d"] = cvd.get("cvd_slope_7d")
        item["binance_cvd_slope"] = cvd.get("cvd_slope")
        item["binance_cvd"] = cvd.get("cvd")
        item["binance_bars"] = len(cvd.get("series") or [])
        item["binance_ok"] = True
    return item


# ════════════════════════════════════════════════════════════════════════
#  Live 取數（唯讀；Binance 免 key、CoinGlass 重用 daemon 共用 source）
# ════════════════════════════════════════════════════════════════════════
async def _binance_cvd(bn, symbol: str) -> dict | None:
    """免 key 取 Binance 原始 klines（含 row[10] taker buy quote）→ 認證純函式自算 CVD。

    用 bn._get 直取原始 klines（get_candles 會丟掉 row[10]，無法算 CVD），與 v61
    回測閘 _binance_cvd 同一條取數路徑。失敗回 None（不 raise）。
    """
    from backtest.binance_cvd_validate import cvd_slopes_from_klines
    try:
        sym = bn._sym(symbol)
        body = await bn._get(
            "/fapi/v1/klines",
            {"symbol": sym, "interval": _CVD_INTERVAL,
             "limit": min(max(_CVD_LIMIT, 1), 1500)},
            symbol, "cvd_shadow",
        )
        if isinstance(body, dict) and body.get("error"):
            return None
        if not isinstance(body, list) or not body:
            return None
        return cvd_slopes_from_klines(body)
    except Exception:
        return None


async def _universe_factors(source, n: int) -> list[dict]:
    """取 universe 因子快照（bounded N，重用 daemon source 共用限流器 + TTL 快取）。

    回 get_strength_universe 的 items（每筆含 0.0 cvd_slope_7d stub 缺口）；失敗回 []。
    """
    if source is None or not hasattr(source, "get_strength_universe"):
        return []
    try:
        r = await source.get_strength_universe(limit=n)
        if isinstance(r, dict) and not r.get("error"):
            return list(r.get("items") or [])
    except Exception:
        return []
    return []


async def _run_cycle(source=None, n: int = _UNIVERSE_N) -> dict:
    """跑一輪 CVD 影子觀測，回一個可序列化摘要 dict。

    `source`＝daemon 主 source（backend=coinglass 時即 CoinGlassSource）；
    None → 延遲 get_source()（供一次性測試）。
    """
    from market_intel_mcp.sources.binance_perp import get_binance_perp

    if source is None:
        try:
            from market_intel_mcp.sources import get_source
            source = get_source()
        except Exception:
            source = None

    bn = get_binance_perp()

    # 1) universe 因子快照（strength 排名器實際吃到的；cvd_slope_7d 是 0.0 缺口）
    factor_items = await _universe_factors(source, n)
    syms = [it.get("symbol") for it in factor_items if it.get("symbol")]

    # 2) 對同一批幣免費自算 Binance CVD（序列化，尊重 source 限流；Binance 免 key）
    cvd_results = await asyncio.gather(
        *[_binance_cvd(bn, s) for s in syms],
        return_exceptions=True,
    )
    cvd_map: dict[str, dict | None] = {}
    for s, r in zip(syms, cvd_results):
        cvd_map[s] = r if isinstance(r, dict) else None

    # 3) 併記錄（純函式 _build_item）
    items = []
    for it in factor_items:
        sym = it.get("symbol")
        if not sym:
            continue
        items.append(_build_item(sym, it, cvd_map.get(sym)))

    n_binance_ok = sum(1 for it in items if it.get("binance_ok"))
    return {
        "ts": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "universe_n": n,
        "captured": len(items),
        "binance_ok": n_binance_ok,
        "items": items,
        "note": "shadow-only: binance_cvd_slope_7d 從不寫回 universe/strength/fire；"
                "補捉 universe 路徑缺的 cvd_slope_7d（現為 coinglass.py:666 的 0.0 stub）"
                "供日後 EV 閘離線反事實重排序；CVD 數學＝v61 回測閘認證純函式",
    }


async def run_cvd_shadow_loop(source=None, interval_seconds: int = 3600):
    """task#20 CVD 影子觀測常駐迴圈（每 interval 跑一輪，純觀測寫 cvd_shadow.jsonl）。

    `source`＝daemon 主 source（用其 get_strength_universe 取 universe 因子，bounded
    + 共用 TTL 快取）。Binance CVD 走 binance_perp 單例（免 key、零額度）。
    """
    # 啟動稍緩，避開開機尖峰（與其他影子 worker 一致）
    await asyncio.sleep(90)
    while True:
        try:
            summary = await _run_cycle(source)
            _append_jsonl(summary)
            print(f"[cvd_shadow] universe_n={summary['universe_n']} "
                  f"captured={summary['captured']} "
                  f"binance_ok={summary['binance_ok']}")
        except Exception as e:  # 整輪保護：任何意外吞掉續跑，不拖垮 daemon
            print(f"[cvd_shadow] cycle error: {e}")
        await asyncio.sleep(max(60, int(interval_seconds)))


# ════════════════════════════════════════════════════════════════════════
#  純函式自測（離線、零網路）
# ════════════════════════════════════════════════════════════════════════
def _selftest() -> int:
    # 1) 正常併記：factors 帶 0.0 stub、cvd 有值 → item 並排兩者、binance_ok=True
    factors = {
        "return_7d_pct": 5.0, "vol_24h_usd": 1e9, "vol_24h_vs_30d": 1.2,
        "oi_delta_7d_pct": 3.0, "cvd_slope_7d": 0.0, "top_trader_dev": 0.05,
        "btc_corr_30d": 0.7, "funding": 0.0001,
    }
    cvd = {"cvd": 1234.5, "cvd_slope": 8.9, "cvd_slope_7d": 12.34,
           "series": [{"ts": 1, "value": 1.0}] * 168, "deltas": []}
    item = _build_item("BTC", factors, cvd)
    assert item["symbol"] == "BTC"
    assert item["stub_cvd_slope_7d"] == 0.0
    assert item["factors_live"]["cvd_slope_7d"] == 0.0      # 缺口被忠實保留
    assert item["binance_cvd_slope_7d"] == 12.34
    assert item["binance_cvd_slope"] == 8.9
    assert item["binance_bars"] == 168
    assert item["binance_ok"] is True
    print("  ✅ 正常併記：0.0 stub 與 Binance 值並排、binance_ok=True")

    # 2) Binance 取數失敗（cvd=None）→ 誠實記 None、binance_ok=False（不捏造，紅線③）
    item2 = _build_item("ETH", factors, None)
    assert item2["binance_cvd_slope_7d"] is None
    assert item2["binance_ok"] is False
    assert item2["binance_bars"] == 0
    assert item2["factors_live"]["return_7d_pct"] == 5.0    # 因子仍完整保留
    print("  ✅ Binance 缺料 → 誠實 None、binance_ok=False")

    # 3) 只保留白名單因子鍵，雜鍵不外漏（避免 sink 膨脹/夾帶非預期欄）
    noisy = dict(factors, secret="x", _internal=1)
    item3 = _build_item("SOL", noisy, cvd)
    assert "secret" not in item3["factors_live"] and "_internal" not in item3["factors_live"]
    assert set(item3["factors_live"].keys()) == set(_FACTOR_KEYS)
    print("  ✅ 因子鍵白名單：雜鍵不外漏")

    # 4) 記錄可 JSON 序列化（sink 寫得出去）
    rec = {"ts": "2026-01-01T00:00:00+00:00", "universe_n": 1, "captured": 1,
           "binance_ok": 1, "items": [item], "note": "x"}
    s = json.dumps(rec, ensure_ascii=False)
    assert "BTC" in s and "binance_cvd_slope_7d" in s
    print("  ✅ 記錄可 JSON 序列化")

    print("自測通過：影子 item 併記（0.0 缺口並排/誠實 None/因子白名單）+ 可序列化 ✅")
    return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        raise SystemExit(_selftest())
    # 一次性 live 試跑（唯讀；需 .env 的 CoinGlass 金鑰才取得到 universe 因子）
    import os
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    # ⚠️ 環境陷阱：MARKET_INTEL_BACKEND 預設 "mock"（settings.py:19），且 daemon 是用
    # run_bot 程式內設成 "coinglass"、非寫在 .env，故獨立 CLI 不設就會悄悄打到 MockSource
    # 拿到「假的非零 cvd_slope_7d」誤判缺口已補。這裡 setdefault 成 coinglass 讓 dev 一次性
    # 試跑打到真源（須在第一次 import market_intel_mcp.settings 凍結 SETTINGS 之前設）。
    os.environ.setdefault("MARKET_INTEL_BACKEND", "coinglass")

    async def _once():
        summary = await _run_cycle()
        print(json.dumps(summary, ensure_ascii=False, indent=2)[:2000])
        return 0
    raise SystemExit(asyncio.run(_once()))
