"""v21-A: 全市場資金流向四象限圖 — 一張圖看懂主力行為。

座標：X = 價格變化%、Y = OI（未平倉量）變化%，預設 4 小時窗口。
    Q1 (+,+) 多頭建倉    價漲且新錢進場 — 趨勢最健康
    Q2 (+,-) 空頭平倉    價漲但 OI 降 — 軋空回補，動能可能不持久
    Q3 (-,-) 多頭平倉    價跌且 OI 降 — 多頭撤退、去槓桿
    Q4 (-,+) 空頭建倉    價跌且新錢進場做空 — 趨勢向下最兇

資料：scanner.db snapshots（market_scanner 每 5 分鐘已在累積，零額外 API）。
輸出：charts/quadrant_{ts}.png，每日 09:00 台北由 scanner loop 推送。
"""
from __future__ import annotations

import math
import sqlite3
import time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import rcParams

from botpaths import db_path as _db_path
from l3_dispatcher.chart_render import BG, CHART_DIR, DOWN, FG, UP, _prune_old

DB_PATH = _db_path("scanner.db")

rcParams["font.sans-serif"] = ["Microsoft JhengHei", "Microsoft YaHei",
                               "SimHei", "Arial Unicode MS", "sans-serif"]
rcParams["axes.unicode_minus"] = False

MIN_VOL_USD = 10_000_000     # 與 market_scanner 一致的流動性下限
AXIS_CLIP = 12.0             # 軸範圍 ±12%，極端值壓在邊緣並標記
LABEL_TOP_N = 14             # 標註離原點最遠的前 N 檔

Q_COLORS = {1: "#26a69a", 2: "#7fc8a9", 3: "#f0a35e", 4: "#ef5350"}
Q_NAMES = {1: "多頭建倉", 2: "空頭平倉", 3: "多頭平倉", 4: "空頭建倉"}


def _quadrant(px: float, oi: float) -> int:
    if px >= 0 and oi >= 0:
        return 1
    if px >= 0:
        return 2
    if oi < 0:
        return 3
    return 4


def _load_pair(window_min: int) -> tuple[list[dict], int] | None:
    """取最新快照與 N 分鐘前快照，算每檔的 價格%/OI% 變化。"""
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute("SELECT MAX(ts) FROM snapshots").fetchone()
        if not row or not row[0]:
            return None
        ts_now = row[0]
        target = ts_now - window_min * 60
        past_ts = conn.execute(
            "SELECT ts FROM snapshots WHERE ts BETWEEN ? AND ? "
            "ORDER BY ABS(ts - ?) LIMIT 1",
            (target - 600, target + 600, target)).fetchone()
        if not past_ts:
            return None
        cur = {r[0]: r for r in conn.execute(
            "SELECT inst, last, oi_usd, vol24h_usd FROM snapshots WHERE ts=?",
            (ts_now,)).fetchall()}
        past = {r[0]: r for r in conn.execute(
            "SELECT inst, last, oi_usd FROM snapshots WHERE ts=?",
            (past_ts[0],)).fetchall()}
    finally:
        conn.close()

    pts = []
    for sym, (_, last, oi, vol) in cur.items():
        p = past.get(sym)
        if not p or not last or not oi or (vol or 0) < MIN_VOL_USD:
            continue
        p_last, p_oi = p[1], p[2]
        if not p_last or not p_oi:
            continue
        px_chg = (last - p_last) / p_last * 100
        oi_chg = (oi - p_oi) / p_oi * 100
        pts.append({"sym": sym, "px": px_chg, "oi": oi_chg, "vol": vol})
    return (pts, ts_now) if pts else None


def render_quadrant_chart(window_min: int = 240) -> Path | None:
    """渲染四象限資金流向圖。資料不足（冷啟動）回 None。"""
    try:
        loaded = _load_pair(window_min)
        if not loaded:
            return None
        pts, ts_now = loaded

        fig, ax = plt.subplots(figsize=(11, 8.5), facecolor=BG)
        ax.set_facecolor(BG)
        lim = AXIS_CLIP
        # 象限底色
        ax.axhspan(0, lim, xmin=0.5, xmax=1.0, color=UP, alpha=0.07)
        ax.axhspan(-lim, 0, xmin=0.5, xmax=1.0, color="#7fc8a9", alpha=0.05)
        ax.axhspan(-lim, 0, xmin=0.0, xmax=0.5, color="#f0a35e", alpha=0.05)
        ax.axhspan(0, lim, xmin=0.0, xmax=0.5, color=DOWN, alpha=0.07)
        ax.axhline(0, color=FG, lw=0.8, alpha=0.5)
        ax.axvline(0, color=FG, lw=0.8, alpha=0.5)

        # 象限角落標籤
        corner = {1: (lim * 0.97, lim * 0.95, "right", "top"),
                  2: (lim * 0.97, -lim * 0.95, "right", "bottom"),
                  3: (-lim * 0.97, -lim * 0.95, "left", "bottom"),
                  4: (-lim * 0.97, lim * 0.95, "left", "top")}
        for q, (x, y, ha, va) in corner.items():
            ax.text(x, y, f"{Q_NAMES[q]}\nQ{q}", ha=ha, va=va,
                    color=Q_COLORS[q], fontsize=13, fontweight="bold", alpha=0.85)

        # 散點（壓邊處理 + 量能決定點大小）
        for p in pts:
            x = max(-lim, min(lim, p["px"]))
            y = max(-lim, min(lim, p["oi"]))
            q = _quadrant(p["px"], p["oi"])
            size = max(18, min(420, math.sqrt(p["vol"] / 1e6) * 9))
            edge = "#ffffff" if (abs(p["px"]) > lim or abs(p["oi"]) > lim) else "none"
            ax.scatter(x, y, s=size, color=Q_COLORS[q], alpha=0.75,
                       edgecolors=edge, linewidths=0.8, zorder=3)

        # 標註最突出的前 N 檔
        ranked = sorted(pts, key=lambda p: p["px"] ** 2 + p["oi"] ** 2,
                        reverse=True)[:LABEL_TOP_N]
        for p in ranked:
            x = max(-lim, min(lim, p["px"]))
            y = max(-lim, min(lim, p["oi"]))
            tag = p["sym"]
            if abs(p["px"]) > lim or abs(p["oi"]) > lim:
                tag += f" ({p['px']:+.0f}%,{p['oi']:+.0f}%)"
            ax.annotate(tag, (x, y), textcoords="offset points", xytext=(6, 5),
                        color=FG, fontsize=8.5, zorder=4)

        ax.set_xlim(-lim, lim)
        ax.set_ylim(-lim, lim)
        ax.set_xlabel(f"價格變化 %（{window_min // 60} 小時）", color=FG, fontsize=11)
        ax.set_ylabel(f"OI 未平倉量變化 %（{window_min // 60} 小時）", color=FG, fontsize=11)
        ax.tick_params(colors=FG)
        for s in ax.spines.values():
            s.set_color("#2a2e39")

        tstr = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts_now))
        n_q = {q: sum(1 for p in pts if _quadrant(p["px"], p["oi"]) == q)
               for q in (1, 2, 3, 4)}
        ax.set_title(
            f"全市場資金流向圖　{tstr} 台北　共 {len(pts)} 檔（量>$10M）\n"
            f"建倉多 {n_q[1]}｜軋空 {n_q[2]}｜多平 {n_q[3]}｜建倉空 {n_q[4]}",
            color=FG, fontsize=13, pad=12)

        CHART_DIR.mkdir(parents=True, exist_ok=True)
        out = CHART_DIR / f"quadrant_{int(time.time())}.png"
        fig.tight_layout()
        fig.savefig(out, facecolor=BG, bbox_inches="tight", dpi=110)
        plt.close(fig)
        _prune_old()
        return out
    except Exception as e:
        print(f"[quadrant] render error: {type(e).__name__}: {e}")
        return None


def quadrant_summary_line(window_min: int = 240) -> str:
    """給 caption 用的一行摘要（無 HTML）。"""
    loaded = _load_pair(window_min)
    if not loaded:
        return ""
    pts, _ = loaded
    n_q = {q: sum(1 for p in pts if _quadrant(p["px"], p["oi"]) == q)
           for q in (1, 2, 3, 4)}
    lead = max(n_q, key=n_q.get)
    return (f"主導行為：{Q_NAMES[lead]}（{n_q[lead]}/{len(pts)} 檔）｜"
            f"多頭建倉 {n_q[1]}・軋空 {n_q[2]}・多平 {n_q[3]}・空頭建倉 {n_q[4]}")


if __name__ == "__main__":
    import sys
    p = render_quadrant_chart()
    print(f"chart: {p}")
    print(quadrant_summary_line())
    sys.exit(0 if p else 1)
