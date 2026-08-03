"""L2 觸發引擎共用型別。

設計原則：
- 純 dataclass、frozen=True、無外部依賴
- 缺料用 Optional[float] = None 表示（呼應 MCP 的 stale_fields）
- 每個 eval_* 入口先 snapshot.is_stale(...) 檢查 → STALE 不參與投票
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal, Optional


# =============================================================================
# 列舉
# =============================================================================
class Source(str, Enum):
    """數據來源標籤"""
    coinglass = "coinglass"
    okx = "okx"
    local = "local"
    mock = "mock"


class SignalState(str, Enum):
    """訊號狀態。
    STALE = 缺料，該訊號不計入投票（不同於 NEUTRAL）
    BLOCK = 閘關閉，整包 HOLD（BTC 閘專用）
    """
    BULL = "bull"
    BEAR = "bear"
    NEUTRAL = "neutral"
    BLOCK = "block"
    STALE = "stale"


class TriggerAction(str, Enum):
    FIRE = "fire"
    HOLD = "hold"


# =============================================================================
# 訊號結果
# =============================================================================
@dataclass(frozen=True)
class SignalResult:
    """單一訊號評估結果。evidence 限 JSON-serializable primitives。"""
    name: str
    state: SignalState
    score: float                          # -1..+1（負偏空、正偏多）
    evidence: dict


# =============================================================================
# 市場快照（L2 引擎輸入）
# =============================================================================
@dataclass(frozen=True)
class MarketSnapshot:
    """完整市場快照。

    缺料欄位 = None；eval_* 透過 is_stale() 偵測並回傳 STALE。
    這樣單一來源掛掉不會整包 HOLD。
    """
    # === 識別 ===
    symbol: str
    ts: int                                       # epoch ms
    price: float
    tf: str = "1h"                                # 主分析時框

    # --- Setup A 即時點資料 -----------------------------------------------
    oi: Optional[float] = None
    oi_delta_pct: Optional[float] = None          # OI 24h 變化%
    price_chg_24h_pct: Optional[float] = None     # 價格 24h 變化%（與 oi_delta_pct 同窗；復盤象限價格方向後備，純觀測）
    funding: Optional[float] = None               # 當前資金費率（小數，如 -0.0001 = -0.01%）
    funding_predicted: Optional[float] = None     # 預估下次
    cvd: Optional[float] = None
    cvd_slope: Optional[float] = None             # 近期斜率
    cvd_price_divergence: Literal["bull", "bear", "none"] = "none"
    ls_ratio: Optional[float] = None              # 散戶多空比
    top_trader_ratio: Optional[float] = None      # 大戶多空比
    liq_long: Optional[float] = None
    liq_short: Optional[float] = None

    # --- BTC 閘 -----------------------------------------------------------
    btc_gate_open: Optional[bool] = None
    btc_regime: Optional[str] = None              # trend_up | trend_down | range

    # --- Setup A 趨勢/進場確認 ---------------------------------------------
    above_4h_200ma: Optional[bool] = None         # 4h 收盤 > 4h 200MA
    breakout_1h_high: Optional[bool] = None       # 1h 突破近 24h 高

    # --- Hot 標記 ---------------------------------------------------------
    is_hot: bool = False                          # 是否在 Hot watchlist
    strength_score: Optional[float] = None        # 0-100 由 mi_get_strength_rank 算

    # --- Setup B 結構欄位（7d 視窗）---------------------------------------
    atr_pct_7d: Optional[float] = None            # 7d ATR / price (%)
    vol_24h_vs_30d: Optional[float] = None        # 24h vol / 30d 均量
    cvd_slope_7d: Optional[float] = None
    top_trader_slope_7d: Optional[float] = None
    oi_delta_7d_pct: Optional[float] = None
    higher_lows_7d: Optional[bool] = None         # 7d 高低點抬升

    # --- Setup C: MA 穿越欄位 --------------------------------------------
    ma200_4h: Optional[float] = None              # 4h 200 期 SMA
    prev_close_4h: Optional[float] = None         # 前一根 4h 收盤（穿越偵測用）

    # --- 美股永續 us_breakout 欄位（v17）-----------------------------------
    us_breakout_dir: Literal["bull", "bear", "none"] = "none"  # snapshot 建構期算好
    us_break_level: Optional[float] = None        # 被突破的 24h 高/低
    us_vol_mult: Optional[float] = None           # 突破K量 / 前24根1h均量
    us_taker_ratio: Optional[float] = None        # 近4根1H ΣbuyVol/ΣsellVol
    us_session: Optional[str] = None              # 'rth' | 'ext' | 'off'
    qqq_chg_24h_pct: Optional[float] = None       # QQQ 永續 24h 變化%
    atr_1h_pct: Optional[float] = None            # 14期 1h ATR / price (%)

    # --- 元資料 -----------------------------------------------------------
    stale_fields: tuple[str, ...] = ()
    sources_used: tuple[str, ...] = ()

    def is_stale(self, *fields: str) -> bool:
        """任一欄位 None 或列在 stale_fields → True"""
        for f in fields:
            val = getattr(self, f, None)
            if val is None or f in self.stale_fields:
                return True
        return False

    def unknown_fields(self) -> tuple[str, ...]:
        """回「這一份快照裡，哪些欄位我不知道值」——is_stale() 的全體版本。

        v241：⛔ 不可用 stale_fields 單獨回答這個問題。兩者的意思不一樣：

            stale_fields   = 來源明確回報失敗（MCP 那端標的）
            值是 None      = 源根本沒給這一欄（例如衍生的 7d 視窗欄）

        引擎問的是 is_stale()＝兩者聯集，所以任何拿 stale_fields 生報表或
        告警的地方，都會比引擎少數幾欄——同一份快照，兩個「未知」答案，
        而少報的那一邊會把「我不知道」讀成「正常」。

        線上實測（2026-08-03，CG 停權後）：stale_fields 3 欄，實際未知 11 欄。
        """
        from dataclasses import fields as _dc_fields
        meta = {"symbol", "tf", "stale_fields", "sources_used"}
        out = [f.name for f in _dc_fields(self)
               if f.name not in meta and self.is_stale(f.name)]
        seen = set(out)
        # stale_fields 可能含 dataclass 上沒有的欄名（MCP 端的欄位集較廣）。
        # 那些也是「我不知道」，不可因為這邊沒有對應屬性就丟掉。
        out.extend(f for f in self.stale_fields
                   if f not in seen and f not in meta)
        return tuple(out)


# =============================================================================
# 觸發設定（每個 Setup 一份）
# =============================================================================
@dataclass(frozen=True)
class TriggerConfig:
    """所有閾值集中在此。setup_name 會跟著 FIRE 訊息傳給 L3。"""
    setup_name: str                                # "intraday" | "ambush"

    # --- 方向型訊號閾值（Setup A 主用）-----------------------------------
    cvd_slope_min: float = 0.15                    # 背離成立的最低 CVD 斜率
    cvd_slope_ref: float = 0.50                    # score 正規化基準
    funding_neg_thr: float = -0.0001               # 空方付錢門檻
    funding_hot_thr: float = 0.0008                # 多殺多風險門檻
    top_trader_long_thr: float = 1.15              # 大戶轉多
    top_trader_short_thr: float = 0.87             # 大戶轉空（獨立，非 1/x）
    retail_short_thr: float = 0.90                 # 散戶偏空
    retail_long_thr: float = 1.11                  # 散戶偏多

    # --- OI 蓄勢 ----------------------------------------------------------
    oi_rise_min_pct: float = 3.0                   # 24h OI 上升 ≥ 此值才算蓄勢
    require_oi_fuel: bool = True

    # --- 閘與要求 ---------------------------------------------------------
    require_gate_open: bool = True                 # BTC 閘必開
    require_hot: bool = False                      # symbol 須在 Hot 名單
    require_trend_4h: bool = False                 # 4h 收 > 200MA

    # --- 投票政策 ---------------------------------------------------------
    min_confirmations: int = 2                     # ≥2 同向、對向 = 0 才 FIRE

    # --- 結構訊號閾值（Setup B 主用）-------------------------------------
    atr_coil_max_pct: float = 4.0                  # 7d ATR/price < 此值算 coiling
    vol_dry_max_ratio: float = 0.70                # 24h/30d 量比 < 此值算枯竭
    cvd_slope_7d_min: float = 0.05                 # 7d CVD 緩升門檻
    top_trader_slope_7d_min: float = 0.005         # 7d 大戶緩升門檻
    oi_steady_min_pct: float = -2.0                # OI 7d 穩定區間下界
    oi_steady_max_pct: float = 5.0                 # OI 7d 穩定區間上界

    # --- 風控（不影響 FIRE 判斷，給 L3/Telegram/L4 用）-------------------
    risk_per_trade_usd: float = 100.0
    default_leverage: int = 15
    tp_r_multiples: tuple[float, ...] = (1.0, 1.5, 2.0)
    sl_buffer_pct: float = 3.5                     # 止損距離（從進場價，%）
    hold_max_hours: int = 24                       # 持倉時間上限


# =============================================================================
# 觸發決策（L2 引擎輸出）
# =============================================================================
@dataclass(frozen=True)
class TriggerDecision:
    """L2 引擎輸出。FIRE → dispatcher 包成 TriggerEvent 推給 L3。"""
    action: TriggerAction
    direction: SignalState                          # BULL / BEAR / NEUTRAL
    setup_name: str
    confirmed: tuple[SignalResult, ...]             # 參與投票的訊號（含 NEUTRAL/STALE 也記）
    composite_score: float                          # 同向訊號 score 合計
    snapshot: MarketSnapshot
    reason: str                                     # 人類可讀
