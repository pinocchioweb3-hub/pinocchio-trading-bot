"""task#75：紅線③措辭守門 — FIRE 卡/LLM 提示不得出現無依據「勝率較高/高勝率」績效宣稱。

背景：我方 crypto-only EV n=12 t≈0.97 未顯著、實倉 0 筆。任何「順勢進場勝率較高」「續勢
勝率高」「是高勝率反轉前置」「＝高勝率」這類絕對勝率宣稱＝捏造績效（紅線③）。教科書的
策略-狀態傾向可以講，但須以「條件佔優/較順風/勝算傾向」+「非勝率保證」的口徑呈現，與
glossary.py:91「信心分不是勝率預測」、synthesizer.py:167「匯合多≠勝率高」一致。

本檔把治本後的措辭鎖死：禁回填這幾條確切的灌水措辭；並驗證誠實限定詞已就位。
純靜態原始碼掃描，零 import 副作用、零網路、零真錢。
"""
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MACRO = (_ROOT / "l3_dispatcher" / "macro.py").read_text(encoding="utf-8")
_SYN = (_ROOT / "l3_dispatcher" / "synthesizer.py").read_text(encoding="utf-8")

# 治本後不得再出現的「絕對勝率宣稱」確切片語（紅線③）
BANNED_MACRO = ["順勢進場勝率較高"]
BANNED_SYN = ["續勢勝率高", "是高勝率反轉前置", "做空＝高勝率"]


def test_macro_no_unsupported_winrate_claim():
    for phrase in BANNED_MACRO:
        assert phrase not in _MACRO, f"macro.py 又出現無依據勝率宣稱：{phrase}"


def test_synthesizer_no_unsupported_winrate_claim():
    for phrase in BANNED_SYN:
        assert phrase not in _SYN, f"synthesizer.py 又出現無依據勝率宣稱：{phrase}"


def test_honest_qualifiers_present():
    """治本後的誠實限定詞必須在位（證明是『改口徑』而非『刪教育』）。"""
    assert "非勝率保證" in _MACRO
    assert "非勝率保證" in _SYN


def test_glossary_honest_anchor_intact():
    """誠實錨點（信心分≠勝率預測）未被動到。"""
    gloss = (_ROOT / "l3_dispatcher" / "glossary.py").read_text(encoding="utf-8")
    assert "不是勝率預測" in gloss
