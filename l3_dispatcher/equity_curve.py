"""紙上驗證帳「累積 R 走勢圖」— 朋友回饋 Q1：做成像交易所跟單系統那樣的走勢圖。

設計：把 paper_trades 已平倉（排除 entry_expired 限價未成交）依平倉時間排序，
    各引擎獨立累積 R（risk-multiple），畫成時間軸折線圖（深色，TradingView 風格）。
    加密 deepdive 與美股 us_breakout 分兩條線——合併會誤導（兩引擎數學完全不同），
    且美股樣本量不足、統計不可信，必須在圖上誠實標註（紅線③：不捏造績效）。

誠實邊界：
    * 標題與頁尾明示「紙上模擬驗證 · 非實盤績效」。實倉 trades 表 0 筆，畫不出實盤戰績。
    * 美股線以虛線 + 灰字 +「樣本不足·統計不可信」標註，避免 +7.8R 視覺誤導。
    * Y 軸是 R（每筆風險的倍數），不是報酬 %，不宣稱獲利率。

用途：macro.run_performance_loop 每日績效推播附圖（Telegram 私群，非對外公開網頁）。
CLI：python -m l3_dispatcher.equity_curve
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import rcParams

from botpaths import db_path as _db_path
from l3_dispatcher.chart_render import BG, CHART_DIR, FG, GRID, _prune_old

DB_PATH = _db_path("trade_journal.db")

rcParams["font.sans-serif"] = ["Microsoft JhengHei", "Microsoft YaHei",
                               "SimHei", "Arial Unicode MS", "sans-serif"]
rcParams["axes.unicode_minus"] = False

MUTED = "#787f8c"
CRYPTO_COL = "#4c9be8"   # 加密 deepdive（藍實線）
US_COL = "#ffa726"       # 美股 us_breakout（橘虛線，實驗）
WIN_GREEN = "#26a69a"


def _fetch_series() -> dict[str, list[tuple[float, float]]]:
    """讀已平倉紙上交易（排除 entry_expired），各引擎依平倉時間累積 R。

    回 {"crypto": [(exit_at_ms, cum_r), ...], "us": [...]}，已依時間排序。
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT setup, realized_r, exit_at FROM paper_trades "
            "WHERE status='closed' AND IFNULL(exit_reason,'')!='entry_expired' "
            "AND exit_at IS NOT NULL "
            "ORDER BY exit_at ASC").fetchall()
    finally:
        conn.close()

    series: dict[str, list[tuple[float, float]]] = {"crypto": [], "us": []}
    cum = {"crypto": 0.0, "us": 0.0}
    for setup, r, exit_at in rows:
        key = "us" if setup == "us_breakout" else "crypto"
        cum[key] += (r or 0.0)
        series[key].append((float(exit_at), cum[key]))
    return series


def _stats(points: list[tuple[float, float]], raw_rs: list[float]) -> dict:
    n = len(points)
    cum_r = points[-1][1] if points else 0.0
    wins = sum(1 for r in raw_rs if r > 0)
    wr = (wins / n * 100.0) if n else 0.0
    return {"n": n, "cum_r": cum_r, "win_rate": wr}


def _raw_rs() -> dict[str, list[float]]:
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT setup, realized_r FROM paper_trades "
            "WHERE status='closed' AND IFNULL(exit_reason,'')!='entry_expired' "
            "AND exit_at IS NOT NULL ORDER BY exit_at ASC").fetchall()
    finally:
        conn.close()
    out: dict[str, list[float]] = {"crypto": [], "us": []}
    for setup, r in rows:
        key = "us" if setup == "us_breakout" else "crypto"
        out[key].append(r or 0.0)
    return out


def render_equity_curve() -> Path | None:
    """渲染紙上驗證帳累積 R 走勢圖。無已平倉資料回 None。"""
    try:
        series = _fetch_series()
        if not series["crypto"] and not series["us"]:
            return None
        raw = _raw_rs()
        cs = _stats(series["crypto"], raw["crypto"])
        ss = _stats(series["us"], raw["us"])

        fig, ax = plt.subplots(figsize=(10.0, 5.4), facecolor=BG)
        ax.set_facecolor(BG)

        # 時間軸：用「天數序列」而非原始 epoch（兩引擎時間範圍不同，對齊到各自第一筆）
        def _xy(points):
            if not points:
                return [], []
            t0 = points[0][0]
            xs = [(p[0] - t0) / 86400000.0 for p in points]  # 距首筆天數
            ys = [p[1] for p in points]
            return xs, ys

        if series["crypto"]:
            xs, ys = _xy(series["crypto"])
            ax.plot(xs, ys, color=CRYPTO_COL, linewidth=2.2, marker="o",
                    markersize=3.5, label=(
                        f"加密 deepdive　n={cs['n']}　"
                        f"累積 {cs['cum_r']:+.2f}R　勝率 {cs['win_rate']:.0f}%"))
            ax.fill_between(xs, ys, 0, color=CRYPTO_COL, alpha=0.07)
            ax.annotate(f"{cs['cum_r']:+.2f}R", (xs[-1], ys[-1]),
                        color=CRYPTO_COL, fontsize=11, fontweight="bold",
                        xytext=(6, 0), textcoords="offset points", va="center")

        if series["us"]:
            xs, ys = _xy(series["us"])
            ax.plot(xs, ys, color=US_COL, linewidth=1.8, linestyle="--",
                    marker="s", markersize=3.0, alpha=0.85, label=(
                        f"美股 us_breakout（實驗·樣本不足·統計不可信）　n={ss['n']}　"
                        f"累積 {ss['cum_r']:+.2f}R"))
            ax.annotate(f"{ss['cum_r']:+.2f}R", (xs[-1], ys[-1]),
                        color=US_COL, fontsize=10, xytext=(6, 0),
                        textcoords="offset points", va="center", alpha=0.9)

        ax.axhline(0, color=GRID, linewidth=1.0, zorder=0)
        ax.grid(True, color=GRID, linewidth=0.5, alpha=0.5)
        ax.tick_params(colors=MUTED, labelsize=9)
        for spine in ax.spines.values():
            spine.set_color(GRID)
        ax.set_xlabel("距首筆交易天數", color=MUTED, fontsize=10)
        ax.set_ylabel("累積 R（每筆風險倍數，非報酬%）", color=MUTED, fontsize=10)

        leg = ax.legend(loc="upper left", facecolor=BG, edgecolor=GRID,
                        labelcolor=FG, fontsize=9.5)
        leg.get_frame().set_alpha(0.85)

        fig.text(0.07, 0.965, "皮諾丘紙上驗證帳　·　累積 R 走勢",
                 color=FG, fontsize=15, fontweight="bold", va="top")
        fig.text(0.07, 0.915,
                 "紙上模擬驗證　·　非實盤績效（實倉 0 筆）　·　已排除限價未成交",
                 color=MUTED, fontsize=10, va="top")

        fig.text(0.07, 0.02,
                 "Y 軸為 R（風險倍數），非報酬率　|　兩引擎數學不同故分線　|　"
                 "美股樣本量不足、勝率不可作績效宣稱　|　輸贏全記，不挑單",
                 color=MUTED, fontsize=8.5, va="bottom")

        plt.subplots_adjust(top=0.86, bottom=0.13, left=0.085, right=0.96)

        CHART_DIR.mkdir(parents=True, exist_ok=True)
        out = CHART_DIR / f"equity_{int(time.time())}.png"
        fig.savefig(out, facecolor=BG, dpi=130)
        plt.close(fig)
        _prune_old()
        return out
    except Exception as e:
        print(f"[equity_curve] render error: {type(e).__name__}: {e}")
        return None


if __name__ == "__main__":
    import sys

    p = render_equity_curve()
    print(f"equity curve: {p}")
    sys.exit(0 if p else 1)
