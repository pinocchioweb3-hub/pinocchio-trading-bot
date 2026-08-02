"""SMC 圖表標記渲染（v18-F）：K 線圖上畫 FVG/OB/BoS/Swing + 交易計畫線 → PNG。

使用者理想 2(a) 的實現：「AI 直接在圖表上標記 SMC 結構」。
數據：OkxCandlesSource 4h×120 根 + smc_levels.compute_smc_levels（現成）。
輸出：%LOCALAPPDATA%/TradingBot/charts/{sym}_{ts}.png（保留最近 50 張）。
"""
from __future__ import annotations

import asyncio
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
# v51 Fibonacci 回撤
FIB = "#90a4ae"        # 回撤線（藍灰，與其他疊加層區隔）
FIB_GOLD = "#d4a017"   # 黃金口袋（0.618–0.786 高勝率回撤帶）
FIB_RATIOS = (0.236, 0.382, 0.5, 0.618, 0.786)

# v219：圖上 SMC 分量「這輪沒算出來」與「算過、確認沒有」的判準。
# 判準取自產出端 market_intel_mcp/smc_levels.py::compute_smc_levels()——它成功時
# **一律寫鍵**（沒東西就寫空 list＝答案是「沒有」），失敗才留 `<name>_error`；而
# order_blocks 巢狀在 `if swings is not None:` 底下，上游 swing 一爆就整個鍵不寫、
# 連 *_error 都沒有。⇒ 鍵在＝答案（含空的）；鍵不在＝這輪沒算出來。
#
# ⛔ 只列**圖上真的會畫**的三個分量。premium_discount／ote 圖上不畫；圖上的
#    BoS/CHoCH 與掃單標記是本檔用 swing_points 自算的（detect_structure_breaks／
#    _detect_sweeps），不讀 smc 的 bos_choch／liquidity 鍵——把它們納入判定
#    ＝標了一個圖上看不到的東西，天天誤報。
# ⛔ swing_points 的標籤要連帶點名 BoS/掃單：那兩個圖層是它的下游，swing 一爆
#    三個圖層同時空白，只說「Swing 沒算出來」仍會讓人把空白讀成「沒有結構變化」。
_SMC_CHART_COMPONENTS = [
    ("swing_points", "Swing 結構（連帶 BoS/CHoCH、掃單標記）"),
    ("order_blocks", "Order Block"),
    ("fvg", "FVG"),
]

# v222：佐證框裡「這輪沒算出來」的字樣。純中文＋全形括號，Microsoft JhengHei 有字。
_SC_MISSING = "〔缺料〕"

# CoinGlass get_structure 的 7 個分量（上游逐格填、缺的留 None）。逐格判斷用。
_ST_FIELDS = ("atr_pct_7d", "vol_24h_vs_30d", "cvd_slope_7d",
              "top_trader_slope_7d", "oi_delta_7d_pct",
              "higher_lows_7d", "above_4h_200ma")


def _smc_unknown_note(smc: dict | None) -> str | None:
    """回傳圖上該標的缺料字樣；全部算過（含算出空的）回 None＝圖上保持安靜。"""
    missing = [label for key, label in _SMC_CHART_COMPONENTS
               if not isinstance(smc, dict) or key not in smc]
    if not missing:
        return None
    # ⛔ 字面只用微軟正黑體有的字：U+26A0（⚠）在 Microsoft JhengHei 缺字，
    #    會被 matplotlib 畫成豆腐方塊——警語本身變成亂碼是最糟的下場。
    return ("〔缺料〕這輪沒算出來：" + "、".join(missing) +
            "\n　 圖上沒畫 ≠ 沒有這個結構（本圖此圖層無資料，非「已確認不存在」）")


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


def _fib_levels(candles: list[dict], window: int = 60) -> dict | None:
    """v51：近期主擺盪腿（swing leg）的 Fibonacci 回撤位。
    取近 window 根的絕對高/低，依先後定方向：低在前→上升腿（回撤＝找支撐）、
    高在前→下降腿（回撤＝找阻力）。level(r)=end - r*(end-start)，r∈FIB_RATIOS。
    腿幅不足全圖 1/4 → 回 None（回撤參考意義低、不畫以免雜訊）。"""
    if len(candles) < 12:
        return None
    seg = candles[-min(len(candles), window):]
    off = len(candles) - len(seg)            # seg[0] 在全序列的 index
    hi_i = max(range(len(seg)), key=lambda i: seg[i]["high"])
    lo_i = min(range(len(seg)), key=lambda i: seg[i]["low"])
    hi, lo = seg[hi_i]["high"], seg[lo_i]["low"]
    if hi <= lo:
        return None
    full_rng = (max(c["high"] for c in candles) - min(c["low"] for c in candles))
    if full_rng and (hi - lo) < full_rng * 0.25:
        return None                          # 腿幅 < 全圖 1/4 → 不畫
    up = lo_i < hi_i                          # 低在前 → 上升腿
    start = lo if up else hi                  # 腿起點
    end = hi if up else lo                    # 腿終點（最新極值）
    x_start = off + (lo_i if up else hi_i)
    return {
        "up": up, "start": start, "end": end, "x_start": x_start,
        "levels": [(r, end - r * (end - start)) for r in FIB_RATIOS],
    }


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


def _liq_clusters(candles: list[dict], long_series: list | None,
                  short_series: list | None, bins: int = 12,
                  top: int = 3) -> list[dict]:
    """M1：把近期清算規模沿『價格』分桶，找出清算密集價帶（流動性磁吸參考）。
    用 aggregated-history（非被鎖的熱力圖）→ 屬事後估計分佈，非真實掛單，須標註。
    long_series/short_series 與 candles 同為 4h、皆以「現在」結尾 → 依位置對齊末段。
    回 [{low, high, mid, long_usd, short_usd, total, dominant}]（依 total 由大到小）。"""
    long_series = long_series or []
    short_series = short_series or []
    if not candles or not long_series or not short_series:
        return []
    L = min(len(candles), len(long_series), len(short_series))
    if L < 6:
        return []
    cs = candles[-L:]
    ls = long_series[-L:]
    ss = short_series[-L:]
    prices = [(c["high"] + c["low"] + c["close"]) / 3.0 for c in cs]
    pmin, pmax = min(prices), max(prices)
    if pmax <= pmin:
        return []
    width = (pmax - pmin) / bins
    buckets = [{"long": 0.0, "short": 0.0} for _ in range(bins)]
    for p, lo_usd, sh_usd in zip(prices, ls, ss):
        bi = min(bins - 1, int((p - pmin) / width))
        buckets[bi]["long"] += float(lo_usd or 0)
        buckets[bi]["short"] += float(sh_usd or 0)
    out = []
    for bi, b in enumerate(buckets):
        tot = b["long"] + b["short"]
        if tot <= 0:
            continue
        dom = ("long" if b["long"] > b["short"] * 1.3 else
               "short" if b["short"] > b["long"] * 1.3 else "balanced")
        out.append({
            "low": round(pmin + bi * width, 6),
            "high": round(pmin + (bi + 1) * width, 6),
            "mid": round(pmin + (bi + 0.5) * width, 6),
            "long_usd": round(b["long"], 0),
            "short_usd": round(b["short"], 0),
            "total": round(tot, 0),
            "dominant": dom,
        })
    out.sort(key=lambda x: x["total"], reverse=True)
    return out[:top]


def _ts_sec(ts) -> float | None:
    """ts 正規化成秒（自動判別毫秒 vs 秒）：>1e11 視為毫秒。跨來源比對防單位坑。"""
    try:
        t = float(ts)
    except (TypeError, ValueError):
        return None
    return t / 1000.0 if t > 1e11 else t


def _oi_delta_around(oi_list: list | None, ago_bars: int, w: int = 2,
                     oi_ts: list | None = None, event_ts=None,
                     tf_sec: int = 14400) -> float | None:
    """M4：取結構事件當下的 OI 變化%（事件 bar vs 其前 w 根）。對齊不上回 None。

    OI(CoinGlass 聚合) 與 K 線(OKX 單一所) 是兩條獨立抓取的序列。若提供 oi_ts+event_ts，
    改用『時間戳容差比對』把事件對到正確的 OI 根（容差＝半根時框），對不上→回 None（寧缺勿錯，
    避免任一來源缺根/末端不同步時把 OI 標到相鄰錯誤事件、使真/假突破標反）。
    無 ts 時退回原本的『距末端位置』對齊。"""
    if not oi_list:
        return None
    n = len(oi_list)
    idx_ev = None
    # 優先：ts 對齊（跨來源防錯位）。oi_ts 須與 oi_list 同切片同長度。
    if oi_ts and event_ts is not None and len(oi_ts) == n:
        ev_s = _ts_sec(event_ts)
        if ev_s is not None:
            tol = tf_sec / 2.0
            best = tol
            for j in range(n):
                tj = _ts_sec(oi_ts[j])
                if tj is None:
                    continue
                d = abs(tj - ev_s)
                if d <= best:
                    best = d
                    idx_ev = j
            if idx_ev is None:
                return None   # 事件落在 OI 序列容差外 → 跨來源對不上，寧缺勿錯
    if idx_ev is None:
        # 退回位置對齊（無 ts 可用時）
        idx_ev = n - 1 - int(ago_bars or 0)
    idx_prev = idx_ev - w
    if idx_prev < 0 or idx_ev >= n or idx_ev < 0:
        return None
    try:
        prev = float(oi_list[idx_prev])
        ev = float(oi_list[idx_ev])
    except (TypeError, ValueError):
        return None
    if not prev:
        return None
    return round((ev - prev) / prev * 100, 2)


def _prune_old():
    try:
        CHART_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(CHART_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime)
        for p in files[:-KEEP_CHARTS]:
            p.unlink(missing_ok=True)
    except Exception:
        pass


def _structure_scorecard_lines(overlays: dict) -> list[str]:
    """v33：把 CoinGlass 結構評分卡 + 基差 + 情緒組成圖上佐證框文字。無資料回空。

    v222：分量「這輪沒算出來」不得折成數字 0。上游
    market_intel_mcp/sources/coinglass.py::get_structure 是**逐分量誠實**的——:1194
    先把 7 欄全設 None，再由 4 條獨立子請求（price／oi／positioning／cvd）各自填得
    出來的那幾格；任一條掛掉、或 bars 不足門檻（vols<30 → 量比留 None、tseries<6 →
    大戶斜率留 None、<200 根 4h → above_4h_200ma 留 None）該格就是 None。**部分缺格
    是常態不是邊角。** 舊碼 `(st.get(k) or 0):.2f` 把缺格寫成「量比 0.00」「大戶斜率
    +0.00」——那不是中性佔位符，而是兩個**有意義的讀數**（量塌到零／大戶一動也沒動），
    看圖的人分不出是量出來的還是沒量到。
    ⛔ 邊界：真的算出 0.0 仍照印（0.0 是答案，不是未知的代名詞）；整份 structure 一格
       都沒有則整個框不畫（維持 v183——框不出現不對盤面做任何斷言，而畫一個全是
       〔缺料〕的框＝天天噪音）。
    ⛔ 字面只用 Microsoft JhengHei 有的字：本輪實測 U+2713（✓）在該字型缺字，舊碼
       「墊高低點✓」長年被畫成空心豆腐方塊 ⇒ 讀者根本分不出那個記號是「是」還是壞字。
    """
    lines: list[str] = []
    st = overlays.get("structure") or {}
    sv = {k: st.get(k) for k in _ST_FIELDS}
    if any(v is not None for v in sv.values()):
        lines.append("◆ 結構評分（7d）")
        atr, volr = sv["atr_pct_7d"], sv["vol_24h_vs_30d"]
        if atr is not None or volr is not None:
            lines.append(f"  ATR% {f'{atr:.1f}' if atr is not None else _SC_MISSING}"
                         f"　量比 {f'{volr:.2f}' if volr is not None else _SC_MISSING}")
        cvs, tts = sv["cvd_slope_7d"], sv["top_trader_slope_7d"]
        if cvs is not None or tts is not None:
            lines.append(
                f"  CVD斜率 {f'{cvs:+.2f}' if cvs is not None else _SC_MISSING}"
                f"　大戶斜率 {f'{tts:+.2f}' if tts is not None else _SC_MISSING}")
        oid = sv["oi_delta_7d_pct"]
        if oid is not None:
            lines.append(f"  OI 7d {oid:+.1f}%")
        flags = []
        if sv["higher_lows_7d"] is not None:
            flags.append("已墊高低點" if sv["higher_lows_7d"] else "未墊高低點")
        if sv["above_4h_200ma"] is not None:
            flags.append("已站上4h_200MA" if sv["above_4h_200ma"] else "在4h_200MA下")
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
                # v183：標明時框——圖上是本時框的 Wyckoff 讀數,與卡文的週線段不同口徑,
                # 不標時框會被讀成互相矛盾（BCH 卡活案例）
                ax.text(0.01, 0.02, f"Wyckoff（{tf}）：{wy['narrative']}　〔{wy.get('caveat','')}〕",
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

        # === Fibonacci 回撤（v51：近期主擺盪腿；金色＝黃金口袋 0.618–0.786 高勝率回撤帶）===
        fib = _fib_levels(candles)
        if fib:
            xs = fib["x_start"]
            gp = {round(r, 3): p for r, p in fib["levels"]}
            g_lo, g_hi = sorted((gp[0.618], gp[0.786]))
            ax.axhspan(g_lo, g_hi, color=FIB_GOLD, alpha=0.06, zorder=1)   # 黃金口袋帶
            for r, price in fib["levels"]:
                is_key = r in (0.5, 0.618)
                col = FIB_GOLD if r == 0.618 else FIB
                ax.plot([xs, n], [price, price], color=col,
                        linewidth=1.0 if is_key else 0.6,
                        linestyle=(0, (5, 4)) if is_key else (0, (1, 3)),
                        alpha=0.8 if is_key else 0.5, zorder=1)
                # v183：標籤只標關鍵級且移到最後一根K的右側——原本六個框疊在圖中央
                # 遮住價格行為（2026-08-01 BCH 卡使用者反映「說不出哪裡怪」的主因）
                if r in (0.382, 0.5, 0.618, 0.786):
                    ax.text(n + 0.6, price, f"{r:.3f}·{price:,.6g}", color=col,
                            fontsize=6.4, va="center", ha="left", alpha=0.9, zorder=4)
            _leg = "上升腿回撤·找支撐" if fib["up"] else "下降腿回撤·找阻力"
            ax.text(xs + 0.3, fib["end"], f"Fib {_leg}", color=FIB_GOLD,
                    fontsize=6.8, fontweight="bold",
                    va="bottom" if fib["up"] else "top", ha="left", alpha=0.95, zorder=4)

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
            # v222：斜率沒算出來時不得折成 0——`or 0` 會讓面板判「多空均衡（+0.00）」，
            # 而那正是 SMC 真假突破的核心判讀之一。⚠️ 曲線本身是真資料（series 有值），
            # 所以警語只能否認**斜率判讀**，不可說整個面板沒資料。
            slope = overlays.get("cvd_slope")
            slope_known = slope is not None
            cvd_color = FG if not slope_known else (UP if slope >= 0 else DOWN)
            axc.plot(list(cx), cvd, color=cvd_color, linewidth=1.3, zorder=3)
            axc.fill_between(list(cx), cvd, min(cvd), color=cvd_color, alpha=0.12)
            axc.axhline(0, color=FG, linewidth=0.4, alpha=0.3)
            axc.set_ylabel("CVD", color=FG, fontsize=8)
            if slope_known:
                trend = ("買盤主導 ↑" if slope > 0.5
                         else "賣盤主導 ↓" if slope < -0.5 else "多空均衡")
                note = f"CVD 主動買賣淨力：{trend}（斜率 {slope:+.2f}）"
            else:
                note = ("CVD 主動買賣淨力：" + _SC_MISSING
                        + "斜率這輪沒算出來（曲線為實際 CVD 累計，僅缺判讀）")
            axc.text(0.01, 0.92, note,
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
        # v183：≥2 行（標題+至少一行實資料）才畫——CG 停權後全 None 會剩孤兒標題框
        if _sc and len(_sc) >= 2:
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
        # === v219：SMC 分量缺料標註 ===
        # 圖上三個 SMC 圖層都是「取不到就什麼都不畫」，缺料與「算過、確認沒有」在圖上
        # 長得一模一樣，而看圖的人只會有一種解讀。標題又寫著「SMC＋SNR 結構」＝承諾了
        # 這些圖層，更不該讓空白替它回答。
        # ⛔ 畫在**框線外**（標題下方）而非面板左上：面板內四角已被右上評分卡、左下
        #    Wyckoff、以及浮動的 Fib 腿標籤佔用——實測初版擺左上會蓋掉 Fib 標籤，
        #    那正是 v183 使用者反映「說不出哪裡怪」的同一種傷害（警語蓋掉資訊）。
        _smc_note = _smc_unknown_note(smc)
        ax.set_title(f"{symbol}/USDT 永續・{tf.upper()}・SMC＋SNR 結構{dir_str}"
                     f"（現價 {cur:,.6g}）",
                     color=FG, fontsize=11, pad=26 if _smc_note else None)
        if _smc_note:
            ax.text(0.5, 1.005, _smc_note, transform=ax.transAxes,
                    color="#ffca28", fontsize=7.2, va="bottom", ha="center", zorder=6,
                    bbox=dict(boxstyle="round,pad=0.3", fc="#1a1f2e",
                              ec="#ffca28", lw=0.6, alpha=0.9))
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
                 "sentiment": None, "liq_24h": None,
                 # M5: OI 加權 funding（極端值＝擁擠/反指風險量表，非方向訊號）
                 "funding_oi_weighted": None}
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
         basis, struct, senti, fwt) = await _aio.gather(
            _safe(src.get_cvd_series(symbol, tf, n)),
            _safe(src.get_oi(symbol, tf, n)),
            _safe(src.get_funding(symbol)),
            _safe(src.get_positioning(symbol, "top_trader_account", tf, n)),
            _safe(src.get_funding_series(symbol, tf, n)),
            _safe(src.get_liquidation_series(symbol, tf, n)),
            _safe(src.get_spot_futures_basis(symbol)),
            _safe(src.get_structure(symbol)),
            _safe(src.get_sentiment()),
            _safe(src.get_funding_weighted(symbol, "oi", tf, n)),  # M5
        )
        if cvd and not cvd.get("error") and cvd.get("series"):
            out["cvd"] = [s["value"] for s in cvd["series"]][-n:]
            out["cvd_slope"] = cvd.get("cvd_slope")
        if oi and not oi.get("error") and oi.get("series"):
            out["oi"] = [s["value"] for s in oi["series"]][-n:]
            out["oi_ts"] = [s.get("ts") for s in oi["series"]][-n:]   # M4 ts 對齊用（與 oi 同切片）
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
        if fwt and not fwt.get("error"):   # M5
            out["funding_oi_weighted"] = fwt.get("latest")
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
        # matplotlib 渲染是同步重活——卸到執行緒，別阻塞事件迴圈（v129 watchdog 誤殺同族）
        return await asyncio.to_thread(
            render_smc_chart, symbol, candles, smc, tf=tf, plan=plan, overlays=overlays)
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
