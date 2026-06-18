"""#33/#34 影子觀測『deepdive 顯示層』單元測試（純讀、確定性、紅線③守備）。

驗證 l3_dispatcher.macro 的三個影子顯示 helper：
  * 正常輸入 → 含階段/焦點幣且帶『觀察中』
  * 缺料/壞輸入 → 回空字串（絕不中斷 deepdive 發送）
  * 絕不出現績效字眼（勝率/報酬%/年化/買進訊號…）＝紅線③
  * #33 焦點橫幅：只列 triple_present、依 convergence_score 由高到低、只讀最後一輪
"""
import importlib
import json

macro = importlib.import_module("l3_dispatcher.macro")

# 紅線③：顯示層絕不可出現的績效/誘導字眼
_BANNED = ["勝率", "報酬%", "年化", "必漲", "獲利", "買進訊號", "保證"]


def _assert_clean(text: str) -> None:
    for b in _BANNED:
        assert b not in text, f"紅線③違規字眼出現：{b} in {text!r}"


# ---------------------------------------------------------------- #34 階段行
def test_tf_nesting_line_normal():
    line = macro._shadow_tf_nesting_line({
        "tf_nesting": {
            "layer_count": 7, "stage_label": "上升擴張",
            "alignment_score": 0.714,
            "trade_side": {"side": "right"},
            "false_break": {"is_false_break": False},
        }
    })
    assert "上升擴張" in line
    assert "7層" in line
    assert "對齊71%" in line
    assert "右側順勢" in line
    assert "觀察中" in line
    assert "僅OKX原生時框" in line
    _assert_clean(line)


def test_tf_nesting_line_false_break_flag():
    line = macro._shadow_tf_nesting_line({
        "tf_nesting": {
            "layer_count": 5, "stage_label": "派發見頂",
            "alignment_score": 0.4,
            "trade_side": {"side": "neutral"},
            "false_break": {"is_false_break": True, "confidence": 0.8},
        }
    })
    assert "疑似假突破" in line
    assert "派發見頂" in line
    _assert_clean(line)


def test_tf_nesting_line_empty_inputs():
    assert macro._shadow_tf_nesting_line({}) == ""
    assert macro._shadow_tf_nesting_line({"tf_nesting": {}}) == ""
    assert macro._shadow_tf_nesting_line(
        {"tf_nesting": {"layer_count": 0, "stage_label": "盤整"}}) == ""
    assert macro._shadow_tf_nesting_line({"tf_nesting": None}) == ""
    # 壞型別不崩、回空
    assert macro._shadow_tf_nesting_line({"tf_nesting": "x"}) == ""


# ---------------------------------------------------------------- #33 焦點橫幅
def test_convergence_focus_line_reads_last_record(tmp_path, monkeypatch):
    import botpaths
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)
    recs = [
        {"focus": [{"symbol": "OLD", "triple_present": True, "convergence_score": 0.9}]},
        {"focus": [
            {"symbol": "BTC", "triple_present": True, "convergence_score": 0.8},
            {"symbol": "ETH", "triple_present": True, "convergence_score": 0.95},
            {"symbol": "DOGE", "triple_present": False, "convergence_score": 0.99},
        ]},
    ]
    p = tmp_path / "convergence_shadow.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    line = macro._shadow_convergence_focus_line()
    assert "ETH" in line and "BTC" in line       # 兩個 triple_present 都在
    assert "DOGE" not in line                     # triple_present=False 被濾掉
    assert "OLD" not in line                      # 只讀最後一輪、不混入上一輪
    assert line.index("ETH") < line.index("BTC")  # convergence_score 由高到低
    assert ("觀察中" in line) or ("參考" in line)
    _assert_clean(line)


def test_convergence_focus_line_no_triple(tmp_path, monkeypatch):
    import botpaths
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)
    p = tmp_path / "convergence_shadow.jsonl"
    p.write_text(json.dumps(
        {"focus": [{"symbol": "X", "triple_present": False, "convergence_score": 1.0}]}
    ) + "\n", encoding="utf-8")
    assert macro._shadow_convergence_focus_line() == ""


def test_convergence_focus_line_missing_file(tmp_path, monkeypatch):
    import botpaths
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)
    assert macro._shadow_convergence_focus_line() == ""


def test_convergence_focus_line_bad_json(tmp_path, monkeypatch):
    import botpaths
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)
    p = tmp_path / "convergence_shadow.jsonl"
    p.write_text("{not valid json\n", encoding="utf-8")
    assert macro._shadow_convergence_focus_line() == ""


# ---------------------------------------------------------------- 組合
def test_observe_prefix_combines_and_never_raises(tmp_path, monkeypatch):
    import botpaths
    monkeypatch.setattr(botpaths, "data_dir", lambda: tmp_path)  # 無 JSONL → 焦點行空
    out = macro._shadow_observe_prefix({"tf_nesting": {
        "layer_count": 3, "stage_label": "盤整", "alignment_score": 0.5,
        "trade_side": {"side": "neutral"}, "false_break": {}}})
    assert "盤整" in out
    _assert_clean(out)
    # 全壞輸入也只回字串、不拋例外
    assert isinstance(macro._shadow_observe_prefix({}), str)
    assert isinstance(macro._shadow_observe_prefix({"tf_nesting": 123}), str)
