"""#33 跨源匯流「影子觀測」Session（接線層，純觀測、零訊號數學變更）。

把 #33 三個純函式核心（presence_index / convergence）接進常駐 daemon，
每隔一段時間：
    1. 用既有零/低成本 I/O 蒐集「全宇宙跨源存在度」（OKX 讀 scanner.db、
       Binance 一次列全永續、HL 一次 metaAndAssetCtxs）→ compute_presence。
       ⚠️ CoinGlass 全市場覆蓋端點(coins-markets)在 $79 Startup 被鎖，無法在不爆
       額度下取全宇宙 CoinGlass 覆蓋 → universe 層只算「OKX∧Binance 雙源」，不謊報
       全宇宙 triple（紅線③不捏造）。
    2. 從中挑「OKX∧Binance 雙源齊全 ∧ 流動性 deep/medium」的 ≤12 焦點幣（前濾），
       對這些焦點幣逐幣查 CoinGlass per-coin `pairs-markets`（額度受控 ≤12 次/輪），
       既「確認第三源覆蓋(triple_present)」又取 CoinGlass 聚合 funding，連同 OKX/
       Binance/HL 算「跨源 funding 共振」，並產出 **僅供觀測** 的 strength_multiplier。
    3. 把整輪結果寫成一行 JSONL 到獨立 sink：data_dir()/convergence_shadow.jsonl。

════════════════════════════════════════════════════════════════════════════
影子鐵則（絕不可違反）：
    * 本 worker **永不** 乘 strength_multiplier 進 strength_score；
      **永不** 寫 fire_queue / snapshot / symbol_gate / 任何下單路徑；
      **永不** import market_intel_mcp.strength。
    * 唯一輸出 = 自己的 convergence_shadow.jsonl（觀測用，給日後 A/B 回測）。
    * 整個迴圈體包 try/except；任何源失敗都吞掉續跑，絕不拖垮 daemon
      （外層另有 supervise() 崩潰隔離 + 退避重啟）。
    * 不發 Telegram（純背景觀測，不打擾使用者）。
════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json

# 焦點幣數（對它們做 Binance funding 逐幣查；上限保守，避免額度浪費）
_FOCUS_TOP_N = 12
# 焦點幣篩選：流動性層
_FOCUS_TIERS = ("deep", "medium")
# JSONL sink 軟上限（位元組）；超過就先改名 .1 再重開，避免無限長
_SINK_MAX_BYTES = 5_000_000


def _sink_path():
    from botpaths import data_dir
    return data_dir() / "convergence_shadow.jsonl"


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


async def _hl_funding_map(hl) -> dict:
    """HL 一次 overview → {canonical: funding_8h_pct}。失敗回空（不 raise）。"""
    from market_intel_mcp.symbol_mapping import to_canonical_aliased
    out: dict[str, float] = {}
    try:
        ov = await hl.get_overview(top_n=50)
        if isinstance(ov, dict) and not ov.get("error"):
            for row in (ov.get("top_by_oi") or []):
                coin = row.get("coin")
                f = row.get("funding_8h_pct")
                if coin is None or not isinstance(f, (int, float)):
                    continue
                out[to_canonical_aliased(coin)] = float(f)
    except Exception:
        return {}
    return out


async def _binance_funding(bn, symbol: str):
    """單幣 Binance funding（float）或 None（失敗/缺料，不 raise）。"""
    try:
        r = await bn.get_funding(symbol)
        if isinstance(r, dict) and not r.get("error"):
            v = r.get("funding")
            return float(v) if isinstance(v, (int, float)) else None
    except Exception:
        return None
    return None


async def _coinglass_focus_map(source, focus_syms: list[str]) -> dict:
    """焦點幣 CoinGlass per-coin 覆蓋 + 聚合 funding（bounded：≤len(focus) 次）。

    為何只查焦點幣：CoinGlass `futures/coins-markets`（單呼叫全市場覆蓋）在
    $79 Startup 被鎖；可用的是 per-coin `pairs-markets`。對 641 檔全宇宙逐幣查
    會爆額度，故只對「OKX∧Binance 雙源齊全 ∧ 深/中流動性」的 ≤12 檔焦點幣查，
    既能真確認第三源（CoinGlass）覆蓋、又能取 CoinGlass 聚合 funding 作第三方
    共振確認，且 ≤12 次/30 分 << 80rpm。重用 daemon 既有 source（共用限流器 +
    TTL 快取），不另建實例、不另開額度。

    回 {canonical: {"funding": float|None, "vol_usd": float|None}}；
    缺料/失敗/未覆蓋的幣不入 map（不在 map = CoinGlass 未覆蓋該幣）。
    """
    out: dict[str, dict] = {}
    if not focus_syms or source is None or not hasattr(source, "get_strength_universe"):
        return out
    try:
        from market_intel_mcp.symbol_mapping import to_canonical_aliased
        r = await source.get_strength_universe(
            limit=len(focus_syms), candidate_symbols=list(focus_syms))
        if isinstance(r, dict) and not r.get("error"):
            for it in (r.get("items") or []):
                sym = it.get("symbol")
                if sym is None:
                    continue
                f = it.get("funding")
                out[to_canonical_aliased(sym)] = {
                    "funding": float(f) if isinstance(f, (int, float)) else None,
                    "vol_usd": it.get("vol_24h_usd"),
                }
    except Exception:
        return {}
    return out


async def _run_cycle(source=None) -> dict:
    """跑一輪跨源匯流觀測，回一個可序列化的摘要 dict。

    `source`＝daemon 主 source（backend=coinglass 時即 CoinGlassSource 實例）；
    僅在「焦點幣第三源確認」階段使用其 get_strength_universe（bounded 額度）。
    None → 延遲 get_source()（供一次性測試）。
    """
    from l3_dispatcher.convergence import (
        aggregate_convergence, direction_of, metric_convergence,
    )
    from l3_dispatcher.presence_index import (
        _load_okx_snapshot, collect_presence_universe,
    )
    from market_intel_mcp.sources.binance_perp import get_binance_perp
    from market_intel_mcp.sources.hyperliquid import HyperliquidSource

    if source is None:
        try:
            from market_intel_mcp.sources import get_source
            source = get_source()
        except Exception:
            source = None

    bn = get_binance_perp()
    hl = HyperliquidSource()

    # 1) 全宇宙跨源存在度（cheap：1 Binance + 1 HL + scanner.db 讀）。
    #    注意：CoinGlass 全市場覆蓋端點被鎖，故此處全宇宙只有 OKX/Binance/HL，
    #    第三源（CoinGlass）覆蓋改在第 4 步對「焦點幣」逐幣確認（額度受控）。
    presence = await collect_presence_universe(binance_source=bn, hl_source=hl)

    # 2) 全宇宙統計（誠實：universe 層只能算「雙源 OKX∧Binance」存在度，
    #    無法在不爆額度下取全市場 CoinGlass 覆蓋 → 不謊報全宇宙 triple）。
    tier_counts = {"deep": 0, "medium": 0, "shallow": 0}
    n_dual = 0
    for p in presence.values():
        tier_counts[p.get("liquidity_tier", "shallow")] = \
            tier_counts.get(p.get("liquidity_tier", "shallow"), 0) + 1
        present = set(p.get("exchanges_present") or [])
        if "okx" in present and "binance" in present:
            n_dual += 1

    # 3) 焦點幣前濾（cheap）= OKX∧Binance 雙源齊全 ∧ 流動性 deep/medium，
    #    按流動性深度排序取 top N。第三源 CoinGlass 留待第 4 步逐幣確認。
    candidates = []
    for sym, p in presence.items():
        present = set(p.get("exchanges_present") or [])
        if {"okx", "binance"} <= present and p.get("liquidity_tier") in _FOCUS_TIERS:
            candidates.append((sym, p))
    candidates.sort(key=lambda kv: kv[1].get("liquidity_depth_usd", 0.0),
                    reverse=True)
    focus = candidates[:_FOCUS_TOP_N]
    focus_syms = [sym for sym, _ in focus]

    # 4) 焦點幣跨源 funding 共振（OKX scanner ∧ Binance 逐幣 ∧ HL overview ∧
    #    CoinGlass 聚合）。CoinGlass 同時確認「第三源覆蓋」(triple_present)。
    okx_snap = {}
    try:
        okx_snap = _load_okx_snapshot()
    except Exception:
        okx_snap = {}
    hl_funding = await _hl_funding_map(hl)
    cg_map = await _coinglass_focus_map(source, focus_syms)

    # Binance funding 逐幣（bounded gather，失敗各自 None）
    bn_results = await asyncio.gather(
        *[_binance_funding(bn, s) for s in focus_syms],
        return_exceptions=True,
    )
    bn_funding = {}
    for s, r in zip(focus_syms, bn_results):
        bn_funding[s] = r if isinstance(r, (int, float)) else None

    focus_out = []
    n_triple_confirmed = 0
    for sym, pres in focus:
        sigs = {}
        okx_f = (okx_snap.get(sym) or {}).get("funding")
        if isinstance(okx_f, (int, float)):
            sigs["okx"] = direction_of("funding", okx_f)
        if isinstance(bn_funding.get(sym), (int, float)):
            sigs["binance"] = direction_of("funding", bn_funding[sym])
        cg_f = (cg_map.get(sym) or {}).get("funding")
        if isinstance(cg_f, (int, float)):
            sigs["coinglass"] = direction_of("funding", cg_f)
        if isinstance(hl_funding.get(sym), (int, float)):
            sigs["hyperliquid"] = direction_of("funding", hl_funding[sym])

        # triple_present = OKX∧Binance（前濾已保證）∧ CoinGlass 覆蓋（本步確認）
        cg_covered = sym in cg_map
        if cg_covered:
            n_triple_confirmed += 1

        metric_results = {"funding": metric_convergence("funding", sigs)}
        agg = aggregate_convergence(sym, metric_results, pres)
        focus_out.append({
            "symbol": sym,
            "liquidity_tier": pres.get("liquidity_tier"),
            "liquidity_depth_usd": pres.get("liquidity_depth_usd"),
            "presence_score": pres.get("presence_score"),
            "triple_present": cg_covered,      # OKX∧Binance∧CoinGlass 三方共現
            "funding_sources": sigs,           # {okx/binance/coinglass/hl: -1/0/+1}
            "funding_convergent": metric_results["funding"].get("is_convergent"),
            "convergence_score": agg.get("convergence_score"),
            "dominant_direction": agg.get("dominant_direction"),
            "strength_multiplier_SHADOW": agg.get("strength_multiplier"),  # 觀測，永不施用
        })

    return {
        "ts": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "universe_size": len(presence),
        "n_dual_present": n_dual,                 # 全宇宙 OKX∧Binance（cheap）
        "n_triple_confirmed": n_triple_confirmed,  # 焦點幣中 CoinGlass 已確認覆蓋
        "tier_counts": tier_counts,
        "focus_count": len(focus_out),
        "focus": focus_out,
        "note": "shadow-only: strength_multiplier 從不施用於 strength_score/fire；"
                "universe 層為 OKX∧Binance 雙源(CoinGlass全市場端點被鎖)，"
                "三方共現(triple_present)僅對焦點幣逐幣確認",
    }


async def run_convergence_shadow_loop(source=None, interval_seconds: int = 1800):
    """#33 跨源匯流影子觀測常駐迴圈（每 interval 跑一輪，純觀測寫 JSONL）。

    `source` 參數保留以與其他 worker 簽名一致；本 worker 不經主 source，
    直接用 binance_perp / hyperliquid 單例 + scanner.db。
    """
    # 啟動稍緩，避開開機尖峰
    await asyncio.sleep(60)
    while True:
        try:
            summary = await _run_cycle(source)
            _append_jsonl(summary)
            print(f"[convergence_shadow] universe={summary['universe_size']} "
                  f"dual={summary['n_dual_present']} "
                  f"triple={summary['n_triple_confirmed']} "
                  f"focus={summary['focus_count']}")
        except Exception as e:  # 整輪保護：任何意外吞掉續跑，不拖垮 daemon
            print(f"[convergence_shadow] cycle error: {e}")
        await asyncio.sleep(max(60, int(interval_seconds)))
