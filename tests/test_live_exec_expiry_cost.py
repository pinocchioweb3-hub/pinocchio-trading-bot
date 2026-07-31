# -*- coding: utf-8 -*-
"""斷流的**實質代價**要進監督帳本，不能只躺在健康檔（v169・監督員 r67）。

consume_intents 這端（v169）已經把「過期丟棄」記成數字；但使用者不讀健康檔，
他讀的是帳本裡那一行阻塞描述。目前那行只說「連續 N 輪被擋、零損失」——
「零損失」是對的（沒有下錯單），卻讀起來像「不急」。實際上 2026-07-31 那場
18 小時斷流已經永久吃掉 4 筆訊號（過了 expires_at 就不補送）。

代價要跟阻塞描述貼在一起，使用者才有辦法判斷該多急著修。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from l3_dispatcher.ceo_oversight import live_exec_verdict  # noqa: E402

NOW = 1_785_000_000.0


def _health(**kw):
    base = {"consecutive_fail_rounds": 1071, "updated_at": NOW - 30,
            "last_fail_class": "auth_ip_whitelist", "first_fail_ts": NOW - 64000}
    base.update(kw)
    return base


def test_expiry_cost_is_surfaced_in_the_blocker_text():
    v = live_exec_verdict(_health(expired_dropped_during_fault=4), now_s=NOW)
    assert v is not None
    assert v.get("expired_dropped") == 4
    assert "4" in v["text"]
    assert "永久" in v["text"] or "不會補送" in v["text"], \
        "要講清楚是永久丟失、不是延後執行——否則使用者會以為修好就會自動補上"


def test_no_drops_means_no_extra_noise():
    """反向護欄：沒丟就不提。每輪都掛一句『已丟棄 0 筆』會讓這行字失去警示力。"""
    v = live_exec_verdict(_health(), now_s=NOW)
    assert v is not None
    assert not v.get("expired_dropped")
    assert "丟棄" not in v["text"]


def test_expiry_cost_never_changes_whether_it_blocks():
    """代價只影響措辭，不可影響判斷。streak 未達門檻仍然是 None——
    ⛔ 不可因為『有丟棄』就把未達門檻的雜訊升成阻塞。"""
    assert live_exec_verdict(_health(consecutive_fail_rounds=1,
                                     expired_dropped_during_fault=99),
                             now_s=NOW) is None
    # 舊快照（執行器沒在跑）同理不可因為有代價數字就復活
    assert live_exec_verdict(_health(updated_at=NOW - 99999,
                                     expired_dropped_during_fault=99),
                             now_s=NOW) is None


def test_corrupt_cost_field_never_breaks_the_verdict():
    """健康檔是自製檔，欄位可能是壞值。代價讀不出來不可以把整個阻塞判定弄消失
    ——那會讓一場真斷流從帳本上憑空不見（fail-closed 方向：判定照舊、代價從缺）。"""
    for bad in ("many", None, [], {"n": 4}, float("nan")):
        v = live_exec_verdict(_health(expired_dropped_during_fault=bad), now_s=NOW)
        assert v is not None, f"壞值 {bad!r} 不可害死阻塞判定"
        assert "連續 1071 輪" in v["text"]


def test_text_carries_no_key_id_or_ip():
    """帳本會被貼進聊天室／截圖：只可出現次數，不可帶出金鑰 id 或出口 IP。"""
    v = live_exec_verdict(_health(expired_dropped_during_fault=4), now_s=NOW)
    assert "whitelist" not in v["text"].lower()
    assert not any(c.isdigit() and "." in v["text"][max(0, i - 3):i + 3]
                   for i, c in enumerate(v["text"]) if False)  # 佔位：無 IP 樣式
    import re
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", v["text"]), "不可出現 IP"
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", v["text"]), "不可出現 key-id"
