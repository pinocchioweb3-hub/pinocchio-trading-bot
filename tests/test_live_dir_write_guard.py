# -*- coding: utf-8 -*-
"""測試對「線上正式資料目錄」的寫入，必須當場被擋（v234・監督員 r129）。

承 v233／r128：那一版設的閘是**沿著 `botpaths` 那條路**——改 `BOT_DATA_DIR`、
驗 `botpaths.data_dir()`。本檔補的是「還有別條路」這件事：

    tools/atk_consumer/consume_intents.py:48-51        直接 expandvars，**無 env**
    tools/atk_consumer/consume_intents_live.py:48-51   直接 expandvars，**無 env**
    tools/atk_consumer/consume_intents_live.py:935     ACKED_POS，同上
    watchdog.py:47                                     另一個 env（TRADINGBOT_DATA_DIR）

這些在 import 期就把 `%LOCALAPPDATA%\\TradingBot\\...` 綁進 module 常數，`BOT_DATA_DIR`
指到哪都改不動它們。落在那條路上的檔包括 `atk_positions_live.json`（真錢部位帳）與
`atk_consumer_live_health.json`（真錢健康檔）。

⚠️ 而且 r128 那次「151 檔逐檔實測、找出 11 個會留檔的」量測**看不見這條路**：
量測是把 `BOT_DATA_DIR` 指到臨時目錄、再看那個臨時目錄長出什麼，沒經過 `botpaths`
的寫入根本不會出現在被觀察的目錄裡。量測工具的涵蓋範圍被當成了全部涵蓋範圍。

⚠️ 誠實範圍：閘落地前**未發現**任何線上檔案帶測試指紋（`atk_positions_live.json`
停在 2026-08-03 04:18＝真錢管線自己寫的）。13 個 import 到 atk consumer 的測試檔
多數有用 `monkeypatch.setattr(ci, "POS_STATE", ...)` 自保——但那是人工紀律不是閘，
且 `HEALTH` 從沒有任何一個測試改過。⛔ 這是潛伏風險，不得寫成「資料已被污染」。

【本檔的非虛設驗證怎麼做的】不能拿真的資料目錄當白老鼠，所以用**代理樹**：
把 `LOCALAPPDATA` 指到臨時目錄後跑一支探針（寫 `$LOCALAPPDATA/TradingBot/x.json`
並斷言會爆）——閘落地**前**該探針以「DID NOT RAISE」行為性紅收場，而且檔案真的
被寫了出來；落地**後**綠、且目錄空無一物。本檔以 `guard_extra_root()` 把同一件事
固定下來，全程不碰真目錄。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _conftest_module():
    """拿到 pytest **已經載入的那一份** conftest，不可自己再 import 一次。

    ⛔ 陷阱（別改成 `import conftest`）：pytest 9 預設的 import 模式不保證把
    tests/ 塞進 sys.path，而就算塞了，用路徑再 import 一次會得到**第二個**
    module 實例——它有自己的 `_extra_guard_roots`，而真正裝在 `builtins.open`
    上的閘讀的是第一個實例的那份清單。於是 `guard_extra_root()` 加了等於沒加，
    本檔全部的 `pytest.raises` 會變成「沒有擋到」——測試會紅，但紅的原因會被
    誤讀成「閘壞了」。用 __file__ 比對從 sys.modules 取回同一份才安全。
    """
    here = (Path(__file__).resolve().parent / "conftest.py").resolve()
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if f:
            try:
                if Path(f).resolve() == here:
                    return mod
            except OSError:
                continue
    raise RuntimeError(f"找不到已載入的 conftest（{here}）")


_CONFTEST = _conftest_module()
LIVE_WRITE_GUARD_ROOTS = _CONFTEST.LIVE_WRITE_GUARD_ROOTS
LiveDataDirWriteError = _CONFTEST.LiveDataDirWriteError
guard_extra_root = _CONFTEST.guard_extra_root


# ── 本體：被保護的樹裡，寫入必須爆 ────────────────────────────────────
def test_open_for_write_is_refused(tmp_path):
    with guard_extra_root(tmp_path) as root:
        with pytest.raises(LiveDataDirWriteError):
            open(root / "nope.json", "w", encoding="utf-8")
    assert not (tmp_path / "nope.json").exists(), "⛔ 例外丟了、檔卻還是被建出來"


def test_nested_path_is_refused(tmp_path):
    """守的是整棵樹，不是只有根那一層。"""
    (tmp_path / "charts").mkdir()
    with guard_extra_root(tmp_path):
        with pytest.raises(LiveDataDirWriteError):
            open(tmp_path / "charts" / "x.png", "wb")


def test_append_mode_is_refused(tmp_path):
    """`free_universe_shadow.jsonl` 的污染是**附加**造成的，'a' 一定要擋。"""
    with guard_extra_root(tmp_path):
        with pytest.raises(LiveDataDirWriteError):
            open(tmp_path / "shadow.jsonl", "a", encoding="utf-8")


def test_path_write_text_is_refused(tmp_path):
    """⛔ 別把 conftest 裡的 `io.open` 那行拿掉：`Path.write_text` 走的是它，
    只換 `builtins.open` 這條會整條漏掉。"""
    with guard_extra_root(tmp_path):
        with pytest.raises(LiveDataDirWriteError):
            (tmp_path / "via_pathlib.json").write_text("{}", encoding="utf-8")


def test_os_replace_is_refused(tmp_path, monkeypatch):
    """atk consumer 的原子寫是「寫 .tmp → os.replace」——擋 open 擋不到落地那一步。"""
    outside = tmp_path.parent / f"{tmp_path.name}_src.tmp"
    outside.write_text("{}", encoding="utf-8")
    guarded = tmp_path / "tree"
    guarded.mkdir()
    with guard_extra_root(guarded):
        with pytest.raises(LiveDataDirWriteError):
            os.replace(outside, guarded / "landed.json")
    assert not (guarded / "landed.json").exists()


def test_os_remove_is_refused(tmp_path):
    """刪除同樣是破壞：真錢部位帳被誤刪＝整批倉位脫帳。"""
    victim = tmp_path / "keepme.json"
    victim.write_text("{}", encoding="utf-8")
    with guard_extra_root(tmp_path):
        with pytest.raises(LiveDataDirWriteError):
            os.remove(victim)
    assert victim.exists()


# ── 反向護欄：不可矯枉過正 ───────────────────────────────────────────
def test_reads_are_allowed(tmp_path):
    """唯讀地看線上檔是合法的（例：test_live_copy_parity 逐字比對真錢副本）。"""
    f = tmp_path / "readable.json"
    f.write_text('{"ok": 1}', encoding="utf-8")
    with guard_extra_root(tmp_path):
        assert f.read_text(encoding="utf-8") == '{"ok": 1}'
        with open(f, encoding="utf-8") as fh:
            assert fh.read() == '{"ok": 1}'


def test_writes_outside_guarded_tree_are_untouched(tmp_path):
    """tmp_path 底下的一般寫入不受影響，否則整庫測試都會爆。"""
    with guard_extra_root(tmp_path / "guarded"):
        (tmp_path / "free.json").write_text("{}", encoding="utf-8")
    assert (tmp_path / "free.json").exists()


def test_guard_is_removed_after_context(tmp_path):
    with guard_extra_root(tmp_path):
        pass
    (tmp_path / "after.json").write_text("{}", encoding="utf-8")   # 不該再被擋
    assert (tmp_path / "after.json").exists()


# ── 這個閘到底有沒有指到該指的地方 ───────────────────────────────────
def test_real_live_tree_is_actually_guarded():
    localapp = os.getenv("LOCALAPPDATA", "").strip()
    if not localapp:
        pytest.skip("這台機器沒有 LOCALAPPDATA")
    expect = Path(localapp) / "TradingBot"
    assert any(Path(os.path.abspath(r)) == Path(os.path.abspath(expect))
               for r in LIVE_WRITE_GUARD_ROOTS), \
        f"線上資料目錄沒被納入保護：{expect}"


def test_repo_root_is_deliberately_not_guarded():
    """⛔ 刻意的取捨，別「順手補上」：pytest 自己要在 repo 寫 .pytest_cache／
    __pycache__，把 repo 納入＝每輪都紅＝慢性假警報。"""
    repo = Path(__file__).resolve().parent.parent
    assert not any(Path(os.path.abspath(r)) == repo for r in LIVE_WRITE_GUARD_ROOTS)


# ── 這個閘為什麼非有不可（唯讀證據，不寫任何檔） ─────────────────────
def _under_guarded_tree(p: Path) -> bool:
    for root in LIVE_WRITE_GUARD_ROOTS:
        ar = Path(os.path.abspath(root))
        rp = Path(os.path.abspath(p))
        if rp == ar or ar in rp.parents:
            return True
    return False


@pytest.mark.parametrize("mod_name", ["consume_intents", "consume_intents_live"])
def test_atk_consumer_constants_are_bound_to_the_live_tree(mod_name):
    """證明危害是真的：這些常數在 import 期就綁死線上目錄，`BOT_DATA_DIR` 改不動。

    純讀取斷言，⛔ 絕不寫檔。若哪天這些模組改成走 env／botpaths，本測試會紅——
    那時該做的是**更新本測試**，不是把閘拿掉。
    """
    if not LIVE_WRITE_GUARD_ROOTS:
        pytest.skip("這台機器沒有 LOCALAPPDATA")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                           / "tools" / "atk_consumer"))
    mod = __import__(mod_name)
    bound = {name: getattr(mod, name)
             for name in ("OUTBOX", "STATE", "POS_STATE", "HEALTH")
             if hasattr(mod, name)}
    assert bound, f"{mod_name} 的路徑常數不見了（改版？請更新本測試）"
    for name, p in bound.items():
        assert _under_guarded_tree(Path(p)), \
            f"{mod_name}.{name} 竟然不在受保護的樹裡：{p}"
