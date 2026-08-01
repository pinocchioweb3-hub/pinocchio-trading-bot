# -*- coding: utf-8 -*-
"""r86/v192：intent 檔「存在但讀不出來」不可折成「沒這筆」（同物種第 12 次）。

盤點怎麼找到的
--------------
r85 交辦「下一個族群換 tools/atk_consumer/（真錢副本模板本體）」。整檔 26 個
except 逐一判讀，只有這一處還把「未知」折成「確認沒有」：

    for p in sorted(OUTBOX.glob("*.json")):
        try:
            intent = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue              # ← 讀不出來 == 沒這筆，連一行 print 都沒有

為什麼這一處比前幾處更嚴重（不是「反正下輪會重試」）
----------------------------------------------------
訊號產生端 l4_execution/intent_outbox.py 是**依檔名冪等**的：

    p = OUTBOX_DIR / f"{intent['intent_id']}.json"
    if p.exists():
        continue               # 冪等：同訊號永不重寫

⇒ 壞檔一旦生成就**永遠不會被重寫**。於是這筆訊號每輪被靜默跳過，直到 expires_at
到期永久消失——而且連 v169 的「過期丟棄」數字都算不到它頭上（解析失敗發生在讀
expires_at 之前）。整個過程零 print、零健康帳、零告警：與 v164（讀失敗→當成沒有
故障史）、v170（損益查不到→無條件 pop）完全同一物種。

⛔ 本輪已實測：intent_outbox 現有 46 檔全部可解析、健康檔 class_counts 無此類別
⇒ 這是**預防性封堵**（同 v190），⛔ 不得宣稱「已在線上實證」。

順帶封掉的一條當機路徑
----------------------
舊碼的 try 只包 json.loads，下一行 `intent.get(...)` 在 try 之外——檔案內容若是
合法 JSON 但不是物件（例如 `[]`），AttributeError 會直接掀掉整輪消費器。
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "atk_consumer"))

import consume_intents as ci  # noqa: E402
from l3_dispatcher import ceo_oversight as co  # noqa: E402

NOW = 1_785_000_000.0


# ── 消費器端：讀不出來要出聲、要記帳 ─────────────────────────────────────
def test_broken_intent_is_recorded_not_silently_skipped(tmp_path, capsys):
    """最低要求：跳過一筆就要有一個類別長出來，而不是一片安靜。"""
    p = tmp_path / "deadbeef.json"
    p.write_text('{"intent_id": "deadbeef", "symbol": "SOX', encoding="utf-8")  # 半截 JSON
    ci._ROUND_FAILS.clear()
    assert ci._read_intent(p) is None, "讀不出來必須回 None（不可硬吞成空字典）"
    assert "intent_unreadable" in ci._ROUND_FAILS, \
        "讀不出來必須進本輪故障帳，否則永遠不會有人知道有一筆訊號在被跳過"
    assert "deadbeef" in ci._ROUND_FAILS["intent_unreadable"], \
        "樣本必須指名是哪一個檔（檔名＝intent_id），否則人拿不到可動作的資訊"
    assert p.read_text(encoding="utf-8").startswith("{"), "⛔ 偵測端永遠不得動壞檔（那是證據）"


def test_valid_json_that_is_not_an_object_is_also_a_read_failure(tmp_path):
    """`[]` 是合法 JSON 但不是 intent——舊碼會讓它逃出 try，在下一行 .get 掀掉整輪。"""
    p = tmp_path / "cafe0001.json"
    p.write_text("[]", encoding="utf-8")
    ci._ROUND_FAILS.clear()
    assert ci._read_intent(p) is None
    assert "intent_unreadable" in ci._ROUND_FAILS


def test_good_intent_still_reads_through_unchanged(tmp_path):
    """反向護欄：正常檔必須原封不動讀出來，且**不可**記任何故障。"""
    intent = {"intent_id": "ok-1", "symbol": "SOXL", "execution_policy": "demo_only"}
    p = tmp_path / "ok-1.json"
    p.write_text(json.dumps(intent), encoding="utf-8")
    ci._ROUND_FAILS.clear()
    assert ci._read_intent(p) == intent
    assert ci._ROUND_FAILS == {}, "健康的一輪被記成故障＝狼來了，比不記還糟"


def test_new_class_is_wired_into_priority_and_hint_tables():
    """新類別若沒進兩張表，worst_class 選不到它、告警也印不出處置方式。"""
    assert "intent_unreadable" in ci._CLASS_PRIORITY
    assert "intent_unreadable" in ci._CLASS_HINT
    prio = list(ci._CLASS_PRIORITY)
    assert prio.index("intent_unreadable") > prio.index("query_fail"), (
        "⛔ 必須排在連線類**之後**：讀本地檔的失敗在斷流輪照樣會發生，"
        "排太前面會把『對外連不上』這個真正主因從代表類別擠掉（_CLASS_PRIORITY 開頭已載明此原則）"
    )
    assert prio.index("intent_unreadable") < prio.index("other")


def test_main_loop_reads_intents_through_the_helper():
    """反迴歸（源碼層）：主迴圈不得再出現「就地 json.loads + 裸 continue」的舊寫法。

    純函式測試擋不住「helper 寫好了、迴圈卻沒接上」——v157 那一類「改了但沒過河」
    在本專案出現過不只一次。"""
    src = (ROOT / "tools" / "atk_consumer" / "consume_intents.py").read_text(encoding="utf-8")
    assert "_read_intent(p)" in src, "主迴圈必須改走 _read_intent（否則等於沒修）"
    assert "intent = json.loads(p.read_text(encoding=\"utf-8\"))" not in src, \
        "舊的就地解析寫法必須消失，否則兩條路徑並存"


# ── 帳本端：局部跳過不可講成「管線實質停擺」 ─────────────────────────────
def _health(cls, rounds=5):
    return {"consecutive_fail_rounds": rounds, "last_fail_class": cls,
            "updated_at": NOW - 60, "first_fail_ts": NOW - 600,
            "class_counts": {cls: rounds},
            "last_fail_sample": "intent 檔讀不出來（deadbeef.json，JSONDecodeError）"}


def test_ledger_does_not_call_a_skipped_intent_a_pipeline_stall():
    """r77 治過同一種「把代價講錯」：通用句會說『管線實質停擺』——這裡是假的。"""
    v = co.live_exec_verdict(_health("intent_unreadable"), now_s=NOW)
    assert v is not None, "連續 5 輪讀不出來仍必須是可見的故障"
    assert "管線實質停擺" not in v["text"], \
        "⛔ 只有一筆訊號被跳過，其餘照常運作——講成整條管線停擺會把人導向錯誤處置"
    assert "照常" in v["text"], "必須明講『其餘部分照常』，否則讀者只能自己猜嚴重度"
    assert "永久" in v["text"], "必須明講過期後永久消失，這是唯一會逼人動手的資訊"


def test_skipped_intent_is_a_system_fault_not_a_user_blocker():
    """球在工程端（壞檔要查為什麼壞、確認後刪掉），不是「等使用者去 OKX 操作」。"""
    v = co.live_exec_verdict(_health("intent_unreadable"), now_s=NOW)
    assert v["user_actionable"] is False


def test_unknown_classes_still_get_the_stalled_wording():
    """反向護欄：⛔ 不可為了本輪把通用句整個改軟——真的斷流仍要說停擺。"""
    v = co.live_exec_verdict(_health("some_new_unknown_class"), now_s=NOW)
    assert "管線實質停擺" in v["text"]
