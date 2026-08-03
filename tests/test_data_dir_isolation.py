"""測試資料目錄隔離閘本身的測試（監督員 r128／v233）。

守的是這條線：**跑子集也不能打到線上正式資料目錄**。
改動前（沒有 conftest.py 時）單獨跑 `botpaths.data_dir()` 會回
%LOCALAPPDATA%\\TradingBot——本檔的 test_data_dir_is_not_the_live_dir
在那個狀態下確實會紅（r128 以臨時探針實測過，是**行為性**紅、不是 import 錯）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import botpaths  # noqa: E402

from conftest import (  # noqa: E402
    is_live_data_dir,
    live_data_dir_candidates,
)


# ── 閘的效果 ──

def test_data_dir_is_not_the_live_dir():
    """跑測試時，資料目錄不得是線上正式目錄。"""
    assert not is_live_data_dir(botpaths.data_dir())


def test_env_var_is_set_to_somewhere_writable_and_temporary():
    """BOT_DATA_DIR 有被指定（不能是空的＝落回正式目錄）。"""
    env = os.environ.get("BOT_DATA_DIR", "").strip()
    assert env, "BOT_DATA_DIR 是空的 ⇒ botpaths 會落回線上正式資料目錄"
    assert not is_live_data_dir(env)


def test_writing_a_file_lands_outside_the_live_dir(tmp_path):
    """實際寫一個檔，確認它不在正式目錄底下。"""
    p = botpaths.data_dir() / "isolation_probe.tmp"
    p.write_text("r128", encoding="utf-8")
    try:
        assert p.exists()
        for cand in live_data_dir_candidates():
            assert p.parent.resolve() != cand.resolve()
    finally:
        p.unlink(missing_ok=True)


# ── 判定函式本身（別讓閘自己變成「永遠回 False」的擺設） ──

def test_is_live_flags_the_localappdata_dir():
    localapp = os.getenv("LOCALAPPDATA", "").strip()
    if not localapp:
        pytest.skip("這台機器沒有 LOCALAPPDATA")
    assert is_live_data_dir(Path(localapp) / "TradingBot")


def test_is_live_flags_the_project_root_fallback():
    """botpaths 在 LOCALAPPDATA 缺席時會把 .db 寫回 repo —— 那也算正式目錄。"""
    assert is_live_data_dir(ROOT)


def test_is_live_does_not_flag_a_temp_dir(tmp_path):
    assert not is_live_data_dir(tmp_path)


def test_is_live_handles_none_and_garbage():
    """回 False 必須是『真的不是正式目錄』，不是把例外吞掉。"""
    assert is_live_data_dir(None) is False
    assert is_live_data_dir(tmp_dir_that_does_not_exist()) is False


def tmp_dir_that_does_not_exist() -> Path:
    return Path(os.getenv("TEMP", "/tmp")) / "r128_no_such_dir_54f3a9"
