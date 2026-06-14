"""market-intel-mcp FastMCP 主入口。

執行方式：
    python -m market_intel_mcp.server                      # stdio 啟動
    MARKET_INTEL_BACKEND=mock python -m market_intel_mcp.server

工具：mi_get_positioning / mi_get_oi / mi_get_funding / mi_get_liquidations
     mi_get_cvd / mi_get_btc_gate / mi_get_strength_rank / mi_get_snapshot
     mi_query_view（白名單）/ mi_health

所有工具 readOnlyHint=True，無下單/寫入。錯誤透過 errors.make_error()
回傳結構化 dict（保留 upstream_status / suggestion）以利 LLM 自我修復。
"""
from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal

try:
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations
    _MCP_AVAILABLE = True
except ImportError:
    # 允許未安裝 mcp 套件時仍可 import tools 給單元測試/demo 用
    _MCP_AVAILABLE = False
    FastMCP = None  # type: ignore

from pydantic import Field

from .errors import make_error
from .settings import SETTINGS
from .sources import get_source
from .strength import WEIGHTS, compute_strength_scores
from .symbol_mapping import (
    CORE_SYMBOLS,
    HOT_SYMBOLS,
    normalize,
)

# ===========================================================================
# 共用：source 單例 + ToolAnnotations
# ===========================================================================
SOURCE = get_source()
_READONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True) if _MCP_AVAILABLE else None

if _MCP_AVAILABLE:
    mcp = FastMCP("market-intel-mcp")
else:
    mcp = None  # demo 模式可直接呼叫底下的 _impl_* 函式


def _tool(fn):
    """裝飾器：若 MCP 可用就註冊；否則只回傳原函式（讓 demo 直跑）"""
    if mcp is not None:
        return mcp.tool(annotations=_READONLY)(fn)
    return fn


# ===========================================================================
# 工具實作
# ===========================================================================
@_tool
async def mi_get_positioning(
    symbol: Annotated[str, Field(description="OKX (SUI-USDT-SWAP) 或 CoinGlass (SUIUSDT) 形式，自動正規化")],
    ratio_type: Annotated[
        Literal["account", "position", "top_trader_account", "top_trader_position"],
        Field(description="account=散戶帳戶比；top_trader_*=大戶（按帳戶或持倉）。預設 top_trader_position（最常用）"),
    ] = "top_trader_position",
    window: Annotated[str, Field(description="1h | 4h | 1d | 1w；預設 4h")] = "4h",
    limit: Annotated[int, Field(ge=1, le=500, description="歷史點數")] = 96,
) -> dict:
    """多空比 / 大戶多空比。latest>1 偏多，<1 偏空。

    回傳：{symbol, source, ratio_type, latest, series:[{ts,value}], delta_pct}
    錯誤格式：{error:True, code, message, suggestion, ...}
    """
    sym = normalize(symbol)
    return await SOURCE.get_positioning(sym, ratio_type, window, limit)


@_tool
async def mi_get_oi(
    symbol: Annotated[str, Field()],
    window: Annotated[str, Field(description="K 線時框")] = "1h",
    limit: Annotated[int, Field(ge=1, le=500)] = 96,
) -> dict:
    """聚合 OI（未平倉量）+ 24h 變化% + 時序。"""
    return await SOURCE.get_oi(normalize(symbol), window, limit)


@_tool
async def mi_get_funding(
    symbol: Annotated[str, Field()],
) -> dict:
    """當前資金費率 + 預估下次。負值 = 空方付錢（軋空燃料）；過熱 = 多殺多風險。"""
    return await SOURCE.get_funding(normalize(symbol))


@_tool
async def mi_get_liquidations(
    symbol: Annotated[str, Field()],
    window: Annotated[str, Field(description="統計視窗")] = "24h",
) -> dict:
    """指定視窗內多/空清算量。liq_short > liq_long 通常代表軋空。"""
    return await SOURCE.get_liquidations(normalize(symbol), window)


@_tool
async def mi_get_cvd(
    symbol: Annotated[str, Field()],
    window: Annotated[str, Field()] = "1h",
    limit: Annotated[int, Field(ge=1, le=500)] = 96,
) -> dict:
    """CVD（累計委託深度差）時序 + 短期斜率 + 7d 斜率 + 價量背離旗標。
    本工具優先用 LocalSource（從成交明細算）。
    """
    return await SOURCE.get_cvd_series(normalize(symbol), window, limit)


@_tool
async def mi_get_btc_gate() -> dict:
    """BTC 宏觀閘狀態：閘開規則 = 4h 收 > 4h 200MA AND regime ≠ trend_down。
    閘關 → L2 整包 HOLD（不論其他訊號）。
    """
    return await SOURCE.get_btc_gate()


@_tool
async def mi_get_strength_rank(
    limit: Annotated[int, Field(ge=1, le=200, description="候選池上限")] = 50,
    top_n: Annotated[int, Field(ge=1, le=50, description="回傳前 N 名")] = 20,
) -> dict:
    """強勢標的排行（6 因子加權，0-100 分）。

    硬性過濾門檻（由 source 端先做）+ z-score 加權公式（在 strength.py）。
    回傳：{ts, source, weights, items:[排序後]}
    Top N → 餵給 watchlist 自動更新（每週一/每日重排）
    """
    universe = await SOURCE.get_strength_universe(limit)
    if isinstance(universe, dict) and universe.get("error"):
        return universe

    items = universe.get("items", [])
    scored = compute_strength_scores(items)
    return {
        "source": universe.get("source", SOURCE.name),
        "ts": universe.get("ts"),
        "weights": WEIGHTS,
        "items": scored[:top_n],
    }


@_tool
async def _fill_stale_from_binance(sym: str, tf: str, snap: dict,
                                   stale_fields: list) -> None:
    """v33：CoinGlass/OKX 欄位 stale 時，用 Binance 永續(免key)補資料，提升資料品質。
    僅補 Binance 能提供的欄位；補到的從 stale_fields 移除。任何失敗靜默略過。"""
    fillable = {"price", "ts", "oi", "oi_delta_pct", "funding",
                "funding_predicted", "top_trader_ratio", "ls_ratio"}
    need = fillable & set(stale_fields)
    if not need:
        return
    try:
        from .sources.binance_perp import get_binance_perp
        src = get_binance_perp()

        async def _s(c):
            try:
                return await c
            except Exception:
                return None
        tasks, keys = [], []
        if {"price", "ts"} & need:
            tasks.append(_s(src.get_candles(sym, tf, 2))); keys.append("k")
        if {"oi", "oi_delta_pct"} & need:
            tasks.append(_s(src.get_oi(sym, tf, 30))); keys.append("oi")
        if {"funding", "funding_predicted"} & need:
            tasks.append(_s(src.get_funding(sym))); keys.append("f")
        if "top_trader_ratio" in need:
            tasks.append(_s(src.get_positioning(sym, tf, 5))); keys.append("tt")
        if "ls_ratio" in need:
            tasks.append(_s(src.get_global_positioning(sym, tf, 5))); keys.append("ls")
        res = dict(zip(keys, await asyncio.gather(*tasks)))

        def _good(r):
            return isinstance(r, dict) and not r.get("error")
        filled = []
        if "k" in res and _good(res["k"]) and res["k"].get("candles"):
            c = res["k"]["candles"][-1]
            if "price" in need:
                snap["price"] = c["close"]; filled.append("price")
            if "ts" in need:
                snap["ts"] = c["ts"]; filled.append("ts")
        if "oi" in res and _good(res["oi"]) and res["oi"].get("latest") is not None:
            if "oi" in need:
                snap["oi"] = res["oi"]["latest"]; filled.append("oi")
            if "oi_delta_pct" in need:
                snap["oi_delta_pct"] = res["oi"].get("delta_pct_24h"); filled.append("oi_delta_pct")
        if "f" in res and _good(res["f"]) and res["f"].get("funding") is not None:
            if "funding" in need:
                snap["funding"] = res["f"]["funding"]; filled.append("funding")
            if "funding_predicted" in need:
                snap["funding_predicted"] = res["f"]["funding"]; filled.append("funding_predicted")
        if "tt" in res and _good(res["tt"]) and res["tt"].get("latest") is not None:
            snap["top_trader_ratio"] = res["tt"]["latest"]; filled.append("top_trader_ratio")
        if "ls" in res and _good(res["ls"]) and res["ls"].get("latest") is not None:
            snap["ls_ratio"] = res["ls"]["latest"]; filled.append("ls_ratio")
        if filled:
            for fld in filled:
                if fld in stale_fields:
                    stale_fields.remove(fld)
            snap["_binance_filled"] = filled
    except Exception:
        pass


async def mi_get_snapshot(
    symbol: Annotated[str, Field(description="目標標的（任意命名空間）")],
    tf: Annotated[str, Field(description="主分析時框")] = "1h",
    lookback: Annotated[int, Field(ge=12, le=500, description="回看根數")] = 96,
) -> dict:
    """一次組好完整快照供 L2/L3 用（核心工具）。

    並行呼叫多個下游、任一失敗 → 該欄填 None 並列入 `stale_fields`，
    不整包失敗。輸出欄位對齊 l2_trigger.types.MarketSnapshot。

    回傳：{symbol, ts, price, oi, funding, cvd, ls_ratio, top_trader_ratio,
          liq_long, liq_short, btc_gate_open, btc_regime, above_4h_200ma,
          is_hot, strength_score, atr_pct_7d, vol_24h_vs_30d, cvd_slope_7d,
          top_trader_slope_7d, oi_delta_7d_pct, higher_lows_7d,
          stale_fields, sources_used}
    """
    sym = normalize(symbol)

    # 並行拉所有底層
    results = await asyncio.gather(
        SOURCE.get_positioning(sym, "top_trader_position", "4h", 24),
        SOURCE.get_positioning(sym, "account", "4h", 24),
        SOURCE.get_oi(sym, tf, lookback),
        SOURCE.get_funding(sym),
        SOURCE.get_liquidations(sym, "24h"),
        SOURCE.get_cvd_series(sym, tf, lookback),
        SOURCE.get_price_series(sym, tf, lookback),
        SOURCE.get_btc_gate(),
        SOURCE.get_strength_universe(50),
        SOURCE.get_structure(sym),
        return_exceptions=True,
    )
    top_pos, retail_pos, oi, funding, liq, cvd, price, gate, universe, structure = results

    stale_fields: list[str] = []
    sources_used: set[str] = set()

    def _ok(r: Any) -> bool:
        return isinstance(r, dict) and not r.get("error") and not isinstance(r, Exception)

    def _stale(*fields: str) -> None:
        stale_fields.extend(fields)

    # === 整裝 ===
    snap: dict[str, Any] = {"symbol": sym, "tf": tf}

    # 價格 + ts
    if _ok(price):
        snap["price"] = price.get("price")
        snap["ts"] = price["series"][-1]["ts"] if price.get("series") else None
        sources_used.add(price.get("source", "?"))
    else:
        _stale("price", "ts")
        snap["price"] = None
        snap["ts"] = None

    # OI
    if _ok(oi):
        snap["oi"] = oi.get("latest")
        snap["oi_delta_pct"] = oi.get("delta_pct_24h")
        sources_used.add(oi.get("source", "?"))
    else:
        _stale("oi", "oi_delta_pct")

    # Funding
    if _ok(funding):
        snap["funding"] = funding.get("funding")
        snap["funding_predicted"] = funding.get("funding_predicted")
        sources_used.add(funding.get("source", "?"))
    else:
        _stale("funding", "funding_predicted")

    # CVD + 背離（在 snapshot 層算，比對 price 與 cvd 序列）
    if _ok(cvd):
        snap["cvd"] = cvd.get("cvd")
        snap["cvd_slope"] = cvd.get("cvd_slope")
        # 比對近期 12 根（12h on 1h）的 price 趨勢 vs CVD slope 判定背離
        divergence = "none"
        if _ok(price):
            pseries = price.get("series", [])
            cseries = cvd.get("series", [])
            if len(pseries) >= 12 and len(cseries) >= 12:
                p12 = pseries[-12:]
                p_change_pct = (p12[-1]["value"] - p12[0]["value"]) / p12[0]["value"] * 100 if p12[0]["value"] else 0
                cs = cvd.get("cvd_slope", 0) or 0
                # 價走平/跌 (Δ ≤ +0.5%) + CVD 短斜率 ≥ +0.5 → 看漲背離
                if p_change_pct <= 0.5 and cs >= 0.5:
                    divergence = "bull"
                elif p_change_pct >= -0.5 and cs <= -0.5:
                    divergence = "bear"
        snap["cvd_price_divergence"] = divergence
        sources_used.add(cvd.get("source", "?"))
    else:
        _stale("cvd", "cvd_slope")
        snap["cvd_price_divergence"] = "none"

    # 大戶 / 散戶
    if _ok(top_pos):
        snap["top_trader_ratio"] = top_pos.get("latest")
        sources_used.add(top_pos.get("source", "?"))
    else:
        _stale("top_trader_ratio")

    if _ok(retail_pos):
        snap["ls_ratio"] = retail_pos.get("latest")
    else:
        _stale("ls_ratio")

    # 清算
    if _ok(liq):
        snap["liq_long"] = liq.get("liq_long")
        snap["liq_short"] = liq.get("liq_short")
    else:
        _stale("liq_long", "liq_short")

    # BTC 閘
    if _ok(gate):
        snap["btc_gate_open"] = gate.get("btc_gate_open")
        snap["btc_regime"] = gate.get("btc_regime")
        sources_used.add(gate.get("source", "?"))
    else:
        _stale("btc_gate_open", "btc_regime")

    # 4h 趨勢 above_4h_200ma：由 structure 算（從 4h × 200 OHLC）
    # 若 structure 也無法算（資料不足）再 fallback 用 btc gate
    snap["above_4h_200ma"] = None
    if _ok(structure) and structure.get("above_4h_200ma") is not None:
        snap["above_4h_200ma"] = structure.get("above_4h_200ma")
    elif _ok(gate) and gate.get("btc_gate_open"):
        snap["above_4h_200ma"] = True
    if snap["above_4h_200ma"] is None:
        _stale("above_4h_200ma")

    # 強勢分數 + is_hot
    if _ok(universe):
        scored = compute_strength_scores(universe.get("items", []))
        match = next((s for s in scored if s["symbol"] == sym), None)
        snap["strength_score"] = match.get("strength_score") if match else None
        snap["is_hot"] = sym in CORE_SYMBOLS or sym in HOT_SYMBOLS or (
            match is not None and match.get("strength_score", 0) >= 70
        )
    else:
        _stale("strength_score")
        snap["strength_score"] = None
        snap["is_hot"] = sym in CORE_SYMBOLS

    # 結構（Setup B 用）
    if _ok(structure):
        for f in ("atr_pct_7d", "vol_24h_vs_30d", "cvd_slope_7d",
                  "top_trader_slope_7d", "oi_delta_7d_pct", "higher_lows_7d"):
            snap[f] = structure.get(f)
        sources_used.add(structure.get("source", "?"))
    else:
        for f in ("atr_pct_7d", "vol_24h_vs_30d", "cvd_slope_7d",
                  "top_trader_slope_7d", "oi_delta_7d_pct", "higher_lows_7d"):
            _stale(f)

    # v33: 有 stale 欄位 → 用 Binance 第二來源補（提升資料品質、減少 stale）
    if stale_fields:
        await _fill_stale_from_binance(sym, tf, snap, stale_fields)
        if snap.get("_binance_filled"):
            sources_used.add("binance-perp(fallback)")

    snap["stale_fields"] = tuple(stale_fields)
    snap["sources_used"] = tuple(sorted(sources_used))
    return snap


# ===========================================================================
# 白名單 view 查詢（mi_query_view）
# ===========================================================================
_VIEW_WHITELIST: dict[str, dict] = {
    "v_trades_agg_1m": {
        "params": ["symbol", "lookback_minutes"],
        "description": "1 分鐘聚合的成交資料（給 CVD 計算用）",
    },
    "v_oi_series_1h": {
        "params": ["symbol", "lookback_hours"],
        "description": "1 小時 OI 時序",
    },
    "v_btc_gate_state": {
        "params": [],
        "description": "目前 BTC 4h 收盤 / 4h 200MA / regime",
    },
    "v_strength_universe": {
        "params": ["limit"],
        "description": "強勢候選池（pre-aggregated 7d 指標）",
    },
}


@_tool
async def mi_query_view(
    view: Annotated[str, Field(description="白名單 view 名稱（看 list_views）")],
    params: Annotated[dict, Field(description="參數綁定（key→value，無任意 SQL）")] = None,
) -> dict:
    """**白名單** TimescaleDB view 查詢，禁任意 SQL。

    可用 view：v_trades_agg_1m / v_oi_series_1h / v_btc_gate_state / v_strength_universe
    呼叫前可先用 mi_list_views() 取得 schema。
    """
    if params is None:
        params = {}
    if view not in _VIEW_WHITELIST:
        return make_error(
            tool="mi_query_view", symbol=None, source="local",
            code="VIEW_NOT_WHITELISTED",
            message=f"view '{view}' not in whitelist",
            suggestion=f"choose from {list(_VIEW_WHITELIST.keys())}",
        )

    spec = _VIEW_WHITELIST[view]
    missing = [p for p in spec["params"] if p not in params]
    if missing:
        return make_error(
            tool="mi_query_view", symbol=None, source="local",
            code="MISSING_PARAMS",
            message=f"missing params: {missing}",
            suggestion=f"view '{view}' requires {spec['params']}",
        )

    # v0：LocalSource stub
    return make_error(
        tool="mi_query_view", symbol=params.get("symbol"), source="local",
        code="BACKEND_NOT_READY",
        message="LocalSource not implemented (Task 10).",
        suggestion="MockSource doesn't expose views; this will work after L1 daemon + TimescaleDB are up.",
    )


@_tool
async def mi_list_views() -> dict:
    """列出白名單 view 與其參數。"""
    return {"source": SOURCE.name, "views": _VIEW_WHITELIST}


# ===========================================================================
# 健康檢查
# ===========================================================================
@_tool
async def mi_get_etf_flows(
    symbol: Annotated[Literal["BTC", "ETH"], Field()] = "BTC",
    lookback_days: Annotated[int, Field(ge=1, le=30)] = 7,
) -> dict:
    """ETF 機構流向（淨流入/流出，USD）。
    BTC 619 天歷史、ETH 481 天。流出 = 機構在減倉 = bearish。
    """
    if hasattr(SOURCE, "get_etf_flows"):
        return await SOURCE.get_etf_flows(symbol, lookback_days)
    return make_error(tool="mi_get_etf_flows", symbol=symbol, source=SOURCE.name,
                      code="NOT_AVAILABLE", message=f"backend={SOURCE.name} does not support ETF flows")


@_tool
async def mi_get_sentiment() -> dict:
    """市場情緒：Fear-Greed Index + AHR999 估值指標。
    F&G < 20 極度恐懼（底部）、> 80 極度貪婪（頂部）。
    AHR999 < 0.45 適合定投、> 1.2 高估區。
    """
    if hasattr(SOURCE, "get_sentiment"):
        return await SOURCE.get_sentiment()
    return make_error(tool="mi_get_sentiment", symbol=None, source=SOURCE.name,
                      code="NOT_AVAILABLE", message=f"backend={SOURCE.name} does not support sentiment")


@_tool
async def mi_get_liquidation_scan(
    top_n: Annotated[int, Field(ge=1, le=50)] = 20,
) -> dict:
    """掃整個市場（1220+ 幣）找近 24h 清算最大者。
    short 清算 > long 清算 → 擠壓燃料、可能 bull 反彈。
    long 清算 > short 清算 → 多殺多、可能 bear 延續。
    imbalance: +1 全空清算（極端 squeeze）/-1 全多清算（極端 cascade）
    """
    if hasattr(SOURCE, "get_liquidation_scan"):
        return await SOURCE.get_liquidation_scan(top_n)
    return make_error(tool="mi_get_liquidation_scan", symbol=None, source=SOURCE.name,
                      code="NOT_AVAILABLE", message=f"backend={SOURCE.name} does not support broad scan")


@_tool
async def mi_get_hyperliquid_whales(
    top_n: Annotated[int, Field(ge=1, le=50, description="回前 N 個鯨魚倉位")] = 20,
) -> dict:
    """Hyperliquid DEX 鯨魚倉位（真實 whale 數據，無需付費 Whale Alert）。
    回傳：top_positions (按 notional 大小)、per_symbol_aggregate (每幣多空淨倉 net_long_pct +/-100)
    net_long_pct > +50 = 鯨魚壓倒性做多；< -50 = 壓倒性做空。
    """
    if hasattr(SOURCE, "get_hyperliquid_whales"):
        return await SOURCE.get_hyperliquid_whales(top_n)
    return make_error(tool="mi_get_hyperliquid_whales", symbol=None,
                      source=SOURCE.name, code="NOT_AVAILABLE",
                      message=f"backend={SOURCE.name} does not support whale data")


@_tool
async def mi_get_market_cycle() -> dict:
    """BTC 市場週期定位指標：
    - Pi Cycle Top: 110d MA vs 350d MA × 2，逼近交叉 = 週期頂訊號
    - Puell Multiple: 礦工營收 vs 365d MA，>4 頂部、<0.5 底部
    - Stock-to-Flow: 基於減半的稀缺性模型
    用於宏觀 regime 補充判斷。
    """
    if hasattr(SOURCE, "get_market_cycle"):
        return await SOURCE.get_market_cycle()
    return make_error(tool="mi_get_market_cycle", symbol=None, source=SOURCE.name,
                      code="NOT_AVAILABLE", message="not supported")


@_tool
async def mi_get_funding_outliers(
    top_n: Annotated[int, Field(ge=1, le=50)] = 15,
) -> dict:
    """掃 1233 個永續找 funding 極端值。
    hottest = 多頭過熱（多殺多風險）。coldest = 空方付錢（軋空燃料）。
    可定位「過熱」候選做空、「過冷」候選做多。
    """
    if hasattr(SOURCE, "get_funding_outliers"):
        return await SOURCE.get_funding_outliers(top_n)
    return make_error(tool="mi_get_funding_outliers", symbol=None, source=SOURCE.name,
                      code="NOT_AVAILABLE", message="not supported")


@_tool
async def mi_get_funding_weighted(
    symbol: Annotated[str, Field()],
    weight: Annotated[Literal["oi", "vol"], Field(description="oi=OI 加權, vol=Volume 加權")] = "oi",
    interval: Annotated[str, Field()] = "1h",
    limit: Annotated[int, Field(ge=1, le=500)] = 24,
) -> dict:
    """加權 funding rate 時序。
    OI 加權版比簡單平均更穩，因為大倉位主導真實多頭壓力。
    過熱判定建議用此版本而非單一交易所 funding。
    """
    if hasattr(SOURCE, "get_funding_weighted"):
        return await SOURCE.get_funding_weighted(symbol, weight, interval, limit)
    return make_error(tool="mi_get_funding_weighted", symbol=symbol, source=SOURCE.name,
                      code="NOT_AVAILABLE", message="not supported")


@_tool
async def mi_get_funding_arbitrage(
    top_n: Annotated[int, Field(ge=1, le=30)] = 10,
) -> dict:
    """跨所 funding 套利機會（買 funding 低的所、賣 funding 高的所）。
    回 APR > 5% 的，按 APR 降序。中性策略，無方向曝險。
    """
    if hasattr(SOURCE, "get_funding_arbitrage"):
        return await SOURCE.get_funding_arbitrage(top_n)
    return make_error(tool="mi_get_funding_arbitrage", symbol=None, source=SOURCE.name,
                      code="NOT_AVAILABLE", message="not supported")


@_tool
async def mi_get_spot_futures_basis(
    symbol: Annotated[str, Field()],
) -> dict:
    """現貨 vs 期貨 基差%。
    > +0.1% 期貨溢價 = 看多情緒；< -0.1% 期貨折價 = 看跌情緒。
    極端基差通常預示反轉（>1% 太貴、< -1% 恐慌折價）。
    """
    if hasattr(SOURCE, "get_spot_futures_basis"):
        return await SOURCE.get_spot_futures_basis(symbol)
    return make_error(tool="mi_get_spot_futures_basis", symbol=symbol, source=SOURCE.name,
                      code="NOT_AVAILABLE", message="not supported")


@_tool
async def mi_get_options_market(
    symbol: Annotated[Literal["BTC", "ETH"], Field()] = "BTC",
) -> dict:
    """期權市場跨所 OI 分佈 + 24h OI 變化（機構建倉 vs 出清）。
    BTC 期權主要在 Deribit、CME；OI 24h +5% = 機構在加倉。
    """
    if hasattr(SOURCE, "get_options_market"):
        return await SOURCE.get_options_market(symbol)
    return make_error(tool="mi_get_options_market", symbol=symbol, source=SOURCE.name,
                      code="NOT_AVAILABLE", message="not supported")


@_tool
async def mi_get_market_cycle_full() -> dict:
    """全套 BTC 週期定位指標（5 個一次回）：
    Pi Cycle Top / Puell Multiple / Stock-to-Flow / Golden Ratio Multiplier / 2-year MA Multiplier
    取代 mi_get_market_cycle，提供更完整的週期定位。
    """
    if hasattr(SOURCE, "get_market_cycle_full"):
        return await SOURCE.get_market_cycle_full()
    return make_error(tool="mi_get_market_cycle_full", symbol=None, source=SOURCE.name,
                      code="NOT_AVAILABLE", message="not supported")


@_tool
async def mi_get_tradfi_snapshot() -> dict:
    """傳統金融跨資產快照（Yahoo Finance，免費）：
    SPY/QQQ（美股大盤+科技）、^VIX（恐慌指數）、^TNX（10年債息）、DXY（美元指數）、
    GLD（黃金）、COIN/MSTR（加密股）、NVDA/TSLA/AAPL（科技代理）。

    每個 ticker 回 current、1d/7d/30d 報酬、距 3 個月高點 drawdown。
    給 LLM 做跨資產脈絡分析用。
    """
    from .sources.tradfi import get_tradfi
    src = get_tradfi()
    return await src.get_full_snapshot()


@_tool
async def mi_get_kline_multi(
    symbol: Annotated[str, Field()],
    timeframes: Annotated[
        list[str] | None,
        Field(description="預設 [5m, 15m, 1h, 4h, 12h, 1d, 1w]"),
    ] = None,
    limit_per_tf: Annotated[int, Field(ge=20, le=300)] = 100,
) -> dict:
    """多時框 K 線（OKX 公開端點，無需 key）。
    一次拉所有時框並行，給 LLM 做跨時框型態分析。
    支援 5m/15m/30m/1h/2h/4h/6h/8h/12h/1d/2d/3d/1w/1M。
    """
    from .sources.okx_candles import get_okx_candles
    return await get_okx_candles().get_multi_tf(symbol, timeframes, limit_per_tf)


@_tool
async def mi_get_pattern_analysis(
    symbol: Annotated[str, Field()],
    timeframes: Annotated[
        list[str] | None,
        Field(description="預設 [15m, 1h, 4h, 12h, 1d]，包含日內到日線型態"),
    ] = None,
) -> dict:
    """跨時框型態分析（趨勢方向 + 支撐阻力 + 量價配合 + 蠟燭型態 + 多時框共識）。

    回傳結構化分析給 LLM 做更深層解讀：
    - trend: HH/HL/LH/LL 趨勢判定
    - sr: 量加權的支撐/阻力位（與當前距離%）
    - volume_price: 量價配合度
    - patterns: 吞噬/錘子/十字星/流星
    - consensus: 跨時框共識（strong_uptrend / mixed / strong_downtrend 等）
    """
    from .pattern_analysis import summarize_multi_tf
    from .sources.okx_candles import get_okx_candles

    tfs = timeframes or ["15m", "1h", "4h", "12h", "1d"]
    multi = await get_okx_candles().get_multi_tf(symbol, tfs, 100)
    if isinstance(multi, dict) and multi.get("error"):
        return multi
    return summarize_multi_tf(symbol, multi.get("by_timeframe", {}))


@_tool
async def mi_get_okx_news(
    hours_back: Annotated[int, Field(ge=1, le=168, description="回看小時數")] = 24,
    max_items: Annotated[int, Field(ge=1, le=50)] = 20,
    watchlist_symbols: Annotated[
        list[str] | None,
        Field(description="若提供 → 額外標出含這些 symbol 的公告（如 [BTC, ETH, SUI]）"),
    ] = None,
) -> dict:
    """OKX 官方公告（free 公開端點，無需 key）：
    新上市 / 下架 / 費率變動 / 入出金中斷 / 重大事件。

    新上市通常 24h 內爆漲 5-20%；下架則必須立即出場。
    若提供 watchlist_symbols，會額外回傳 watchlist_relevant 列表。
    """
    from .sources.okx_news import get_okx_news
    src = get_okx_news()
    if watchlist_symbols:
        return await src.get_relevant_for_symbols(watchlist_symbols, hours_back)
    return await src.get_recent_critical(hours_back, max_items)


@_tool
async def mi_get_news(
    currencies: Annotated[list[str], Field(description="標的代號清單，如 [BTC, ETH]")] = None,
    filter_kind: Annotated[
        Literal["rising", "hot", "bullish", "bearish", "important"],
        Field(description="篩選類型：important=大新聞、bullish/bearish=情緒導向"),
    ] = "important",
    page: Annotated[int, Field(ge=1, le=10)] = 1,
) -> dict:
    """CryptoPanic 即時新聞（需 CRYPTOPANIC_TOKEN env）。
    important = 高重要性（監管/政治/巨額消息）
    回傳含標題、來源、發佈時間、票數（社群評分重要性）。
    """
    from .sources.cryptopanic import get_cryptopanic
    cp = get_cryptopanic()
    return await cp.get_posts(currencies=currencies, filter_kind=filter_kind, page=page)


@_tool
async def mi_health() -> dict:
    """檢查 source 是否活著。"""
    info = await SOURCE.health()
    return {
        "backend": SETTINGS.backend,
        "source_name": SOURCE.name,
        "watchlist_core": list(CORE_SYMBOLS),
        "watchlist_hot": sorted(HOT_SYMBOLS),
        **info,
    }


# ===========================================================================
# Entry point
# ===========================================================================
def main() -> None:
    if not _MCP_AVAILABLE:
        raise RuntimeError(
            "mcp 套件未安裝。pip install 'mcp[cli]>=1.2.0' 後再啟動 server。"
        )
    mcp.run()


if __name__ == "__main__":
    main()
