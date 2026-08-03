"""條件式跳過必須「已申報」——監督員 r138。

同物種（未知／沒跑過 被折成 已確認／跑過）在**測試層**的落點。

實測（2026-08-03，commit 27bafad）：
  * 本機工作區   `pytest --collect-only -q` → **2104** 筆
  * 乾淨 worktree（同一 commit、`git worktree add --detach`）→ **2096** 筆
  * 差 **8 筆**，全部出自 `tests/test_trust_source_unreadable.py` 一個檔。

成因不是 bug，是**刻意**的 module-level skip：該檔測的是 `platform/api/data_access.py`，
而 `platform/` 整棵子樹目前未納版控（待使用者決策（b）），新 clone 沒有它 ⇒ 整個模組跳過。

問題出在「跳過是靜音的」：`pytest -q` 的綠燈與退出碼 0 完全一樣，
只有把兩邊的收集數並排才看得出來。於是「CI 全綠」被讀成「這 8 條防線跑過了」——
而那 8 條守的正好是紅線③的形狀（信任頁資料源壞掉時，不得只剩數字、掉了「這是紙上交易」的限定語）。

本檔的作用：讓「條件式跳過」只能以**已申報**的形式存在。
新增一個未申報的 module-level skip / importorskip ⇒ 這裡紅，逼申報人寫下成因與代價。
⛔ 本檔不修那 8 條的跳過本身（那要等使用者決定 platform/ 是否納版控），只讓它不再靜音。

執行：pytest tests/test_collection_gap_visibility.py
"""
from __future__ import annotations

import re
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

# 檔名 -> (成因, 在缺條件環境下的代價)
DECLARED_CONDITIONAL_SKIPS = {
    "test_trust_source_unreadable.py": (
        "platform/api/data_access.py 不在版控內（platform/ 整棵未納版控，待使用者決策）",
        "任何乾淨 clone／CI 都不會跑這 8 條；它們守的是信任頁『資料源壞掉時不得只剩數字』（紅線③形狀）",
    ),
    "test_chart_scorecard_unknown_vs_zero_v222.py": (
        "matplotlib 為可選相依（pytest.importorskip）",
        "無繪圖相依的環境不跑圖表誠實性檢查",
    ),
    "test_chart_smc_unknown_vs_none_v219.py": (
        "matplotlib 為可選相依（pytest.importorskip）",
        "無繪圖相依的環境不跑圖表誠實性檢查",
    ),
    "test_oi_delta_unknown_v224.py": (
        "matplotlib 為可選相依（pytest.importorskip）",
        "無繪圖相依的環境不跑圖表誠實性檢查",
    ),
}

_SKIP_PATTERNS = (
    re.compile(r"allow_module_level\s*=\s*True"),
    re.compile(r"\bpytest\.importorskip\("),
)


def _files_with_conditional_skip() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:  # 讀不到 ≠ 沒有——出聲，不要折成「這檔沒問題」
            raise AssertionError(f"掃描不到 {path.name}：{exc}") from exc
        hits = [p.pattern for p in _SKIP_PATTERNS if p.search(text)]
        if hits:
            found[path.name] = hits
    return found


def test_scanner_is_not_vacuous():
    """掃描器本身必須真的抓得到東西——否則這份檢定是虛設的。"""
    found = _files_with_conditional_skip()
    assert found, "掃描器在 tests/ 找不到任何條件式跳過＝掃描器壞了，不是真的沒有"
    assert "test_trust_source_unreadable.py" in found, (
        "已知的 module-level skip 沒被抓到，掃描規則失效"
    )


def test_every_conditional_skip_is_declared():
    """未申報的條件式跳過 ⇒ 紅。跳過可以存在，靜音不行。"""
    found = _files_with_conditional_skip()
    undeclared = sorted(set(found) - set(DECLARED_CONDITIONAL_SKIPS))
    assert not undeclared, (
        "以下測試檔會在缺條件的環境（CI／新 clone）靜默不收集，但沒有申報：\n"
        + "\n".join(f"  - {name}（命中：{', '.join(found[name])}）" for name in undeclared)
        + "\n請在 DECLARED_CONDITIONAL_SKIPS 寫下（成因, 缺條件環境下的代價）。"
        + "\n⛔ 綠燈不等於全部跑過——沒申報的跳過會讓覆蓋率變成一個沒人核對過的數字。"
    )


def test_declared_entries_still_exist():
    """申報清單不得腐爛：檔案改名／刪除或跳過已移除 ⇒ 紅，逼人回來清。"""
    found = _files_with_conditional_skip()
    stale = sorted(set(DECLARED_CONDITIONAL_SKIPS) - set(found))
    assert not stale, (
        "申報清單裡的以下項目已不再有條件式跳過（或檔案已不存在），請移除申報：\n"
        + "\n".join(f"  - {name}" for name in stale)
    )


def test_declaration_records_both_cause_and_cost():
    for name, entry in DECLARED_CONDITIONAL_SKIPS.items():
        assert isinstance(entry, tuple) and len(entry) == 2, f"{name} 的申報格式應為 (成因, 代價)"
        cause, cost = entry
        assert cause.strip(), f"{name} 沒寫成因"
        assert cost.strip(), f"{name} 沒寫代價——只寫成因會讓人以為跳過是免費的"
