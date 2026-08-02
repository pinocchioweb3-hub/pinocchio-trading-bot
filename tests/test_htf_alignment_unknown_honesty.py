"""v220：HTF(1d)→LTF(4h) 對齊驗證的「這輪沒算出來」不可折成「確認沒有這個結構」。

同物種第 40 次。落點 l3_dispatcher/macro.py::_compute_htf_alignment()——與 v217/v218
同一條播出線（deepdive prompt），但這次不是渲染層折數字，而是**衍生結論**：
它把三個 SMC 分量壓成 verdict + note，note 由 synthesizer 原文放進餵給 LLM 的
prompt 標題，verdict 則進復盤紀錄（_review_context 的 htf_verdict_1d4h）。

判準（與產出端 compute_smc_levels 對齊）：鍵在＝答案（含空 list、含 equilibrium）；
鍵不在／有 <name>_error／整個時框缺或回 error＝未知。

⛔ 邊界線（沿用 v208-v219）：算過而確實沒有，仍然是答案，照舊保持安靜、不加缺料
   標記——否則盤面乾淨的標的每天都變缺料告警。
"""
import pytest

from l3_dispatcher.macro import _compute_htf_alignment as htf
from l3_dispatcher.macro import _htf_input

BULL = [{"type": "BOS", "direction": "bull", "level": 100.0, "ago_bars": 3}]
BEAR = [{"type": "BOS", "direction": "bear", "level": 100.0, "ago_bars": 3}]


# --- 1. 分量分類器：鍵在＝答案，鍵不在/有 _error/時框缺＝未知 ---
def test_htf_input_classifies_answer_vs_unknown():
    assert _htf_input({"bos_choch": []}, "bos_choch") == ([], None)       # 空 list 是答案
    assert _htf_input({"bos_choch": BULL}, "bos_choch") == (BULL, None)
    assert _htf_input({"bos_choch_error": "boom"}, "bos_choch")[1] is not None
    assert _htf_input({"fvg": []}, "bos_choch")[1] is not None            # 鍵不在＝未知
    assert _htf_input({}, "bos_choch")[1] is not None                     # 整個時框沒抓到
    assert _htf_input({"error": "insufficient_candles"}, "bos_choch")[1] is not None
    # 鍵在但值是 None：呼叫端用 .get() 重組字典時的形狀（backtest/smc_walkforward.py:169）
    # ——上游沒算出來被抹平成「鍵存在」，仍須判未知，不可當成「答案：沒有結構」
    assert _htf_input({"bos_choch": None}, "bos_choch")[1] is not None
    # _error 優先於同名鍵：兩者並存時以失敗為準
    assert _htf_input({"bos_choch": [], "bos_choch_error": "boom"}, "bos_choch")[1] is not None


# --- 2. 「1d 趨勢算失敗」不可被講成「無 1d 結構」 ---
def test_1d_structure_failure_not_stated_as_no_structure():
    unknown = htf({"bos_choch": BULL},
                  {"bos_choch_error": "boom",
                   "premium_discount": {"zone": "discount"}})
    answer = htf({"bos_choch": BULL},
                 {"bos_choch": [],
                  "premium_discount": {"zone": "discount"}})
    # 改動前兩者輸出一模一樣（實測）——這就是折疊
    assert unknown["note"] != answer["note"]
    assert "無 1d 結構" not in unknown["note"]
    assert "沒算出來" in unknown["note"]
    assert unknown["unknown_inputs"]
    # 邊界：算過就是沒有 → 維持原措辭、不喊缺料
    assert "無 1d 結構" in answer["note"]
    assert "沒算出來" not in answer["note"]
    assert answer["unknown_inputs"] == []


# --- 3. 「區位算失敗」不可被講成「位於均衡區」 ---
def test_zone_failure_not_stated_as_equilibrium():
    unknown = htf({"bos_choch": BULL},
                  {"bos_choch": BULL, "premium_discount_error": "boom"})
    answer = htf({"bos_choch": BULL},
                 {"bos_choch": BULL, "premium_discount": {"zone": "equilibrium"}})
    assert unknown["note"] != answer["note"]
    # 只禁肯定句「位於均衡區」；文中明講「⛔ 不等於均衡區」是澄清、不是宣稱
    assert "位於均衡區" not in unknown["note"]
    assert "沒算出來" in unknown["note"]
    assert unknown["unknown_inputs"]
    assert "位於均衡區" in answer["note"]      # 邊界：equilibrium 是答案
    assert answer["unknown_inputs"] == []


# --- 4. 缺的是 4h 時，不可只怪 1d（歸因要對） ---
def test_missing_4h_is_not_blamed_on_1d():
    out = htf({}, {"bos_choch": BEAR, "premium_discount": {"zone": "premium"}})
    assert out["verdict"] == "unknown"
    assert "4h" in out["note"], "4h 才是缺的那一邊，note 卻只提 1d"
    assert any("4h" in u for u in out["unknown_inputs"])


# --- 5. 未知 vs 全部算過但都是空的：後者不可誤報缺料 ---
def test_all_computed_but_empty_stays_silent():
    out = htf({"bos_choch": []},
              {"bos_choch": [], "premium_discount": {"zone": "equilibrium"}})
    assert out["verdict"] == "unknown"
    assert out["unknown_inputs"] == []
    assert "沒算出來" not in out["note"]
    assert "算出來就是沒有" in out["note"]


def test_missing_1d_timeframe_reports_unknown_not_absence():
    for s1d in ({}, {"error": "insufficient_candles", "needed": 30, "got": 12}):
        out = htf({"bos_choch": BULL}, s1d)
        assert out["verdict"] == "unknown"
        assert out["unknown_inputs"], s1d
        assert "沒算出來" in out["note"]
        assert "未知≠沒有結構" in out["note"]


# --- 6. 回歸：真的算得出來時 verdict 不變（下游 htf_verdict_1d4h 靠它） ---
@pytest.mark.parametrize("s4,s1d,expect", [
    ({"bos_choch": BULL},
     {"bos_choch": BULL, "premium_discount": {"zone": "discount"}}, "aligned"),
    ({"bos_choch": BULL},
     {"bos_choch": BEAR, "premium_discount": {"zone": "premium"}}, "conflict"),
    ({"bos_choch": BULL},
     {"bos_choch": BULL, "premium_discount": {"zone": "premium"}}, "partial"),
])
def test_verdict_unchanged_when_everything_computed(s4, s1d, expect):
    out = htf(s4, s1d)
    assert out["verdict"] == expect
    assert out["unknown_inputs"] == []
    assert "沒算出來" not in out["note"]


# --- 7. 產出端：未知時整段不可從 prompt 消失（v218 的 (A) 種下場） ---
def _prompt_for(s4: dict, s1d: dict) -> str:
    """實際跑 deepdive 的 per-symbol prompt 組裝，看那一段有沒有出現。"""
    from l3_dispatcher.synthesizer import _format_symbol_data
    return _format_symbol_data("TEST", {"symbol": "TEST",
                                        "htf_alignment": htf(s4, s1d)})


def test_unknown_with_missing_data_is_visible_in_prompt():
    # 1d 整個沒抓到＝未知：舊碼 verdict=="unknown" 直接整段跳過，LLM 只會讀成
    # 「多時框沒問題」。必須看得見，且要標明是缺料。
    text = _prompt_for({"bos_choch": BULL}, {})
    assert "多時框對齊" in text, "缺料的未知被整段吞掉＝對 LLM 宣稱 HTF 沒意見"
    assert "沒算出來" in text
    assert "缺料" in text


def test_unknown_without_missing_data_stays_silent_in_prompt():
    # 邊界：全部算過、就是沒有結構/在均衡區 → 照舊安靜，不可天天喊缺料
    text = _prompt_for({"bos_choch": []},
                       {"bos_choch": [], "premium_discount": {"zone": "equilibrium"}})
    assert "多時框對齊" not in text
