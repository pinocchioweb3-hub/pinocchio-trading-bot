# -*- coding: utf-8 -*-
"""cycle_regime.py — 「熊底→牛頂」週期/部位層(shadow) step1：純函式週期狀態分類。

定位（研究 cycle-position-layer-research 對抗審查定案，務必遵守）：
    本層只辨識「機率較高的深度價值累積區間」，**不是底點承諾**。加密只有 ~3-4 個完整週期、
    且彼此非獨立（同一條 BTC 序列＋同一全球流動性週期）→ 結構上**永遠無法達統計顯著**。
    故此層永遠 **shadow / display-only**、與開單數學嚴格隔離、每次輸出都帶誠實免責。

🟢 可建（本檔做的）：Mayer Multiple + 200週均線 + 距 ATH 回撤 +（可選）MVRV Z-score 的「合流」，
    歸納成『價值區間 + 週期階段』的啟發式描述。
🔴 永不（本檔絕不做）：Pi Cycle（過擬合）、宣稱「已證實 edge」、投射任何具體倍數、宣稱「抓到底」。

純函式、零 IO、零網路、不 import strength/evaluate/fire——可離線單元測試。
"""
from __future__ import annotations

from typing import Optional

# 固定免責（紅線③）：任何消費端渲染都應附上，避免被讀成底點/必漲承諾。
DISCLAIMER = ("週期樣本 n≈3-4 且非獨立，結構上無統計顯著性；此為『機率較高的價值區間』"
              "啟發式描述，非底點承諾、非投資建議。歷史相似≠未來重演；分批累積非梭哈。")


def mayer_multiple(price: float, ma200d: float) -> Optional[float]:
    """Mayer Multiple = 現價 / 200日均線。深底常見 <0.8、便宜 <0.6、過熱 >2.4。"""
    if not price or not ma200d or ma200d <= 0:
        return None
    return round(price / ma200d, 3)


def dist_from_200wma_pct(price: float, ma200w: float) -> Optional[float]:
    """現價相對 200 週均線的 %。<=0 ＝ 在歷史底部支撐帶之下/之上一點。"""
    if not price or not ma200w or ma200w <= 0:
        return None
    return round((price / ma200w - 1) * 100, 1)


def drawdown_from_ath_pct(price: float, ath: float) -> Optional[float]:
    """距歷史高點回撤 %（負值）。週期底歷史常見 -75~85%（但逐輪變淺、且會被破）。"""
    if not price or not ath or ath <= 0:
        return None
    return round((price / ath - 1) * 100, 1)


def classify_cycle_phase(price: float, ma200d: float, ma200w: float, ath: float,
                         mvrv_z: Optional[float] = None) -> dict:
    """把週期指標合流成啟發式『價值區間 + 階段』描述（非統計 edge、非底點）。

    回 dict：mayer / dist_200wma_pct / drawdown_pct / mvrv_z / value_zone / phase /
            confluence_n（價值訊號合流數，0-4）/ label（人話）/ disclaimer。
    任何缺料→該欄 None，永不臆測（紅線③）。"""
    mayer = mayer_multiple(price, ma200d)
    dist_w = dist_from_200wma_pct(price, ma200w)
    dd = drawdown_from_ath_pct(price, ath)

    # 合流計數：幾個「深度價值」訊號同時成立（越多＝越接近歷史累積帶，但仍非底點）
    signals = []
    if mayer is not None and mayer < 0.8:
        signals.append("mayer<0.8")
    if dist_w is not None and dist_w <= 5:           # 貼著或低於 200 週線（±5% 內算貼）
        signals.append("at/below_200wma")
    if dd is not None and dd <= -60:                 # 已回撤 ≥60%
        signals.append("drawdown<=-60%")
    if mvrv_z is not None and mvrv_z < 0.5:          # 鏈上低估（有才算）
        signals.append("mvrv_z<0.5")
    confluence_n = len(signals)

    # value_zone：以 Mayer 為主軸 + 合流佐證（啟發式、BTC 校準）
    if (mayer is not None and mayer < 0.6) or confluence_n >= 3:
        value_zone = "deep_value"      # 深度價值（歷史機率較高的累積帶）
    elif (mayer is not None and mayer < 0.8) or confluence_n >= 2:
        value_zone = "value"           # 偏便宜
    elif mayer is not None and mayer > 3.0:
        value_zone = "euphoria"        # 過熱/亢奮（分批減碼帶）
    elif mayer is not None and mayer > 2.4:
        value_zone = "elevated"        # 偏貴
    else:
        value_zone = "neutral"

    # phase：粗略 Wyckoff 映射（價 vs 200週線 + Mayer），純描述
    if price and ma200w and price < ma200w:
        phase = "markdown/accumulation"   # 在底部帶下方：下跌末段或吸籌
    elif mayer is not None and mayer < 1.0:
        phase = "early_markup"            # 站回均線上方、仍便宜：復甦初段
    elif mayer is not None and mayer > 2.4:
        phase = "distribution"            # 偏貴：派發帶
    else:
        phase = "markup"                  # 上行段

    zone_zh = {"deep_value": "深度價值帶", "value": "偏便宜", "neutral": "中性",
               "elevated": "偏貴", "euphoria": "過熱/亢奮"}[value_zone]
    phase_zh = {"markdown/accumulation": "下跌末段/吸籌帶", "early_markup": "復甦初段",
                "markup": "上行段", "distribution": "派發帶"}[phase]
    label = f"{zone_zh}・{phase_zh}（合流 {confluence_n}/4）"

    return {
        "mayer": mayer, "dist_200wma_pct": dist_w, "drawdown_pct": dd, "mvrv_z": mvrv_z,
        "value_zone": value_zone, "phase": phase, "confluence_n": confluence_n,
        "signals": signals, "label": label, "disclaimer": DISCLAIMER,
    }


def render_cycle_line(symbol: str, c: dict) -> str:
    """把 classify 結果渲染成一行人話（給 Telegram 週期主題用，純顯示）。"""
    m = c.get("mayer")
    dw = c.get("dist_200wma_pct")
    dd = c.get("drawdown_pct")
    parts = [f"<b>{symbol}</b>：{c['label']}"]
    sub = []
    if m is not None:
        sub.append(f"Mayer {m}")
    if dw is not None:
        sub.append(f"200週線 {dw:+.0f}%")
    if dd is not None:
        sub.append(f"距ATH {dd:+.0f}%")
    if c.get("mvrv_z") is not None:
        sub.append(f"MVRV-Z {c['mvrv_z']}")
    if sub:
        parts.append("　" + " ｜ ".join(sub))
    return "\n".join(parts)


if __name__ == "__main__":  # 自測（純函式、無 IO）
    def chk(cond, msg):
        print(("✓ " if cond else "✗ ") + msg)
        assert cond, msg

    # 深底：Mayer 0.5 + 貼200週線下方 + 回撤80% → deep_value
    deep = classify_cycle_phase(price=16000, ma200d=32000, ma200w=18000, ath=69000)
    chk(deep["mayer"] == 0.5, f"mayer={deep['mayer']}")
    chk(deep["value_zone"] == "deep_value", f"deep value_zone={deep['value_zone']}")
    chk(deep["phase"] == "markdown/accumulation", f"phase={deep['phase']}")
    chk(deep["confluence_n"] >= 3, f"confluence={deep['confluence_n']}")

    # 過熱：Mayer 3.2 → euphoria/distribution
    hot = classify_cycle_phase(price=100000, ma200d=31000, ma200w=45000, ath=100000)
    chk(hot["value_zone"] == "euphoria", f"hot zone={hot['value_zone']}")
    chk(hot["phase"] == "distribution", f"hot phase={hot['phase']}")

    # 缺料：ma200w=0 → dist None，不臆測
    miss = classify_cycle_phase(price=60000, ma200d=70000, ma200w=0, ath=126000)
    chk(miss["dist_200wma_pct"] is None, "缺 200週線→None")
    chk(miss["mayer"] is not None, "mayer 仍可算")

    # MVRV 加入合流
    withz = classify_cycle_phase(price=60000, ma200d=76000, ma200w=61000, ath=126000, mvrv_z=0.3)
    chk("mvrv_z<0.5" in withz["signals"], "mvrv_z 進合流")

    # 免責一定在
    chk(DISCLAIMER in deep["disclaimer"], "免責存在")
    print("--- cycle_regime 自測全過 ---")
