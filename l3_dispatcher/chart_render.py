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
from matplotlib.patches import Rectangle

from botpaths import data_dir

CHART_DIR = data_dir() / "charts"
KEEP_CHARTS = 50

UP = "#26a69a"      # 綠
DOWN = "#ef5350"    # 紅
BG = "#131722"      # TradingView 深色
FG = "#d1d4dc"
GRID = "#2a2e39"


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
                     plan: dict | None = None) -> Path | None:
    """畫圖。plan（可選）= {entry, stop, tp1, tp2, tp3, direction}。
    回 PNG 路徑；失敗回 None。"""
    try:
        if not candles or len(candles) < 30:
            return None
        n = len(candles)
        xs = range(n)

        fig, ax = plt.subplots(figsize=(12, 6.5), dpi=110)
        fig.patch.set_facecolor(BG)
        ax.set_facecolor(BG)

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

        # === Swing 高低點 ===
        for s in (smc.get("swing_points") or [])[:10]:
            x0 = max(0, n - 1 - int(s.get("ago_bars") or 0))
            is_high = s.get("type") == "high"
            ax.annotate("⌃" if is_high else "⌄",
                        (x0, s["level"]),
                        color=(DOWN if is_high else UP), fontsize=10,
                        ha="center",
                        va="bottom" if is_high else "top", zorder=5)

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

        # === 樣式 ===
        ax.grid(color=GRID, linewidth=0.4, alpha=0.5)
        ax.tick_params(colors=FG, labelsize=8)
        for spine in ax.spines.values():
            spine.set_color(GRID)
        cur = candles[-1]["close"]
        dir_str = ""
        if plan and plan.get("direction"):
            dir_str = "・做多" if plan["direction"] == "bull" else "・做空"
        ax.set_title(f"{symbol}/USDT 永續・{tf.upper()}・SMC 結構{dir_str}"
                     f"（現價 {cur:,.6g}）",
                     color=FG, fontsize=11)
        ax.set_xlim(-1, n + 6)

        _prune_old()
        out = CHART_DIR / f"{symbol}_{int(time.time())}.png"
        fig.tight_layout()
        fig.savefig(out, facecolor=BG, bbox_inches="tight")
        plt.close(fig)
        return out
    except Exception as e:
        print(f"[chart] render error: {type(e).__name__}: {e}")
        try:
            plt.close("all")
        except Exception:
            pass
        return None


async def render_symbol_chart(symbol: str, tf: str = "4h", bars: int = 120,
                              plan: dict | None = None) -> Path | None:
    """抓數據 + 算 SMC + 渲染。失敗回 None（絕不阻塞呼叫端）。"""
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
        return render_smc_chart(symbol, candles, smc, tf=tf, plan=plan)
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
