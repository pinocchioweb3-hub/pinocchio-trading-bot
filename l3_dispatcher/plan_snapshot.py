"""plan_snapshot.py — 進場計畫快照（復盤引擎 L1 前置層，v56 / step1）。

為什麼存在（回應使用者的「真復盤」哲學）：
    真復盤 ≠ 結果論。要能在每筆單『平倉後』回答三件事——
      ① 這個結果的原因是什麼？
      ② 跟我們進場時的計畫一樣，還是超出計畫？
      ③ 當初預期『什麼情況會止損』，現在真止損了，是不是跟預期一樣？
    這些都需要一份『進場當下』就凍結下來的計畫＋當時的市場上下文。事後再回頭看
    K 線是污染過的（後見之明），所以必須在進場那一刻把『預期劇本／止損劇本／當下
    抓到的關鍵數據』就地存檔。本模組就是那份凍結快照的建構器。

設計鐵則（與三紅線並存）：
    • 純資料組裝，零策略數學：**不 import strength、不呼叫 evaluate / eval_cvd_divergence**。
      它只把『已經算好的計畫值』與『已經觀測到的上下文』打包成穩定 schema，不產生任何
      新訊號、不改變任何下單決策。
    • 全函式 exception-safe：build_plan_snapshot 任何環節出錯一律回 None，
      呼叫端（dispatcher / paper_journal）對 None 必須無痛降級——快照是觀測層，
      壞掉絕不能拖垮真正的出單/記帳流程。
    • context / regime 採『鍵恆在、值可空』：schema 一次宣告我們相信重要的所有維度；
      某欄在進場時是 None，本身就是訊號——代表那一刻我們沒在抓這個數據。隨引擎成長把
      欄位逐一補實，missing_context_keys 會縮短，就能回測『缺哪個數據時最容易誤判』。
"""
from __future__ import annotations

import json
import time

SCHEMA_VER = 1

# 我們相信會左右一筆單成敗的市場上下文維度（進場時凍結；現多為 None，逐步補實）。
# 順序即「復盤時想問的數據清單」——缺的就是最關鍵的待補因素。
_CONTEXT_KEYS: tuple[str, ...] = (
    "breadth_up_pct",        # 全市場上漲廣度
    "avg_funding",           # 平均資金費率
    "oi_delta_pct",          # 未平倉量變化
    "cvd_slope",             # 累積成交量差斜率
    "top_trader_ratio",      # 大戶多空比
    "btc_above_200ma_4h",    # BTC 是否站上 4h 200MA（大盤濾網）
    "whale_net",             # 巨鯨淨流向
    "wyckoff_phase",         # 威科夫階段
    "htf_aligned",           # 高時框是否同向
    "macro_confluence_score",  # 宏觀共振分數
)

# regime（行情狀態）向量骨架；step4 會把這些填實，現在先佔位。
# vol_trend 由各加密進場路徑以 vol_regime_from_atr() 統一帶入 ATR 波動分桶（見該函式說明）。
_REGIME_KEYS: tuple[str, ...] = (
    "vol_trend",             # ATR 波動分桶 extreme/high/low/unknown（趨勢方向看 oi_price_quadrant）
    "funding_state",         # 資金費率狀態
    "oi_price_quadrant",     # OI×價格象限
    "cvd_state",             # CVD 狀態
)


def vol_regime_from_atr(atr) -> str:
    """以 7 日 ATR%（波動度）把進場當下分桶成 vol_trend——復盤引擎跨進場路徑的唯一口徑。

    為什麼要共用一個函式：direct_fire / waiting_trigger / macro_deepdive 三條加密進場
    路徑都會寫 regime_at_entry.vol_trend。若各填各的語意（有人填波動分桶、有人填趨勢
    字串），同一學習欄位就混進兩種意義＝資料污染，正是本復盤引擎最該避免的坑。這裡把
    唯一真相鎖成一個純函式、門檻只此一份，三處都呼叫它，就不會再漂移。

    語意＝『波動度』而非『趨勢方向』——趨勢方向已由 oi_price_quadrant（OI×價格象限）承載。
    缺料（atr 為 None 或非數值）→ "unknown"（誠實留空，紅線③，絕不硬塞分桶）。
    門檻：≥8.0 extreme、≥5.0 high、其餘 low（與歷史 direct_fire 完全一致，行為等價）。
    """
    try:
        a = float(atr)
    except (TypeError, ValueError):
        return "unknown"
    if a >= 8.0:
        return "extreme"
    if a >= 5.0:
        return "high"
    return "low"


_ENGINE_EPOCH_MS: int | None = None


def get_engine_epoch_ms() -> int:
    """復盤引擎上線時刻（穩定的前向驗證邊界）。第一次呼叫惰性建立並寫檔，之後永遠回同值。

    L2 統計用它區分『引擎上線後即時捕捉的單』vs『歷史回補單』——只有前者算進前向
    holdout / registered hypothesis（避免拿被優化過程汙染的舊樣本宣稱顯著）。
    任何 I/O 失敗回 0（表示『上線時刻未知』，L2 視為全部歷史、不得當前向樣本）。"""
    global _ENGINE_EPOCH_MS
    if _ENGINE_EPOCH_MS is not None:
        return _ENGINE_EPOCH_MS
    try:
        from botpaths import data_dir
        f = data_dir() / "review_engine_epoch.json"
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            _ENGINE_EPOCH_MS = int(data["engine_epoch_ms"])
            return _ENGINE_EPOCH_MS
        now = int(time.time() * 1000)
        f.write_text(json.dumps({"engine_epoch_ms": now, "schema_ver": SCHEMA_VER},
                                ensure_ascii=False, indent=2), encoding="utf-8")
        _ENGINE_EPOCH_MS = now
        return now
    except Exception:
        return 0


def _safe_rr(direction: str, entry, stop, target):
    """目標相對進場的 R 倍數（風險＝|entry-stop|）。任何不合法輸入（含除以零）回 None。"""
    try:
        entry = float(entry)
        stop = float(stop)
        target = float(target)
        risk = abs(entry - stop)
        if risk <= 0:
            return None
        if direction in ("bull", "long"):
            r = (target - entry) / risk
        else:
            r = (entry - target) / risk
        return round(r, 3)
    except Exception:
        return None


def _overlay(keys: tuple[str, ...], provided) -> dict:
    """以骨架鍵建 dict（值全 None），再把 provided（dict 或 None）中已知鍵覆蓋上去。
    只認骨架內的鍵，外來雜鍵忽略——確保 schema 穩定。"""
    out = {k: None for k in keys}
    if isinstance(provided, dict):
        for k in keys:
            if k in provided and provided[k] is not None:
                out[k] = provided[k]
    return out


def build_plan_snapshot(*, source: str, direction: str,
                        entry_price, planned_stop,
                        tp1, tp2, tp3,
                        fire_id=None, signal_msg_id=None,
                        regime=None, thesis: str = "", confidence=None,
                        plan_captured_at_ms: int | None = None,
                        entry_filled_at_ms: int | None = None,
                        regime_vector=None, context=None,
                        expected_stop_scenario=None,
                        news_context=None) -> dict | None:
    """組裝一份進場計畫快照（純資料、exception-safe，失敗回 None）。

    參數：
      source                進場來源（direct_fire / macro_deepdive / us_breakout / waiting_trigger）
      direction             bull/bear（或 long/short）
      entry_price/planned_stop/tp1..tp3   已算好的計畫價位
      fire_id/signal_msg_id 回連 fire 佇列與 TG 訊息的 join key
      regime                當下 regime 字串（暫填入 regime_vector.vol_trend）
      thesis                為何進場（decision.reason）
      confidence            交叉驗證信心（若有）
      regime_vector/context 進階向量；現多為 None，step4 起逐步帶入（鍵恆在值可空）
      expected_stop_scenario 自訂止損劇本；不給則用預設（價格觸及失效價＝planned_stop）
      news_context          消息面進場觀測（task#66 Q2 Phase 0）；呼叫端用
                            news_score.capture_entry_news_context() 算好後傳入，本函式只
                            原樣打包成 news_at_entry 觀測欄。**純觀測**：絕不進 rr/expected_r/
                            score/方向判定；閘②回測未過前對任何下單數學影響嚴格為零。維持本
                            函式『純資料、不呼叫策略/不做活查詢』鐵則——活查詢在呼叫端做。
    """
    try:
        now = int(time.time() * 1000)
        cap_ms = int(plan_captured_at_ms) if plan_captured_at_ms else now

        rr = {
            "tp1": _safe_rr(direction, entry_price, planned_stop, tp1),
            "tp2": _safe_rr(direction, entry_price, planned_stop, tp2),
            "tp3": _safe_rr(direction, entry_price, planned_stop, tp3),
        }

        regime_at_entry = _overlay(_REGIME_KEYS, regime_vector)
        if regime is not None and regime_at_entry.get("vol_trend") is None:
            regime_at_entry["vol_trend"] = regime

        context_at_entry = _overlay(_CONTEXT_KEYS, context)
        missing = sorted(k for k, v in context_at_entry.items() if v is None)

        if expected_stop_scenario is None:
            expected_stop_scenario = {
                # 預設止損劇本：價格觸及『失效價』（planned_stop）即出場；預期最大不利
                # 位移不超過約 1R。復盤時比對『真實 MAE / 真實出場原因』是否吻合此劇本。
                "trigger_type": "invalidation_level",
                "trigger_level": planned_stop,
                "expected_mae_ceiling_r": 1.0,
            }

        return {
            "schema_ver": SCHEMA_VER,
            "engine_epoch_ms": get_engine_epoch_ms(),
            "source": source,
            "plan_captured_at_ms": cap_ms,
            "entry_filled_at_ms": int(entry_filled_at_ms) if entry_filled_at_ms else None,
            "join": {
                "fire_id": fire_id,
                "signal_msg_id": signal_msg_id,
                "paper_id": None,        # 由帳本寫入後回填（此刻尚未有 id）
            },
            "direction": direction,
            "thesis": thesis or "",
            "confidence": confidence,
            "expected_stop_scenario": expected_stop_scenario,
            "planned_entry": entry_price,
            "planned_stop": planned_stop,
            "planned_tp": {"tp1": tp1, "tp2": tp2, "tp3": tp3},
            "rr_to_tp": rr,
            "expected_r": rr["tp1"],     # 頭條預期 R＝到最近目標（base-case 勝）
            "regime_at_entry": regime_at_entry,
            "context_at_entry": context_at_entry,
            "missing_context_keys": missing,  # 進場當下『沒抓到的數據』＝最該回補的因素
            # task#66 Q2 Phase 0：消息面進場觀測（純觀測欄；缺料/未接 → None，誠實留空）。
            # 永不參與上面任何 rr/expected_r/方向欄——只供復盤引擎事後歸因『進場那刻消息面偏哪』。
            "news_at_entry": news_context if isinstance(news_context, dict) else None,
        }
    except Exception:
        return None
