"""決策佇列去重語意測試 —— 守住「seed_once 種子題重啟不復活」治本。

背景（resurrection bug）：
    add_decision 預設只比對 open 去重；一旦使用者把某卡 resolved，下次 daemon 重啟
    呼叫 seed_known_decisions() 又會建一張新 open 卡，致 ledger 反覆 open_decisions=1
    / should_nudge=true，對使用者早已表態（如 revenue_waterfall「暫不定案」）的題目
    反覆騷擾。治本＝seed_once=True 對「曾存在（含 resolved）」去重。
"""
import importlib

import pytest


@pytest.fixture()
def reg(tmp_path, monkeypatch):
    """每測一個乾淨的 decisions.json（指向 tmp_path）。"""
    import l3_dispatcher.decision_registry as dr
    monkeypatch.setattr(dr, "_PATH", tmp_path / "decisions.json")
    importlib.reload  # noqa: B018 — 保持 import 不被優化掉
    return dr


def test_default_dedup_only_against_open(reg):
    """預設語意：同 key resolved 後可再開新 open 卡（允許同題重提）。"""
    a = reg.add_decision(key="q", title="t")
    reg.resolve(a, "done")
    b = reg.add_decision(key="q", title="t")
    assert b != a, "resolved 後預設應允許重開新卡"
    assert len(reg.list_open()) == 1


def test_seed_once_does_not_resurrect_after_resolved(reg):
    """治本核心：seed_once=True 一旦拍板（含擱置）就不復活。"""
    a = reg.add_decision(key="seed", title="t", seed_once=True)
    reg.resolve(a, "使用者：暫不定案，先擱置")
    # 模擬 daemon 重啟再次 seed
    b = reg.add_decision(key="seed", title="t", seed_once=True)
    assert b == a, "seed_once 不得在 resolved 後復活成新卡"
    assert reg.list_open() == [], "重啟後不應再有 open 卡騷擾使用者"


def test_seed_once_idempotent_while_open(reg):
    """seed_once 對 open 也是 idempotent（重複 seed 回原 id）。"""
    a = reg.add_decision(key="seed", title="t", seed_once=True)
    b = reg.add_decision(key="seed", title="t", seed_once=True)
    assert a == b
    assert len(reg.list_open()) == 1


def test_seed_known_decisions_no_resurrection(reg):
    """整合：revenue_waterfall_v1 被使用者 resolved 後，再跑 seed 不復活。"""
    reg.seed_known_decisions()
    opens = reg.list_open()
    rev = [it for it in opens if it["key"] == "revenue_waterfall_v1"]
    assert len(rev) == 1
    reg.resolve(rev[0]["id"], "使用者：暫不定案，先擱置")
    # daemon 重啟再 seed
    reg.seed_known_decisions()
    rev_after = [it for it in reg.list_open() if it["key"] == "revenue_waterfall_v1"]
    assert rev_after == [], "重啟不得復活已擱置的分潤決策"
