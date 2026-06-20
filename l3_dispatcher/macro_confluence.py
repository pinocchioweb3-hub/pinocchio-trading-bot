"""Session C：綜合宏觀指標合成（影子層，永不影響下單）。

把多個彼此獨立的宏觀分量（funding / OI / 清算 / 巨鯨 / ETF / DXY / 市場廣度
breadth …）用「確定性規則」合成成單一 macro_confluence_score（-100..+100，
+ 偏多/risk-on、- 偏空/risk-off）+ 各分量明細，每小時寫一行 JSONL 到獨立
sink：data_dir()/macro_confluence.jsonl。另以 SQLite 持續累積 OI/CVD/funding
近 500 根快照（補「從今天起往前累積」的未來歷史；補不回過去，誠實標明）。

════════════════════════════════════════════════════════════════════════════
影子鐵則（最高優先，絕不可違反；仿 convergence.py 明文鐵則）：
    1. macro_confluence_score 與其任何分量是 **SHADOW 專用**。
       **永不** 乘進/加進 strength_score、**永不** 寫 snapshot、
       **永不** 進 fire / 進場 / symbol_gate / 任何下單路徑。
       它只進獨立影子 sink（jsonl + macro_history.db）供日後 A/B 回測評估。
    2. 本模組 **不得 import market_intel_mcp.strength**、不得改 strength.py /
       eval_cvd_divergence，不寫 fire_queue / paper / trade 任何帳。
    3. 純讀：零下單路徑（紅線①）；資料蒐集失敗一律中性化（不臆測方向），
       吞例外續跑，絕不拖垮 daemon（外層另有 supervise() 崩潰隔離）。
    4. 不發 Telegram（純背景觀測）；顯示層函式只「回字串」供 daily macro 卡取用，
       由呼叫端決定是否顯示，本模組自身不推播。
    5. 誠實（紅線③）：無績效/勝率/年化字眼；分數是「盤面氛圍描述」非交易訊號，
       輸出帶誠實標註「影子觀測／非進場訊號」。
════════════════════════════════════════════════════════════════════════════

複用 Core 既有資料源（不重複打 CoinGlass）：
    * funding / OI / 清算 / 巨鯨 / ETF：經 daemon 主 source（CoinGlassSource，
      共用限流器 + TTL 快取）。同 (path,params) 在 TTL 內回上次成功值，吃掉重複。
    * DXY：tradfi（Yahoo Finance 免費，與 daily macro 同源）。
    * breadth：market_scanner.get_latest_breadth()（純讀 scanner.db）。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sqlite3
import time

# JSONL sink 軟上限（位元組）；超過先輪替 .1 再重開，避免無限長（仿 convergence_shadow）
_SINK_MAX_BYTES = 5_000_000

# 分量權重（總和 = 1.0）。確定性、可調但目前固定；shadow 不影響任何下單故可自由校準。
# 採 N/100.0 形式（points 表整數加總 = 100，最穩健抗未來改權重 / 浮點誤差）。
# 第一批 5 個 CoinGlass 端點（coinbase_premium / coin_netflow / btc_dominance /
# altcoin_season / btc_vs_m2）已併入（合計 16 points）。
# 第二批 3 個（v58；orderbook_imbalance 掛單牆 / spot_perp_ratio 現貨-合約量比 /
# agg_cvd_slope 官方聚合CVD）：自 oi(9→7)、coinbase_premium(8→6)、coin_netflow(6→4)
# 各騰出 2 points 共 6，分給新 3 項（3+2+1），維持「ETF / DXY / breadth 仍主導、
# 新端點為弱輔助觀測」的相對序，整體加總仍 = 100。仍全程影子、不影響下單。
_WEIGHTS = {
    "etf": 18 / 100.0,            # 0.18  ETF 機構淨流（最強的趨勢資金訊號）
    "dxy": 14 / 100.0,            # 0.14  美元指數（升→風險資產逆風）
    "breadth": 12 / 100.0,        # 0.12  全市場廣度（risk-on/off 旗標來源）
    "funding": 11 / 100.0,        # 0.11  資金費率（過熱→偏空燃料）
    "oi": 7 / 100.0,             # 0.07  未平倉量趨勢（v58 9→7 騰權重）
    "liquidation": 8 / 100.0,     # 0.08  清算失衡（空清算多→軋空燃料）
    "whales": 6 / 100.0,         # 0.06  HL 巨鯨淨倉
    "coinbase_premium": 6 / 100.0,  # 0.06  美國現貨買壓（v58 8→6 騰權重）
    "coin_netflow": 4 / 100.0,    # 0.04  現貨主動淨買賣(taker buy−sell,正=偏多;v58 6→4)
    "btc_dominance": 4 / 100.0,   # 0.04  BTC 市占（避險／資金外溢山寨）
    "altcoin_season": 2 / 100.0,  # 0.02  山寨季氛圍
    "btc_vs_m2": 2 / 100.0,       # 0.02  流動性估值（最弱輔助）
    "orderbook_imbalance": 3 / 100.0,  # 0.03  新：掛單牆買/賣盤深度失衡（供需牆）
    "spot_perp_ratio": 2 / 100.0,  # 0.02  新：現貨/合約量比（過濾槓桿假突破）
    "agg_cvd_slope": 1 / 100.0,   # 0.01  新：官方聚合 CVD 斜率（校準用，最弱）
}

# breadth<這個門檻 → 掛 risk_off 旗標
_BREADTH_RISKOFF = 35

# 重正規化（task#69）：合成分數除以「有料分量的權重總和」而非全 1.0，避免缺料
# 分量（sub=0）把分數系統性拉向中性而稀釋（約 30% macro 訊號曾因三死分量+缺料
# 靜默扁平化）。地板防止「只有稀疏/低權重分量在線」時過度放大；0.25 ≈ breadth
# (0.12)+dxy(0.14) 常駐基線，故地板僅在嚴重缺料輪次才綁定。score_method 標記讓
# jsonl 行自我區分新舊口徑（紅線③：不回填既有已收斂 snapshot，新口徑只對未來
# 生效）。仍全程影子、不影響下單。
_MIN_PRESENT_MASS = 0.25
_SCORE_METHOD = "v2_renorm_present_mass"

# task#71 暖機（startup-burst 飢餓治本）：daemon 開機時 daily macro / 全市場掃描 /
# 各 worker 首輪幾乎同時打 CoinGlass → 429 → macro_confluence 首輪嚴重缺料 →
# present_mass < _MIN_PRESENT_MASS → 分數 floor-bound（低品質、被地板綁定），且要乾
# 等一整個 interval（預設 1h）才有下一輪。治本＝①把啟動延遲從 90s 拉長到 ~6 分鐘讓
# 尖峰先散②若首輪仍 floor-bound，用短間隔重試（不乾等一小時），拿到一輪「非 floor-
# bound」健康分數或用盡重試額度後，才回到正常 hourly 節奏。純觀測：不改任何分數數
# 學、不回填、floor-bound 行照常落盤帶 score_method provenance（紅線③）。皆可由環
# 境變數覆寫（測試/調參用）。
_WARMUP_DELAY_S_DEFAULT = 360       # 啟動延遲（取代舊固定 90s）
_WARMUP_RETRY_S_DEFAULT = 300       # 暖機期 floor-bound 的短重試間隔
_WARMUP_MAX_RETRIES_DEFAULT = 3     # 暖機短重試次數上限（之後回正常節奏）


# ===========================================================================
# 確定性規則：把每個分量原始值映射到 [-1, +1] 的「對多方燃料方向強度」。
# 全為純函式：零 I/O、零隨機、不改輸入。語意統一：+1 偏多/risk-on、-1 偏空。
# ===========================================================================
def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _num(x) -> bool:
    """是否為『真實數值』（int/float 且非 bool）。用於缺料偵測：判定某分量是否
    有資料進場（present），與其 sub_score 是否為 0 無關——『有料但中性』(如 DXY
    持平=0.0) 仍算 present，其權重須計入重正規化分母，否則中性觀測反放大他項。"""
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def score_etf(cum_7d_flow_usd) -> float:
    """ETF 近 7d 累積淨流 → [-1,+1]。淨流入加分、淨流出扣分。
    ±$2B（7d）視為滿格（近年 BTC 現貨 ETF 強週的量級）。
    """
    if not isinstance(cum_7d_flow_usd, (int, float)):
        return 0.0
    return _clamp(cum_7d_flow_usd / 2_000_000_000.0)


def score_dxy(dxy_change_pct) -> float:
    """美元指數變化% → [-1,+1]。DXY 升＝風險資產逆風（扣分，故反號）。
    ±2%（區間內變動）視為滿格。
    """
    if not isinstance(dxy_change_pct, (int, float)):
        return 0.0
    return _clamp(-dxy_change_pct / 2.0)


def score_breadth(b: dict | None) -> tuple[float, bool]:
    """全市場廣度 → ([-1,+1], risk_off_flag)。
    用 24h 漲跌家數淨佔比當方向；n_total<門檻或缺料 → (0.0, False) 不臆測。
    risk_off：1h 下跌家數佔比過半且 n_total 足夠 → True（風險趨避旗標）。
    """
    if not isinstance(b, dict):
        return 0.0, False
    n_total = b.get("n_total") or 0
    if n_total < 30:                      # 廣度樣本太少 → 中性，不掛旗標
        return 0.0, False
    up24 = b.get("n_up24h") or 0
    dn24 = b.get("n_down24h") or 0
    denom24 = up24 + dn24
    direction = ((up24 - dn24) / denom24) if denom24 > 0 else 0.0

    up1 = b.get("n_up1h") or 0
    dn1 = b.get("n_down1h") or 0
    denom1 = up1 + dn1
    dn_ratio_1h = (dn1 / denom1) if denom1 > 0 else 0.0
    # risk_off：1h 明顯偏空（下跌佔比≥65%）或廣度本身已低於門檻意義的 n_total
    risk_off = (dn_ratio_1h >= 0.65 and dn1 >= 15)
    return _clamp(direction), bool(risk_off)


def score_funding(avg_funding_8h) -> float:
    """平均資金費率（8h 小數，0.0009=0.09%）→ [-1,+1]。
    funding 翻正過熱＝多頭付錢＝過熱／回調風險 → 扣分（反號，與 convergence 一致）；
    funding 偏負＝空方付錢＝軋空燃料 → 加分。±0.05%(8h) 視為滿格。
    """
    if not isinstance(avg_funding_8h, (int, float)):
        return 0.0
    return _clamp(-avg_funding_8h / 0.0005)


def score_oi(oi_delta_pct) -> float:
    """OI 近期變化% → [-1,+1]。增倉視為趨勢動能（順方向加分的『絕對動能』分量；
    方向由其他分量決定，這裡只給『有沒有新資金進場』的溫和正權重）。
    純增倉 +、去槓桿 -。±10% 視為滿格。
    """
    if not isinstance(oi_delta_pct, (int, float)):
        return 0.0
    return _clamp(oi_delta_pct / 10.0)


def score_liquidation(long_liq_usd, short_liq_usd) -> float:
    """清算失衡 → [-1,+1]。空單清算遠大於多單＝軋空燃料（偏多 +）；
    多單清算遠大於空單＝多殺多（偏空 -）。用 (short-long)/(short+long)。
    """
    sl = short_liq_usd if isinstance(short_liq_usd, (int, float)) else 0.0
    ll = long_liq_usd if isinstance(long_liq_usd, (int, float)) else 0.0
    total = sl + ll
    if total <= 0:
        return 0.0
    return _clamp((sl - ll) / total)


def score_whales(net_long_pct) -> float:
    """巨鯨淨多比%（+100 全多 / -100 全空）→ [-1,+1]。直接線性映射。"""
    if not isinstance(net_long_pct, (int, float)):
        return 0.0
    return _clamp(net_long_pct / 100.0)


def score_coinbase_premium(premium_value) -> float:
    """Coinbase 溢價率 → [-1,+1]。>0＝美國現貨買盤強＝偏多(+)；<0＝偏空。
    輸入為 premium_rate（**百分比 %**，client get_coinbase_premium_index 的
    latest）；±0.5% 視為滿格（task#69 實測量級校準；舊 ÷50 誤把 % 當 bps 故此
    分量近乎恆 0）。缺料/非數字回 0.0（中性化）。
    """
    if not isinstance(premium_value, (int, float)):
        return 0.0
    return _clamp(premium_value / 0.5)


def score_coin_netflow(netflow_usd) -> float:
    """現貨主動淨買賣流（taker buy−sell USD）→ [-1,+1]。>0＝主動買盤淨多＝
    偏多(+)、<0＝主動賣盤淨多＝偏空(−)。**不反號**（client 端 net_flow_usd
    已是主動單方向流，與舊『流入交易所＝賣壓』假設相反，task#69 治本）。
    ±$6.5 億視為滿格（實測量級校準）。缺料/非數字回 0.0（中性化）。
    """
    if not isinstance(netflow_usd, (int, float)):
        return 0.0
    return _clamp(netflow_usd / 650_000_000.0)


def score_btc_dominance(dominance_pct) -> float:
    """BTC 市占 → [-1,+1]（影子層採『對整體加密 risk 氛圍』解讀）。
    市占升＝資金回流 BTC 避險、山寨失血＝整體 risk_off 傾向(反號,-)；
    市占降＝資金外溢山寨＝risk_on(+)。以 50% 為中性錨，±10 個百分點
    （40~60%）視為滿格。缺料/非數字回 0.0（中性化）。
    """
    if not isinstance(dominance_pct, (int, float)):
        return 0.0
    return _clamp(-(dominance_pct - 50.0) / 10.0)


def score_altcoin_season(season_index) -> float:
    """Altcoin Season Index（0-100）→ [-1,+1]。高＝山寨季＝risk_on(+)；
    低＝比特幣季＝risk_off(-)。以 50 為中性，線性映射 (idx-50)/50。
    缺料/非數字回 0.0（中性化）。
    """
    if not isinstance(season_index, (int, float)):
        return 0.0
    return _clamp((season_index - 50.0) / 50.0)


def score_btc_vs_m2(deviation_pct) -> float:
    """BTC 相對 M2 的超漲/落後% → [-1,+1]（估值／流動性對照）。
    BTC 漲幅超過 M2（正偏離）＝流動性順風下的動能延續＝溫和偏多(+)；
    落後＝偏空(-)。±30 個百分點視為滿格（此分量權重最低，僅給弱訊號）。
    缺料/非數字回 0.0（中性化）。
    """
    if not isinstance(deviation_pct, (int, float)):
        return 0.0
    return _clamp(deviation_pct / 30.0)


def score_orderbook_imbalance(imbalance) -> float:
    """掛單牆深度失衡 → [-1,+1]。client 端已算成 (bid-ask)/(bid+ask) ∈ [-1,+1]：
    買牆厚於賣牆＝下方支撐強＝偏多(+)；賣牆厚＝上方壓力強＝偏空(-)。
    已正規化故直接 clamp。缺料/非數字回 0.0（中性化）。
    """
    if not isinstance(imbalance, (int, float)):
        return 0.0
    return _clamp(imbalance)


def score_spot_perp_ratio(ratio) -> float:
    """現貨/合約量比 → [-1,+1]。錨點 1.0（現貨≈合約量）為中性：
    >1＝現貨主導＝真實買賣盤推動（健康，偏多+）；<1＝合約槓桿主導＝
    易為假突破（偏空/降權-）。以 (ratio-1.0) 映射、±1.0 視為滿格。
    錨點 1.0 為暫定基準（影子觀測，落地後可依實測分布再校）。
    缺料/非數字回 0.0（中性化）。
    """
    if not isinstance(ratio, (int, float)):
        return 0.0
    return _clamp(ratio - 1.0)


def score_agg_cvd_slope(slope) -> float:
    """官方聚合 CVD 斜率 → [-1,+1]。client 端已算成
    Σ(主買-主賣)/Σ(主買+主賣) ∈ [-1,+1] 的無量綱正規化斜率：
    主動買量淨多＝偏多(+)；主動賣量淨多＝偏空(-)。已正規化故直接 clamp。
    ⚠️ 此值為影子校準用，**永不**餵進 strength.py 既有的 cvd_slope_7d。
    缺料/非數字回 0.0（中性化）。
    """
    if not isinstance(slope, (int, float)):
        return 0.0
    return _clamp(slope)


def _btc_vs_m2_deviation(series, window: int = 30, max_stale_days: int = 45):
    """從 get_bitcoin_vs_m2 的 series 算『近 window 根 BTC 漲幅% − M2 漲幅%』偏離。
    取尾端 last 與 last-window 兩點（非 first-vs-last 全史，避免長窗永遠飽和）。

    這是蒐集層 derive 輔助（非純 scorer：會讀 wall clock 做時效防呆）。資料過期
    （last 點 > max_stale_days 天）一律回 None＝誠實缺料（紅線③：一個恆飽和的
    錯接分量比『誠實缺席』更糟）。資料不足/欄位壞/分母為 0 → None。
    """
    if not series or len(series) < window + 1:
        return None
    last = series[-1]
    prev = series[-1 - window]
    if not isinstance(last, dict) or not isinstance(prev, dict):
        return None
    ts = last.get("ts") or 0
    if ts and ts > 1e12:          # 毫秒 → 秒
        ts = ts / 1000.0
    if ts and (time.time() - ts) > max_stale_days * 86400:
        return None               # 過期 → 誠實缺料
    p, m = last.get("price"), last.get("m2")
    p0, m0 = prev.get("price"), prev.get("m2")
    if not all(isinstance(x, (int, float)) and x for x in (p, m, p0, m0)):
        return None
    return (p - p0) / p0 * 100.0 - (m - m0) / m0 * 100.0


def compute_confluence(components: dict) -> dict:
    """把各分量原始輸入用確定性規則合成 macro_confluence_score + 明細。**純函式**。

    參數 components（各鍵皆可缺，缺則該分量中性化）：
        etf_cum_7d_flow_usd, dxy_change_pct, breadth(dict), avg_funding_8h,
        oi_delta_pct, liq_long_usd, liq_short_usd, whale_net_long_pct,
        coinbase_premium_value, coin_netflow_usd, btc_dominance_pct,
        altcoin_season_index, btc_vs_m2_deviation_pct,
        orderbook_imbalance_value, spot_perp_ratio_value, agg_cvd_slope_value
    回傳
        macro_confluence_score: float ∈ [-100,+100]（+偏多/risk-on，-偏空/risk-off）
            ＝ Σ(sub*weight) / max(有料權重總和, _MIN_PRESENT_MASS) × 100（重正規化）
        components: {name: {raw, sub_score∈[-1,1], weight, contribution, present}}
        risk_off: bool（breadth 風險趨避旗標）
        n_present: 有料（有資料進場）分量數（含有料但中性=0 者）
        present_mass: 有料分量的權重總和（重正規化分母，套地板前）
        score_method: 計分口徑標記（區分新舊 jsonl 行；紅線③不回填舊 snapshot）
        bias: 'risk_on' | 'risk_off' | 'neutral'（依分數帶）

    ⚠️ 此分數為 SHADOW 專用：永不乘進/加進 strength_score、永不進 fire/下單。
    """
    c = components or {}
    breadth_score, risk_off = score_breadth(c.get("breadth"))

    subs = {
        "etf": (score_etf(c.get("etf_cum_7d_flow_usd")),
                c.get("etf_cum_7d_flow_usd")),
        "dxy": (score_dxy(c.get("dxy_change_pct")), c.get("dxy_change_pct")),
        "breadth": (breadth_score,
                    (c.get("breadth") or {}).get("n_total")
                    if isinstance(c.get("breadth"), dict) else None),
        "funding": (score_funding(c.get("avg_funding_8h")),
                    c.get("avg_funding_8h")),
        "oi": (score_oi(c.get("oi_delta_pct")), c.get("oi_delta_pct")),
        "liquidation": (score_liquidation(c.get("liq_long_usd"),
                                          c.get("liq_short_usd")),
                        {"long": c.get("liq_long_usd"),
                         "short": c.get("liq_short_usd")}),
        "whales": (score_whales(c.get("whale_net_long_pct")),
                   c.get("whale_net_long_pct")),
        # 新增 5 個 CoinGlass 綜合宏觀端點（影子輔助觀測；鍵名須與 _WEIGHTS 一致）
        "coinbase_premium": (score_coinbase_premium(c.get("coinbase_premium_value")),
                             c.get("coinbase_premium_value")),
        "coin_netflow": (score_coin_netflow(c.get("coin_netflow_usd")),
                         c.get("coin_netflow_usd")),
        "btc_dominance": (score_btc_dominance(c.get("btc_dominance_pct")),
                          c.get("btc_dominance_pct")),
        "altcoin_season": (score_altcoin_season(c.get("altcoin_season_index")),
                           c.get("altcoin_season_index")),
        "btc_vs_m2": (score_btc_vs_m2(c.get("btc_vs_m2_deviation_pct")),
                      c.get("btc_vs_m2_deviation_pct")),
        # 第二批 3 個 CoinGlass 端點（v58；影子輔助觀測；鍵名須與 _WEIGHTS 一致）
        "orderbook_imbalance": (
            score_orderbook_imbalance(c.get("orderbook_imbalance_value")),
            c.get("orderbook_imbalance_value")),
        "spot_perp_ratio": (
            score_spot_perp_ratio(c.get("spot_perp_ratio_value")),
            c.get("spot_perp_ratio_value")),
        "agg_cvd_slope": (
            score_agg_cvd_slope(c.get("agg_cvd_slope_value")),
            c.get("agg_cvd_slope_value")),
    }

    # 缺料偵測（present＝該分量有資料進場，與 sub 是否為 0 無關）。breadth 與
    # liquidation 依其 scorer 的「無訊號」語意特判：breadth 須 n_total≥30、
    # liquidation 須 long/short 至少一者為數值且總額>0，否則視為缺料不計分母。
    _b = c.get("breadth")
    breadth_present = isinstance(_b, dict) and (_b.get("n_total") or 0) >= 30
    _ll, _sl = c.get("liq_long_usd"), c.get("liq_short_usd")
    liq_total = (_ll if _num(_ll) else 0.0) + (_sl if _num(_sl) else 0.0)
    liq_present = bool(_num(_ll) or _num(_sl)) and liq_total > 0
    present_map = {
        "etf": _num(c.get("etf_cum_7d_flow_usd")),
        "dxy": _num(c.get("dxy_change_pct")),
        "breadth": breadth_present,
        "funding": _num(c.get("avg_funding_8h")),
        "oi": _num(c.get("oi_delta_pct")),
        "liquidation": liq_present,
        "whales": _num(c.get("whale_net_long_pct")),
        "coinbase_premium": _num(c.get("coinbase_premium_value")),
        "coin_netflow": _num(c.get("coin_netflow_usd")),
        "btc_dominance": _num(c.get("btc_dominance_pct")),
        "altcoin_season": _num(c.get("altcoin_season_index")),
        "btc_vs_m2": _num(c.get("btc_vs_m2_deviation_pct")),
        "orderbook_imbalance": _num(c.get("orderbook_imbalance_value")),
        "spot_perp_ratio": _num(c.get("spot_perp_ratio_value")),
        "agg_cvd_slope": _num(c.get("agg_cvd_slope_value")),
    }

    detail: dict[str, dict] = {}
    weighted_sum = 0.0
    present_mass = 0.0
    n_present = 0
    for name, (sub, raw) in subs.items():
        w = _WEIGHTS.get(name, 0.0)
        present = bool(present_map.get(name, False))
        contrib = sub * w
        weighted_sum += contrib
        if present:
            present_mass += w
            n_present += 1
        detail[name] = {
            "raw": raw,
            "sub_score": round(sub, 4),
            "weight": w,
            "contribution": round(contrib, 4),
            "present": present,
        }

    # 重正規化：除以有料權重總和（地板 _MIN_PRESENT_MASS 防稀疏過度放大）。
    # 缺料分量 sub=0 不再把分數系統性稀釋向中性。
    denom = max(present_mass, _MIN_PRESENT_MASS)
    norm = (weighted_sum / denom) if denom > 0 else 0.0
    score = round(_clamp(norm, -1.0, 1.0) * 100, 2)
    if risk_off:
        bias = "risk_off"
    elif score >= 20:
        bias = "risk_on"
    elif score <= -20:
        bias = "risk_off"
    else:
        bias = "neutral"

    return {
        "macro_confluence_score": score,   # SHADOW only — 永不施用於 strength/fire
        "components": detail,
        "risk_off": bool(risk_off),
        "n_present": n_present,
        "present_mass": round(present_mass, 4),
        "score_method": _SCORE_METHOD,
        "bias": bias,
    }


# ===========================================================================
# 顯示層（純顯示）：把一輪結果組成「綜合分數儀表板」文字供 daily macro 卡顯示。
# 不過 LLM、不推播；任何缺料/錯誤回安全字串。帶誠實標註（紅線③）。
# ===========================================================================
def render_dashboard(summary: dict | None) -> str:
    """回「綜合宏觀儀表板」純文字。summary = compute_confluence(...) 之回傳
    （另可含 'ts'）。缺料/壞 dict → 回安全提示字串，永不 raise。
    """
    try:
        s = summary or {}
        score = s.get("macro_confluence_score")
        if not isinstance(score, (int, float)):
            return "📊 綜合宏觀儀表板：累積數據中…（影子觀測）"
        bias_zh = {"risk_on": "🟢 偏多 / Risk-On",
                   "risk_off": "🔴 偏空 / Risk-Off",
                   "neutral": "⚪ 中性"}.get(s.get("bias"), "⚪ 中性")
        comps = s.get("components") or {}
        name_zh = {"etf": "ETF淨流", "dxy": "美元DXY", "breadth": "市場廣度",
                   "funding": "資金費率", "oi": "未平倉OI",
                   "liquidation": "清算失衡", "whales": "巨鯨淨倉",
                   "coinbase_premium": "CB溢價", "coin_netflow": "現貨淨買賣",
                   "btc_dominance": "BTC市占", "altcoin_season": "山寨季",
                   "btc_vs_m2": "M2流動性", "orderbook_imbalance": "掛單牆",
                   "spot_perp_ratio": "現貨/合約量比", "agg_cvd_slope": "官方CVD"}
        # 取貢獻度絕對值前 4 大分量列出（方向＋/−）
        ranked = sorted(
            ((name_zh.get(k, k), v.get("sub_score", 0.0))
             for k, v in comps.items() if isinstance(v, dict)),
            key=lambda kv: abs(kv[1]), reverse=True)
        parts = []
        for label, sub in ranked[:4]:
            if abs(sub) < 1e-9:
                continue
            arrow = "▲" if sub > 0 else "▼"
            parts.append(f"{label}{arrow}{abs(sub):.2f}")
        drivers = "　".join(parts) if parts else "各分量皆中性"
        riskoff_tag = "　⚠️廣度risk-off旗標" if s.get("risk_off") else ""
        n_present = s.get("n_present", 0)
        return (
            f"📊 <b>綜合宏觀儀表板（影子觀測，非進場訊號）</b>\n"
            f"　綜合分數 <code>{score:+.1f}</code>／100　{bias_zh}"
            f"（{n_present} 個分量在線）{riskoff_tag}\n"
            f"　主導分量：{drivers}\n"
            f"　<i>※ 確定性規則合成；永不影響訊號/下單，僅供盤面氛圍參考。</i>"
        )
    except Exception:
        return "📊 綜合宏觀儀表板：暫時無法顯示（影子觀測）"


# ===========================================================================
# 影子 JSONL sink
# ===========================================================================
def _sink_path():
    from botpaths import data_dir
    return data_dir() / "macro_confluence.jsonl"


def _append_jsonl(record: dict) -> None:
    """把一輪觀測寫一行 JSONL（純本地檔；超過軟上限就輪替一次）。失敗吞掉。"""
    path = _sink_path()
    try:
        if path.exists() and path.stat().st_size > _SINK_MAX_BYTES:
            backup = path.with_suffix(".jsonl.1")
            try:
                if backup.exists():
                    backup.unlink()
                path.rename(backup)
            except OSError:
                pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ===========================================================================
# history-logger：每小時把當下 OI/CVD/funding 最近 500 根快照持續累積寫 SQLite。
# ---------------------------------------------------------------------------
# 誠實標明：這是「從今天起往前累積」的未來歷史。CoinGlass history 端點硬卡 500
# 根、present-anchored、無時間分頁 → 補不回過去；本表只負責「從現在開始，每小時
# 落一次盤，日積月累出跨年綜合歷史」。每筆標 captured_at（落盤時刻）。
# 用獨立 DB 檔（macro_history.db），不碰 trade_journal.db（影子資料不入帳本）。
# ===========================================================================
def _history_db_path():
    from botpaths import db_path as _db_path
    return _db_path("macro_history.db")


def _hist_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_history_db_path(), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_history_db() -> None:
    """建影子歷史表（冪等）。metric ∈ {oi, cvd, funding}；bar_ts=該根原始時間戳。
    主鍵 (symbol,metric,bar_ts) → 同一根重抓 INSERT OR IGNORE 不重複累積。
    captured_at＝本機落盤毫秒（誠實標『何時開始累積』）。
    """
    conn = _hist_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS macro_metric_history (
                symbol TEXT NOT NULL,
                metric TEXT NOT NULL,        -- 'oi' | 'cvd' | 'funding'
                bar_ts INTEGER NOT NULL,     -- 該根原始時間戳（ms/s 依源；原樣保存）
                value REAL,
                interval TEXT,               -- 抓取視窗（如 '1h'）
                captured_at INTEGER NOT NULL,  -- 本機落盤毫秒（未來歷史起算點）
                PRIMARY KEY (symbol, metric, bar_ts)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mmh_metric_ts "
            "ON macro_metric_history(metric, bar_ts)")
    finally:
        conn.close()


def _persist_snapshot(symbol: str, metric: str,
                      series: list[dict], interval: str = "1h") -> int:
    """把一條 {ts,value} 序列（最多 500 根）INSERT OR IGNORE 累積。回實際新增筆數。
    series 元素需含 'ts' + ('value' 或 cvd 用 'value')；缺值 row 跳過。失敗回 0。
    """
    if not series:
        return 0
    now_ms = int(time.time() * 1000)
    rows = []
    for pt in series[-500:]:
        if not isinstance(pt, dict):
            continue
        ts = pt.get("ts")
        val = pt.get("value")
        if ts is None or not isinstance(val, (int, float)):
            continue
        rows.append((symbol, metric, int(ts), float(val), interval, now_ms))
    if not rows:
        return 0
    conn = _hist_conn()
    try:
        cur = conn.executemany(
            "INSERT OR IGNORE INTO macro_metric_history "
            "(symbol, metric, bar_ts, value, interval, captured_at) "
            "VALUES (?,?,?,?,?,?)", rows)
        return cur.rowcount if cur.rowcount is not None else 0
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


async def _log_history(source, symbols=("BTC", "ETH", "SOL")) -> dict:
    """對 symbols 抓 OI/CVD/funding 各 ≤500 根並累積進 macro_history.db。
    複用 daemon 主 source（共用限流器 + TTL 快取）。任何源失敗該項跳過、不 raise。
    回 {"inserted": n, "errors": [...]}（觀測用統計）。
    """
    init_history_db()
    inserted = 0
    errors: list[str] = []
    if source is None:
        return {"inserted": 0, "errors": ["no source"]}
    for sym in symbols:
        # OI（1h，多拉以盡量補滿 500 根）
        try:
            r = await source.get_oi(sym, "1h", 500)
            if isinstance(r, dict) and not r.get("error"):
                inserted += _persist_snapshot(sym, "oi", r.get("series") or [], "1h")
        except Exception as e:
            errors.append(f"{sym}/oi:{type(e).__name__}")
        # CVD（1h）
        try:
            r = await source.get_cvd_series(sym, "1h", 500)
            if isinstance(r, dict) and not r.get("error"):
                inserted += _persist_snapshot(sym, "cvd", r.get("series") or [], "1h")
        except Exception as e:
            errors.append(f"{sym}/cvd:{type(e).__name__}")
        # funding（1h 序列）
        try:
            r = await source.get_funding_series(sym, "1h", 500)
            if isinstance(r, dict) and not r.get("error"):
                inserted += _persist_snapshot(sym, "funding",
                                              r.get("series") or [], "1h")
        except Exception as e:
            errors.append(f"{sym}/funding:{type(e).__name__}")
    return {"inserted": inserted, "errors": errors}


# ===========================================================================
# 蒐料（I/O 薄層）：複用 Core 既有源，缺料一律 None / 中性化，絕不 raise。
# ===========================================================================
async def _collect_components(source) -> dict:
    """蒐集合成所需的各分量原始值。任何單項失敗 → 該鍵缺/None（compute 端中性化）。

    複用既有源（不重複打 CoinGlass）：
        ETF：source.get_etf_flows('BTC',7)（共用限流器 + TTL 快取）
        funding/OI/清算：source（BTC 代理整體加密 risk 氛圍）
        巨鯨：source.get_hyperliquid_whales()
        DXY：tradfi（Yahoo Finance）
        breadth：market_scanner.get_latest_breadth()（純讀 scanner.db）
        新增 5 個 CoinGlass 綜合宏觀端點（coinbase premium / coin netflow /
        btc dominance / altcoin season / btc vs m2）：全走同一 source（共用
        限流器 + TTL 快取），各自獨立 try/except；任何缺料一律中性化、不計
        n_present 分母（沿用既有優雅降級模式，永不臆測方向、永不拖垮 daemon）。
    """
    out: dict = {}
    if source is None:
        try:
            from market_intel_mcp.sources import get_source
            source = get_source()
        except Exception:
            source = None

    # --- breadth（純讀本地 scanner.db，最便宜，先拿）---
    try:
        from l3_dispatcher.market_scanner import get_latest_breadth
        b = get_latest_breadth()
        if isinstance(b, dict):
            out["breadth"] = b
            af = b.get("avg_funding")
            if isinstance(af, (int, float)):
                out["avg_funding_8h"] = af   # breadth 已含全市場均資費，免再打
    except Exception:
        pass

    # --- DXY（tradfi）---
    try:
        from market_intel_mcp.sources.tradfi import get_tradfi
        # DX=F 24h 期貨優先（全天候），缺則 DX-Y.NYB
        for tk in ("DX=F", "DX-Y.NYB"):
            r = await get_tradfi().get_ticker(tk)
            if isinstance(r, dict) and not r.get("error"):
                out["dxy_change_pct"] = r.get("change_1d_pct")
                break
    except Exception:
        pass

    if source is None:
        return out

    # --- ETF 7d 累積淨流（BTC 為主流代理）---
    try:
        r = await source.get_etf_flows("BTC", 7)
        if isinstance(r, dict) and not r.get("error"):
            out["etf_cum_7d_flow_usd"] = r.get("cumulative_7d_flow_usd")
    except Exception:
        pass

    # --- funding（若 breadth 沒給均資費，退而用 BTC funding 代理）---
    if "avg_funding_8h" not in out:
        try:
            r = await source.get_funding("BTC")
            if isinstance(r, dict) and not r.get("error"):
                out["avg_funding_8h"] = r.get("funding")
        except Exception:
            pass

    # --- OI 24h 變化%（BTC 代理整體槓桿動能）---
    try:
        r = await source.get_oi("BTC", "1h", 24)
        if isinstance(r, dict) and not r.get("error"):
            out["oi_delta_pct"] = r.get("delta_pct_24h")
    except Exception:
        pass

    # --- 清算失衡（BTC 近 24h 多/空清算 USD）---
    try:
        r = await source.get_liquidations("BTC", "24h")
        if isinstance(r, dict) and not r.get("error"):
            out["liq_long_usd"] = r.get("liq_long")
            out["liq_short_usd"] = r.get("liq_short")
    except Exception:
        pass

    # --- 巨鯨（HL）BTC 淨多比 ---
    try:
        r = await source.get_hyperliquid_whales(50)
        if isinstance(r, dict) and not r.get("error"):
            for it in (r.get("per_symbol_aggregate") or []):
                if (it.get("symbol") or "").upper() in ("BTC", "BTCUSDT", "XBT"):
                    out["whale_net_long_pct"] = it.get("net_long_pct")
                    break
    except Exception:
        pass

    # ----------------------------------------------------------------------
    # 新增 5 個 CoinGlass 綜合宏觀端點（v56 預留→正式接入 confluence 影子分數）。
    # 各自獨立 try/except（一塊崩潰不波及他塊），全部 `not r.get('error')` 才寫鍵；
    # 缺料一律不寫鍵 → compute 端 score_* 收 None 回 0.0 → 不計 n_present 分母。
    # 全程影子：只寫 out dict 供純函式合成 + 顯示，永不入 strength/fire/下單。
    # ----------------------------------------------------------------------
    # --- CB 溢價（美國現貨買壓代理；latest=premium_rate 百分比%）---
    try:
        r = await source.get_coinbase_premium_index("1h", 24)
        if isinstance(r, dict) and not r.get("error"):
            out["coinbase_premium_value"] = r.get("latest")
    except Exception:
        pass

    # --- 現貨主動淨買賣流（taker buy−sell；正＝買盤淨多偏多，不反號）---
    try:
        r = await source.get_coin_netflow("BTC", "1h", 24)
        if isinstance(r, dict) and not r.get("error"):
            out["coin_netflow_usd"] = r.get("latest")
    except Exception:
        pass

    # --- BTC 市占（避險／資金外溢山寨）---
    try:
        r = await source.get_bitcoin_dominance(30)
        if isinstance(r, dict) and not r.get("error"):
            out["btc_dominance_pct"] = r.get("latest")
    except Exception:
        pass

    # --- 山寨季氛圍（Altcoin Season Index 0-100）---
    try:
        r = await source.get_altcoin_season(30)
        if isinstance(r, dict) and not r.get("error"):
            out["altcoin_season_index"] = r.get("latest")
    except Exception:
        pass

    # --- BTC vs M2（此端點無 'latest'，僅 series；用近端窗偏離 + 時效防呆）---
    # 改 first-vs-last 全史 → 近 30 根尾端窗（避免長窗永遠飽和）；資料過期一律
    # 回 None＝誠實缺料不寫鍵（紅線③：恆飽和的錯接分量比『誠實缺席』更糟）。
    try:
        r = await source.get_bitcoin_vs_m2("global", 120)
        if isinstance(r, dict) and not r.get("error"):
            dev = _btc_vs_m2_deviation(r.get("series") or [])
            if dev is not None:
                out["btc_vs_m2_deviation_pct"] = dev
    except Exception:
        pass

    # ----------------------------------------------------------------------
    # 第二批 3 個 CoinGlass 端點（v58；$79 即有，影子輔助觀測）。同樣各自
    # 獨立 try/except，缺料一律不寫鍵 → score_* 收 None 回 0.0 → 不計 n_present。
    # 全程影子：只寫 out dict 供純函式合成 + 顯示，永不入 strength/fire/下單。
    # ----------------------------------------------------------------------
    # --- 掛單牆深度失衡（聚合 ask/bids 歷史；client 已算 (bid-ask)/(bid+ask)）---
    try:
        r = await source.get_orderbook_ask_bids_history("BTC", "1h", 24)
        if isinstance(r, dict) and not r.get("error"):
            out["orderbook_imbalance_value"] = r.get("latest_imbalance")
    except Exception:
        pass

    # --- 現貨/合約量比（client 由現貨vs合約主動量 derive，1.0 為中性錨）---
    try:
        r = await source.get_futures_spot_volume_ratio("BTC", "1h", 24)
        if isinstance(r, dict) and not r.get("error"):
            out["spot_perp_ratio_value"] = r.get("latest")
    except Exception:
        pass

    # --- 官方聚合 CVD 斜率（校準用；client 已算成 [-1,+1] 正規化斜率）---
    # ⚠️ 此值僅供影子合成/校準，永不餵進 strength.py 的 cvd_slope_7d。
    try:
        r = await source.get_aggregated_cvd_history("BTC", "1h", 168)
        if isinstance(r, dict) and not r.get("error"):
            out["agg_cvd_slope_value"] = r.get("latest_slope")
    except Exception:
        pass

    return out


async def _run_cycle(source=None) -> dict:
    """跑一輪：蒐料 → 確定性合成 → 順手累積一次歷史快照 → 回可序列化摘要 dict。

    ⚠️ 全程影子：回傳/落盤的分數從不施用於 strength/fire/下單。
    """
    components = await _collect_components(source)
    summary = compute_confluence(components)
    summary["ts"] = dt.datetime.now(tz=dt.timezone.utc).isoformat()
    summary["note"] = ("shadow-only: macro_confluence_score 從不施用於 "
                       "strength_score/fire/下單；確定性規則合成，僅供 A/B 觀測。")

    # 順手累積一次歷史快照（未來歷史；補不回過去，誠實標 captured_at）
    try:
        hist = await _log_history(source)
        summary["history_inserted"] = hist.get("inserted", 0)
    except Exception as e:
        summary["history_inserted"] = 0
        summary["history_error"] = f"{type(e).__name__}: {e}"

    return summary


async def run_macro_confluence_loop(source=None, interval_seconds: int = 3600):
    """Session C 綜合宏觀合成常駐迴圈（每 interval 跑一輪，純觀測寫 JSONL + SQLite）。

    `source`＝daemon 主 source（與其他 worker 簽名一致），複用其限流器/TTL 快取；
    None → 延遲 get_source()（供一次性測試）。

    影子鐵則：永不影響 strength/fire/下單、不發 Telegram、整輪包 try/except 續跑。
    用 asyncio.sleep 讓出事件迴圈，不吞例外成 busy-loop。

    task#71 暖機：啟動延遲拉長以避開開機尖峰；首輪若 floor-bound（present_mass <
    _MIN_PRESENT_MASS，表示尖峰仍未散、CoinGlass 大量 429）改短間隔重試，拿到一輪
    健康分數或用盡額度後回正常 hourly 節奏。詳見模組頂部 _WARMUP_* 常數說明。
    """
    def _env_int(key: str, default: int) -> int:
        try:
            return int(os.getenv(key, str(default)))
        except (TypeError, ValueError):
            return default

    warmup_delay = max(0, _env_int("MACRO_CONFLUENCE_WARMUP_S", _WARMUP_DELAY_S_DEFAULT))
    retry_s = _env_int("MACRO_CONFLUENCE_WARMUP_RETRY_S", _WARMUP_RETRY_S_DEFAULT)
    warmup_retries_left = max(0, _env_int(
        "MACRO_CONFLUENCE_WARMUP_MAX_RETRIES", _WARMUP_MAX_RETRIES_DEFAULT))

    # 啟動延遲（取代舊固定 90s）：讓 daily macro / 全市場掃描 / 各 worker 首輪散開
    await asyncio.sleep(warmup_delay)
    while True:
        floor_bound = False
        try:
            summary = await _run_cycle(source)
            _append_jsonl(summary)
            # floor-bound＝有料權重總和不足地板（嚴重缺料/開機尖峰飢餓）。仍照常落盤、
            # 帶 score_method provenance，僅用來決定「暖機期是否短重試」，不改任何數學。
            floor_bound = (summary.get("present_mass") or 0.0) < _MIN_PRESENT_MASS
            _warm_tag = (" [floor-bound→暖機短重試]"
                         if (floor_bound and warmup_retries_left > 0) else "")
            print(f"[macro_confluence] score={summary.get('macro_confluence_score')} "
                  f"bias={summary.get('bias')} "
                  f"n_present={summary.get('n_present')} "
                  f"present_mass={summary.get('present_mass')} "
                  f"risk_off={summary.get('risk_off')} "
                  f"hist+{summary.get('history_inserted', 0)}{_warm_tag}")
        except Exception as e:  # 整輪保護：任何意外吞掉續跑，不拖垮 daemon
            print(f"[macro_confluence] cycle error: {type(e).__name__}: {e}")

        # 暖機期：floor-bound 且仍有重試額度 → 短間隔重試，不乾等一整個 interval。
        # 注意僅限開機暖機窗：拿到健康分數或用盡額度即清零，之後（數小時後）若再
        # floor-bound 屬真實資料缺口、由 provenance 誠實標註、下輪自癒，不再短重試
        # （避免在 CoinGlass 真故障時反覆加打已下線的 API）。
        if floor_bound and warmup_retries_left > 0:
            warmup_retries_left -= 1
            await asyncio.sleep(max(60, retry_s))
            continue
        warmup_retries_left = 0
        # 確定性間隔睡眠（>=60s），讓出事件迴圈，非 busy-loop
        await asyncio.sleep(max(60, int(interval_seconds)))
