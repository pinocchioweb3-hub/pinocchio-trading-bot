"""CEO 簡報「🩺 系統」那一行 —— 未知不得印成「核心管線正常」（紅線③）。

同物種第 49 次。落點是**整份 CEO 日報的第一句健康結論**，而且線上每天 100% 觸發。

實測（本機真資料目錄，2026-08-03）：
    _probe_backtest_freshness() -> ('ok', '回測結果新鮮（-20640457.4 天前）')

兩個獨立破口疊在同一行上：

① **單位錯讀，讓「回測多久沒跑」這個檢查結構上永遠不會響**。
   backtest_session.py:154 寫入 `run_ts = int(time.time() * 1000)`＝**毫秒**
   （telegram_bot/callbacks.py:382 讀的時候有 `// 1000`，全 repo 只有 ceo_session
   這一個消費端沒有）。ceo_session 拿毫秒直接和 `time.time()` 的秒相減 ⇒ age 恆為
   約 −2064 萬天 ⇒ `age_d > 10` 永遠不成立 ⇒ 不論回測停跑多久，探針一律回
   「新鮮」。這不是「數字難看」而已：這條探針是「回測 Session 卡住」的唯一自動偵測，
   它從寫下的第一天起就沒有偵測能力。

② **探針明講「讀不出來」的那一票，在彙總時被靜靜丟掉**。
   `_probe_queue` / `_probe_backtest_freshness` 讀取失敗時回的是 `info`，而產出端
   `_section_normal()` 只收 `sev == "warn"` ⇒ 一句「queue 狀態讀取失敗」被丟掉之後，
   剩下空集合 ⇒ 走 else 印出「🩺 系統：全部 worker 由監督器看顧，核心管線正常」。
   ⇒ 探針講「我不知道」，簡報印「一切正常」。這正是 v225/v226/v227/v228 同一個物種，
   只是這次折出來的是**整份日報的健康總結句**。

③ 順帶（同一句）：「全部 worker 由監督器看顧」這件事**沒有任何一支探針量過**。
   全套探針只有三支：交易帳本可讀、queue 深度、回測新鮮度。斷言 worker 受監督
   屬於無憑據的正面宣稱，改為只講量到的那三件事。

判準寫在**產出端**（沿用 v227/v228 的「鍵在＝答案」作法）：探針把「答不出來」表示成
獨立的 `unknown` 嚴重度，產出端三態渲染——有 warn 印 warn；沒 warn 但有 unknown 印
🟡 並具名是哪一項未知、明講「狀態未知，不是沒問題」；三支都真的量到才維持 ✅。

⛔ 邊界另一側同樣釘死（不得矯枉過正）：
  * 真的量到且都健康時，仍要給乾淨的正面結論，不得冒出未知字樣；
  * 探針永不拋例外（CEO 簡報必須永遠產得出來）；
  * DB 異常、queue 塞單這兩個既有的 warn 行為逐字不變。

執行（任一）：
    pytest tests/test_ceo_health_probes.py
    python tests/test_ceo_health_probes.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import l3_dispatcher.ceo_session as ceo

DAY = 86400.0


# ---------------------------------------------------------------- helpers
def _health_line(health: list[tuple[str, str]]) -> str:
    """把一組探針結果餵進真正的產出端，取回 🩺 那一段（含 ↳ 續行）。"""
    with mock.patch.object(ceo, "feature_health", lambda: list(health)):
        out = ceo._section_normal()
    block: list[str] = []
    for line in out.split("\n"):
        if line.startswith("🩺"):
            block.append(line)
        elif block:
            if line.startswith("　　↳"):
                block.append(line)
            else:
                break
    if not block:
        raise AssertionError("CEO 簡報缺少 🩺 系統行")
    return "\n".join(block)


def _bt_probe(strats, latest):
    """在受控的 backtest/registry 之下跑回測新鮮度探針。"""
    import backtest.backtest_session as bs
    import l2_trigger.registry as reg
    with mock.patch.object(reg, "scheduler_strategies", lambda: strats), \
         mock.patch.object(bs, "latest_backtest", latest):
        return ceo._probe_backtest_freshness()


# ============================================================ 正向側（HEAD 上必須紅）
def test_stale_backtest_must_warn_despite_ms_timestamp():
    """回測 40 天沒跑（run_ts 是毫秒）→ 必須警告，不得回「新鮮」。"""
    ts_ms = int((time.time() - 40 * DAY) * 1000)
    sev, msg = _bt_probe(["intraday"], lambda sid: {"run_ts": ts_ms})
    assert sev == "warn", f"回測停跑 40 天卻回 {sev!r}：{msg}"
    assert "新鮮" not in msg
    assert "40" in msg


def test_fresh_backtest_age_must_be_plausible():
    """run_ts 是毫秒 → 換算出來的天數必須是真的天數，不得是負兩千萬天。"""
    ts_ms = int((time.time() - 2 * DAY) * 1000)
    sev, msg = _bt_probe(["intraday"], lambda sid: {"run_ts": ts_ms})
    assert sev == "ok", msg
    assert "2.0 天前" in msg, msg
    assert "-" not in msg, f"age 算成負數＝單位讀錯：{msg}"


def test_future_timestamp_is_unknown_not_fresh():
    """時間戳落在未來（時鐘偏移／寫壞）→ 算不出屋齡就說算不出來，不得宣稱新鮮。"""
    ts_ms = int((time.time() + 5 * DAY) * 1000)
    sev, msg = _bt_probe(["intraday"], lambda sid: {"run_ts": ts_ms})
    assert sev == "unknown", f"未來時間戳卻回 {sev!r}：{msg}"
    assert "回測結果新鮮" not in msg, msg      # ⛔ 不得斷言新鮮
    assert "算不出來" in msg, msg


def test_backtest_read_failure_reaches_the_brief():
    """回測新鮮度讀取失敗 → 🩺 那一行必須講出來，不得印成一切正常。"""
    def _boom(sid):
        raise OSError("db locked")
    sev, msg = _bt_probe(["intraday"], _boom)
    assert sev == "unknown", f"讀取失敗卻回 {sev!r}：{msg}"
    line = _health_line([("ok", "交易帳本 DB 可讀"), ("ok", "訊號 queue 暢通"), (sev, msg)])
    assert "正常" not in line, line
    assert "回測" in line and "未知" in line, line


def test_queue_read_failure_reaches_the_brief():
    """queue 狀態讀不出來 → 🩺 那一行必須講出來，不得被靜靜丟掉。"""
    import l3_dispatcher.fire_queue as fq

    def _boom():
        raise RuntimeError("fire_queue.db missing")
    with mock.patch.object(fq, "stats", _boom):
        sev, msg = ceo._probe_queue()
    assert sev == "unknown", f"讀取失敗卻回 {sev!r}：{msg}"
    line = _health_line([("ok", "交易帳本 DB 可讀"), (sev, msg), ("ok", "回測結果新鮮（1.0 天前）")])
    assert "正常" not in line, line
    assert "queue" in line and "未知" in line, line


def test_no_backtest_history_is_unknown():
    """一筆回測歷史都沒有 → 無從斷言新鮮，屬未知，且必須浮上 🩺 行。"""
    sev, msg = _bt_probe(["intraday"], lambda sid: None)
    assert sev == "unknown", f"零歷史卻回 {sev!r}：{msg}"
    line = _health_line([("ok", "交易帳本 DB 可讀"), ("ok", "訊號 queue 暢通"), (sev, msg)])
    assert "正常" not in line, line


def test_all_clear_must_not_claim_unmeasured_worker_supervision():
    """三支探針都過時，正面結論只能講量到的那三件事。"""
    line = _health_line([("ok", "交易帳本 DB 可讀"),
                         ("ok", "訊號 queue 暢通（queued=0）"),
                         ("ok", "回測結果新鮮（1.2 天前）")])
    assert "worker" not in line, f"斷言了沒有任何探針量過的事：{line}"
    assert "帳本" in line and "queue" in line and "回測" in line, line


def test_warn_and_unknown_coexist_warn_wins_but_unknown_not_lost():
    """同時有故障與未知 → 故障優先，但未知不得因此消失。"""
    line = _health_line([("warn", "交易帳本 DB 異常：OperationalError"),
                         ("unknown", "queue 狀態讀取失敗：RuntimeError"),
                         ("ok", "回測結果新鮮（1.0 天前）")])
    assert "交易帳本 DB 異常" in line, line
    assert "queue" in line and "未知" in line, line


# ============================================================ 反向側（HEAD 上就必須綠）
def test_db_failure_still_warns_verbatim():
    """既有 warn 行為逐字不變：DB 讀不到＝異常，不是未知。"""
    with mock.patch.object(ceo, "_TJ_DB", "Z:/no/such/dir/trade_journal.db"):
        sev, msg = ceo._probe_db()
    assert sev == "warn"
    assert "交易帳本 DB 異常" in msg
    line = _health_line([(sev, msg), ("ok", "訊號 queue 暢通"), ("ok", "回測結果新鮮（1.0 天前）")])
    assert "交易帳本 DB 異常" in line


def test_queue_backlog_still_warns_with_count():
    """既有 warn 行為逐字不變：queue 塞 12 筆仍要報 12。"""
    import l3_dispatcher.fire_queue as fq
    with mock.patch.object(fq, "stats", lambda: {"queued": 12}):
        sev, msg = ceo._probe_queue()
    assert sev == "warn"
    assert "12" in msg


def test_healthy_line_stays_clean():
    """真的全部量到且健康時，不得冒出未知/讀不出來字樣（不矯枉過正）。"""
    line = _health_line([("ok", "交易帳本 DB 可讀"),
                         ("ok", "訊號 queue 暢通（queued=0）"),
                         ("ok", "回測結果新鮮（1.2 天前）")])
    for bad in ("未知", "讀不出來", "⚠️", "🟡"):
        assert bad not in line, f"健康時不該出現 {bad}：{line}"


def test_probes_never_raise():
    """CEO 簡報必須永遠產得出來：底層全炸，探針仍要回三組結果。"""
    import backtest.backtest_session as bs
    import l2_trigger.registry as reg
    import l3_dispatcher.fire_queue as fq

    def _boom(*a, **k):
        raise RuntimeError("everything is on fire")
    with mock.patch.object(ceo, "_TJ_DB", "Z:/no/such/dir/x.db"), \
         mock.patch.object(fq, "stats", _boom), \
         mock.patch.object(reg, "scheduler_strategies", _boom), \
         mock.patch.object(bs, "latest_backtest", _boom):
        health = ceo.feature_health()
    assert len(health) == 3
    assert all(isinstance(x, tuple) and len(x) == 2 for x in health)


def test_queue_normal_still_ok():
    """queue 正常時維持 ok（不得把暢通誤標成未知）。"""
    import l3_dispatcher.fire_queue as fq
    with mock.patch.object(fq, "stats", lambda: {"queued": 0}):
        sev, msg = ceo._probe_queue()
    assert sev == "ok"
    assert "暢通" in msg


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
