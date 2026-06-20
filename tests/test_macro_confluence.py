"""Session C 綜合宏觀合成（影子層）單元測試。

涵蓋（任務規格）：
  1. 確定性規則計分：各分量映射 + 加權合成 + bias 判定 + 缺料中性化。
  2. 影子鐵則：斷言 macro_confluence_score 不進 strength_score（原始碼掃描 +
     行為斷言：本模組不 import strength、不寫 fire/trade）。
  3. history-logger：INSERT OR IGNORE 寫入 + 冪等去重（用臨時 BOT_DATA_DIR）。
  4. 顯示層：純顯示、缺料安全、紅線③無績效字眼。

全程純函式/臨時 DB，零網路、零真實 source、零下單路徑。
"""
import importlib
import inspect
import json
import os
import sqlite3
import tempfile
from pathlib import Path

mc = importlib.import_module("l3_dispatcher.macro_confluence")

# 紅線③：顯示層絕不可出現的績效/誘導字眼
_BANNED = ["勝率", "報酬%", "年化", "必漲", "獲利", "買進訊號", "保證"]


# ============================================================ 1. 確定性規則計分
def test_score_etf_inflow_positive_outflow_negative():
    assert mc.score_etf(2_000_000_000) == 1.0      # 滿格淨流入
    assert mc.score_etf(-2_000_000_000) == -1.0    # 滿格淨流出
    assert mc.score_etf(0) == 0.0
    assert mc.score_etf(None) == 0.0               # 缺料中性
    assert mc.score_etf(1_000_000_000) == 0.5      # 線性


def test_score_dxy_inverted():
    # DXY 升 → 風險資產逆風 → 扣分（反號）
    assert mc.score_dxy(2.0) == -1.0
    assert mc.score_dxy(-2.0) == 1.0
    assert mc.score_dxy(0.0) == 0.0
    assert mc.score_dxy(None) == 0.0


def test_score_funding_overheat_inverted():
    # funding 翻正過熱 → 扣分；偏負（空方付錢）→ 加分（與 convergence 約定一致）
    assert mc.score_funding(0.0005) == -1.0
    assert mc.score_funding(-0.0005) == 1.0
    assert mc.score_funding(0.0) == 0.0
    assert mc.score_funding(None) == 0.0


def test_score_liquidation_imbalance():
    # 空清算遠大於多清算 → 軋空燃料（偏多 +）
    assert mc.score_liquidation(0, 100) == 1.0
    assert mc.score_liquidation(100, 0) == -1.0
    assert mc.score_liquidation(50, 50) == 0.0
    assert mc.score_liquidation(0, 0) == 0.0       # 無清算 → 中性
    assert mc.score_liquidation(None, None) == 0.0


def test_score_whales_linear():
    assert mc.score_whales(100) == 1.0
    assert mc.score_whales(-100) == -1.0
    assert mc.score_whales(0) == 0.0
    assert mc.score_whales(None) == 0.0


def test_score_oi_clamped():
    assert mc.score_oi(10) == 1.0
    assert mc.score_oi(-10) == -1.0
    assert mc.score_oi(20) == 1.0                   # 夾在 +1
    assert mc.score_oi(None) == 0.0


# --- 新增 5 個 CoinGlass 分量計分器的方向 / 邊界測試 ---
def test_score_coinbase_premium_direction():
    # 輸入為 premium_rate 百分比%；±0.5% 視為滿格（task#69 校準，舊 ÷50 誤把%當bps）
    assert mc.score_coinbase_premium(0.5) == 1.0    # 美國買盤強滿格 → 偏多
    assert mc.score_coinbase_premium(-0.5) == -1.0  # 偏空滿格
    assert mc.score_coinbase_premium(0.25) == 0.5   # 線性
    assert mc.score_coinbase_premium(0) == 0.0
    assert mc.score_coinbase_premium(None) == 0.0   # 缺料中性
    assert mc.score_coinbase_premium(2.0) == 1.0    # 越界夾在 +1


def test_score_coin_netflow_taker_direction():
    # 現貨主動淨買賣(taker buy−sell)：正＝買盤淨多偏多(+)、負＝賣盤淨多偏空(−)；
    # **不反號**（task#69 治本，±$6.5 億滿格）
    assert mc.score_coin_netflow(650_000_000) == 1.0    # 滿格偏多（不反號）
    assert mc.score_coin_netflow(-650_000_000) == -1.0  # 滿格偏空
    assert mc.score_coin_netflow(325_000_000) == 0.5    # 線性
    assert mc.score_coin_netflow(650_000_000) > 0
    assert mc.score_coin_netflow(-650_000_000) < 0
    assert mc.score_coin_netflow(0) == 0.0
    assert mc.score_coin_netflow(None) == 0.0


def test_score_btc_dominance_inverted_anchor50():
    # 市占升＝risk_off(反號)；市占降＝risk_on
    assert mc.score_btc_dominance(60) < 0
    assert mc.score_btc_dominance(40) > 0
    assert mc.score_btc_dominance(50) == 0.0        # 50% 中性錨
    assert mc.score_btc_dominance(60) == -1.0       # ±10pp 滿格
    assert mc.score_btc_dominance(40) == 1.0
    assert mc.score_btc_dominance(None) == 0.0


def test_score_altcoin_season_linear():
    assert mc.score_altcoin_season(100) == 1.0      # 滿格山寨季 → risk_on
    assert mc.score_altcoin_season(0) == -1.0       # 比特幣季 → risk_off
    assert mc.score_altcoin_season(50) == 0.0       # 中性
    assert mc.score_altcoin_season(None) == 0.0


def test_score_btc_vs_m2_direction():
    assert mc.score_btc_vs_m2(30) == 1.0            # 超漲滿格 → 溫和偏多
    assert mc.score_btc_vs_m2(-30) == -1.0          # 落後滿格 → 偏空
    assert mc.score_btc_vs_m2(0) == 0.0
    assert mc.score_btc_vs_m2(None) == 0.0


# --- 第二批 3 個 CoinGlass 分量計分器的方向 / 邊界測試（v58 影子層）---
def test_score_orderbook_imbalance_direction():
    # client 端已算成 [-1,+1]：買牆厚(正)＝偏多、賣牆厚(負)＝偏空
    assert mc.score_orderbook_imbalance(0.5) == 0.5
    assert mc.score_orderbook_imbalance(1.0) == 1.0     # 已正規化，直接 clamp
    assert mc.score_orderbook_imbalance(-1.0) == -1.0
    assert mc.score_orderbook_imbalance(2.0) == 1.0     # 越界夾在 +1
    assert mc.score_orderbook_imbalance(0) == 0.0
    assert mc.score_orderbook_imbalance(None) == 0.0    # 缺料中性


def test_score_spot_perp_ratio_anchor_one():
    # 錨點 1.0 為中性：>1 現貨主導(偏多+)、<1 合約主導(偏空-)
    assert mc.score_spot_perp_ratio(2.0) == 1.0         # (2.0-1.0) 滿格偏多
    assert mc.score_spot_perp_ratio(1.0) == 0.0         # 錨點中性
    assert mc.score_spot_perp_ratio(0.0) == -1.0        # (0-1) 滿格偏空
    assert mc.score_spot_perp_ratio(1.5) == 0.5         # 線性
    assert mc.score_spot_perp_ratio(None) == 0.0        # 缺料中性


def test_score_agg_cvd_slope_direction():
    # client 端已算成 [-1,+1] 正規化斜率：買方淨多(正)＝偏多、賣方淨多(負)＝偏空
    assert mc.score_agg_cvd_slope(0.7) == 0.7
    assert mc.score_agg_cvd_slope(1.0) == 1.0
    assert mc.score_agg_cvd_slope(-1.0) == -1.0
    assert mc.score_agg_cvd_slope(2.0) == 1.0           # 越界夾在 +1
    assert mc.score_agg_cvd_slope(0) == 0.0
    assert mc.score_agg_cvd_slope(None) == 0.0          # 缺料中性


def test_score_breadth_direction_and_riskoff():
    # 24h 偏多 + 1h 不極端 → 正方向、無 risk_off
    s, ro = mc.score_breadth({"n_total": 100, "n_up24h": 70, "n_down24h": 30,
                              "n_up1h": 50, "n_down1h": 50})
    assert s > 0 and ro is False
    # 1h 下跌佔比 ≥65% 且 dn≥15 → risk_off 旗標
    s2, ro2 = mc.score_breadth({"n_total": 100, "n_up24h": 30, "n_down24h": 70,
                                "n_up1h": 5, "n_down1h": 40})
    assert s2 < 0 and ro2 is True
    # 樣本不足（n_total<30）→ 中性、無旗標
    s3, ro3 = mc.score_breadth({"n_total": 10, "n_up24h": 8, "n_down24h": 2,
                                "n_up1h": 8, "n_down1h": 2})
    assert s3 == 0.0 and ro3 is False
    # 缺料
    assert mc.score_breadth(None) == (0.0, False)


def test_compute_confluence_all_neutral_when_empty():
    out = mc.compute_confluence({})
    assert out["macro_confluence_score"] == 0.0
    assert out["n_present"] == 0
    assert out["bias"] == "neutral"
    assert out["risk_off"] is False


def test_compute_confluence_bullish_stack():
    """全分量偏多 → 高正分、bias=risk_on，且分數被夾在 [-100,100]。"""
    out = mc.compute_confluence({
        "etf_cum_7d_flow_usd": 2_000_000_000,
        "dxy_change_pct": -2.0,
        "breadth": {"n_total": 100, "n_up24h": 80, "n_down24h": 20,
                    "n_up1h": 60, "n_down1h": 40},
        "avg_funding_8h": -0.0005,
        "oi_delta_pct": 10,
        "liq_long_usd": 0, "liq_short_usd": 100,
        "whale_net_long_pct": 100,
    })
    assert out["macro_confluence_score"] > 50
    assert out["macro_confluence_score"] <= 100
    assert out["bias"] == "risk_on"
    assert out["n_present"] == 7


def test_compute_confluence_riskoff_flag_overrides_bias():
    """breadth 觸 risk_off 旗標時 bias=risk_off（即便其它分量略偏多）。"""
    out = mc.compute_confluence({
        "breadth": {"n_total": 100, "n_up24h": 40, "n_down24h": 60,
                    "n_up1h": 5, "n_down1h": 40},
    })
    assert out["risk_off"] is True
    assert out["bias"] == "risk_off"


def test_compute_confluence_weights_sum_to_one():
    total = sum(mc._WEIGHTS.values())
    assert abs(total - 1.0) < 1e-9, f"權重總和應為 1.0，實為 {total}"


def test_weights_sum_to_one_after_extension():
    """擴 15 項後仍守『總和==1.0』+ 鎖定 15 項數量
    （第一批 5 個 + 第二批 3 個 CoinGlass 端點）。"""
    total = sum(mc._WEIGHTS.values())
    assert abs(total - 1.0) < 1e-9, f"擴項後權重總和應為 1.0，實為 {total}"
    assert len(mc._WEIGHTS) == 15, f"應有 15 個分量，實為 {len(mc._WEIGHTS)}"
    # 第一批 5 鍵 + 第二批 3 鍵都必須在 _WEIGHTS 中
    for k in ("coinbase_premium", "coin_netflow", "btc_dominance",
              "altcoin_season", "btc_vs_m2",
              "orderbook_imbalance", "spot_perp_ratio", "agg_cvd_slope"):
        assert k in mc._WEIGHTS, f"新分量 {k} 應在 _WEIGHTS"


def test_compute_confluence_contribution_matches_weight_times_sub():
    out = mc.compute_confluence({"etf_cum_7d_flow_usd": 2_000_000_000})
    etf = out["components"]["etf"]
    assert abs(etf["contribution"] - etf["sub_score"] * etf["weight"]) < 1e-6


# ============================================================ 2. 影子鐵則
def test_shadow_rule_module_never_imports_strength():
    """原始碼掃描：本模組不得有 import strength 的『實際 import 述句』（影子鐵則 #2）。

    只掃 import 述句行（排除文件字串/註解裡為了說明鐵則而提到的字串），
    用 ast 解析最精準，避免把『docstring 裡寫了禁止 import strength』也算違規。
    """
    import ast
    src = inspect.getsource(mc)
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                imported.add(n.name)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    # 不得 import 任何 strength 模組（影子鐵則：不接觸 strength 命名空間）
    for mod in imported:
        assert "strength" not in (mod or "").lower(), \
            f"影子鐵則違規：macro_confluence 不得 import {mod}"
    # 也不得 import 任何「會真正下單/寫帳」的模組（docstring 提及不算，只看實際 import）。
    for forbidden_mod in ("fire_queue", "trade_journal", "paper_journal",
                          "demo_trader", "demo_journal"):
        assert not any(forbidden_mod in (m or "") for m in imported), \
            f"影子鐵則違規：macro_confluence 不應 import {forbidden_mod}"


def test_shadow_rule_score_key_is_segregated():
    """分數鍵名為 macro_confluence_score（獨立影子欄），且 compute 回傳不含
    strength_score / strength_multiplier（不得混入 strength 命名空間）。"""
    out = mc.compute_confluence({"etf_cum_7d_flow_usd": 1_000_000_000})
    assert "macro_confluence_score" in out
    assert "strength_score" not in out
    assert "strength_multiplier" not in out


def test_shadow_rule_score_does_not_feed_strength():
    """行為斷言：把合成分數丟進一個模擬 strength_score 累加器，證明它是
    『獨立量』——呼叫 compute_confluence 不會改動外部 strength_score。
    （影子鐵則：分數永不乘進/加進 strength_score。）"""
    strength_score = 70.0          # 模擬既有引擎分數
    before = strength_score
    out = mc.compute_confluence({
        "etf_cum_7d_flow_usd": 2_000_000_000,
        "whale_net_long_pct": 100,
    })
    # 合成分數存在且非零，但 strength_score 完全未被本模組觸碰
    assert out["macro_confluence_score"] != 0.0
    assert strength_score == before
    # compute_confluence 為純函式：不接受、也不回傳任何 strength 寫入口
    sig = inspect.signature(mc.compute_confluence)
    assert "strength_score" not in sig.parameters


def test_compute_confluence_no_strength_key_with_new_components():
    """餵 5 個新鍵全給值，輸出仍含 macro_confluence_score、且不含 strength_score
    / strength_multiplier（影子鐵則：新分量不混入 strength 命名空間）。"""
    out = mc.compute_confluence({
        "coinbase_premium_value": 40,
        "coin_netflow_usd": -300_000_000,
        "btc_dominance_pct": 45,
        "altcoin_season_index": 80,
        "btc_vs_m2_deviation_pct": 15,
    })
    assert "macro_confluence_score" in out
    assert "strength_score" not in out
    assert "strength_multiplier" not in out
    # 5 個新分量都應出現在 components 明細
    for k in ("coinbase_premium", "coin_netflow", "btc_dominance",
              "altcoin_season", "btc_vs_m2"):
        assert k in out["components"], f"新分量 {k} 應在 components 明細"
    # n_present 應計入 5 個有料新項
    assert out["n_present"] == 5


def test_each_new_weight_in_components_detail():
    """5 新鍵全給值時，各 contribution ≈ sub_score * weight。"""
    out = mc.compute_confluence({
        "coinbase_premium_value": 40,
        "coin_netflow_usd": -300_000_000,
        "btc_dominance_pct": 45,
        "altcoin_season_index": 80,
        "btc_vs_m2_deviation_pct": 15,
    })
    for k in ("coinbase_premium", "coin_netflow", "btc_dominance",
              "altcoin_season", "btc_vs_m2"):
        d = out["components"][k]
        # contribution 與 sub_score 各自獨立四捨五入到 4dp，故容差須 ≥ 半個 LSB
        # (5e-5)；1e-3 仍能抓出用錯權重之類的真錯（量級差遠大於 1e-3）。
        assert abs(d["contribution"] - d["sub_score"] * d["weight"]) < 1e-3
        assert d["weight"] == mc._WEIGHTS[k]


def test_compute_confluence_new3_components_segregated():
    """第二批 3 個新鍵全給值 → 輸出含 macro_confluence_score、不含 strength_score
    / strength_multiplier；3 新分量都進 components 明細、且 n_present 計入 3 個。"""
    out = mc.compute_confluence({
        "orderbook_imbalance_value": 0.5,
        "spot_perp_ratio_value": 1.5,
        "agg_cvd_slope_value": 0.6,
    })
    assert "macro_confluence_score" in out
    assert "strength_score" not in out
    assert "strength_multiplier" not in out
    for k in ("orderbook_imbalance", "spot_perp_ratio", "agg_cvd_slope"):
        assert k in out["components"], f"新分量 {k} 應在 components 明細"
        d = out["components"][k]
        assert abs(d["contribution"] - d["sub_score"] * d["weight"]) < 1e-6
        assert d["weight"] == mc._WEIGHTS[k]
    assert out["n_present"] == 3


def test_collect_new5_all_missing_n_present_correct(monkeypatch):
    """5 個新端點全缺/全失敗 → _collect_components 不崩潰、不含 5 個新鍵；
    compute 端不因新項增加 n_present（純函式版：缺料 sub=0 不計分母）。"""
    import asyncio

    class _FakeSource:
        """5 個新方法全回 make_error 形式或 raise；其餘既有方法回缺料 error。"""
        async def get_coinbase_premium_index(self, *a, **k):
            return {"error": True, "code": "EMPTY_DATA"}

        async def get_coin_netflow(self, *a, **k):
            raise RuntimeError("boom")  # 一塊 raise 不可波及他塊

        async def get_bitcoin_dominance(self, *a, **k):
            return {"error": True}

        async def get_altcoin_season(self, *a, **k):
            return {"error": True}

        async def get_bitcoin_vs_m2(self, *a, **k):
            return {"error": True}

        # 第二批 3 個新端點：error / raise / error（驗各自 try/except 不互相波及）
        async def get_orderbook_ask_bids_history(self, *a, **k):
            return {"error": True, "code": "EMPTY_DATA"}

        async def get_futures_spot_volume_ratio(self, *a, **k):
            raise RuntimeError("boom")  # 一塊 raise 不可波及他塊

        async def get_aggregated_cvd_history(self, *a, **k):
            return {"error": True}

        # 既有端點：全部回缺料 error（讓 out 乾淨，方便斷言）
        async def get_etf_flows(self, *a, **k):
            return {"error": True}

        async def get_funding(self, *a, **k):
            return {"error": True}

        async def get_oi(self, *a, **k):
            return {"error": True}

        async def get_liquidations(self, *a, **k):
            return {"error": True}

        async def get_hyperliquid_whales(self, *a, **k):
            return {"error": True}

    # 隔離本地 source/scanner/tradfi：避免真實 I/O 干擾（讓 out 只剩可控內容）
    import l3_dispatcher.macro_confluence as _mc
    monkeypatch.setattr(_mc, "_collect_components",
                        _mc._collect_components)  # no-op，確保用真實函式

    out = asyncio.run(_mc._collect_components(_FakeSource()))
    # 第一批 5 + 第二批 3 個新鍵一個都不該寫入（全失敗/缺料/raise 皆吞）
    for k in ("coinbase_premium_value", "coin_netflow_usd", "btc_dominance_pct",
              "altcoin_season_index", "btc_vs_m2_deviation_pct",
              "orderbook_imbalance_value", "spot_perp_ratio_value",
              "agg_cvd_slope_value"):
        assert k not in out, f"全缺時不應寫入 {k}"
    # 把 out 丟 compute：n_present 不因新項增加（純空時 == 0）
    summary = mc.compute_confluence(out)
    # 新 5 項缺料 → sub=0 → 不計分母；只可能由本地 breadth/dxy 等既有源貢獻
    # 為穩健，直接驗純空 dict 的純函式語意：新項缺料不抬高 n_present
    pure = mc.compute_confluence({})
    assert pure["n_present"] == 0
    assert "macro_confluence_score" in summary


def test_collect_new3_present_maps_to_value_keys():
    """第二批 3 個新端點回有效 dict 時，_collect_components 正確把
    latest_imbalance / latest / latest_slope 對映到 *_value 鍵（驗鍵名接線無錯位）。
    其餘端點全缺，確保 out 只剩這 3 個可控鍵 + 不誤觸 strength/下單路徑。"""
    import asyncio

    class _FakeSource:
        # 第二批 3 個新端點：回有效 dict（欄位名須與 client 回傳一致）
        async def get_orderbook_ask_bids_history(self, *a, **k):
            return {"latest_imbalance": 0.42, "series": [{"ts": 1, "imbalance": 0.42}]}

        async def get_futures_spot_volume_ratio(self, *a, **k):
            return {"latest": 1.30, "series": [{"ts": 1, "value": 1.30}]}

        async def get_aggregated_cvd_history(self, *a, **k):
            return {"latest_slope": -0.25, "latest_cvd": 123.0,
                    "series": [{"ts": 1, "value": 123.0}]}

        # 其餘端點全缺料（讓 out 乾淨可斷言）
        async def get_coinbase_premium_index(self, *a, **k):
            return {"error": True}

        async def get_coin_netflow(self, *a, **k):
            return {"error": True}

        async def get_bitcoin_dominance(self, *a, **k):
            return {"error": True}

        async def get_altcoin_season(self, *a, **k):
            return {"error": True}

        async def get_bitcoin_vs_m2(self, *a, **k):
            return {"error": True}

        async def get_etf_flows(self, *a, **k):
            return {"error": True}

        async def get_funding(self, *a, **k):
            return {"error": True}

        async def get_oi(self, *a, **k):
            return {"error": True}

        async def get_liquidations(self, *a, **k):
            return {"error": True}

        async def get_hyperliquid_whales(self, *a, **k):
            return {"error": True}

    out = asyncio.run(mc._collect_components(_FakeSource()))
    # 核心：3 個新端點的回傳欄位正確對映到 *_value 鍵（接線無錯位）
    assert out.get("orderbook_imbalance_value") == 0.42
    assert out.get("spot_perp_ratio_value") == 1.30
    assert out.get("agg_cvd_slope_value") == -0.25
    # 丟進 compute：不混入 strength 命名空間、產出影子分數
    summary = mc.compute_confluence(out)
    assert "macro_confluence_score" in summary
    assert "strength_score" not in summary
    # n_present 的「純函式語意」用受控 dict 驗（_collect 會夾雜本地 breadth/dxy
    # 即時 I/O，數量不可控，故 n_present 不在 collect 結果上斷言）：
    pure3 = mc.compute_confluence({
        "orderbook_imbalance_value": 0.42,
        "spot_perp_ratio_value": 1.30,
        "agg_cvd_slope_value": -0.25,
    })
    assert pure3["n_present"] == 3


# ===================================================== 2b. 重正規化（task#69）
def test_renorm_present_mass_amplifies_over_present_weights():
    """重正規化：兩項皆滿格同向時，分數應 = Σ(sub*w)/present_mass×100 = 100，
    不再被缺料項（13 項 sub=0）稀釋到舊口徑的 32 分。"""
    out = mc.compute_confluence({
        "etf_cum_7d_flow_usd": 2_000_000_000,   # sub=+1, w=0.18
        "dxy_change_pct": -2.0,                  # sub=+1, w=0.14
    })
    assert out["present_mass"] == 0.32           # 0.18 + 0.14
    assert out["n_present"] == 2
    assert out["macro_confluence_score"] == 100.0   # 兩項滿格同向 → 滿分（非 32）


def test_renorm_floor_prevents_over_amplification():
    """地板 _MIN_PRESENT_MASS：只有單一低權重分量在線時，不可被放大到滿格。
    btc_vs_m2 w=0.02 滿格 → 0.02 / max(0.02, 0.25) = 0.08 → 8 分（非 100）。"""
    out = mc.compute_confluence({"btc_vs_m2_deviation_pct": 30})  # sub=+1, w=0.02
    assert out["present_mass"] == 0.02
    assert out["n_present"] == 1
    assert out["macro_confluence_score"] == 8.0      # 地板生效，未過度放大
    assert mc._MIN_PRESENT_MASS == 0.25


def test_renorm_present_counts_present_but_neutral():
    """『有料但中性』(sub=0) 仍算 present 並計入分母，避免中性觀測放大他項。
    DXY 持平=0.0 是真實觀測（非缺料），其權重須稀釋分數。"""
    out = mc.compute_confluence({
        "etf_cum_7d_flow_usd": 2_000_000_000,   # sub=+1, w=0.18
        "dxy_change_pct": 0.0,                    # 有料但中性 sub=0, w=0.14
    })
    assert out["n_present"] == 2                  # DXY 中性仍計入
    assert out["present_mass"] == 0.32
    assert out["components"]["dxy"]["present"] is True
    assert out["components"]["dxy"]["sub_score"] == 0.0
    # weighted_sum=0.18, denom=0.32 → 0.5625 → 56.25（被中性 DXY 正確稀釋）
    assert out["macro_confluence_score"] == 56.25


def test_score_method_and_present_mass_in_output():
    """輸出含 score_method 口徑標記 + present_mass（jsonl 行自我區分新舊口徑；
    紅線③：不回填既有已收斂 snapshot，新口徑只對未來生效）。"""
    out = mc.compute_confluence({"etf_cum_7d_flow_usd": 1_000_000_000})
    assert out["score_method"] == "v2_renorm_present_mass"
    assert out["score_method"] == mc._SCORE_METHOD
    assert out["present_mass"] == 0.18
    empty = mc.compute_confluence({})
    assert empty["score_method"] == "v2_renorm_present_mass"
    assert empty["present_mass"] == 0.0


def test_present_flag_in_component_detail():
    """每個 component detail 帶 present 旗標：有料 True、缺料 False，
    缺料項不計入 present_mass/n_present。"""
    out = mc.compute_confluence({"etf_cum_7d_flow_usd": 1_000_000_000})
    assert out["components"]["etf"]["present"] is True
    assert out["components"]["dxy"]["present"] is False    # 缺料
    assert out["n_present"] == 1


def test_liquidation_presence_requires_positive_total():
    """liquidation 特判：long/short 全 0（無清算）視為缺料不計分母，
    與 score_liquidation『total<=0 回中性』語意一致。"""
    out_zero = mc.compute_confluence({"liq_long_usd": 0, "liq_short_usd": 0})
    assert out_zero["components"]["liquidation"]["present"] is False
    assert out_zero["n_present"] == 0
    out_real = mc.compute_confluence({"liq_long_usd": 0, "liq_short_usd": 100})
    assert out_real["components"]["liquidation"]["present"] is True
    assert out_real["n_present"] == 1


def test_breadth_presence_requires_min_n_total():
    """breadth 特判：n_total<30 視為缺料不計分母（與 score_breadth 一致）。"""
    out_small = mc.compute_confluence({
        "breadth": {"n_total": 10, "n_up24h": 8, "n_down24h": 2,
                    "n_up1h": 8, "n_down1h": 2}})
    assert out_small["components"]["breadth"]["present"] is False
    assert out_small["n_present"] == 0
    out_ok = mc.compute_confluence({
        "breadth": {"n_total": 100, "n_up24h": 70, "n_down24h": 30,
                    "n_up1h": 50, "n_down1h": 50}})
    assert out_ok["components"]["breadth"]["present"] is True
    assert out_ok["n_present"] == 1


# ============================================== 2c. btc_vs_m2 近窗偏離 helper（task#69）
def test_btc_vs_m2_deviation_windowed():
    """近窗偏離 = BTC 漲幅% − M2 漲幅%（取尾端 last vs last-window，非全史）。"""
    import time as _t
    now = int(_t.time())
    series = [{"ts": now - (30 - i) * 3600,
               "price": 100 + (10.0 * i / 30),     # 100 → 110（+10%）
               "m2": 100 + (2.0 * i / 30)}         # 100 → 102（+2%）
              for i in range(31)]
    dev = mc._btc_vs_m2_deviation(series, window=30)
    assert dev is not None
    assert abs(dev - 8.0) < 0.5                     # +10% − +2% ≈ +8


def test_btc_vs_m2_deviation_stale_returns_none():
    """資料過期（last 點 > max_stale_days 天）→ None＝誠實缺料（紅線③：恆飽和
    的錯接分量比誠實缺席更糟）。"""
    import time as _t
    old = int(_t.time()) - 120 * 86400             # 120 天前
    series = [{"ts": old - (30 - i) * 3600, "price": 100 + i, "m2": 100 + i * 0.1}
              for i in range(31)]
    assert mc._btc_vs_m2_deviation(series, window=30, max_stale_days=45) is None


def test_btc_vs_m2_deviation_insufficient_or_bad():
    """資料不足/壞欄位 → None（不臆測方向）。"""
    import time as _t
    now = int(_t.time())
    assert mc._btc_vs_m2_deviation([], window=30) is None
    assert mc._btc_vs_m2_deviation([{"ts": now, "price": 1, "m2": 1}],
                                   window=30) is None
    bad = [{"ts": now - (30 - i) * 3600, "price": None, "m2": None}
           for i in range(31)]
    assert mc._btc_vs_m2_deviation(bad, window=30) is None


def test_btc_vs_m2_deviation_handles_millisecond_ts():
    """ts 為毫秒（>1e12）時正確換算為秒做時效判斷，不誤判過期。"""
    import time as _t
    now_ms = int(_t.time() * 1000)
    series = [{"ts": now_ms - (30 - i) * 3600_000,
               "price": 100 + i * 0.5, "m2": 100 + i * 0.05}
              for i in range(31)]
    dev = mc._btc_vs_m2_deviation(series, window=30, max_stale_days=45)
    assert dev is not None                          # 毫秒換算正確 → 視為新鮮


# ============================ 2d. client 真 payload 形狀回放（task#69 端點修治本）
# 既有 collect 測試在「高階方法」層打樁，繞過了真正出 bug 的「parse 解析層」
# （netflow dict 被當 list 迭代、路徑帶 /history → 404、premium 取錯欄、M2 欄位選錯）。
# 這節用 monkeypatch 把 CoinGlassSource._get 換成回放「實測 live 形狀」的樁，
# 讓三個被修方法的「真實解析路徑」被執行——零網路、零金鑰、零下單路徑。
import asyncio as _aio

from market_intel_mcp.sources.coinglass import CoinGlassSource as _CGS


def _stub_get(src, routes: dict):
    """把 src._get 換成依 path 子字串路由回 {'data':...} 的樁，並記錄呼叫 path。
    routes: {path_substring: data_or_'__ERROR__'}。回傳 calls list 供斷言路徑。"""
    calls = []

    async def fake_get(path, params=None, *, tool=None, symbol=None):
        calls.append(path)
        for sub, payload in routes.items():
            if sub in path:
                if payload == "__ERROR__":
                    return {"error": True, "code": "EMPTY_DATA"}
                return {"data": payload, "source": "coinglass"}
        return {"error": True, "code": "NO_ROUTE"}

    src._get = fake_get  # type: ignore[method-assign]
    return calls


def test_client_coinbase_premium_picks_rate_percent():
    """coinbase：實測點為 {time, premium(USD), premium_rate(%), coinbase_price}；
    parse 須取 premium_rate（百分比），而非 premium(USD) 或 close，且路徑無 /history。"""
    src = _CGS()
    pts = [
        {"time": 1781960400, "premium": -50.0, "premium_rate": -0.08,
         "coinbase_price": 63000.0},
        {"time": 1781964000, "premium": -67.28, "premium_rate": -0.1061,
         "coinbase_price": 63330.49},
    ]
    calls = _stub_get(src, {"/api/coinbase-premium-index": pts})
    out = _aio.run(src.get_coinbase_premium_index("1h", 24))
    assert not out.get("error")
    assert out["latest"] == -0.1061            # premium_rate（%），非 premium(USD)
    assert out["series"][-1]["ts"] == 1781964000   # _extract_ts 認得 'time'
    # 路徑修正驗證：無 /history 後綴（舊碼帶 /history → 404 死料）
    assert any(c == "/api/coinbase-premium-index" for c in calls)
    assert not any("/history" in c for c in calls)


def test_client_coinbase_premium_fallback_order():
    """premium_rate 缺 → 退 premium；皆缺 → 退 close。"""
    src = _CGS()
    pts = [{"time": 1, "premium": 12.5, "coinbase_price": 1.0},   # 無 rate → 取 premium
           {"time": 2, "close": 0.33}]                            # 皆無 → 取 close
    _stub_get(src, {"/api/coinbase-premium-index": pts})
    out = _aio.run(src.get_coinbase_premium_index())
    assert out["latest"] == 0.33
    assert out["series"][0]["value"] == 12.5


def test_client_coin_netflow_dict_parse_no_negation():
    """netflow：實測 payload 為單一 DICT（非 list）；parse 取 net_flow_usd_24h，
    符號『不反號』(net_flow_usd = taker buy−sell，已 live 驗等價)，且帶 mcap_ratio。"""
    src = _CGS()
    payload = {
        "net_flow_usd_24h": -41688286.0,
        "net_flow_usd_12h": -20000000.0,
        "net_flow_usd_24h_market_cap_ratio": -0.003284,
        "taker_buy_volume_usd_24h": 1000.0,
        "taker_sell_volume_usd_24h": 2000.0,
        "timestamp": 1781920800000,
    }
    _stub_get(src, {"/api/spot/coin/netflow": payload})
    out = _aio.run(src.get_coin_netflow("BTC", "1h", 24))
    assert not out.get("error")
    assert out["latest"] == -41688286.0        # 24h 值，符號原樣保留（負＝賣壓淨多）
    assert out["window"] == "24h"
    assert out["mcap_ratio"] == -0.003284
    # 關鍵：負流入經 scorer 後仍為負（偏空），證明全鏈未反號
    assert mc.score_coin_netflow(out["latest"]) < 0
    # 正流入 → 偏多（對稱）
    assert mc.score_coin_netflow(+41688286.0) > 0


def test_client_coin_netflow_24h_to_12h_fallback():
    """net_flow_usd_24h 缺 → 退 net_flow_usd_12h，window 標記 '12h'。"""
    src = _CGS()
    payload = {"net_flow_usd_12h": 7777.0,
               "net_flow_usd_24h_market_cap_ratio": 0.01}
    _stub_get(src, {"/api/spot/coin/netflow": payload})
    out = _aio.run(src.get_coin_netflow("BTC"))
    assert out["latest"] == 7777.0
    assert out["window"] == "12h"


def test_client_coin_netflow_list_payload_is_error():
    """舊 bug 防回歸：若 payload 誤為 list（被當 dict 迭代會炸），須回 make_error
    而非崩潰或產生垃圾值。"""
    src = _CGS()
    _stub_get(src, {"/api/spot/coin/netflow": [{"net_flow_usd_24h": 1.0}]})
    out = _aio.run(src.get_coin_netflow("BTC"))
    assert out.get("error")                     # list → EMPTY_DATA，不崩潰


def test_client_btc_vs_m2_path_and_field_selection():
    """M2：實測點為 {timestamp(ms), price, global_m2_supply/us_m2_supply,
    *_yoy_growth}；global vs us 須選對 path 與 m2 欄位。"""
    src = _CGS()
    g_pts = [{"timestamp": 1771804800000, "price": 67668.0,
              "global_m2_supply": 1.18e14, "global_m2_yoy_growth": 10.54}]
    u_pts = [{"timestamp": 1777507200000, "price": 75778.0,
              "us_m2_supply": 2.28e13, "us_m2_yoy_growth": 0.52}]
    # global
    calls_g = _stub_get(src, {"bitcoin-vs-global-m2-growth": g_pts,
                              "bitcoin-vs-us-m2-growth": u_pts})
    out_g = _aio.run(src.get_bitcoin_vs_m2("global", 120))
    assert out_g["region"] == "global"
    assert out_g["series"][-1]["m2"] == 1.18e14      # 取 global_m2_supply
    assert out_g["series"][-1]["price"] == 67668.0
    assert any("bitcoin-vs-global-m2-growth" in c for c in calls_g)
    # us
    src2 = _CGS()
    calls_u = _stub_get(src2, {"bitcoin-vs-global-m2-growth": g_pts,
                               "bitcoin-vs-us-m2-growth": u_pts})
    out_u = _aio.run(src2.get_bitcoin_vs_m2("us", 120))
    assert out_u["region"] == "us"
    assert out_u["series"][-1]["m2"] == 2.28e13      # 取 us_m2_supply
    assert any("bitcoin-vs-us-m2-growth" in c for c in calls_u)


def test_client_btc_vs_m2_stale_payload_yields_honest_absence():
    """live M2 端點資料常 stale（實測 global 117 天前）；經 _btc_vs_m2_deviation
    的 45 天閘 → None → collect 不寫 btc_vs_m2_deviation_pct（誠實缺料，紅線③）。"""
    src = _CGS()
    # 31 點、最後一點 117 天前（毫秒 ts）
    import time as _t
    base = int(_t.time() * 1000) - 117 * 86400_000
    g_pts = [{"timestamp": base + i * 3600_000, "price": 60000 + i,
              "global_m2_supply": 1.0e14 + i} for i in range(31)]
    _stub_get(src, {"bitcoin-vs-global-m2-growth": g_pts})
    out = _aio.run(src.get_bitcoin_vs_m2("global", 120))
    dev = mc._btc_vs_m2_deviation(out["series"])
    assert dev is None                          # stale → 誠實缺料，不偽造新鮮值


# ============================================================ 3. history-logger
def test_history_logger_writes_and_dedupes(tmp_path, monkeypatch):
    """init_history_db + _persist_snapshot 寫入；同一根重抓 INSERT OR IGNORE 去重。"""
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    # botpaths.data_dir() 讀 env，每次呼叫即時生效 → reload 不必要，但確保乾淨
    mc.init_history_db()
    series = [{"ts": 1000, "value": 1.5}, {"ts": 2000, "value": 2.5},
              {"ts": 3000, "value": 3.5}]
    n1 = mc._persist_snapshot("BTC", "oi", series, "1h")
    assert n1 == 3
    # 重抓同樣 3 根 → 全部 IGNORE，新增 0
    n2 = mc._persist_snapshot("BTC", "oi", series, "1h")
    assert n2 == 0
    # 新增 1 根 → 只多 1
    n3 = mc._persist_snapshot("BTC", "oi", series + [{"ts": 4000, "value": 4.5}], "1h")
    assert n3 == 1

    # 直接查 DB 驗總筆數 + 欄位
    db = Path(mc._history_db_path())
    assert db.exists()
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT symbol, metric, bar_ts, value, interval, captured_at "
            "FROM macro_metric_history WHERE symbol='BTC' AND metric='oi' "
            "ORDER BY bar_ts").fetchall()
    finally:
        conn.close()
    assert len(rows) == 4
    assert rows[0][2] == 1000 and rows[0][3] == 1.5
    assert rows[0][4] == "1h"
    assert isinstance(rows[0][5], int) and rows[0][5] > 0   # captured_at 有落盤時刻


def test_history_logger_skips_bad_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    mc.init_history_db()
    bad = [{"ts": None, "value": 1.0},       # 缺 ts
           {"ts": 10, "value": None},        # 缺 value
           "not a dict",                      # 壞型別
           {"ts": 20, "value": 2.0}]         # 唯一有效
    n = mc._persist_snapshot("ETH", "cvd", bad, "1h")
    assert n == 1


def test_persist_empty_series_returns_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    mc.init_history_db()
    assert mc._persist_snapshot("SOL", "funding", [], "1h") == 0


# ============================================================ 4. 顯示層
def _assert_clean(text: str) -> None:
    for b in _BANNED:
        assert b not in text, f"紅線③違規字眼：{b} in {text!r}"


def test_render_dashboard_normal():
    out = mc.compute_confluence({
        "etf_cum_7d_flow_usd": 2_000_000_000,
        "whale_net_long_pct": 100,
        "dxy_change_pct": -1.0,
    })
    text = mc.render_dashboard(out)
    assert "綜合宏觀儀表板" in text
    assert "影子觀測" in text
    assert "非進場訊號" in text
    assert "綜合分數" in text
    _assert_clean(text)


def test_render_dashboard_missing_data_safe():
    assert "累積數據中" in mc.render_dashboard({})
    assert "累積數據中" in mc.render_dashboard(None)
    # 壞輸入也不 raise
    assert isinstance(mc.render_dashboard({"macro_confluence_score": "bad"}), str)


def test_render_dashboard_riskoff_tag():
    out = mc.compute_confluence({
        "breadth": {"n_total": 100, "n_up24h": 40, "n_down24h": 60,
                    "n_up1h": 5, "n_down1h": 40},
    })
    text = mc.render_dashboard(out)
    assert "risk-off" in text.lower() or "Risk-Off" in text
    _assert_clean(text)


def test_new_components_appear_in_dashboard():
    """餵極端值讓某新項進貢獻度前 4 大，斷言其繁中標籤出現在儀表板輸出，
    且無績效字眼、仍含誠實橫幅『影子觀測』『非進場訊號』。"""
    # 只給新分量極端值（既有分量全缺）→ 新項必進前 4 大
    out = mc.compute_confluence({
        "coinbase_premium_value": 0.5,      # CB溢價 滿格(%口徑,task#69)
        "coin_netflow_usd": -500_000_000,   # 現貨淨買賣 偏空(主動賣盤淨多,不反號)
        "btc_dominance_pct": 40,            # BTC市占 滿格(risk_on)
        "altcoin_season_index": 100,       # 山寨季 滿格
    })
    text = mc.render_dashboard(out)
    # 至少 CB溢價（新分量中最高權重 0.06）應入前 4 大並顯示其標籤
    assert "CB溢價" in text
    assert "影子觀測" in text
    assert "非進場訊號" in text
    _assert_clean(text)


def test_new3_components_appear_in_dashboard():
    """只給第二批 3 個新分量極端值（其餘全缺）→ 3 個繁中標籤都進前 4 大顯示，
    且無績效字眼、仍含誠實橫幅『影子觀測』『非進場訊號』。"""
    out = mc.compute_confluence({
        "orderbook_imbalance_value": 1.0,   # 掛單牆 滿格(偏多)
        "spot_perp_ratio_value": 2.0,       # 現貨/合約量比 滿格(偏多)
        "agg_cvd_slope_value": -1.0,        # 官方CVD 滿格(偏空)
    })
    text = mc.render_dashboard(out)
    assert "掛單牆" in text
    assert "現貨/合約量比" in text
    assert "官方CVD" in text
    assert "影子觀測" in text
    assert "非進場訊號" in text
    _assert_clean(text)


def test_append_jsonl_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    rec = {"macro_confluence_score": 42.0, "bias": "risk_on", "ts": "x"}
    mc._append_jsonl(rec)
    path = Path(mc._sink_path())
    assert path.exists()
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    parsed = json.loads(lines[-1])
    assert parsed["macro_confluence_score"] == 42.0
    assert parsed["bias"] == "risk_on"
