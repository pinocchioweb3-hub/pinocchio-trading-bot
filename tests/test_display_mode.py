"""task#10(2e) DISPLAY_MODE 新手/專家顯示模式測試。

設計核心（為何安全）：
    deepdive 卡的 `text` 是 LLM 親筆論述（無確定性結構），機器計畫數字另存在
    meta["plan"] 與 paper_trades。若用「拆 prose」做新手/專家切換，會有漏掉計畫
    數字的風險。故改用**確定性附加**：新手模式在卡尾接一段『純術語定義小抄』
    （render_novice_legend，零計畫數字），專家模式不接。

    ⇒ 兩模式的『唯一差異』是這段定義小抄；卡片本體（LLM 論述）與計畫數字逐位元
      相同。本檔把這個不變量釘成 parity 斷言：
        • 專家輸出 == 原文（一字不改）
        • 新手輸出 == 原文 + "\\n\\n" + 小抄（原文為前綴、未被竄改）
        • 小抄不含任何傳入卡片的計畫數字（用特異數字探針反證）

⛔ 純呈現層：_display_mode/_with_display_mode 不碰 strength/l2_trigger 任何訊號或
   下單數學（只操作要送 TG 的字串）。

執行方式：
    pytest tests/test_display_mode.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import botconfig
import l3_dispatcher.macro as macro
from l3_dispatcher.glossary import NOVICE_LEGEND_KEYS, TERMS, render_novice_legend


def _force_mode(monkeypatch, mode: str):
    """把 DISPLAY_MODE 釘成指定值（不碰 bot_settings.json）。
    _display_mode 內部 `from botconfig import get_str`，故 patch botconfig.get_str。"""
    real = botconfig.get_str

    def fake(key, default=None):
        if key == "DISPLAY_MODE":
            return mode
        return real(key, default)

    monkeypatch.setattr(botconfig, "get_str", fake)


# ── 1. render_novice_legend：非空、含 5 個核心術語、零計畫數字 ──────────────────
def test_legend_nonempty_and_terms_present():
    legend = render_novice_legend()
    assert legend, "5 個核心術語都在 glossary，小抄不該為空"
    assert legend.startswith("🔰 <b>新手白話小抄</b>")
    by_key = {t.key: t for t in TERMS}
    for k in NOVICE_LEGEND_KEYS:
        t = by_key[k]               # 不存在會 KeyError → 測試紅，逼回填
        assert t.zh in legend, f"小抄應含術語中文名 {t.zh!r}"
    # 尾巴導去 /指標 完整說明
    assert "/指標" in legend


def test_legend_contains_no_plan_numbers():
    """小抄是『不帶任何交易資料』生成的純定義，結構上不可能含某張卡的計畫數字。
    用特異探針數字反證：這些數字不在小抄裡。"""
    legend = render_novice_legend()
    for probe in ("12345.67", "98765.43", "111.222", "0.00073"):
        assert probe not in legend


# ── 2. _display_mode：override 生效 + 壞值落回 novice ──────────────────────────
def test_display_mode_reads_override(monkeypatch):
    _force_mode(monkeypatch, "expert")
    assert macro._display_mode() == "expert"
    _force_mode(monkeypatch, "novice")
    assert macro._display_mode() == "novice"


def test_display_mode_bad_value_falls_back_novice(monkeypatch):
    for bad in ("", "  ", "garbage", "NOVICE2", "pro"):
        _force_mode(monkeypatch, bad)
        assert macro._display_mode() == "novice", f"{bad!r} 應落回 novice"


def test_display_mode_case_insensitive(monkeypatch):
    _force_mode(monkeypatch, "EXPERT")
    assert macro._display_mode() == "expert"
    _force_mode(monkeypatch, " Novice ")
    assert macro._display_mode() == "novice"


# ── 3. parity：專家＝原文一字不改 ─────────────────────────────────────────────
SAMPLE_CARD = (
    "📈 <b>BTC 看多</b>\n"
    "進場 12345.67、止損 98765.43、TP1 111.222 TP2 0.00073\n"
    "論述：結構突破 + CVD 同向。R≈2.0。"
)


def test_expert_mode_is_byte_identical(monkeypatch):
    _force_mode(monkeypatch, "expert")
    assert macro._with_display_mode(SAMPLE_CARD) == SAMPLE_CARD


# ── 4. parity：新手＝原文 + 小抄；原文為前綴且未被竄改 ────────────────────────
def test_novice_mode_appends_legend_only(monkeypatch):
    _force_mode(monkeypatch, "novice")
    out = macro._with_display_mode(SAMPLE_CARD)
    legend = render_novice_legend()
    # 新手輸出 = 原文 + "\n\n" + 小抄
    assert out == f"{SAMPLE_CARD}\n\n{legend}"
    # 原文是前綴（一字未改）
    assert out.startswith(SAMPLE_CARD)
    # 移除原文前綴後，剩下的就是「\n\n + 小抄」，不多不少
    assert out[len(SAMPLE_CARD):] == f"\n\n{legend}"


def test_novice_vs_expert_differ_only_by_legend(monkeypatch):
    """同一張卡：新手與專家輸出的『差集』恰好是小抄（含分隔）。"""
    _force_mode(monkeypatch, "expert")
    exp = macro._with_display_mode(SAMPLE_CARD)
    _force_mode(monkeypatch, "novice")
    nov = macro._with_display_mode(SAMPLE_CARD)
    legend = render_novice_legend()
    assert nov == exp + f"\n\n{legend}"


def test_plan_numbers_preserved_in_both_modes(monkeypatch):
    """計畫數字（進場/止損/TP/R）在兩模式下出現次數完全一致——
    證明顯示模式只『加定義』、絕不改/刪計畫數字（紅線③：不竄改呈現）。"""
    probes = ("12345.67", "98765.43", "111.222", "0.00073", "R≈2.0")
    _force_mode(monkeypatch, "expert")
    exp = macro._with_display_mode(SAMPLE_CARD)
    _force_mode(monkeypatch, "novice")
    nov = macro._with_display_mode(SAMPLE_CARD)
    for p in probes:
        assert exp.count(p) == 1
        assert nov.count(p) == exp.count(p), f"{p!r} 在兩模式出現次數不一致"


# ── 4b. 容錯：小抄渲染拋例外 → 新手模式安全退回原文（不漏卡、不竄改） ──────────
def test_novice_legend_failure_degrades_to_plain(monkeypatch):
    """_with_display_mode 對 render_novice_legend 包了 try/except：小抄一旦渲染失敗
    （glossary 損壞/不可達），新手模式應安全退回『原文一字不改』，而非崩潰或漏送卡。
    這釘住 macro.py 的容錯分支（驗證稽核 low：import 失敗路徑無測試）。"""
    import l3_dispatcher.glossary as g

    def boom(*a, **k):
        raise RuntimeError("simulated glossary failure")

    _force_mode(monkeypatch, "novice")
    monkeypatch.setattr(g, "render_novice_legend", boom)
    # 新手模式但小抄炸了 → 應原樣回傳（降級安全，不附小抄）
    assert macro._with_display_mode(SAMPLE_CARD) == SAMPLE_CARD


# ── 5. 純呈現層：_with_display_mode 不觸訊號/下單數學 ─────────────────────────
def test_with_display_mode_is_pure_display():
    """_with_display_mode 的可執行碼不得出現任何訊號強度/觸發/下單字眼。"""
    import ast
    import inspect
    import textwrap
    src = textwrap.dedent(inspect.getsource(macro._with_display_mode))
    node = ast.parse(src).body[0]
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)):
        node.body = node.body[1:]          # 去 docstring
    code = ast.unparse(node)
    for needle in ("strength", "l2_trigger", "place_order", "fire_queue",
                   "record_paper_entry", "set_override"):
        assert needle not in code, f"_with_display_mode 不該含 {needle!r}"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
