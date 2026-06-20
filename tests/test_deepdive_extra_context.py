"""#56 復盤引擎缺料回補 #2 單元測試（純讀、確定性、紅線③守備）。

驗證 l3_dispatcher.macro 兩個新 helper，過去 deepdive 早已抓好卻丟棄的資料，
現在純資料重用地餵進進場快照影子 context（零新網路請求、缺料→省略鍵=誠實留空）：

  * _deepdive_extra_context(sym, sym_state)
      - wyckoff_phase          ← sym_state.wyckoff.phase
      - whale_net              ← per_symbol_aggregate 配對 symbol 的 net_long_pct
      - macro_confluence_score ← _read_macro_confluence_score()（市場層末行分數）
      - 缺料/壞型別 → 該鍵省略；整體例外 → 回 {}（絕不阻塞建單路徑）
  * _read_macro_confluence_score()
      - 讀 macro_confluence.jsonl 末行 macro_confluence_score
      - 檔缺/空/壞 JSON/無此鍵/非數值 → None

執行：pytest tests/test_deepdive_extra_context.py
"""
import importlib
import json

macro = importlib.import_module("l3_dispatcher.macro")

# 紅線③：context 萃取絕不可摻入績效/誘導語意（這層只搬原始觀測值）
_BANNED = ["勝率", "報酬%", "年化", "必漲", "獲利", "保證"]


# ----------------------------------------------------- _read_macro_confluence_score
def _write_mc(tmp_path, lines):
    p = tmp_path / "macro_confluence.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in lines) + "\n", encoding="utf-8")
    return p


def test_read_mc_score_reads_last_line(tmp_path, monkeypatch):
    import botpaths
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)
    _write_mc(tmp_path, [
        {"macro_confluence_score": 3.0, "ts": 1},
        {"macro_confluence_score": 7.5, "ts": 2},   # 末行優先
    ])
    assert macro._read_macro_confluence_score() == 7.5


def test_read_mc_score_int_ok(tmp_path, monkeypatch):
    import botpaths
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)
    _write_mc(tmp_path, [{"macro_confluence_score": 0}])   # 0 是合法數值，非缺料
    assert macro._read_macro_confluence_score() == 0


def test_read_mc_score_missing_file(tmp_path, monkeypatch):
    import botpaths
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)
    assert macro._read_macro_confluence_score() is None


def test_read_mc_score_empty_file(tmp_path, monkeypatch):
    import botpaths
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)
    (tmp_path / "macro_confluence.jsonl").write_text("", encoding="utf-8")
    assert macro._read_macro_confluence_score() is None


def test_read_mc_score_bad_json(tmp_path, monkeypatch):
    import botpaths
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)
    (tmp_path / "macro_confluence.jsonl").write_text("{not valid\n", encoding="utf-8")
    assert macro._read_macro_confluence_score() is None


def test_read_mc_score_missing_key(tmp_path, monkeypatch):
    import botpaths
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)
    _write_mc(tmp_path, [{"bias": "up", "ts": 1}])    # 無 macro_confluence_score 鍵
    assert macro._read_macro_confluence_score() is None


def test_read_mc_score_non_numeric(tmp_path, monkeypatch):
    import botpaths
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)
    _write_mc(tmp_path, [{"macro_confluence_score": "高"}])   # 字串非數值 → None
    assert macro._read_macro_confluence_score() is None


# --------------------------------------------------------- _deepdive_extra_context
def test_extra_context_full(monkeypatch):
    """wyckoff_phase + whale_net（配對 symbol）+ macro_confluence_score 全填。"""
    monkeypatch.setattr(macro, "_read_macro_confluence_score", lambda: 6.5)
    sym_state = {
        "wyckoff": {"phase": "accumulation"},
        "whales": {"per_symbol_aggregate": [
            {"symbol": "ETH", "net_long_pct": 12.0},
            {"symbol": "BTC", "net_long_pct": 55.5},   # 目標
        ]},
    }
    out = macro._deepdive_extra_context("BTC", sym_state)
    assert out["wyckoff_phase"] == "accumulation"
    assert out["whale_net"] == 55.5                     # 配對到 BTC，非 ETH
    assert out["macro_confluence_score"] == 6.5
    for b in _BANNED:
        assert b not in json.dumps(out, ensure_ascii=False)


def test_extra_context_whale_symbol_mismatch(monkeypatch):
    """per_symbol_aggregate 無此標的 → whale_net 省略（不亂配）。"""
    monkeypatch.setattr(macro, "_read_macro_confluence_score", lambda: None)
    sym_state = {"whales": {"per_symbol_aggregate": [
        {"symbol": "ETH", "net_long_pct": 12.0}]}}
    out = macro._deepdive_extra_context("BTC", sym_state)
    assert "whale_net" not in out
    assert "macro_confluence_score" not in out          # helper 回 None → 省略


def test_extra_context_whale_error_skipped(monkeypatch):
    """whales 帶 error → 整段跳過，不取殘值。"""
    monkeypatch.setattr(macro, "_read_macro_confluence_score", lambda: None)
    sym_state = {"whales": {"error": "rate_limited",
                            "per_symbol_aggregate": [
                                {"symbol": "BTC", "net_long_pct": 99.0}]}}
    out = macro._deepdive_extra_context("BTC", sym_state)
    assert "whale_net" not in out


def test_extra_context_whale_net_none_skipped(monkeypatch):
    """配對到 symbol 但 net_long_pct 為 None → 省略（誠實留空，不填 None 進去）。"""
    monkeypatch.setattr(macro, "_read_macro_confluence_score", lambda: None)
    sym_state = {"whales": {"per_symbol_aggregate": [
        {"symbol": "BTC", "net_long_pct": None}]}}
    out = macro._deepdive_extra_context("BTC", sym_state)
    assert "whale_net" not in out


def test_extra_context_wyckoff_only(monkeypatch):
    monkeypatch.setattr(macro, "_read_macro_confluence_score", lambda: None)
    out = macro._deepdive_extra_context("BTC", {"wyckoff": {"phase": "markup"}})
    assert out == {"wyckoff_phase": "markup"}


def test_extra_context_empty_sources(monkeypatch):
    """全缺料 → 回空 dict（assemble 不覆蓋，骨架維持 None）。"""
    monkeypatch.setattr(macro, "_read_macro_confluence_score", lambda: None)
    assert macro._deepdive_extra_context("BTC", None) == {}
    assert macro._deepdive_extra_context("BTC", {}) == {}
    assert macro._deepdive_extra_context("BTC", {"wyckoff": {}, "whales": {}}) == {}


def test_extra_context_exception_safe(monkeypatch):
    """壞型別不拋例外、回 {}（絕不影響任何 FIRE/建單路徑）。"""
    monkeypatch.setattr(macro, "_read_macro_confluence_score", lambda: None)
    # whales 非 dict、wyckoff 非 dict → 安全降級
    out = macro._deepdive_extra_context("BTC", {"wyckoff": "x", "whales": 123})
    assert isinstance(out, dict)
    assert "wyckoff_phase" not in out
    assert "whale_net" not in out


def test_extra_context_inner_raise_returns_empty(monkeypatch):
    """_read_macro_confluence_score 內部炸 → 整體仍回 {}，不外漏例外。"""
    def _boom():
        raise RuntimeError("mc down")
    monkeypatch.setattr(macro, "_read_macro_confluence_score", _boom)
    out = macro._deepdive_extra_context("BTC", {"wyckoff": {"phase": "markup"}})
    assert out == {}        # 例外被 except 攔截 → 回 {}（fail-safe）


def test_extra_context_keys_are_schema_subset(monkeypatch):
    """治本鐵則：萃取出的鍵必須全在 plan_snapshot._CONTEXT_KEYS 內，
    否則 assemble 會默默丟棄（白做工）或污染 schema。"""
    import l3_dispatcher.plan_snapshot as ps
    monkeypatch.setattr(macro, "_read_macro_confluence_score", lambda: 5.0)
    sym_state = {
        "wyckoff": {"phase": "accumulation"},
        "whales": {"per_symbol_aggregate": [{"symbol": "BTC", "net_long_pct": 10.0}]},
    }
    out = macro._deepdive_extra_context("BTC", sym_state)
    assert set(out.keys()) <= set(ps._CONTEXT_KEYS)
