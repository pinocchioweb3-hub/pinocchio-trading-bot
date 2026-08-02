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
    # v221：_read_cycle_value_zone 也要擋掉——它讀的是真實資料目錄的
    # cycle_shadow.jsonl，本機一旦有 BTC 讀數，這條「只該有 wyckoff」的斷言就會
    # 被線上資料污染而翻紅（非程式壞掉，是測試不密封）。
    monkeypatch.setattr(macro, "_read_macro_confluence_score", lambda: None)
    monkeypatch.setattr(macro, "_read_cycle_value_zone", lambda _s: None)
    out = macro._deepdive_extra_context("BTC", {"wyckoff": {"phase": "markup"}})
    assert out == {"wyckoff_phase": "markup"}


def test_extra_context_empty_sources(monkeypatch):
    """全缺料 → 回空 dict（assemble 不覆蓋，骨架維持 None）。"""
    monkeypatch.setattr(macro, "_read_macro_confluence_score", lambda: None)
    monkeypatch.setattr(macro, "_read_cycle_value_zone", lambda _s: None)  # 同上，密封
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


# ------------------------------------------- _read_macro_confluence_record（task#70）
def test_read_record_reads_last_line(tmp_path, monkeypatch):
    import botpaths
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)
    _write_mc(tmp_path, [
        {"macro_confluence_score": 3.0, "score_method": "v1", "ts": 1},
        {"macro_confluence_score": 7.5, "score_method": "v2_renorm_present_mass",
         "present_mass": 0.7, "n_present": 12, "ts": 2},   # 末行優先
    ])
    rec = macro._read_macro_confluence_record()
    assert isinstance(rec, dict)
    assert rec["macro_confluence_score"] == 7.5
    assert rec["score_method"] == "v2_renorm_present_mass"


def test_read_record_missing_file(tmp_path, monkeypatch):
    import botpaths
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)
    assert macro._read_macro_confluence_record() is None


def test_read_record_non_dict_last_line(tmp_path, monkeypatch):
    """末行是合法 JSON 但非 dict（數字/list）→ None（不讓壞型別外漏）。"""
    import botpaths
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)
    (tmp_path / "macro_confluence.jsonl").write_text("[1, 2, 3]\n", encoding="utf-8")
    assert macro._read_macro_confluence_record() is None


# ------------------------------------------- _deepdive_macro_provenance（task#70）
def test_macro_provenance_v2_full(tmp_path, monkeypatch):
    """v2 列：score_method/present_mass/n_present 全帶、present_mass≥地板→floor_bound False。"""
    import botpaths
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)
    _write_mc(tmp_path, [{"macro_confluence_score": 8.99,
                          "score_method": "v2_renorm_present_mass",
                          "present_mass": 0.70, "n_present": 12}])
    p = macro._deepdive_macro_provenance()
    assert p == {"score_method": "v2_renorm_present_mass",
                 "present_mass": 0.70, "n_present": 12, "floor_bound": False}


def test_macro_provenance_v1_fallback(tmp_path, monkeypatch):
    """舊 v1 列（只有分數、無 score_method/present_mass/n_present）→ 隱含 'v1'、其餘 None。"""
    import botpaths
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)
    _write_mc(tmp_path, [{"macro_confluence_score": 3.0, "bias": "neutral"}])
    p = macro._deepdive_macro_provenance()
    assert p == {"score_method": "v1", "present_mass": None,
                 "n_present": None, "floor_bound": None}


def test_macro_provenance_floor_bound_true(tmp_path, monkeypatch):
    """present_mass < _MIN_PRESENT_MASS（0.25）→ floor_bound True（分母觸地板、分數偏放大）。"""
    import botpaths
    from l3_dispatcher.macro_confluence import _MIN_PRESENT_MASS
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)
    _write_mc(tmp_path, [{"macro_confluence_score": 50.0,
                          "score_method": "v2_renorm_present_mass",
                          "present_mass": _MIN_PRESENT_MASS - 0.05, "n_present": 2}])
    p = macro._deepdive_macro_provenance()
    assert p["floor_bound"] is True


def test_macro_provenance_no_score_returns_none(tmp_path, monkeypatch):
    """末行無合法數值分數（缺鍵/非數值）→ None（呼叫端據此不寫 provenance 欄）。"""
    import botpaths
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)
    _write_mc(tmp_path, [{"bias": "up", "score_method": "v2_renorm_present_mass"}])
    assert macro._deepdive_macro_provenance() is None
    _write_mc(tmp_path, [{"macro_confluence_score": "高",
                          "score_method": "v2_renorm_present_mass"}])
    assert macro._deepdive_macro_provenance() is None


def test_macro_provenance_missing_file_returns_none(tmp_path, monkeypatch):
    import botpaths
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)
    assert macro._deepdive_macro_provenance() is None


def test_macro_provenance_bad_json_returns_none(tmp_path, monkeypatch):
    import botpaths
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)
    (tmp_path / "macro_confluence.jsonl").write_text("{broken\n", encoding="utf-8")
    assert macro._deepdive_macro_provenance() is None


def test_macro_provenance_non_numeric_present_mass(tmp_path, monkeypatch):
    """present_mass 壞型別（字串）→ present_mass None、floor_bound None（不臆測、不拋例外）。"""
    import botpaths
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)
    _write_mc(tmp_path, [{"macro_confluence_score": 5.0,
                          "score_method": "v2_renorm_present_mass",
                          "present_mass": "x", "n_present": "y"}])
    p = macro._deepdive_macro_provenance()
    assert p["score_method"] == "v2_renorm_present_mass"
    assert p["present_mass"] is None
    assert p["n_present"] is None
    assert p["floor_bound"] is None


# ------------------------------------------------- v114 週/月線層落快照（Fable5 稽核）
def test_extra_context_nesting_htf_cycle(monkeypatch, tmp_path):
    """tf_nesting/M2 verdict/cycle zone 全填（死端→快照）；缺料省略鍵。"""
    import json as _json
    monkeypatch.setattr(macro, "_read_macro_confluence_score", lambda: None)
    # cycle_shadow.jsonl 假資料（最新一筆含 SOL 讀數）
    shadow = tmp_path / "cycle_shadow.jsonl"
    shadow.write_text(_json.dumps({"reads": [
        {"symbol": "SOL", "value_zone": "deep_value"}]}) + "\n", encoding="utf-8")
    import botpaths as _bp
    monkeypatch.setattr(_bp, "data_dir", lambda: tmp_path)
    sym_state = {
        "tf_nesting": {
            "stage_code": "DOWN_BOUNCE", "alignment_score": 0.62,
            "divergence_tf": "1w",
            "layers": [{"tf": "1M", "direction": "down"},
                       {"tf": "1d", "direction": "up"}],
        },
        "htf_alignment": {"verdict": "conflict"},
    }
    out = macro._deepdive_extra_context("SOL", sym_state)
    assert out["nest_stage_code"] == "DOWN_BOUNCE"
    assert out["nest_alignment_pct"] == 62.0
    assert out["nest_divergence_tf"] == "1w"
    assert out["nest_1d_dir"] == "up"
    assert out["htf_verdict_1d4h"] == "conflict"
    assert out["cycle_value_zone"] == "deep_value"


def test_extra_context_nesting_missing_honest(monkeypatch):
    """全缺料 → 新鍵全部省略（骨架 None，紅線③不臆測）。"""
    monkeypatch.setattr(macro, "_read_macro_confluence_score", lambda: None)
    out = macro._deepdive_extra_context("BTC", {})
    for k in ("nest_stage_code", "nest_alignment_pct", "nest_divergence_tf",
              "nest_1d_dir", "htf_verdict_1d4h"):
        assert k not in out
