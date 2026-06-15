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

UP = "#26a69a"      # 綠（看漲/支撐/需求）
DOWN = "#ef5350"    # 紅（看跌/阻力/供給）
BG = "#131722"      # TradingView 深色
FG = "#d1d4dc"
GRID = "#2a2e39"
VOLMA = "#f5d442"   # 量能均線（黃）
OICOL = "#5b9bd5"   # OI 線（藍）
SNR_R = "#ef5350"   # 壓力
SNR_S = "#26a69a"   # 支撐
# v33 語意化配色
BREAKER = "#ab47bc" # 紫（Breaker / 反轉結構）
PLAN_E = "#f5d442"  # 進場線（黃）
FUND_C = "#ffa726"  # 資金費率（橘）
LSCOL = "#26c6da"   # 多空比（青）
SWEEP = "#ffca28"   # 流動性掃單標記（琥珀）


def _vol(c: dict) -> float:
    for k in ("volume", "vol", "vol_ccy", "volCcy", "baseVol"):
        v = c.get(k)
        if v:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


def _compute_snr(candles: list[dict], n_levels: int = 2) -> dict:
    """從近期 swing 高低密集區算支撐壓力『區帶』。
    v33：只保留觸及次數 >=3 的密集區（剔除單次雜訊線；不足則放寬到 >=2）；
    回 {resistance:[(price,cnt,lo,hi)...], support:[...]}（含區帶上下緣供畫 axhspan）。"""
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

    def _cluster(levels: list[float]) -> list[tuple]:
        levels = sorted(levels)
        clusters = []   # [avg, cnt, lo, hi]
        for lv in levels:
            if clusters and abs(lv - clusters[-1][0]) <= tol:
                avg, cnt, lo, hi = clusters[-1]
                cnt2 = cnt + 1
                clusters[-1] = [(avg * cnt + lv) / cnt2, cnt2, min(lo, lv), max(hi, lv)]
            else:
                clusters.append([lv, 1, lv, lv])
        return clusters

    def _pick(clusters, keep, want):
        # v33：只留密集區——先 >=3 次、不足補 >=2 次；單次觸及(雜訊)一律不畫
        for thr in (3, 2):
            sel = sorted([c for c in clusters if c[1] >= thr and keep(c[0])],
                         key=lambda x: -x[1])
            if len(sel) >= want or thr == 2:
                return [(round(c[0], 10), c[1], c[2], c[3]) for c in sel[:want]]
        return []

    res = _pick(_cluster(highs), lambda p: p > cur, n_levels)
    sup = _pick(_cluster(lows), lambda p: p < cur, n_levels)
    return {"resistance": sorted(res, key=lambda x: x[0]),
            "support": sorted(sup, key=lambda x: -x[0])}


def _atr(candles: list[dict], period: int = 14) -> float:
    """簡易 ATR（給 FVG displacement 過濾用）。"""
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return 0.0
    return sum(trs[-period:]) / min(len(trs), period)


def detect_structure_breaks(candles: list[dict], swings: list[dict]) -> list[dict]:
    """v33 自寫 BOS/CHoCH（smartmoneyconcepts 只給 BOS）。
    收盤反向突破前一對向 swing 極值=CHoCH(轉勢)；順勢突破=BOS(續勢)。
    回 [{type:'BOS'|'CHoCH', direction:'bull'|'bear', idx, level}]（最近 6 個）。"""
    n = len(candles)
    pts = sorted(
        [(n - 1 - int(s.get("ago_bars") or 0), s.get("type"), s.get("level"))
         for s in (swings or [])
         if s.get("level") is not None
         and 0 <= n - 1 - int(s.get("ago_bars") or 0) < n],
        key=lambda x: x[0])
    if not pts:
        return []
    breaks, trend = [], None
    active_high = active_low = None   # (idx, level)
    pi = 0
    for i in range(n):
        while pi < len(pts) and pts[pi][0] <= i:
            _idx, _t, _lv = pts[pi]
            if _t == "high":
                active_high = (_idx, _lv)
            else:
                active_low = (_idx, _lv)
            pi += 1
        c = candles[i]["close"]
        # 首次突破(trend=None)無前向「性格」可轉，依突破方向確立趨勢→標 BOS；
        # 僅當收盤突破「明確反向」既有趨勢時才是 CHoCH(轉勢)。
        if active_high and i > active_high[0] and c > active_high[1]:
            breaks.append({"type": "CHoCH" if trend == "down" else "BOS",
                           "direction": "bull", "idx": i, "level": active_high[1]})
            trend, active_high = "up", None
        elif active_low and i > active_low[0] and c < active_low[1]:
            breaks.append({"type": "CHoCH" if trend == "up" else "BOS",
                           "direction": "bear", "idx": i, "level": active_low[1]})
            trend, active_low = "down", None
    return breaks[-6:]


def _detect_sweeps(candles: list[dict], swings: list[dict], n: int,
                   lookahead: int = 6) -> list[dict]:
    """v33：偵測流動性掃單（liquidity sweep）。
    swing 高被後續K影線刺穿但收盤收回下方＝上方流動性被掃（▼，常見假突破/UTAD）；
    swing 低被刺穿但收回上方＝下方流動性被掃（▲，常見 Spring）。回標記清單。"""
    out = []
    rng = (max(c["high"] for c in candles) - min(c["low"] for c in candles)) or 1
    tol = rng * 0.0008
    for s in swings:
        ago = int(s.get("ago_bars") or 0)
        idx = n - 1 - ago
        if idx < 0 or idx >= n:
            continue
        lvl = s["level"]
        is_high = s.get("type") == "high"
        for j in range(idx + 1, min(idx + 1 + lookahead, n)):
            c = candles[j]
            if is_high and c["high"] > lvl + tol and c["close"] < lvl:
                out.append({"x": j, "level": c["high"], "dir": "down"}); break
            if (not is_high) and c["low"] < lvl - tol and c["close"] > lvl:
                out.append({"x": j, "level": c["low"], "dir": "up"}); break
    return out[-5:]   # 只留最近 5 個避免洗版


def _prune_old():
    try:
        CHART_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(CHART_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime)
        for p in files[:-KEEP_CHARTS]:
            p.unlink(missing_ok=True)
    except Exception:
        pass


def _structure_scorecard_lines(overlays: dict) -> list[str]:
    """v33：把 CoinGlass 結構評分卡 + 基差 + 情緒組成圖上佐證框文字。無資料回空。"""
    lines: list[str] = []
    st = overlays.get("structure") or {}
    if st:
        lines.append("◆ 結構評分（7d）")
        if st.get("atr_pct_7d") is not None:
            lines.append(f"  ATR% {st['atr_pct_7d']:.1f}　量比 "
                         f"{(st.get('vol_24h_vs_30d') or 0):.2f}")
        cvs = st.get("cvd_slope_7d")
        tts = st.get("top_trader_slope_7d")
        if cvs is not None or tts is not None:
            lines.append(f"  CVD斜率 {(cvs or 0):+.2f}　大戶斜率 {(tts or 0):+.2f}")
        oid = st.get("oi_delta_7d_pct")
        if oid is not None:
            lines.append(f"  OI 7d {oid:+.1f}%")
        flags = []
        if st.get("higher_lows_7d") is not None:
            flags.append("墊高低點✓" if st["higher_lows_7d"] else "未墊高低點")
        if st.get("above_4h_200ma") is not None:
            flags.append("站上4h_200MA✓" if st["above_4h_200ma"] else "在4h_200MA下")
        if flags:
            lines.append("  " + "　".join(flags))
    basis = overlays.get("basis") or {}
    if basis.get("pct") is not None:
        lines.append(f"◆ 期現基差 {basis['pct']:+.3f}%"
                     + (f"（{basis['interp']}）" if basis.get("interp") else ""))
    senti = overlays.get("sentiment") or {}
    if senti.get("fg") is not None:
        s = f"◆ 恐懼貪婪 {senti['fg']}"
        if senti.get("fg_label"):
            s += f"（{senti['fg_label']}）"
        lines.append(s)
    return lines


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
        has_fund = bool(overlays.get("funding_series"))
        has_liq = bool(overlays.get("liq_long_series"))
        has_ls = bool(overlays.get("ls_series"))

        # v33: 全指標多面板（畫質提升 dpi 180）— 價格/SMC/SNR + 量 + CVD + OI
        #      + 資金費率 + 清算(多空) + 多空比
        panels = [("price", 3.6), ("vol", 1.0)]
        if has_cvd:
            panels.append(("cvd", 1.0))
        if has_oi:
            panels.append(("oi", 1.0))
        if has_fund:
            panels.append(("funding", 0.85))
        if has_liq:
            panels.append(("liq", 0.9))
        if has_ls:
            panels.append(("ls", 0.85))
        fig = plt.figure(figsize=(13, 6 + 1.55 * len(panels)), dpi=180)
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

        # === FVG（v33：displacement 過濾 + 50% CE 中線；被反向收破的標 IFVG 換色）===
        _atr14 = _atr(candles)
        cur_px = candles[-1]["close"]
        _all_fvg = smc.get("fvg") or []
        _fresh, _ifvg = [], []
        for f in _all_fvg:
            is_bull = (f.get("type") or "").lower().startswith("bull")
            # displacement 過濾：形成 FVG 的中間 K 實體須夠大（濾盤整雜訊）
            mid = n - 1 - int(f.get("ago_bars") or 0)
            if 0 <= mid < n and _atr14 > 0:
                body = abs(candles[mid]["close"] - candles[mid]["open"])
                if body < 0.45 * _atr14:
                    continue   # 位移不足，視為雜訊 FVG
            if f.get("mitigated"):
                # IFVG：被填補且現價在反側 → 反轉成對向 S/R
                if (is_bull and cur_px < f["bottom"]) or ((not is_bull) and cur_px > f["top"]):
                    _ifvg.append((f, is_bull))
            else:
                _fresh.append((f, is_bull))
        for f, is_bull in _fresh[:3]:
            x0 = max(0, n - 1 - int(f.get("ago_bars") or 0))
            color = UP if is_bull else DOWN
            ce = (f["top"] + f["bottom"]) / 2
            ax.add_patch(Rectangle((x0, f["bottom"]), n - x0,
                                   f["top"] - f["bottom"],
                                   facecolor=color, alpha=0.12, edgecolor="none", zorder=1))
            ax.plot([x0, n], [ce, ce], color=color, linewidth=0.7,
                    linestyle=(0, (2, 3)), alpha=0.7, zorder=2)
            ax.text(n - 0.5, ce, "FVG·CE", color=color, fontsize=6.8, va="center",
                    ha="right", alpha=0.9, zorder=4,
                    bbox=dict(boxstyle="round,pad=0.12", fc=BG, ec=color, lw=0.4, alpha=0.7))
        # IFVG（反向 FVG）：原 bull FVG 被收破 → 變阻力(紅)；反之變支撐(綠)
        for f, is_bull in _ifvg[:2]:
            x0 = max(0, n - 1 - int(f.get("ago_bars") or 0))
            color = DOWN if is_bull else UP   # 反轉
            ax.add_patch(Rectangle((x0, f["bottom"]), n - x0,
                                   f["top"] - f["bottom"],
                                   facecolor=color, alpha=0.08, edgecolor=color,
                                   linewidth=0.6, linestyle=":", zorder=1))
            ax.text(n - 0.5, (f["top"] + f["bottom"]) / 2, "IFVG", color=color,
                    fontsize=6.8, va="center", ha="right", alpha=0.9, zorder=4,
                    bbox=dict(boxstyle="round,pad=0.12", fc=BG, ec=color, lw=0.4, alpha=0.7))

        # === Order Blocks（v33：只留未緩解最近 3 個；demand=綠 supply=紅）===
        _obs = [o for o in (smc.get("order_blocks") or []) if not o.get("mitigated")][:3]
        for ob in _obs:
            x0 = max(0, n - 1 - int(ob.get("ago_bars") or 0))
            is_bull = (ob.get("type") or "").lower().startswith("bull")
            color = UP if is_bull else DOWN
            lbl = "需求OB" if is_bull else "供給OB"
            ax.add_patch(Rectangle((x0, ob["bottom"]), n - x0,
                                   ob["top"] - ob["bottom"],
                                   facecolor=color, alpha=0.06, edgecolor=color,
                                   linewidth=1.0, linestyle="--", zorder=2))
            ax.text(x0 + 0.3, ob["top"], lbl, color=color, fontsize=6.8,
                    va="bottom", alpha=0.95, zorder=4,
                    bbox=dict(boxstyle="round,pad=0.12", fc=BG, ec=color, lw=0.4, alpha=0.7))

        # === BoS / CHoCH（v33：自寫偵測；CHoCH 實線=結構反轉、BOS 點線=趨勢延續）===
        for b in detect_structure_breaks(candles, smc.get("swing_points") or [])[-3:]:
            x0 = b["idx"]
            color = UP if b["direction"] == "bull" else DOWN
            is_choch = b["type"] == "CHoCH"
            ax.plot([x0, min(x0 + 12, n - 1)], [b["level"], b["level"]],
                    color=color, linewidth=1.2,
                    linestyle="-" if is_choch else (0, (1, 2)),
                    alpha=0.9, zorder=2)
            ax.text(x0, b["level"], b["type"], color=color,
                    fontsize=7, va="bottom",
                    fontweight="bold" if is_choch else "normal", alpha=0.95, zorder=4)

        # === Swing 高低點（v28: 標 HH/HL/LH/LL；v33: 加結構鋸齒連線一眼看懂市場結構）===
        swings = sorted((smc.get("swing_points") or []),
                        key=lambda s: -(int(s.get("ago_bars") or 0)))  # 由舊到新
        prev_high = prev_low = None
        _zig = []   # (x, level) 由舊到新，畫結構連線
        for s in swings[:14]:
            x0 = max(0, n - 1 - int(s.get("ago_bars") or 0))
            is_high = s.get("type") == "high"
            lvl = s["level"]
            _zig.append((x0, lvl))
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
        # 結構鋸齒線（淡，連起 swing 序列，讓 HH/HL/LH/LL 的市場結構一目了然）
        if len(_zig) >= 2:
            _zig.sort(key=lambda p: p[0])
            ax.plot([p[0] for p in _zig], [p[1] for p in _zig],
                    color=FG, linewidth=0.8, alpha=0.35, linestyle="-", zorder=2)

        # === 流動性掃單標記（v33：被掃的 swing 極值 ▲▼，與 Wyckoff Spring/UTAD 互證）===
        for sw in _detect_sweeps(candles, swings, n):
            if sw["dir"] == "down":   # 上方流動性被掃（假突破/UTAD）
                ax.annotate("▼掃", (sw["x"], sw["level"]), color=SWEEP, fontsize=8,
                            ha="center", va="bottom", fontweight="bold", zorder=6)
            else:                      # 下方流動性被掃（Spring）
                ax.annotate("▲掃", (sw["x"], sw["level"]), color=SWEEP, fontsize=8,
                            ha="center", va="top", fontweight="bold", zorder=6)

        # === Wyckoff 階段（v33：TR 箱體 + Spring/UTAD/SOS/SOW 事件 + 角落敘事）===
        wy = overlays.get("wyckoff") or {}
        if wy.get("box_hi") and wy.get("box_lo"):
            ax.axhspan(wy["box_lo"], wy["box_hi"], color="#8e99ab", alpha=0.06, zorder=0)
            for edge in (wy["box_hi"], wy["box_lo"]):
                ax.plot([0, n], [edge, edge], color="#8e99ab", linewidth=0.6,
                        linestyle=(0, (5, 5)), alpha=0.45, zorder=1)
            _evmark = {"Spring": ("▲", UP), "UTAD": ("▼", DOWN),
                       "SOS": ("⬆SOS", UP), "SOW": ("⬇SOW", DOWN)}
            for ev in wy.get("events", []):
                x0 = max(0, n - 1 - int(ev.get("ago_bars") or 0))
                mk, col = _evmark.get(ev["type"], ("•", FG))
                va = "top" if ev["type"] in ("UTAD", "SOW") else "bottom"
                ax.annotate(mk, (x0, ev["level"]), color=col, fontsize=7.5,
                            ha="center", va=va, alpha=0.9, zorder=6)
            if wy.get("narrative"):
                ax.text(0.01, 0.02, f"Wyckoff：{wy['narrative']}　〔{wy.get('caveat','')}〕",
                        transform=ax.transAxes, color="#c9b6e0", fontsize=7.2,
                        va="bottom", ha="left", zorder=6,
                        bbox=dict(boxstyle="round,pad=0.3", fc="#1a1f2e",
                                  ec=BREAKER, lw=0.5, alpha=0.8))

        # === SNR 支撐壓力（v33：畫『區帶』axhspan + 觸及次數，只留密集區）===
        snr = _compute_snr(candles)
        _rng = (max(c["high"] for c in candles) - min(c["low"] for c in candles)) or 1
        _band = _rng * 0.004   # 區帶最小半高
        for price, cnt, lo, hi in snr["resistance"]:
            lo2, hi2 = min(lo, price - _band), max(hi, price + _band)
            ax.axhspan(lo2, hi2, color=SNR_R, alpha=0.10, zorder=1)
            ax.axhline(price, color=SNR_R, linewidth=0.7, linestyle=(0, (6, 4)),
                       alpha=0.5, zorder=1)
            ax.text(n + 0.5, price, f"壓力 {price:,.6g}（×{cnt}）", color=SNR_R,
                    fontsize=7, va="center", ha="left", alpha=0.9, zorder=5)
        for price, cnt, lo, hi in snr["support"]:
            lo2, hi2 = min(lo, price - _band), max(hi, price + _band)
            ax.axhspan(lo2, hi2, color=SNR_S, alpha=0.10, zorder=1)
            ax.axhline(price, color=SNR_S, linewidth=0.7, linestyle=(0, (6, 4)),
                       alpha=0.5, zorder=1)
            ax.text(n + 0.5, price, f"支撐 {price:,.6g}（×{cnt}）", color=SNR_S,
                    fontsize=7, va="center", ha="left", alpha=0.9, zorder=5)

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

        # === 資金費率面板（v33：序列 + 0 軸 + 過熱/過冷帶）===
        if has_fund and "funding" in axes:
            axf = axes["funding"]
            fs = overlays["funding_series"][-n:]
            fx = range(n - len(fs), n)
            fpct = [v * 100 for v in fs]   # 轉 %
            axf.axhspan(0.05, max(max(fpct), 0.06), color=DOWN, alpha=0.06)
            axf.axhspan(min(min(fpct), -0.06), -0.05, color=UP, alpha=0.06)
            axf.plot(list(fx), fpct, color=FUND_C, linewidth=1.2, zorder=3)
            axf.axhline(0, color=FG, linewidth=0.4, alpha=0.3)
            last = fpct[-1] if fpct else 0
            tone = "過熱偏多（軋空風險）" if last > 0.05 else "偏空（空頭擁擠）" if last < -0.05 else "中性"
            axf.set_ylabel("資金費率%", color=FG, fontsize=8)
            axf.text(0.01, 0.9, f"資金費率 {last:+.4f}%/8h（{tone}）",
                     transform=axf.transAxes, color=FUND_C, fontsize=8, va="top", ha="left")

        # === 清算面板（v33：多頭清算紅、空頭清算綠，正負雙向 bar）===
        if has_liq and "liq" in axes:
            axl = axes["liq"]
            ll = overlays["liq_long_series"][-n:]
            sl = overlays["liq_short_series"][-n:]
            lx = list(range(n - len(ll), n))
            # 多頭被清算 = 下殺燃料（紅，畫正向）；空頭被清算 = 軋空燃料（綠，畫負向）
            axl.bar(lx, ll, color=DOWN, alpha=0.7, width=0.8, zorder=3, label="多頭清算")
            axl.bar(lx, [-s for s in sl], color=UP, alpha=0.7, width=0.8, zorder=3, label="空頭清算")
            axl.axhline(0, color=FG, linewidth=0.4, alpha=0.3)
            axl.set_ylabel("清算量", color=FG, fontsize=8)
            l24 = overlays.get("liq_24h") or {}
            if l24:
                axl.text(0.01, 0.9, f"近24h 多 {l24.get('long',0)/1e6:.2f}M／空 {l24.get('short',0)/1e6:.2f}M USD",
                         transform=axl.transAxes, color=FG, fontsize=8, va="top", ha="left")
            axl.legend(loc="upper right", fontsize=6.5, facecolor=BG, edgecolor=GRID,
                       labelcolor=FG, ncol=2)

        # === 多空比面板（v33：大戶帳戶多空比序列 + 均衡線 1.0）===
        if has_ls and "ls" in axes:
            axls = axes["ls"]
            lsv = overlays["ls_series"][-n:]
            lsx = range(n - len(lsv), n)
            axls.plot(list(lsx), lsv, color=LSCOL, linewidth=1.3, zorder=3)
            axls.axhline(1.0, color=FG, linewidth=0.5, linestyle=(0, (4, 4)), alpha=0.4)
            axls.fill_between(list(lsx), lsv, 1.0,
                              where=[v >= 1.0 for v in lsv], color=UP, alpha=0.10)
            axls.fill_between(list(lsx), lsv, 1.0,
                              where=[v < 1.0 for v in lsv], color=DOWN, alpha=0.10)
            last = lsv[-1] if lsv else 1.0
            tone = "大戶偏多" if last > 1.05 else "大戶偏空" if last < 0.95 else "均衡"
            axls.set_ylabel("多空比", color=FG, fontsize=8)
            axls.text(0.01, 0.9, f"大戶帳戶多空比 {last:.2f}（{tone}）",
                      transform=axls.transAxes, color=LSCOL, fontsize=8, va="top", ha="left")

        # === 結構評分卡 + 基差 + 情緒（v33：CoinGlass 佐證，標在價格面板右上）===
        _sc = _structure_scorecard_lines(overlays)
        if _sc:
            ax.text(0.985, 0.97, "\n".join(_sc), transform=ax.transAxes,
                    color=FG, fontsize=7.3, va="top", ha="right", zorder=6,
                    bbox=dict(boxstyle="round,pad=0.4", fc="#1a1f2e", ec=GRID, lw=0.6, alpha=0.85))

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
    """v33: 抓 CoinGlass 全指標 — 可畫序列（CVD/OI/資金費率/清算/多空比）
    + 純量佐證（期現基差/結構評分卡/市場情緒/24h清算）。
    任何失敗都回部分結果，絕不阻斷繪圖。"""
    out: dict = {"cvd": None, "oi": None, "funding": None, "ls_ratio": None,
                 "ls_series": None, "oi_delta_24h": None, "cvd_slope": None,
                 # v33 新增
                 "funding_series": None, "liq_long_series": None,
                 "liq_short_series": None, "basis": None, "structure": None,
                 "sentiment": None, "liq_24h": None}
    try:
        from market_intel_mcp.sources import get_source
        src = get_source()
        import asyncio as _aio

        async def _safe(coro):
            try:
                return await coro
            except Exception:
                return None
        # 多空比用大戶帳戶比（top_trader_account）；"global" 非有效 ratio_type
        (cvd, oi, fund, pos, fser, lser,
         basis, struct, senti) = await _aio.gather(
            _safe(src.get_cvd_series(symbol, tf, n)),
            _safe(src.get_oi(symbol, tf, n)),
            _safe(src.get_funding(symbol)),
            _safe(src.get_positioning(symbol, "top_trader_account", tf, n)),
            _safe(src.get_funding_series(symbol, tf, n)),
            _safe(src.get_liquidation_series(symbol, tf, n)),
            _safe(src.get_spot_futures_basis(symbol)),
            _safe(src.get_structure(symbol)),
            _safe(src.get_sentiment()),
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
            pser = pos.get("series") or []
            out["ls_ratio"] = pos.get("ratio") or pos.get("latest") or (
                pser[-1]["value"] if pser else None)
            out["ls_series"] = [s["value"] for s in pser][-n:] if pser else None
        if fser and not fser.get("error") and fser.get("series"):
            out["funding_series"] = [s["value"] for s in fser["series"]][-n:]
        if lser and not lser.get("error") and lser.get("series"):
            ls_ = lser["series"][-n:]
            out["liq_long_series"] = [s["long_usd"] for s in ls_]
            out["liq_short_series"] = [s["short_usd"] for s in ls_]
            out["liq_24h"] = {
                "long": sum(s["long_usd"] for s in ls_[-6:]),   # 4h×6≈24h
                "short": sum(s["short_usd"] for s in ls_[-6:])}
        if basis and not basis.get("error"):
            out["basis"] = {"pct": basis.get("basis_pct"),
                            "interp": basis.get("interpretation")}
        if struct and not struct.get("error"):
            out["structure"] = {k: struct.get(k) for k in (
                "atr_pct_7d", "vol_24h_vs_30d", "cvd_slope_7d",
                "top_trader_slope_7d", "oi_delta_7d_pct",
                "higher_lows_7d", "above_4h_200ma")}
        if senti and not senti.get("error"):
            out["sentiment"] = {
                "fg": senti.get("fear_greed_now"),
                "fg_label": senti.get("fear_greed_label"),
                "ahr999": senti.get("ahr999_now"),
                "ahr999_label": senti.get("ahr999_label")}
    except Exception as e:
        print(f"[chart] coinglass overlay error: {type(e).__name__}: {e}")
    return out


async def render_symbol_chart(symbol: str, tf: str = "4h", bars: int = 120,
                              plan: dict | None = None,
                              overlays: dict | None = None) -> Path | None:
    """抓數據 + 算 SMC + CoinGlass 疊加 + 渲染。失敗回 None（絕不阻塞呼叫端）。
    v33：overlays 可由呼叫端傳入（deepdive 已抓的同一份 CoinGlass），避免圖文數據打架。"""
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
        # v33: 優先用呼叫端傳入的 overlays（與文章同源）；無才自行抓
        if overlays is None:
            overlays = await _fetch_coinglass_overlays(symbol, tf, len(candles))
        else:
            overlays = dict(overlays)
        # v33: Wyckoff heuristic（呼叫端已附就沿用，確保圖文一致；否則自算）
        if not overlays.get("wyckoff"):
            try:
                from market_intel_mcp.wyckoff import classify_wyckoff
                overlays["wyckoff"] = classify_wyckoff(
                    candles, cvd_slope=overlays.get("cvd_slope"),
                    oi_delta_pct=overlays.get("oi_delta_24h"))
            except Exception:
                pass
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
