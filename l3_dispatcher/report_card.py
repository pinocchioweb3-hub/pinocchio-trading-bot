"""v21-B: 訊號成績卡 — 把誠實帳本變成可分享的圖片。

設計：最近 N 筆已平倉紙上交易渲染成卡片牆（2 欄網格），每卡：
    幣種 + 方向徽章 / 進出場價 / R 值大字（止盈綠、止損紅）/
    持倉時長 / 觸及的 TP 腿 / 日期。
頁首：驗證帳戰績摘要；頁尾：Stage 0 進度（X/100）— 輸贏都上卡，不挑單。

用途：每日績效報告附圖（macro.run_performance_loop 掛點）+
    未來 Threads 建造日誌的素材。
CLI：python -m l3_dispatcher.report_card [N]
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import rcParams
from matplotlib.patches import FancyBboxPatch

from botpaths import db_path as _db_path
from l3_dispatcher.chart_render import BG, CHART_DIR, DOWN, FG, UP, _prune_old

DB_PATH = _db_path("trade_journal.db")

rcParams["font.sans-serif"] = ["Microsoft JhengHei", "Microsoft YaHei",
                               "SimHei", "Arial Unicode MS", "sans-serif"]
rcParams["axes.unicode_minus"] = False

CARD_BG = "#1b2030"
MUTED = "#787f8c"
STAGE1_GATE = 100   # 紙上驗證門檻


def _fetch_closed(n: int) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT symbol, setup, direction, entry_price, stop_price, "
            "pnl_usd, realized_r, exit_reason, legs_hit, entry_at, exit_at "
            "FROM paper_trades WHERE status='closed' "
            "AND IFNULL(exit_reason,'')!='entry_expired' "  # v33: 掛單逾時作廢不上成績卡牆／不計 Stage1 門檻
            "ORDER BY exit_at DESC LIMIT ?", (n,)).fetchall()
        total_closed = conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE status='closed' "
            "AND IFNULL(exit_reason,'')!='entry_expired'").fetchone()[0]
    finally:
        conn.close()
    out = []
    for r in rows:
        out.append({"symbol": r[0], "setup": r[1], "direction": r[2],
                    "entry": r[3], "stop": r[4], "pnl": r[5] or 0,
                    "r": r[6] or 0, "exit_reason": r[7] or "",
                    "legs": r[8] or "", "entry_at": r[9], "exit_at": r[10]})
    return out, total_closed


def _fmt_duration(ms_start: int, ms_end: int) -> str:
    if not ms_start or not ms_end:
        return "—"
    h = (ms_end - ms_start) / 3600000
    if h < 1:
        return f"{h * 60:.0f} 分鐘"
    if h < 48:
        return f"{h:.1f} 小時"
    return f"{h / 24:.1f} 天"


def render_report_cards(n: int = 6) -> Path | None:
    """渲染最近 N 筆已平倉成績卡牆。無已平倉資料回 None。"""
    try:
        trades, total_closed = _fetch_closed(n)
        if not trades:
            return None
        n = len(trades)
        cols = 2
        rows = (n + cols - 1) // cols

        fig_h = 1.7 + rows * 2.1 + 0.6
        fig, _ = plt.subplots(figsize=(9.6, fig_h), facecolor=BG)
        plt.axis("off")

        wins = [t for t in trades if t["r"] > 0]
        sum_r = sum(t["r"] for t in trades)
        # 頁首
        fig.text(0.05, 1 - 0.32 / fig_h, "皮諾丘紙上驗證帳",
                 color=FG, fontsize=17, fontweight="bold", va="top")
        fig.text(0.05, 1 - 0.78 / fig_h,
                 f"最近 {n} 筆已平倉　勝 {len(wins)} 敗 {n - len(wins)}　"
                 f"合計 {sum_r:+.2f}R",
                 color=MUTED, fontsize=11.5, va="top")
        fig.text(0.95, 1 - 0.32 / fig_h,
                 time.strftime("%Y-%m-%d", time.localtime()),
                 color=MUTED, fontsize=11, ha="right", va="top")

        top_margin = 1.25 / fig_h
        bottom_margin = 0.55 / fig_h
        grid_h = 1 - top_margin - bottom_margin
        cell_h = grid_h / rows
        cell_w = 0.9 / cols

        for i, t in enumerate(trades):
            r_i, c_i = divmod(i, cols)
            x0 = 0.05 + c_i * (cell_w + 0.0) + (0.02 if c_i else 0)
            y0 = 1 - top_margin - (r_i + 1) * cell_h + 0.015
            w, h = cell_w - 0.03, cell_h - 0.03

            win = t["r"] > 0
            accent = UP if win else DOWN
            ax = fig.add_axes([x0, y0, w, h])
            ax.axis("off")
            ax.add_patch(FancyBboxPatch(
                (0, 0), 1, 1, transform=ax.transAxes,
                boxstyle="round,pad=0.02,rounding_size=0.04",
                facecolor=CARD_BG, edgecolor=accent, linewidth=1.4))

            dir_zh = "做多" if t["direction"] == "bull" else "做空"
            ax.text(0.05, 0.86, f"{t['symbol']}", transform=ax.transAxes,
                    color=FG, fontsize=14, fontweight="bold", va="center")
            ax.text(0.05, 0.66, f"{dir_zh}・{t['setup']}", transform=ax.transAxes,
                    color=MUTED, fontsize=9, va="center")
            # R 值大字
            ax.text(0.95, 0.78, f"{t['r']:+.2f}R", transform=ax.transAxes,
                    color=accent, fontsize=20, fontweight="bold",
                    ha="right", va="center")
            # 結果標籤（不用 emoji — JhengHei 缺字）
            result = ("● " + (t["legs"].upper().replace(",", "+") or "止盈")
                      if win else "● 止損")
            if "timeout" in (t["exit_reason"] or "").lower():
                result = "● 時間出場"
            ax.text(0.95, 0.58, result, transform=ax.transAxes,
                    color=accent, fontsize=9.5, ha="right", va="center")
            # 進出場與時長（\$ 跳脫 — 裸 $ 會觸發 matplotlib 數學模式）
            ax.text(0.05, 0.38,
                    f"進場 \\${t['entry']:,.6g}　止損 \\${t['stop']:,.6g}",
                    transform=ax.transAxes, color=FG, fontsize=9, va="center")
            ax.text(0.05, 0.18,
                    f"持倉 {_fmt_duration(t['entry_at'], t['exit_at'])}　"
                    + time.strftime("%m/%d %H:%M",
                                    time.localtime((t["exit_at"] or 0) / 1000)),
                    transform=ax.transAxes, color=MUTED, fontsize=8.5, va="center")

        # 頁尾：誠實宣言 + Stage 進度
        fig.text(0.05, 0.18 / fig_h,
                 f"Stage 0 紙上驗證進度 {total_closed}/{STAGE1_GATE}　|　"
                 f"輸贏都公開，不挑單　|　github.com/pinocchioweb3-hub",
                 color=MUTED, fontsize=9.5, va="bottom")

        CHART_DIR.mkdir(parents=True, exist_ok=True)
        out = CHART_DIR / f"report_{int(time.time())}.png"
        fig.savefig(out, facecolor=BG, bbox_inches="tight", dpi=130)
        plt.close(fig)
        _prune_old()
        return out
    except Exception as e:
        print(f"[report_card] render error: {type(e).__name__}: {e}")
        return None


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    p = render_report_cards(n)
    print(f"card: {p}")
    sys.exit(0 if p else 1)
