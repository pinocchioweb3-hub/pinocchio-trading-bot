"""測試資料目錄隔離總閘（監督員 r128／v233）。

為什麼要有這個檔（實測，非推測）
--------------------------------
在此之前全庫**沒有任何 conftest.py**，也沒有 pytest.ini。測試會不會打到
「線上正式資料目錄」（%LOCALAPPDATA%\\TradingBot），完全取決於 pytest 這次
**收集到了哪些檔**：

  * 有四個測試模組在 **import 期**就把 `os.environ["BOT_DATA_DIR"]` 指到自己的
    臨時目錄（test_l2_stat_gates / test_plan_snapshot / test_regime_vector /
    test_universe_provenance）。那是行程級的全域副作用 —— 跑全庫時它會**外溢**
    給其餘所有測試，於是「全庫綠燈」看起來很乾淨。
  * 但只要**跑子集**（單一檔、`-k` 篩選，也就是平常 debug 的跑法），那四個模組
    沒被收集，`BOT_DATA_DIR` 就是空的 ⇒ `botpaths.data_dir()` 回到**線上正式
    資料目錄**，測試直接對真實資料檔讀寫。

r128 實測（151 個未自我隔離的測試檔，逐檔以乾淨臨時目錄單獨跑，並交叉驗證每次
都真的有跑到測試、不是被參數錯誤擋下的假陰性）：其中 **11 個檔會在資料目錄留下
檔案**——trade_journal.db / scanner.db / narrative.db / news_feed.db /
fire_queue.db / backtest_results.db / charts/ / review_engine_epoch.json /
free_universe_shadow.jsonl。資料庫那幾個只建 schema、零列寫入；但
`free_universe_shadow.jsonl`（宇宙截斷量測的原始輸入）會**附加測試造的假列**，
`review_engine_epoch.json`（引擎世代）會被**整檔覆寫**。

⚠️ 誠實範圍：此洞**尚未發生過**——線上 free_universe_shadow.jsonl 245 列全數
為真實列（無測試指紋列）、review_engine_epoch.json 停在 6/20 未被動過。它一直
沒引爆，只是因為大家都跑全庫、剛好吃到那四個模組的外溢。這是潛伏風險，不是已
發生的污染，**不得**寫成「資料已被污染」。

這個檔做兩件事
--------------
1. **收集期就先改 env**（conftest 早於任何測試模組被 import）：`BOT_DATA_DIR`
   未設、或被指向線上正式目錄時，一律改指到本次 session 的臨時目錄。四個自我
   隔離的模組照樣可以覆寫成自己的臨時目錄，不受影響。
2. **每個測試前後各驗一次**（autouse 閘）：`botpaths.data_dir()` 不得解析到線上
   正式目錄。fail-closed —— 將來有人寫出繞過 env 的測試，會**當場紅**，而不是
   靜靜地對真實資料動手。

⛔ 這個閘只保護測試環境，與交易紅線無關；也**不是**「隔離好了＝測試涵蓋率變好」，
它只證明綠燈不再取決於這台機器的資料狀態。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def live_data_dir_candidates() -> list[Path]:
    """`botpaths.data_dir()` 在**沒有** BOT_DATA_DIR 時可能落到的所有正式位置。

    對照 botpaths.data_dir()：env > %LOCALAPPDATA%/TradingBot > 專案根（fallback）。
    專案根也算「正式」——那個 fallback 會把 .db 寫回 OneDrive 內的 repo。
    """
    out: list[Path] = []
    localapp = os.getenv("LOCALAPPDATA", "").strip()
    if localapp:
        out.append(Path(localapp) / "TradingBot")
    out.append(ROOT)
    return out


def is_live_data_dir(p: Path | str | None) -> bool:
    """p 是否就是線上正式資料目錄。無法解析時回 False（保守：不誤擋）。"""
    if p is None:
        return False
    try:
        rp = Path(p).resolve()
    except OSError:
        return False
    for cand in live_data_dir_candidates():
        try:
            if rp == cand.resolve():
                return True
        except OSError:
            continue
    return False


# ── 1. 收集期改 env：早於任何測試模組 import，也早於任何 module-level 常數定型 ──
_env_at_import = os.environ.get("BOT_DATA_DIR", "").strip()
if (not _env_at_import) or is_live_data_dir(_env_at_import):
    SESSION_DATA_DIR = Path(tempfile.mkdtemp(prefix="pytest_botdata_"))
    os.environ["BOT_DATA_DIR"] = str(SESSION_DATA_DIR)
else:
    SESSION_DATA_DIR = Path(_env_at_import)


# ── 2. 每個測試前後各驗一次：fail-closed ──
def assert_data_dir_not_live(when: str) -> None:
    """`botpaths.data_dir()` 不得解析到線上正式資料目錄，否則當場紅。"""
    import botpaths

    d = botpaths.data_dir()
    assert not is_live_data_dir(d), (
        f"[{when}] 測試正對線上正式資料目錄執行：{d}\n"
        "測試不得讀寫真實資料檔（會污染 free_universe_shadow.jsonl／"
        "review_engine_epoch.json 等）。請用 tmp_path，或 monkeypatch "
        "botpaths.data_dir／setenv BOT_DATA_DIR。"
    )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    """在**測試本體**的前後各驗一次。

    ⛔ 為什麼用 hook 而不是 autouse fixture（r128 實測，別改回去）：
    autouse fixture 的 teardown 排在 `monkeypatch` 的還原**之後** —— 測試若在
    body 裡把 `botpaths.data_dir` 指回正式目錄，等 fixture 後置檢查跑到時早就被
    還原了，閘會**靜靜地放行**。實測寫過一個故意違規的探針：fixture 版本
    「1 passed」，hook 版本才會紅。`pytest_runtest_call` 的後置點在 call 階段
    結尾、teardown 之前，才真的看得到測試留下的狀態。

    ⚠️ 誠實範圍：這個閘只包 call 階段。fixture setup 期間的違規不在守備範圍。
    """
    assert_data_dir_not_live("測試開始前")
    res = yield
    assert_data_dir_not_live("測試結束後（monkeypatch 尚未還原）")
    return res
