"""紙上驗證帳「累積 R 走勢圖」— 朋友回饋 Q1：做成像交易所跟單系統那樣的走勢圖。

設計：把 paper_trades 已平倉（排除 entry_expired 限價未成交）依平倉時間排序，
    各引擎獨立累積 R（risk-multiple），畫成時間軸折線圖（深色，TradingView 風格）。
    加密 deepdive 與美股 us_breakout 分兩條線——合併會誤導（兩引擎數學完全不同），
    美股 2026-07-28 起已過預註冊統計閘（PSRc>=0.95），但仍為紙上毛利口徑、
    真錢未驗，圖上照標不作對外宣稱（紅線③雙向誠實）。

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
# v177 重設計：色階經 dataviz 六檢驗證（暗面 OKLCH L 帶 0.48-0.67、CVD ΔE>24、對比>3:1）
CRYPTO_COL = "#4292dd"   # 加密 deepdive（藍實線）
US_COL = "#cc7f16"       # 美股 us_breakout（琥珀實線）
# 加密資料源停權窗（2026-07-08 CoinGlass 到期 → 2026-07-29 v141 OKX 源上線）
_OUTAGE_START_MS = 1783468800000   # 2026-07-08 00:00 UTC
_OUTAGE_END_MS = 1785265313000     # 2026-07-29 03:01 台北（v141 上線）
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


def _net_stats() -> dict[str, dict]:
    """淨帳（扣費）統計。net_r 欄 v118 起才落帳→只統計有資料的列並標明 n，
    永不把毛/淨混算或回填（紅線③）。"""
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT setup, COUNT(net_r), IFNULL(SUM(net_r),0) FROM paper_trades "
            "WHERE status='closed' AND IFNULL(exit_reason,'')!='entry_expired' "
            "AND net_r IS NOT NULL GROUP BY setup='us_breakout'").fetchall()
    finally:
        conn.close()
    out = {"crypto": {"n": 0, "sum": 0.0}, "us": {"n": 0, "sum": 0.0}}
    for setup, n, s in rows:
        key = "us" if setup == "us_breakout" else "crypto"
        out[key] = {"n": int(n), "sum": float(s)}
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
        net = _net_stats()

        # v177 重設計（dataviz 六檢）：真日曆軸/停權帶誠實標註/圖例出圖外/
        # 去點狀噪點/去色塊/僅水平網格。兩引擎共用同一條時間軸＝可直接對照。
        import datetime as _dt

        fig, ax = plt.subplots(figsize=(11.6, 5.8), facecolor=BG)
        ax.set_facecolor(BG)

        def _xy(points):
            xs = [_dt.datetime.fromtimestamp(p[0] / 1000.0) for p in points]
            ys = [p[1] for p in points]
            return xs, ys

        # 停權窗（誠實脈絡：那段加密平線不是沒行情，是資料源死了）
        _os = _dt.datetime.fromtimestamp(_OUTAGE_START_MS / 1000.0)
        _oe = _dt.datetime.fromtimestamp(_OUTAGE_END_MS / 1000.0)
        ax.axvspan(_os, _oe, color=MUTED, alpha=0.07, zorder=0)
        ax.axvline(_oe, color=MUTED, alpha=0.45, linewidth=0.8,
                   linestyle=(0, (2, 3)), zorder=1)

        end_pts = []   # (x, y, 名稱, 值文字, 色)
        if series["crypto"]:
            xs, ys = _xy(series["crypto"])
            ax.plot(xs, ys, color=CRYPTO_COL, linewidth=2.0,
                    solid_capstyle="round", zorder=3)
            end_pts.append((xs[-1], ys[-1], "加密", cs["cum_r"], CRYPTO_COL))
        if series["us"]:
            xs, ys = _xy(series["us"])
            ax.plot(xs, ys, color=US_COL, linewidth=2.0,
                    solid_capstyle="round", zorder=3)
            end_pts.append((xs[-1], ys[-1], "美股", ss["cum_r"], US_COL))

        # 線端直接標註：彩色圓點載識別、文字用墨色（dataviz：文字穿文字色）
        for x, y, name, val, col in end_pts:
            ax.plot([x], [y], marker="o", markersize=6, color=col, zorder=4)
            ax.annotate(f"{name} {val:+.2f}R", (x, y), color=FG, fontsize=10.5,
                        fontweight="bold", xytext=(9, 0),
                        textcoords="offset points", va="center", zorder=4)

        ax.axhline(0, color=MUTED, linewidth=0.9, alpha=0.55, zorder=1)
        ax.grid(True, axis="y", color=GRID, linewidth=0.5, alpha=0.35)
        ax.tick_params(colors=MUTED, labelsize=9, length=0)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("bottom", "left"):
            ax.spines[side].set_color(GRID)
        from matplotlib import dates as _mdates
        ax.xaxis.set_major_locator(_mdates.AutoDateLocator(minticks=5, maxticks=9))
        ax.xaxis.set_major_formatter(_mdates.DateFormatter("%m/%d"))
        ax.set_ylabel("累積 R（每筆風險倍數，非報酬%）", color=MUTED, fontsize=9.5)

        # 停權帶標籤（放圖內頂部、貼帶置中）
        ax.text(_os + (_oe - _os) / 2, 0.985, "加密資料源停權",
                transform=ax.get_xaxis_transform(), ha="center", va="top",
                color=MUTED, fontsize=8.5)
        ax.text(_oe, 0.02, " 換源(OKX)", transform=ax.get_xaxis_transform(),
                ha="left", va="bottom", color=MUTED, fontsize=8.5)

        # 標題區＋圖外圖例（彩色bullet載識別、統計文字用墨色，不再壓在數據上）
        fig.text(0.055, 0.97, "皮諾丘紙上驗證帳　·　累積 R 走勢",
                 color=FG, fontsize=15, fontweight="bold", va="top")
        fig.text(0.055, 0.923,
                 "紙上模擬驗證 · 非實盤績效（實倉 0 筆）· 已排除限價未成交 · 日曆時間軸",
                 color=MUTED, fontsize=9.5, va="top")
        _rows = []
        if series["crypto"]:
            _rows.append((CRYPTO_COL,
                          f"加密 deepdive　n={cs['n']}　累積 {cs['cum_r']:+.2f}R"
                          f"　勝率 {cs['win_rate']:.0f}%"
                          + (f"　·　淨帳(扣費,近{net['crypto']['n']}筆) "
                             f"{net['crypto']['sum']:+.2f}R"
                             if net['crypto']['n'] else "")))
        if series["us"]:
            # 紅線③雙向誠實：過閘照登、紙上毛利口徑與真錢未驗照標
            _rows.append((US_COL,
                          f"美股 us_breakout（已過統計閘 PSRc>=0.95·真錢未驗）"
                          f"　n={ss['n']}　累積 {ss['cum_r']:+.2f}R"
                          + (f"　·　淨帳(扣費,近{net['us']['n']}筆) "
                             f"{net['us']['sum']:+.2f}R"
                             if net['us']['n'] else "")))
        for i, (col, txt) in enumerate(_rows):
            y = 0.878 - i * 0.042
            fig.text(0.058, y, "●", color=col, fontsize=10, va="top")
            fig.text(0.075, y, txt, color=FG, fontsize=9.5, va="top")

        fig.text(0.055, 0.018,
                 "Y 軸為 R（風險倍數），非報酬率　|　兩引擎數學不同故分線　|　"
                 "美股已過統計閘·紙上毛利口徑·真錢未驗·不作對外宣稱　|　輸贏全記，不挑單",
                 color=MUTED, fontsize=8.5, va="bottom")

        plt.subplots_adjust(top=0.775, bottom=0.115, left=0.075, right=0.90)

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
