# -*- coding: utf-8 -*-
"""單實例鎖（v109）：第二個 daemon 啟動應綁不上 → 自退，杜絕雙 daemon 雙重下單。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from run_bot import _acquire_single_instance_lock

_TEST_PORT = 47699   # 測試專屬 port，避開真 daemon 的 47654


def test_second_instance_blocked_until_first_releases():
    s1 = _acquire_single_instance_lock(_TEST_PORT)
    assert s1 is not None, "第一個應綁得上"
    try:
        s2 = _acquire_single_instance_lock(_TEST_PORT)
        assert s2 is None, "第二個必須綁不上（單實例）"
    finally:
        s1.close()
    # 釋放後可再綁（行程死亡→OS 釋放→新 daemon 可起）
    s3 = _acquire_single_instance_lock(_TEST_PORT)
    assert s3 is not None, "釋放後應可重新綁定"
    s3.close()
