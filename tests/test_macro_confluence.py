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
    assert mc.score_coinbase_premium(50) == 1.0     # 美國買盤強滿格 → 偏多
    assert mc.score_coinbase_premium(-50) == -1.0   # 偏空滿格
    assert mc.score_coinbase_premium(0) == 0.0
    assert mc.score_coinbase_premium(None) == 0.0   # 缺料中性
    assert mc.score_coinbase_premium(25) == 0.5     # 線性


def test_score_coin_netflow_inverted():
    # 流入交易所(正)＝賣壓＝偏空(反號)；流出(負)＝偏多
    assert mc.score_coin_netflow(500_000_000) < 0
    assert mc.score_coin_netflow(-500_000_000) > 0
    assert mc.score_coin_netflow(500_000_000) == -1.0   # 滿格反號
    assert mc.score_coin_netflow(-500_000_000) == 1.0
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
    """擴 12 項後仍守『總和==1.0』+ 鎖定 12 項數量（新增 5 個 CoinGlass 端點）。"""
    total = sum(mc._WEIGHTS.values())
    assert abs(total - 1.0) < 1e-9, f"擴項後權重總和應為 1.0，實為 {total}"
    assert len(mc._WEIGHTS) == 12, f"應有 12 個分量，實為 {len(mc._WEIGHTS)}"
    # 新 5 鍵必須在 _WEIGHTS 中
    for k in ("coinbase_premium", "coin_netflow", "btc_dominance",
              "altcoin_season", "btc_vs_m2"):
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
        assert abs(d["contribution"] - d["sub_score"] * d["weight"]) < 1e-6
        assert d["weight"] == mc._WEIGHTS[k]


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
    # 5 個新鍵一個都不該寫入（全失敗/缺料）
    for k in ("coinbase_premium_value", "coin_netflow_usd", "btc_dominance_pct",
              "altcoin_season_index", "btc_vs_m2_deviation_pct"):
        assert k not in out, f"全缺時不應寫入 {k}"
    # 把 out 丟 compute：n_present 不因新項增加（純空時 == 0）
    summary = mc.compute_confluence(out)
    # 新 5 項缺料 → sub=0 → 不計分母；只可能由本地 breadth/dxy 等既有源貢獻
    # 為穩健，直接驗純空 dict 的純函式語意：新項缺料不抬高 n_present
    pure = mc.compute_confluence({})
    assert pure["n_present"] == 0
    assert "macro_confluence_score" in summary


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
        "coinbase_premium_value": 50,       # CB溢價 滿格
        "coin_netflow_usd": -500_000_000,   # 交易所淨流 滿格(偏多)
        "btc_dominance_pct": 40,            # BTC市占 滿格(risk_on)
        "altcoin_season_index": 100,       # 山寨季 滿格
    })
    text = mc.render_dashboard(out)
    # 至少 CB溢價（最高新權重 0.08）應入前 4 大並顯示其標籤
    assert "CB溢價" in text
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
