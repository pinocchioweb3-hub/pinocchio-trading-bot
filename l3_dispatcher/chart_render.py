"""SMC 圖表標記渲染（v18-F）：K 線圖上畫 FVG/OB/BoS/Swing + 交易計畫線 → PNG。

使用者理想 2(a) 的實現：「AI 直接在圖表上標記 SMC 結構」。
數據：OkxCandlesSource 4h×120 根 + smc_levels.compute_smc_levels（現成）。
輸出：%LOCALAPPDATA%/TradingBot/charts/{sym}_{ts}.png（保留最近 50 張）。
"""
from __future__ import annotations

import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # 無頭模式（daemon 內無 GUI）
# 中文字型（Windows 內建微軟正黑體）
matplotlib.rcParams["font.sans-serif"] = ["Microsoft JhengHei", "SimHei", "Arial"]
matplotlib.rcParams["axes.unicode_minus"] = False
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec
from matplotlib.patches import Rectangle

from botpaths import data_dir

CHART_DIR = data_dir() / "charts"
KEEP_CHARTS = 50

UP = "#26a69a"      # 綠
DOWN = "#ef5350"    # 紅
BG = "#131722"      # TradingView 深色
FG = "#d1d4dc"
GRID = "#2a2e39"
VOLMA = "#f5d442"   # 量能均線（黃）
OICOL = "#5b9bd5"   # OI 線（藍）
SNR_R = "#ef5350"   # 壓力
SNR_S = "#26a69a"   # 支撐


def _vol(c: dict) -> float:
    for k in ("volume", "vol", "vol_ccy", "volCcy", "baseVol"):
        v = c.get(k)
        if v:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


def _compute_snr(candles: list[dict], n_levels: int = 3) -> dict:
    """從近期 swing 高低密集區算支撐壓力。回 {resistance:[...], support:[...]}。"""
    if len(candles) < 10:
        return {"resistance": [], "support": []}
    highs, lows = [], []
    w = 2
    for i in range(w, len(candles) - w):
        hi = candles[i]["high"]
        lo = candles[i]["low"]
        if hi == max(candles[j]["high"] for j in range(i - w, i + w + 1)):
            highs.append(hi)
        if lo == min(candles[j]["low"] for j in range(i - w, i + w + 1)):
            lows.append(lo)
    cur = candles[-1]["close"]
    rng = max(c["high"] for c in candles) - min(c["low"] for c in candles)
    tol = rng * 0.012 if rng else cur * 0.005

    def _cluster(levels: list[float]) -> list[tuple[float, int]]:
        levels = sorted(levels)
        clusters = []
        for lv in levels:
            if clusters and abs(lv - clusters[-1][0]) <= tol:
                cnt = clusters[-1][1] + 1
                avg = (clusters[-1][0] * clusters[-1][1] + lv) / cnt
                clusters[-1] = (avg, cnt)
            else:
                clusters.append((lv, 1))
        return sorted(clusters, key=lambda x: -x[1])   # 觸及次數多者優先

    res = [c[0] for c in _cluster(highs) if c[0] > cur][:n_levels]
    sup = [c[0] for c in _cluster(lows) if c[0] < cur][:n_levels]
    return {"resistance": sorted(res), "support": sorted(sup, reverse=True)}


def _prune_old():
    try:
        CHART_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(CHART_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime)
        for p in files[:-KEEP_CHARTS]:
            p.unlink(missing_ok=True)
    except Exception:
        pass


def render_smc_chart(symbol: str, candles: list[dict], smc: dict,
                     tf: str = "4h",
                     plan: dict | None = None,
                     overlays: dict | None = None) -> Path | None:
    """畫圖。plan（可選）= {entry, stop, tp1, tp2, tp3, direction}。
    overlays（v30 CoinGlass）= {cvd:[...], oi:[...], funding, ls_ratio, ...}。
    回 PNG 路徑；失敗回 None。"""
    try:
        if not candles or len(candles) < 30:
            return None
        n = len(candles)
        overlays = overlays or {}
        has_cvd = bool(overlays.get("cvd"))
        has_oi = bool(overlays.get("oi"))

        # v30: 多面板（畫質提升 dpi 150）— 價格/SMC/SNR + 成交量 + CVD + OI
        panels = [("price", 3.4), ("vol", 1.0)]
        if has_cvd:
            panels.append(("cvd", 1.0))
        if has_oi:
            panels.append(("oi", 1.0))
        fig = plt.figure(figsize=(13, 6 + 1.6 * len(panels)), dpi=150)
        fig.patch.set_facecolor(BG)
        gs = GridSpec(len(panels), 1, height_ratios=[p[1] for p in panels],
                      hspace=0.07, figure=fig)
        axes = {}
        ax = fig.add_subplot(gs[0]); axes["price"] = ax
        for i, (name, _) in enumerate(panels[1:], start=1):
            axes[name] = fig.add_subplot(gs[i], sharex=ax)
        axv = axes["vol"]
        for a in axes.values():
            a.set_facecolor(BG)

        # === K 線 ===
        for i, c in enumerate(candles):
            color = UP if c["close"] >= c["open"] else DOWN
            ax.plot([i, i], [c["low"], c["high"]], color=color, linewidth=0.7, zorder=2)
            body_lo, body_hi = sorted((c["open"], c["close"]))
            ax.add_patch(Rectangle((i - 0.35, body_lo), 0.7,
                                   max(body_hi - body_lo, c["close"] * 1e-5),
                                   facecolor=color, edgecolor=color, zorder=3))

        # === FVG（半透明矩形，從出現處延伸到最右）===
        for f in (smc.get("fvg") or [])[:6]:
            if f.get("mitigated"):
                continue
            x0 = max(0, n - 1 - int(f.get("ago_bars") or 0))
            color = UP if (f.get("type") or "").lower().startswith("bull") else DOWN
            ax.add_patch(Rectangle((x0, f["bottom"]), n - x0,
                                   f["top"] - f["bottom"],
                                   facecolor=color, alpha=0.13, edgecolor="none", zorder=1))
            ax.text(n - 0.5, (f["top"] + f["bottom"]) / 2, "FVG",
                    color=color, fontsize=7, va="center", ha="right", alpha=0.8, zorder=4)

        # === Order Blocks（边框矩形）===
        for ob in (smc.get("order_blocks") or [])[:4]:
            if ob.get("mitigated"):
                continue
            x0 = max(0, n - 1 - int(ob.get("ago_bars") or 0))
            color = UP if (ob.get("type") or "").lower().startswith("bull") else DOWN
            ax.add_patch(Rectangle((x0, ob["bottom"]), n - x0,
                                   ob["top"] - ob["bottom"],
                                   facecolor="none", edgecolor=color,
                                   linewidth=1.0, linestyle="--", alpha=0.7, zorder=2))
            ax.text(x0 + 0.3, ob["top"], "OB", color=color, fontsize=7,
                    va="bottom", alpha=0.9, zorder=4)

        # === BoS / CHoCH（水平短線 + 標籤）===
        for b in (smc.get("bos_choch") or [])[:5]:
            x0 = max(0, n - 1 - int(b.get("ago_bars") or 0))
            color = UP if b.get("direction") == "bull" else DOWN
            ax.plot([x0, min(x0 + 12, n - 1)], [b["level"], b["level"]],
                    color=color, linewidth=1.0, alpha=0.85, zorder=2)
            ax.text(x0, b["level"], b.get("type", "BOS"), color=color,
                    fontsize=7, va="bottom", alpha=0.9, zorder=4)

        # === Swing 高低點（v28: 標 HH/HL/LH/LL 市場結構）===
        swings = sorted((smc.get("swing_points") or []),
                        key=lambda s: -(int(s.get("ago_bars") or 0)))  # 由舊到新
        prev_high = prev_low = None
        for s in swings[:14]:
            x0 = max(0, n - 1 - int(s.get("ago_bars") or 0))
            is_high = s.get("type") == "high"
            lvl = s["level"]
            if is_high:
                tag = ("HH" if prev_high is not None and lvl > prev_high
                       else "LH" if prev_high is not None else "H")
                prev_high = lvl
                ax.annotate(f"▔{tag}", (x0, lvl), color=DOWN, fontsize=7.5,
                            ha="center", va="bottom", zorder=5)
            else:
                tag = ("LL" if prev_low is not None and lvl < prev_low
                       else "HL" if prev_low is not None else "L")
                prev_low = lvl
                ax.annotate(f"▁{tag}", (x0, lvl), color=UP, fontsize=7.5,
                            ha="center", va="top", zorder=5)

        # === SNR 支撐壓力（v28：水平虛線，標觸及次數密集區）===
        snr = _compute_snr(candles)
        for r in snr["resistance"]:
            ax.axhline(r, color=SNR_R, linewidth=0.8, linestyle=(0, (6, 4)),
                       alpha=0.55, zorder=1)
            ax.text(n + 0.5, r, f"壓力 {r:,.6g}", color=SNR_R, fontsize=7,
                    va="center", ha="left", alpha=0.9, zorder=5)
        for sp in snr["support"]:
            ax.axhline(sp, color=SNR_S, linewidth=0.8, linestyle=(0, (6, 4)),
                       alpha=0.55, zorder=1)
            ax.text(n + 0.5, sp, f"支撐 {sp:,.6g}", color=SNR_S, fontsize=7,
                    va="center", ha="left", alpha=0.9, zorder=5)

        # === 交易計畫線 ===
        if plan:
            lines = [("entry", plan.get("entry"), "#f5d442", "進場"),
                     ("stop", plan.get("stop"), DOWN, "止損"),
                     ("tp1", plan.get("tp1"), UP, "TP1"),
                     ("tp2", plan.get("tp2"), UP, "TP2"),
                     ("tp3", plan.get("tp3"), UP, "TP3")]
            for key, y, color, label in lines:
                if y is None:
                    continue
                ax.axhline(y, color=color, linewidth=1.1,
                           linestyle="-" if key in ("entry", "stop") else ":",
                           alpha=0.9, zorder=2)
                ax.text(0.5, y, f" {label} {y:,.6g}", color=color, fontsize=8,
                        va="bottom", zorder=5)

        # === 成交量副圖（v28：SMC 真假突破判斷關鍵）===
        vols = [_vol(c) for c in candles]
        for i, c in enumerate(candles):
            color = UP if c["close"] >= c["open"] else DOWN
            axv.add_patch(Rectangle((i - 0.35, 0), 0.7, vols[i],
                                    facecolor=color, edgecolor=color, alpha=0.55, zorder=2))
        # 量能 MA20（突顯爆量 vs 量縮）
        ma_win = 20
        if len(vols) >= ma_win:
            vma = [sum(vols[max(0, i - ma_win + 1):i + 1]) /
                   min(i + 1, ma_win) for i in range(len(vols))]
            axv.plot(range(n), vma, color=VOLMA, linewidth=1.1, zorder=3,
                     label=f"量能MA{ma_win}")
        # 標最後一根是否爆量
        if len(vols) >= ma_win and vma[-1] > 0:
            ratio = vols[-1] / vma[-1]
            tag = ("● 爆量" if ratio >= 1.8 else "○ 量縮" if ratio <= 0.6 else "")
            if tag:
                axv.text(n - 1, vols[-1], f"{tag} {ratio:.1f}x", color=VOLMA,
                         fontsize=7.5, va="bottom", ha="right", zorder=4)
        axv.set_ylabel("成交量", color=FG, fontsize=8)
        axv.legend(loc="upper left", fontsize=7, facecolor=BG, edgecolor=GRID,
                   labelcolor=FG)

        # === CVD 面板（v30：主動買賣淨力 — SMC 真假突破核心）===
        if has_cvd and "cvd" in axes:
            axc = axes["cvd"]
            cvd = overlays["cvd"]
            cx = range(n - len(cvd), n) if len(cvd) <= n else range(n)
            cvd = cvd[-n:]
            slope = overlays.get("cvd_slope") or 0
            cvd_color = UP if slope >= 0 else DOWN
            axc.plot(list(cx), cvd, color=cvd_color, linewidth=1.3, zorder=3)
            axc.fill_between(list(cx), cvd, min(cvd), color=cvd_color, alpha=0.12)
            axc.axhline(0, color=FG, linewidth=0.4, alpha=0.3)
            trend = "買盤主導 ↑" if slope > 0.5 else "賣盤主導 ↓" if slope < -0.5 else "多空均衡"
            axc.set_ylabel("CVD", color=FG, fontsize=8)
            axc.text(0.01, 0.92, f"CVD 主動買賣淨力：{trend}（斜率 {slope:+.2f}）",
                     transform=axc.transAxes, color=cvd_color, fontsize=8,
                     va="top", ha="left")

        # === OI 面板（v30：未平倉量 — 資金進出場）===
        if has_oi and "oi" in axes:
            axo = axes["oi"]
            oi = overlays["oi"][-n:]
            ox = range(n - len(oi), n)
            d24 = overlays.get("oi_delta_24h")
            oi_color = OICOL if (d24 or 0) >= 0 else DOWN
            axo.plot(list(ox), oi, color=oi_color, linewidth=1.3, zorder=3)
            axo.fill_between(list(ox), oi, min(oi), color=oi_color, alpha=0.12)
            axo.set_ylabel("OI", color=FG, fontsize=8)
            extra = []
            if d24 is not None:
                extra.append(f"OI 24h {d24:+.1f}%")
            if overlays.get("funding") is not None:
                extra.append(f"資金費率 {overlays['funding']*100:+.3f}%")
            if overlays.get("ls_ratio") is not None:
                extra.append(f"多空比 {overlays['ls_ratio']:.2f}")
            if extra:
                axo.text(0.01, 0.92, "　".join(extra), transform=axo.transAxes,
                         color=FG, fontsize=8, va="top", ha="left")

        # === 樣式 ===
        for a in axes.values():
            a.grid(color=GRID, linewidth=0.4, alpha=0.5)
            a.tick_params(colors=FG, labelsize=8)
            for spine in a.spines.values():
                spine.set_color(GRID)
        # 只有最底面板顯示 x 軸刻度
        bottom = list(axes.values())[-1]
        for a in axes.values():
            if a is not bottom:
                plt.setp(a.get_xticklabels(), visible=False)
        cur = candles[-1]["close"]
        dir_str = ""
        if plan and plan.get("direction"):
            dir_str = "・做多" if plan["direction"] == "bull" else "・做空"
        ax.set_title(f"{symbol}/USDT 永續・{tf.upper()}・SMC＋SNR 結構{dir_str}"
                     f"（現價 {cur:,.6g}）",
                     color=FG, fontsize=11)
        ax.set_xlim(-1, n + 8)

        _prune_old()
        out = CHART_DIR / f"{symbol}_{int(time.time())}.png"
        fig.savefig(out, facecolor=BG, bbox_inches="tight")   # GridSpec 已配版面
        plt.close(fig)
        return out
    except Exception as e:
        print(f"[chart] render error: {type(e).__name__}: {e}")
        try:
            plt.close("all")
        except Exception:
            pass
        return None


async def _fetch_coinglass_overlays(symbol: str, tf: str, n: int) -> dict:
    """v30: 抓 CoinGlass 可畫序列（CVD / OI）+ 即時指標（資金費率/多空比）。
    任何失敗都回部分結果，絕不阻斷繪圖。"""
    out: dict = {"cvd": None, "oi": None, "funding": None, "ls_ratio": None,
                 "oi_delta_24h": None, "cvd_slope": None}
    try:
        from market_intel_mcp.sources import get_source
        src = get_source()
        import asyncio as _aio

        async def _safe(coro):
            try:
                return await coro
            except Exception:
                return None
        cvd, oi, fund, pos = await _aio.gather(
            _safe(src.get_cvd_series(symbol, tf, n)),
            _safe(src.get_oi(symbol, tf, n)),
            _safe(src.get_funding(symbol)),
            _safe(src.get_positioning(symbol, "global", tf, n)),
        )
        if cvd and not cvd.get("error") and cvd.get("series"):
            out["cvd"] = [s["value"] for s in cvd["series"]][-n:]
            out["cvd_slope"] = cvd.get("cvd_slope")
        if oi and not oi.get("error") and oi.get("series"):
            out["oi"] = [s["value"] for s in oi["series"]][-n:]
            out["oi_delta_24h"] = oi.get("delta_pct_24h")
        if fund and not fund.get("error"):
            out["funding"] = fund.get("funding") or fund.get("latest")
        if pos and not pos.get("error"):
            out["ls_ratio"] = pos.get("ratio") or pos.get("latest")
    except Exception as e:
        print(f"[chart] coinglass overlay error: {type(e).__name__}: {e}")
    return out


async def render_symbol_chart(symbol: str, tf: str = "4h", bars: int = 120,
                              plan: dict | None = None) -> Path | None:
    """抓數據 + 算 SMC + CoinGlass 疊加 + 渲染。失敗回 None（絕不阻塞呼叫端）。"""
    try:
        from market_intel_mcp.smc_levels import compute_smc_levels
        from market_intel_mcp.sources.okx_candles import OkxCandlesSource
        okx = OkxCandlesSource()
        try:
            d = await okx.get_candles(symbol, tf, bars)
        finally:
            await okx.close()
        candles = d.get("candles") if isinstance(d, dict) else None
        if not candles:
            return None
        smc = compute_smc_levels(candles)
        if smc.get("error"):
            smc = {}
        overlays = await _fetch_coinglass_overlays(symbol, tf, len(candles))
        return render_smc_chart(symbol, candles, smc, tf=tf, plan=plan, overlays=overlays)
    except Exception as e:
        print(f"[chart] {symbol} error: {type(e).__name__}: {e}")
        return None


if __name__ == "__main__":
    import asyncio

    async def selftest():
        p = await render_symbol_chart(
            "BTC", "4h", 120,
            plan={"entry": None, "stop": None, "tp1": None, "tp2": None,
                  "tp3": None, "direction": None})
        print(f"chart saved: {p}")
        assert p and p.exists() and p.stat().st_size > 20000, "PNG too small/missing"
        print(f"size: {p.stat().st_size//1024} KB — PASS")
    asyncio.run(selftest())
