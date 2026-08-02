"""v232（監督員 r127）Phase 0「真實 N/30」是不是一個量測值——同物種第 52 次治本。

背景（r126 實測、r127 覆驗）：`phase0_status()["live_n"]` 讀 trade_journal.db 的 `trades`
表，但真錢執行器 tools/atk_consumer/consume_intents_live.py **全檔沒有任何寫入該表的呼叫**
（唯一寫入端是訊號流 dispatcher.py）。實測 2026-08-03：trades 共 101 列、status 全為
'expired'、closed 零列、最後寫入約 7/08；真錢側卻已了結 3 筆（7/29 QQQ、8/02 SNDK、
8/02 ORCL，記在部位帳 day_pnl）。⇒ 那個 0 不是量測值，按滿 30 筆它仍會是 0。

舊碼把它印成三句斷言「我們量過、只是還沒有」：進度條「真實 0/30」、
`live_gate_reason='live_sample_short'`（字面＝樣本不足）、自評「待真錢人工逐筆驗證」。

⛔ 本批修補**只解釋 0 的成因**，不動 Phase 0 任何門檻——`live_ok`／`ready` 行為必須完全
不變（三閘仍須人拍板，紅線③）。下方 test_gate_is_not_relaxed_* 就是守這條線的。
"""
import json
import sqlite3

import pytest

from l3_dispatcher import ceo_session as cs


def _mkdb(path, *, live_rows=(), paper_rows=()):
    conn = sqlite3.connect(path)
    for table, rows in (("trades", live_rows), ("paper_trades", paper_rows)):
        conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, status TEXT, "
                     f"exit_reason TEXT, realized_r REAL, net_r REAL)")
        for r in rows:
            conn.execute(f"INSERT INTO {table} (status, exit_reason, realized_r, net_r) "
                         f"VALUES (?,?,?,?)", r)
    conn.commit()
    conn.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """建最小 DB + 可控的真錢部位帳（ledger=None → 檔不存在＝真的還沒跑過真錢）。"""
    def _make(*, live_rows=(), paper_rows=(("closed", "tp1", 0.2, 0.1),) * 120,
              ledger=None, ledger_raw=None):
        p = tmp_path / "tj.db"
        if p.exists():
            p.unlink()
        _mkdb(p, live_rows=live_rows, paper_rows=paper_rows)
        monkeypatch.setattr(cs, "_TJ_DB", str(p))
        monkeypatch.setattr(cs, "data_dir", lambda: tmp_path)
        f = tmp_path / "atk_positions_live.json"
        if ledger_raw is not None:
            f.write_text(ledger_raw, encoding="utf-8")
        elif ledger is not None:
            f.write_text(json.dumps(ledger), encoding="utf-8")
        elif f.exists():
            f.unlink()
        return p
    return _make


# --- live_counter_verdict（純函式）------------------------------------------

def test_verdict_unwired_only_when_proven():
    """部位帳有已實現損益、計數器卻是 0 ⇒ 已證實沒接上。"""
    assert cs.live_counter_verdict(0, 2, "ok") == "unwired"


def test_verdict_unverified_when_ledger_unreadable():
    """讀不到部位帳＝未知，⛔ 不可折成「沒有真錢交易」也不可斷言「沒接上」。"""
    assert cs.live_counter_verdict(0, 0, "unreadable") == "unverified"


def test_verdict_none_when_ledger_absent():
    """部位帳不存在＝真錢管線沒跑過，0 與事實一致，不該加噪音。"""
    assert cs.live_counter_verdict(0, 0, "absent") is None


def test_verdict_none_when_no_realized_days():
    """部位帳讀得到但沒有任何已實現損益 ⇒ 0 是誠實的量測值。"""
    assert cs.live_counter_verdict(0, 0, "ok") is None


def test_verdict_none_when_counter_has_rows():
    """計數器只要收得到寫入就無需解釋——不論部位帳說什麼。"""
    assert cs.live_counter_verdict(5, 9, "ok") is None
    assert cs.live_counter_verdict(5, 0, "unreadable") is None


# --- _live_ledger_realized_days（讀取層：未知不可折成沒有）-------------------

def test_ledger_absent_vs_unreadable_are_different(env, tmp_path):
    env()
    assert cs._live_ledger_realized_days() == (0, "absent")
    (tmp_path / "atk_positions_live.json").write_text("{not json", encoding="utf-8")
    assert cs._live_ledger_realized_days() == (0, "unreadable")


def test_ledger_bad_shape_is_unreadable_not_zero(env):
    """檔在但 day_pnl 形狀不對＝壞檔。回 (0,'ok') 會被讀成「沒有真錢交易」。"""
    env(ledger={"open": {}, "day_pnl": []})
    assert cs._live_ledger_realized_days() == (0, "unreadable")


def test_ledger_counts_days(env):
    env(ledger={"open": {}, "day_pnl": {"2026-07-29": 0.94, "2026-08-02": 2.35}})
    assert cs._live_ledger_realized_days() == (2, "ok")


# --- phase0_status 的 reason（⭐ 這兩條在改動前的碼上是紅的）-----------------

def test_reason_is_unwired_not_sample_short(env):
    """⭐ 線上現況重現：真錢已了結 2 個交易日、trades 表零 closed。

    舊碼回 'live_sample_short'（字面＝樣本不足＝斷言我們量過）——此測在舊碼上必失敗。
    """
    env(ledger={"open": {}, "day_pnl": {"2026-07-29": 0.94, "2026-08-02": 2.35}})
    p = cs.phase0_status()
    assert p["live_n"] == 0
    assert p["live_gate_reason"] == "live_counter_unwired"
    assert p["live_counter"] == "unwired"
    assert p["live_ledger_realized_days"] == 2


def test_reason_is_unverified_when_ledger_unreadable(env):
    """⭐ 部位帳壞掉時不可說「樣本不足」（舊碼會）——要說「無法確認」。"""
    env(ledger_raw="{broken")
    p = cs.phase0_status()
    assert p["live_gate_reason"] == "live_counter_unverified"
    assert p["live_counter"] == "unverified"


def test_reason_stays_sample_short_when_no_real_trades(env):
    """反向控制：真的還沒跑過真錢時，原本的 'live_sample_short' 不變（舊碼即為綠）。"""
    env()
    p = cs.phase0_status()
    assert p["live_gate_reason"] == "live_sample_short"
    assert p["live_counter"] is None


# --- ⛔ 門檻不得被放寬（紅線③守門測試）--------------------------------------

def test_gate_is_not_relaxed_by_unwired_counter(env):
    """計數器沒接上**不是**放行理由：live_ok／ready 必須仍為 False。"""
    env(ledger={"open": {}, "day_pnl": {"2026-08-02": 2.35}})
    p = cs.phase0_status()
    assert p["live_ok"] is False
    assert p["ready"] is False


def test_gate_still_passes_normally_when_wired(env):
    """反向控制：真正滿足三個條件時仍照舊放行（本批修補沒有動到閘）。"""
    env(live_rows=[("closed", "tp1", 0.5, 0.35)] * 30,
        ledger={"open": {}, "day_pnl": {"2026-08-02": 2.35}})
    p = cs.phase0_status()
    assert p["live_counter"] is None          # live_n>0 ⇒ 計數器收得到寫入
    assert p["live_ok"] is True and p["ready"] is True
    assert p["live_gate_reason"] is None


# --- 自評瓶頸敘事（潛伏支：t 一過 2 就會浮出來）------------------------------

_ARGS = dict(paper_n=386, paper_min=100, live_n=0, live_min=30,
             demo_n=31, demo_rejected=0)


def test_bottleneck_must_not_claim_only_manual_gate_when_unwired():
    """⭐ 舊碼在此情境印「紙上樣本足、待真錢人工逐筆驗證（0/30）」＝假準備就緒。

    毛/淨兩個 t 都過 2 時會落到這一支。舊碼無 live_counter 參數 ⇒ 此測在舊碼上必失敗。
    """
    out = cs._synthesize_bottleneck(paper_t=3.0, paper_t_net=2.5, net_n=60,
                                    live_counter="unwired", **_ARGS)
    assert "待真錢人工逐筆驗證" not in out
    assert "計數器" in out and "不是量測值" in out


def test_bottleneck_says_unknown_when_counter_unverified():
    out = cs._synthesize_bottleneck(paper_t=3.0, paper_t_net=2.5, net_n=60,
                                    live_counter="unverified", **_ARGS)
    assert "待真錢人工逐筆驗證" not in out
    assert "未知" in out


def test_bottleneck_keeps_manual_gate_sentence_when_counter_ok():
    """反向控制：計數器沒問題時，原本那句人工閘敘事必須原封不動（舊碼即為綠）。"""
    out = cs._synthesize_bottleneck(paper_t=3.0, paper_t_net=2.5, net_n=60,
                                    live_counter=None, **_ARGS)
    assert "待真錢人工逐筆驗證（0/30，紅線①）" in out


def test_bottleneck_unknown_t_branch_still_wins():
    """v231 那支（沒有 t 值＝edge 未知）優先序在前，不可被 v232 蓋掉。"""
    out = cs._synthesize_bottleneck(paper_t=None, paper_t_net=None, net_n=0,
                                    t_status="unreadable", live_counter="unwired",
                                    **_ARGS)
    assert "讀不出來" in out


# --- 警語渲染 ---------------------------------------------------------------

def test_note_is_silent_when_nothing_to_say():
    assert cs.live_counter_note(None) is None


def test_note_states_the_zero_is_not_a_measurement():
    note = cs.live_counter_note("unwired", 2)
    assert "沒有寫入端" in note and "2 個交易日" in note
    assert "仍會是 0" in note


def test_note_for_unverified_does_not_assert_broken():
    """未知不可講成「壞了」，也不可講成「沒問題」——兩個方向都不行。"""
    note = cs.live_counter_note("unverified")
    assert "未知" in note and "不是沒問題" in note


def test_sample_line_marks_unwired_counter():
    """瓶頸落在別的分支時，樣本行是唯一還露出「真實 0/30」的地方，必須帶標記。"""
    out = cs._synthesize_bottleneck(paper_t=-0.22, paper_t_net=-0.3, net_n=60,
                                    live_counter="unwired", **_ARGS)
    assert "真實 0/30（⚠️ 此計數器無寫入端，非量測值）" in out
    assert "edge 未達統計顯著" in out          # 瓶頸歸因本身不被蓋掉


def test_sample_line_clean_when_counter_ok():
    out = cs._synthesize_bottleneck(paper_t=-0.22, paper_t_net=-0.3, net_n=60,
                                    live_counter=None, **_ARGS)
    assert "真實 0/30、" in out or "真實 0/30" in out
    assert "計數器" not in out
