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


# ── 3. 線上資料目錄「寫入」總閘（監督員 r129／v234）──────────────────────
"""為什麼 §1/§2 還不夠（實測，非推測）

§1/§2 是**沿著 `botpaths` 那條路**設的閘：改 `BOT_DATA_DIR`、驗
`botpaths.data_dir()`。但這個專案裡解析資料目錄的路**不只一條**：

  * `botpaths.data_dir()`           ← env `BOT_DATA_DIR`，§1/§2 已守
  * `watchdog.py:47`                ← env `TRADINGBOT_DATA_DIR`（**另一個** env）
  * `tools/atk_consumer/consume_intents.py:48-51`      ← 直接 `os.path.expandvars`
  * `tools/atk_consumer/consume_intents_live.py:48-51` ← 直接 `os.path.expandvars`
  * `tools/atk_consumer/consume_intents_live.py:935`   ← `ACKED_POS`

後三者在 **import 期**就把 `%LOCALAPPDATA%\\TradingBot\\...` 綁進 module 常數，
**沒有任何 env 可以改它**——§1 把 `BOT_DATA_DIR` 指到臨時目錄，對它們毫無作用。
其中 `atk_positions_live.json`（真錢部位帳）、`atk_consumer_live_health.json`
（真錢健康檔）、`atk_acknowledged_positions.json` 都在這條路上；r53-r57／v162-v166
追過的「自製壞檔再自誤讀」，如果由一個測試親手做出來，後果與那幾次同級。

⚠️ 更要緊的是：r128 當初那次「151 檔逐檔實測、找出 11 個會留檔的」量測，
**看不見這條路**——量測是把 `BOT_DATA_DIR` 指到臨時目錄、再看臨時目錄長出什麼；
沒經過 `botpaths` 的寫入根本不會出現在被觀察的那個目錄裡。也就是說那次量測的
涵蓋範圍被當成了全部涵蓋範圍（本專案追了 53 次的同一物種，這次落在量測工具本身）。

⚠️ 誠實範圍：目前 13 個 import 到 atk consumer 的測試檔，多數**有**用
`monkeypatch.setattr(ci, "POS_STATE", ...)` 把常數改掉——那是**人工紀律**，
不是閘；`HEALTH` 則從沒有任何一個測試改過。本閘落地前線上檔案未見測試指紋
（`atk_positions_live.json` 停在 08-03 04:18＝真錢管線自己寫的），
所以這是**潛伏風險，不是已發生的污染**，⛔ 不得寫成「資料已被污染」。

這個閘做什麼
------------
把「寫入」攔在原語層（`open(...,'w')`／`os.replace`／`os.rename`／`os.remove`），
只要目標落在線上資料目錄那棵樹裡就當場丟例外。與 §2 的差別是它**不看是誰要寫**、
也不管走的是哪個 env——涵蓋所有解析路徑，將來新增第四條路也自動被守。

⛔ 刻意**不含**專案根：pytest 自己要在 repo 寫 `.pytest_cache`／`__pycache__`，
把 repo 納入會製造慢性假警報（§1 的 `is_live_data_dir` 含專案根是對的，那是
「解析結果不該落在這」的判定，與「這次寫入該不該擋」是兩回事）。
⛔ 刻意**不擋** `mkdir`：`mkdir(exist_ok=True)` 靠攔 `FileExistsError` 運作，
擋它會讓合法的 no-op 爆掉；留下一個空目錄的害處遠小於誤擋。
⚠️ 擋不到 C 層直接開檔（如 `sqlite3.connect` 建 .db）——那條路走 `botpaths`，
已由 §1 改道，兩道閘合起來才完整。
"""
import builtins as _builtins
import io as _io
from contextlib import contextmanager


class LiveDataDirWriteError(AssertionError):
    """測試試圖寫入線上正式資料目錄。"""


def live_write_guard_roots() -> list[Path]:
    """要保護的樹：只有 %LOCALAPPDATA%\\TradingBot。"""
    localapp = os.getenv("LOCALAPPDATA", "").strip()
    if not localapp:
        return []
    return [Path(localapp) / "TradingBot"]


LIVE_WRITE_GUARD_ROOTS: list[Path] = live_write_guard_roots()
_extra_guard_roots: list[Path] = []


@contextmanager
def guard_extra_root(path: Path | str):
    """暫時把另一棵樹也納入保護（給本閘自己的測試用，免得非拿真目錄試不可）。"""
    p = Path(path)
    _extra_guard_roots.append(p)
    try:
        yield p
    finally:
        _extra_guard_roots.remove(p)


def _guarded(target) -> Path | None:
    """target 若落在被保護的樹裡就回那棵樹的根，否則 None。"""
    roots = LIVE_WRITE_GUARD_ROOTS + _extra_guard_roots
    if not roots:
        return None
    try:
        rp = Path(os.path.abspath(os.fsdecode(target)))
    except (TypeError, ValueError, OSError):
        return None            # 記憶體 fd／奇怪型別：不誤擋
    for root in roots:
        try:
            ar = Path(os.path.abspath(root))
        except (TypeError, ValueError, OSError):
            continue
        if rp == ar or ar in rp.parents:
            return ar
    return None


def _refuse(target, how: str) -> None:
    root = _guarded(target)
    if root is None:
        return
    _dbg = os.getenv("LIVE_GUARD_TRACE", "").strip()
    if _dbg:
        import traceback as _tb
        with _orig_open(_dbg, "a", encoding="utf-8") as _fh:
            _fh.write(f"=== {how} -> {target}\n")
            _tb.print_stack(file=_fh)
    raise LiveDataDirWriteError(
        f"測試試圖寫入線上正式資料目錄（{how}）：{target}\n"
        f"受保護的樹：{root}\n"
        "⛔ 那裡放的是真錢部位帳／健康檔／影子資料（atk_positions_live.json、"
        "atk_consumer_live_health.json、free_universe_shadow.jsonl…）。\n"
        "請改用 tmp_path；若模組在 import 期就把路徑綁死（tools/atk_consumer/*、"
        "watchdog.py），用 monkeypatch.setattr(模組, \"POS_STATE\"/\"HEALTH\"/… , "
        "tmp_path / \"...\") 或 monkeypatch.setenv(\"TRADINGBOT_DATA_DIR\", ...) 改掉它。"
    )


_WRITE_MODE_CHARS = frozenset("wxa+")
_orig_open = _builtins.open
_orig_replace = os.replace
_orig_rename = os.rename
_orig_remove = os.remove
_orig_unlink = os.unlink


def _guarded_open(file, mode="r", *args, **kwargs):
    if _WRITE_MODE_CHARS & set(str(mode)):
        _refuse(file, f"open(mode={mode!r})")
    return _orig_open(file, mode, *args, **kwargs)


def _guarded_replace(src, dst, **kwargs):
    _refuse(dst, "os.replace")
    return _orig_replace(src, dst, **kwargs)


def _guarded_rename(src, dst, **kwargs):
    _refuse(dst, "os.rename")
    return _orig_rename(src, dst, **kwargs)


def _guarded_remove(path, **kwargs):
    _refuse(path, "os.remove")
    return _orig_remove(path, **kwargs)


def _guarded_unlink(path, **kwargs):
    _refuse(path, "os.unlink")
    return _orig_unlink(path, **kwargs)


# 兩個名字指向同一個函式物件，但屬性查找是分開的：`Path.open` 走 `io.open`，
# 一般程式碼走 `builtins.open`——兩邊都要換，只換一邊會漏掉 Path.write_text。
_builtins.open = _guarded_open
_io.open = _guarded_open
os.replace = _guarded_replace
os.rename = _guarded_rename
os.remove = _guarded_remove
os.unlink = _guarded_unlink


# ── 4. sqlite 那條路（r129 實測補上）─────────────────────────────────
"""§3 攔的是 Python 層的寫入原語，`sqlite3.connect()` 在 C 層開檔，攔不到——
這是 §3 的已知盲點。r128 那次量測找到的 11 個外洩檔裡有 6 個是 .db，所以這個
盲點非補不可：`.db` 走 `botpaths`（§1 已改道）**且**走 C 層（§3 看不見），
兩道閘都不補的話它就整條沒人守。

⚠️ 誠實範圍（重要，別讓下一輪重跑一次）：本閘落地時**沒有抓到任何**現有測試
連線到線上 .db——全庫跑一輪，`LIVE_GUARD_TRACE` 記到的 6 次攔截全是本閘自己的
測試。它是**預防性**的 fail-closed 覆蓋，不是「抓到了什麼」。

⚠️ 一段走過的冤枉路，寫下來免得重走：量測時發現線上 `trade_journal.db` 的 mtime
兩次都恰好停在測試結束那一秒（09:11:17、09:14:49），中間 155 秒閒置沒動，看起來
像是測試在碰它。**那個結論是錯的。** 決定性反證有兩條：(a) 開 `LIVE_GUARD_TRACE`
全庫跑一輪，`sqlite3.connect` 一次都沒被攔到；(b) 在完全沒有 pytest 的時段，
`trade_journal.db-wal` / `-shm` 仍在持續更新，主檔 mtime 每隔數分鐘往前跳一次
——那是 daemon 的 **WAL checkpoint**，主檔 mtime 只在 checkpoint 時才動，
於是它的節奏（約 3 分鐘）剛好與我那兩次跑測試的節奏對上了。
⇒ 用 mtime 對齊來歸因，在 WAL 模式的 .db 上會給出假的因果；要看的是 `-wal`/`-shm`。

⛔ 唯讀連線（`file:...?mode=ro` URI）是合法的——`platform/api/data_access.py`
就是這樣看線上 .db 的，本閘放行；擋的是可寫連線（sqlite 會為了寫 header／WAL
而碰到主檔）。`:memory:` 一律放行。

`LIVE_GUARD_TRACE=<檔路徑>` 這個 env 是上面那段查證留下來的工具：設了之後每次
攔截都會把呼叫堆疊附加進該檔。⛔ 預設關閉，不設就完全沒有額外成本。
"""
import sqlite3 as _sqlite3

_orig_connect = _sqlite3.connect


def _guarded_connect(database, *args, **kwargs):
    target = database
    if isinstance(target, str):
        if target == ":memory:" or not target:
            return _orig_connect(database, *args, **kwargs)
        if kwargs.get("uri") and target.startswith("file:"):
            if "mode=ro" in target or "mode=memory" in target:
                return _orig_connect(database, *args, **kwargs)
            target = target[len("file:"):].split("?", 1)[0]
    _refuse(target, "sqlite3.connect（可寫連線）")
    return _orig_connect(database, *args, **kwargs)


_sqlite3.connect = _guarded_connect
