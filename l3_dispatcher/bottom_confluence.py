# -*- coding: utf-8 -*-
"""bottom_confluence.py — 熊底合流分數（純函式層，v111）。

研究定案（workflow wynjdvcp3 + 對抗審查）落地。雙層嚴格分離：
    核心分數（0-100）＝只收「在 ≥2 個歷史底部有可查行為」的因子；
    現代疊加（overlay）＝ETF 流(n=0)/資金費率(n=1)/穩定幣動能(落後因子)——只顯示永不計分。

桶結構（總權重 75，B 桶總經依對抗審查降為純背景註記、零權重）：
    A 鏈上估值 40：MVRV-Z(20) + 價<已實現價(10) + Mayer/200WMA 合併(10)　※三者共線、桶內自然封頂
    C 市場結構 20：dominance 90日方向上升(10) + 深度 Bitcoin season(10)
    D 情緒 15：F&G 30 日均 ≤25(半)/≤15(全)
分數 = Σ(得分)/present_mass×100 —— 只除以「當日有資料的權重和」（鏡像 macro_confluence
v74 重正規化教訓）；present_mass 比率 <60% → 不出 band 結論。

⛔ 鐵則：純函式零 IO；不 import strength/fire/下單；display-only 永不進開單數學（紅線①③）；
   每次輸出必帶 DISCLAIMERS 全文（含對抗審查補的「指標選擇偏差」）。
"""
from __future__ import annotations

from typing import Optional

# 強制誠實條款（對抗審查定案六條——渲染端不可省略）
DISCLAIMERS = [
    "週期樣本 n=3-4 且同一條 BTC 序列非獨立——此分數是啟發式定位，結構上永無統計顯著，非機率陳述。",
    "分數標的是『區間』非『底點』：2022-06 本分數估算已達高檔，但距真底(2022-11)還有 5 個月、再跌 ~35%——這正是分批而非一次的理由。",
    "指標選擇偏差：MVRV/200週線等指標正因在過去三個底 in-sample 有效才廣為人知（失敗指標已被遺忘），拿同三個底『驗證』倖存指標＝套套邏輯。",
    "閾值逐輪劣化：200週線下方時長 0→0→6 個月、dominance 底部水位 85%→52%→38%——固定閾值前瞻會失效。",
    "核心分數只適用 BTC(至多 ETH)：alt 可以在 BTC 築底時繼續 -90%，死亡/下市的 alt 不在任何「alt 後見底」樣本裡。",
    "不投射報酬：分數高≠會漲、不附任何歷史倍數；分批框架為呈現參考，執行永遠是使用者本人（紅線①）。",
]

# 桶權重（B 總經=0：DXY n=1、SPX n=2，對抗審查裁定僅背景註記不入分）
_W_MVRV_Z = 20.0
_W_REALIZED = 10.0
_W_MAYER_WMA = 10.0
_W_DOMINANCE = 10.0
_W_ALTSEASON = 10.0
_W_FNG = 15.0
TOTAL_MASS = _W_MVRV_Z + _W_REALIZED + _W_MAYER_WMA + _W_DOMINANCE + _W_ALTSEASON + _W_FNG  # 75


def _state_mvrv_z(z: Optional[float]) -> Optional[float]:
    """MVRV Z-Score：<0.5 半分、<0 全分（歷史四個底皆觸 0 下方）。"""
    if z is None:
        return None
    if z < 0.0:
        return 1.0
    if z < 0.5:
        return 0.5
    return 0.0


def _state_below_realized(price: Optional[float], realized: Optional[float]) -> Optional[float]:
    """價格低於已實現價（全體持幣人平均浮虧）＝1，否則 0。"""
    if price is None or realized is None or realized <= 0:
        return None
    return 1.0 if price < realized else 0.0


def _state_mayer_wma(mayer: Optional[float], dist_200wma_pct: Optional[float]) -> Optional[float]:
    """Mayer<0.8 半、<0.6 全；或貼/破 200 週線（±5% 內）亦計半——兩者取高（共線合併為一票）。"""
    s = None
    if mayer is not None:
        s = 1.0 if mayer < 0.6 else (0.5 if mayer < 0.8 else 0.0)
    if dist_200wma_pct is not None:
        s_w = 0.5 if dist_200wma_pct <= 5 else 0.0
        s = s_w if s is None else max(s, s_w)
    return s


def _state_dominance_rising(dir_90d: Optional[bool]) -> Optional[float]:
    """BTC dominance 90 日方向上升（熊尾資金縮回 BTC）＝1。水位跨輪不可比故只看方向。"""
    if dir_90d is None:
        return None
    return 1.0 if dir_90d else 0.0


def _state_altseason(idx: Optional[float]) -> Optional[float]:
    """山寨季指數 <25＝深度 Bitcoin season（熊底常態）＝1。"""
    if idx is None:
        return None
    return 1.0 if idx < 25 else 0.0


def _state_fng(avg30: Optional[float]) -> Optional[float]:
    """恐懼貪婪 30 日均 ≤15 全分、≤25 半分（單日尖刺不觸發故用月均）。"""
    if avg30 is None:
        return None
    if avg30 <= 15:
        return 1.0
    if avg30 <= 25:
        return 0.5
    return 0.0


def compute_bottom_score(inputs: dict) -> dict:
    """核心合流分數。inputs 鍵（缺料→None，永不臆測）：
        mvrv_z, price, realized_price, mayer, dist_200wma_pct,
        dominance_dir_90d(bool), altseason_idx, fng_avg30
    回：score(0-100|None), band, present_mass_pct, factor_states, earned, disclaimers。"""
    factors = [
        ("mvrv_z", _W_MVRV_Z, _state_mvrv_z(inputs.get("mvrv_z"))),
        ("below_realized", _W_REALIZED,
         _state_below_realized(inputs.get("price"), inputs.get("realized_price"))),
        ("mayer_200wma", _W_MAYER_WMA,
         _state_mayer_wma(inputs.get("mayer"), inputs.get("dist_200wma_pct"))),
        ("dominance_rising", _W_DOMINANCE,
         _state_dominance_rising(inputs.get("dominance_dir_90d"))),
        ("deep_btc_season", _W_ALTSEASON, _state_altseason(inputs.get("altseason_idx"))),
        ("fng_avg30", _W_FNG, _state_fng(inputs.get("fng_avg30"))),
    ]
    earned = 0.0
    mass = 0.0
    states: dict[str, Optional[float]] = {}
    for name, w, s in factors:
        states[name] = s
        if s is not None:
            mass += w
            earned += w * s
    present_pct = round(mass / TOTAL_MASS * 100, 1)
    if mass <= 0:
        return {"score": None, "band": None, "present_mass_pct": 0.0,
                "factor_states": states, "earned": 0.0, "disclaimers": DISCLAIMERS,
                "note": "全部因子缺料，無法計分（誠實不臆測）"}
    score = round(earned / mass * 100, 1)
    if present_pct < 60:
        band = None      # 資料不足：给分數但不給 band 結論
        note = "⚠️ 資料不足(present_mass<60%)，今日分數不可與歷史比較、不出區間結論"
    else:
        note = ""
        if score < 40:
            band = "非累積區"
        elif score < 60:
            band = "邊緣觀察"
        elif score < 80:
            band = "深度價值帶（歷史上的分批累積區間）"
        else:
            band = "歷史極值合流（三個歷史底部的同期水準）"
    return {"score": score, "band": band, "present_mass_pct": present_pct,
            "factor_states": states, "earned": round(earned, 1),
            "disclaimers": DISCLAIMERS, "note": note}


def render_dashboard_block(res: dict, background: Optional[dict] = None,
                           overlay: Optional[dict] = None) -> str:
    """渲染儀表板區塊（給 🌊 卡頂部）。background=DXY/SPX 背景註記；overlay=落後/無校準因子。"""
    L: list[str] = ["🧭 <b>熊底合流儀表板</b>（BTC・核心分數只收有歷史校準的因子）"]
    if res.get("score") is None:
        L.append("　" + (res.get("note") or "資料缺料，今日無法計分"))
        return "\n".join(L)
    band = res.get("band")
    L.append(f"　<b>核心分數 {res['score']}/100</b>"
             + (f"　→ {band}" if band else "")
             + f"　<i>(資料覆蓋 {res['present_mass_pct']}%)</i>")
    if res.get("note"):
        L.append("　" + res["note"])
    zh = {"mvrv_z": "MVRV-Z", "below_realized": "價<已實現價", "mayer_200wma": "Mayer/200週線",
          "dominance_rising": "Dominance升", "deep_btc_season": "深度BTC季", "fng_avg30": "恐貪30日均"}
    on = [zh[k] for k, v in (res.get("factor_states") or {}).items() if v == 1.0]
    half = [zh[k] for k, v in (res.get("factor_states") or {}).items() if v == 0.5]
    if on:
        L.append("　🟢 全亮：" + "、".join(on))
    if half:
        L.append("　🟡 半亮：" + "、".join(half))
    if background:
        bg = [f"{k} {v}" for k, v in background.items() if v]
        if bg:
            L.append("　🌫 總經背景（n≤2 不計分）：" + " ｜ ".join(bg))
    if overlay:
        ov = [f"{k} {v}" for k, v in overlay.items() if v]
        if ov:
            L.append("　🔭 現代疊加（無歷史校準/落後，僅參考不計分）：" + " ｜ ".join(ov))
    L.append("　⚠️ <i>區間非底點；n=3-4 非獨立；指標選擇偏差；分批非梭哈（詳免責）</i>")
    return "\n".join(L)


if __name__ == "__main__":   # 自測
    def chk(c, m):
        print(("✓ " if c else "✗ ") + m)
        assert c, m

    # 2022-11 型深底：MVRV-Z<0 + 價<已實現 + Mayer 0.7 + 破200週線 + dominance升 + F&G 20
    deep = compute_bottom_score({"mvrv_z": -0.2, "price": 16000, "realized_price": 19800,
                                 "mayer": 0.7, "dist_200wma_pct": -12,
                                 "dominance_dir_90d": True, "altseason_idx": 18, "fng_avg30": 18})
    chk(deep["score"] is not None and deep["score"] >= 80, f"深底分數 {deep['score']} 應≥80")
    # 2021-04 牛頂負對照：必須 <15
    top = compute_bottom_score({"mvrv_z": 6.5, "price": 60000, "realized_price": 20000,
                                "mayer": 1.5, "dist_200wma_pct": 250,
                                "dominance_dir_90d": False, "altseason_idx": 80, "fng_avg30": 75})
    chk(top["score"] is not None and top["score"] < 15, f"牛頂負對照 {top['score']} 應<15")
    print("--- bottom_confluence 自測全過 ---")
