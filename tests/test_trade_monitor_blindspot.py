"""task#77：trade_monitor 出場偵測「poll 窗盲區」治本的回歸鎖。

定位：舊版 monitor_once / _monitor_paper 每輪只抓最近 4 根 5m K（20 分鐘窗）。穩態下
（poll 15min > 窗 20min，5min 重疊）雖無盲區，但「漏一輪 poll／抓取暫時失敗（重啟、
限流、缺口）」就讓事件捲出窗永久全盲 → 漏記 TP/SL → 紙上樣本系統性失真（多為悲觀
少記 TP）。這條樣本曲線正是紅線①實盤解鎖判據的統計基礎，故灌水/失真都不可接受。

治本＝「檢查點式 gap-fill」：每筆單記「最後一根已確認 5m bar」的檢查點 last_checked_ts；
每輪改抓「自檢查點之後的所有已確認 bar」（根數由 gap 推算，單次上限 SINCE_FETCH_CAP）；
只在成功抓到 K 線後前進檢查點（抓取失敗不前進＝下輪自然重抓同段窗，no silent loss）；
gap 超過單次上限時誠實留痕 gap_exceeded（no silent cap＝紅線③）。

本檔鎖死的語意：
  1. _get_bars_since：根數依 gap 推算（小 gap→floor 4、中 gap→need、大 gap→cap 300+gap_exceeded）。
  2. _get_bars_since：只納「已確認」且「ts 嚴格大於檢查點」的 bar（成形 bar 剔除、檢查點那根不重判）。
  3. _get_bars_since：抓取失敗→None（呼叫端不前進檢查點）；抓到但無新 bar→latest_confirmed_ts=None。
  4. _resolve_since：檢查點優先；NULL（既有列/首輪）退回「真實成交起點」（split 取 min filled_at，
     full 取 entry_at），⚠️絕不用計畫 entry_at 走訪 split 單（前一 agent 誤判 +11.41R 幽靈 TP 的根因）。
  5. set_trade_checkpoint / set_paper_checkpoint：單調，永不倒退。
  6. record_leg / apply_paper_event：監控腿冪等去重（at-least-once 輪詢重抓同根 bar 不雙記）。
  7. 盲區情境：TP 在窄窗外觸發 → 窄窗(n=4)漏記、寬窗(since-checkpoint)補回（治本價值的直證）。

純離線：helpers 為純函式 / async（FakeOkxSource 免網路）；DB 測 monkeypatch DB_PATH 到 tmp。
零真錢、零訊號數學。註：trades（實倉）路徑同步改了帳本入帳；目前 trades 表 0 列故無 live 風險，
本檔以空表/合成列回歸，真錢上線前須再以空表回歸（使用者已釘此決策）。
"""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from l3_dispatcher import trade_monitor as tm
from l3_dispatcher import trade_journal as tj
from l3_dispatcher import paper_journal as pj
from l3_dispatcher.trade_journal import EntryRecord

# 固定 TP 分批，讓 _check_trade 不受 botconfig.tp_size_split 影響
SPLIT = {"tp1": 0.5, "tp2": 0.3, "tp3": 0.2}

FIXED_NOW_MS = 1_700_000_000_000   # 固定「現在」（ms），time monkeypatch 用
FIXED_NOW_S = FIXED_NOW_MS / 1000.0
FIVE = tm.FIVE_MIN_MS              # 300_000


# ---------------------------------------------------------------------------
# 假資料源：類名小寫須含 "okx"，_get_recent_5m_bars 才會走 source.get_candles
# （否則它會 new 一個真 OkxCandlesSource 打網路）。
# ---------------------------------------------------------------------------
class FakeOkxSource:
    def __init__(self, candles):
        self._candles = candles
        self.last_n = None
        self.calls = 0

    async def get_candles(self, symbol, interval, n):
        self.last_n = n
        self.calls += 1
        return {"candles": [dict(c) for c in self._candles]}

    async def close(self):
        pass


class FailOkxSource:
    """模擬抓取失敗（回 None / 非 dict）→ _get_bars_since 必須回 None。"""
    def __init__(self):
        self.last_n = None

    async def get_candles(self, symbol, interval, n):
        self.last_n = n
        return None

    async def close(self):
        pass


def _c(ts, hi, lo, cl, confirm=True):
    return {"ts": ts, "high": hi, "low": lo, "close": cl,
            "volume": 1.0, "volume_usd": 1.0, "confirm": confirm}


def _trade(direction, stop, tp1, tp2, tp3, legs_hit=None, size_remaining=1.0,
           entry=100.0):
    return {"direction": direction, "entry_price": entry, "stop_price": stop,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "legs_hit": legs_hit or [], "size_remaining": size_remaining}


# ===========================================================================
# 1. _get_bars_since：根數依 gap 推算
# ===========================================================================
def test_bars_since_limit_medium_gap(monkeypatch):
    monkeypatch.setattr(tm.time, "time", lambda: FIXED_NOW_S)
    src = FakeOkxSource([])
    since = FIXED_NOW_MS - 10 * FIVE          # gap = 10 根
    res = asyncio.run(tm._get_bars_since(src, "BTC", since))
    # need = 10 + 3 = 13；limit = max(4, min(13, 300)) = 13
    assert src.last_n == 13, src.last_n
    assert res is not None and res["gap_exceeded"] is False


def test_bars_since_limit_floor_four_on_small_gap(monkeypatch):
    monkeypatch.setattr(tm.time, "time", lambda: FIXED_NOW_S)
    src = FakeOkxSource([])
    res = asyncio.run(tm._get_bars_since(src, "BTC", FIXED_NOW_MS))   # gap=0
    # need = 0 + 3 = 3；但有 floor 4 → limit = 4
    assert src.last_n == 4, src.last_n
    assert res["gap_exceeded"] is False


def test_bars_since_caps_at_300_and_flags_gap_exceeded(monkeypatch):
    monkeypatch.setattr(tm.time, "time", lambda: FIXED_NOW_S)
    src = FakeOkxSource([])
    since = FIXED_NOW_MS - 1000 * FIVE        # gap = 1000 根 → need 1003 > cap
    res = asyncio.run(tm._get_bars_since(src, "BTC", since))
    assert src.last_n == tm.SINCE_FETCH_CAP == 300, src.last_n
    assert res["gap_exceeded"] is True        # 誠實留痕，no silent cap


# ===========================================================================
# 2. _get_bars_since：confirm 過濾 + ts 嚴格大於檢查點
# ===========================================================================
def test_bars_since_filters_unconfirmed_and_at_or_before_since(monkeypatch):
    monkeypatch.setattr(tm.time, "time", lambda: FIXED_NOW_S)
    S = FIXED_NOW_MS - 20 * 60_000            # 檢查點（20 分鐘前）
    candles = [
        _c(S - FIVE, 101, 99, 100),           # 早於檢查點 → 剔除
        _c(S,        102, 100, 101),          # 等於檢查點（非嚴格大於）→ 剔除
        _c(S + FIVE, 106, 101, 104),          # 納入
        _c(S + 2 * FIVE, 107, 103, 105),      # 納入
        _c(S + 3 * FIVE, 108, 104, 106, confirm=False),  # 成形 bar → 剔除
    ]
    src = FakeOkxSource(candles)
    res = asyncio.run(tm._get_bars_since(src, "BTC", S))
    got = res["candles"]
    assert [b["ts"] for b in got] == [S + FIVE, S + 2 * FIVE], got
    assert all(b["confirm"] for b in got)
    assert all(b["ts"] > S for b in got)
    assert res["latest_confirmed_ts"] == S + 2 * FIVE


def test_bars_since_no_new_bars_returns_dict_with_none_latest(monkeypatch):
    """抓到了但全在檢查點之前/等於 → 回 dict（非 None）但 latest=None（呼叫端不前進檢查點）。"""
    monkeypatch.setattr(tm.time, "time", lambda: FIXED_NOW_S)
    S = FIXED_NOW_MS - 20 * 60_000
    src = FakeOkxSource([_c(S - FIVE, 101, 99, 100), _c(S, 102, 100, 101)])
    res = asyncio.run(tm._get_bars_since(src, "BTC", S))
    assert res is not None
    assert res["candles"] == []
    assert res["latest_confirmed_ts"] is None


# ===========================================================================
# 3. _get_bars_since：抓取失敗 → None（契約：呼叫端不前進檢查點）
# ===========================================================================
def test_bars_since_fetch_failure_returns_none(monkeypatch):
    monkeypatch.setattr(tm.time, "time", lambda: FIXED_NOW_S)
    src = FailOkxSource()
    res = asyncio.run(tm._get_bars_since(src, "BTC", FIXED_NOW_MS - 5 * FIVE))
    assert res is None


# ===========================================================================
# 4. _fill_start_from_splits / _resolve_since
# ===========================================================================
def test_fill_start_full_trade_uses_entry_at():
    assert tm._fill_start_from_splits(None, 777) == 777
    assert tm._fill_start_from_splits("", 777) == 777
    assert tm._fill_start_from_splits("[]", 777) == 777   # 有解析、但無成交腿


def test_fill_start_split_uses_min_filled_at():
    splits = [{"price": 1, "frac": 0.6, "filled": 1, "filled_at": 800},
              {"price": 2, "frac": 0.4, "filled": 1, "filled_at": 500}]
    assert tm._fill_start_from_splits(json.dumps(splits), 999) == 500
    # 接受已解析的 list（非 JSON 字串）
    assert tm._fill_start_from_splits(splits, 999) == 500


def test_fill_start_split_partial_only_counts_filled():
    splits = [{"price": 1, "frac": 0.6, "filled": 1, "filled_at": 650},
              {"price": 2, "frac": 0.4, "filled": 0, "filled_at": None}]
    assert tm._fill_start_from_splits(json.dumps(splits), 999) == 650


def test_fill_start_unfilled_splits_fall_back_to_entry_at():
    splits = [{"price": 1, "frac": 0.6, "filled": 0, "filled_at": None},
              {"price": 2, "frac": 0.4, "filled": 0, "filled_at": None}]
    assert tm._fill_start_from_splits(json.dumps(splits), 321) == 321


def test_fill_start_bad_json_falls_back_to_entry_at():
    assert tm._fill_start_from_splits("{not valid json", 555) == 555


def test_resolve_since_prefers_checkpoint():
    t = {"last_checked_ts": 1234, "entry_at": 99, "entry_splits": None}
    assert tm._resolve_since(t) == 1234


def test_resolve_since_null_checkpoint_falls_back_to_entry_at():
    t = {"last_checked_ts": None, "entry_at": 99, "entry_splits": None}
    assert tm._resolve_since(t) == 99
    # ts=0 視同無檢查點（1970 不可能是真檢查點）→ 退回 entry_at
    assert tm._resolve_since({"last_checked_ts": 0, "entry_at": 42,
                              "entry_splits": None}) == 42


def test_resolve_since_split_trade_uses_fill_start_not_signal_entry_at():
    """split 單檢查點為 NULL 時 → 走 min(filled_at)，不從計畫 entry_at 重播（防幽靈 TP）。"""
    splits = [{"price": 1, "frac": 0.6, "filled": 1, "filled_at": 500},
              {"price": 2, "frac": 0.4, "filled": 0, "filled_at": None}]
    t = {"last_checked_ts": None, "entry_at": 99, "entry_splits": json.dumps(splits)}
    assert tm._resolve_since(t) == 500


# ===========================================================================
# 5. set_trade_checkpoint / set_paper_checkpoint：單調，永不倒退
# ===========================================================================
def _read_trade_ckpt(db_path, tid):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT last_checked_ts FROM trades WHERE id=?", (tid,)).fetchone()[0]
    finally:
        conn.close()


def _read_paper_ckpt(db_path, pid):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT last_checked_ts FROM paper_trades WHERE id=?", (pid,)).fetchone()[0]
    finally:
        conn.close()


def test_set_trade_checkpoint_monotonic(monkeypatch, tmp_path):
    db = str(tmp_path / "trade_journal.db")
    monkeypatch.setattr(tj, "DB_PATH", db)
    tj.init_db()
    tid = tj.record_entry(EntryRecord(
        symbol="BTC", setup="deepdive", direction="bull",
        entry_price=100, stop_price=95, tp1=105, tp2=110, tp3=115),
        initial_status="open")
    assert _read_trade_ckpt(db, tid) is None        # 既有/新列預設 NULL
    tj.set_trade_checkpoint(tid, 1000)
    assert _read_trade_ckpt(db, tid) == 1000
    tj.set_trade_checkpoint(tid, 2000)              # 前進
    assert _read_trade_ckpt(db, tid) == 2000
    tj.set_trade_checkpoint(tid, 1500)              # 較舊 → 忽略
    assert _read_trade_ckpt(db, tid) == 2000
    tj.set_trade_checkpoint(tid, 2000)              # 相等 → 不變
    assert _read_trade_ckpt(db, tid) == 2000


def test_set_paper_checkpoint_monotonic(monkeypatch, tmp_path):
    db = str(tmp_path / "trade_journal.db")
    monkeypatch.setattr(pj, "DB_PATH", db)
    pj.init_db()
    pid = pj.record_paper_entry("BTC", "deepdive", "bull", 100, 95, 105, 110, 115)
    assert pid > 0
    assert _read_paper_ckpt(db, pid) is None
    pj.set_paper_checkpoint(pid, 1000)
    assert _read_paper_ckpt(db, pid) == 1000
    pj.set_paper_checkpoint(pid, 3000)
    assert _read_paper_ckpt(db, pid) == 3000
    pj.set_paper_checkpoint(pid, 2999)             # 較舊 → 忽略
    assert _read_paper_ckpt(db, pid) == 3000


# ===========================================================================
# 6. record_leg / apply_paper_event：監控腿冪等去重
# ===========================================================================
def test_record_leg_dedups_monitor_leg(monkeypatch, tmp_path):
    db = str(tmp_path / "trade_journal.db")
    monkeypatch.setattr(tj, "DB_PATH", db)
    tj.init_db()
    tid = tj.record_entry(EntryRecord(
        symbol="ETH", setup="deepdive", direction="bull",
        entry_price=100, stop_price=95, tp1=105, tp2=110, tp3=115),
        initial_status="open")
    r1 = tj.record_leg(tid, "tp1", 0.5, 105.0)
    assert r1.get("duplicate_skipped") is not True
    assert r1["leg_pnl_usd"] != 0.0
    assert r1["cumulative_pct"] == 0.5
    # 同一監控腿重抓 → 去重，零入帳，累計不變、仍 open
    r2 = tj.record_leg(tid, "tp1", 0.5, 105.0)
    assert r2["duplicate_skipped"] is True
    assert r2["leg_pnl_usd"] == 0.0
    assert r2["cumulative_pct"] == 0.5
    assert r2["trade_status"] == "open"
    # 只該有一筆 tp1 leg 列
    conn = sqlite3.connect(db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM trade_legs WHERE trade_id=? AND leg_label='tp1'",
            (tid,)).fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_record_leg_manual_label_not_deduped(monkeypatch, tmp_path):
    """手動腿（非監控標籤）不在冪等名單內 → 沿用原行為，可多次入帳。"""
    db = str(tmp_path / "trade_journal.db")
    monkeypatch.setattr(tj, "DB_PATH", db)
    tj.init_db()
    tid = tj.record_entry(EntryRecord(
        symbol="SOL", setup="deepdive", direction="bull",
        entry_price=100, stop_price=95, tp1=105, tp2=110, tp3=115),
        initial_status="open")
    tj.record_leg(tid, "manual", 0.3, 103.0)
    tj.record_leg(tid, "manual", 0.3, 104.0)
    conn = sqlite3.connect(db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM trade_legs WHERE trade_id=? AND leg_label='manual'",
            (tid,)).fetchone()[0]
    finally:
        conn.close()
    assert n == 2


def test_apply_paper_event_dedups_monitor_leg(monkeypatch, tmp_path):
    db = str(tmp_path / "trade_journal.db")
    monkeypatch.setattr(pj, "DB_PATH", db)
    pj.init_db()
    pid = pj.record_paper_entry("ETH", "deepdive", "bull", 100, 95, 105, 110, 115)
    assert pid > 0
    e1 = pj.apply_paper_event(pid, "tp1", 0.5, 105.0)
    assert e1.get("duplicate_skipped") is not True
    assert e1["leg_pnl"] != 0.0
    assert e1["closed"] is False
    total_after_first = e1["total_pnl"]
    # 重抓同一監控腿 → 去重，總 PnL/倉位不變
    e2 = pj.apply_paper_event(pid, "tp1", 0.5, 105.0)
    assert e2["duplicate_skipped"] is True
    assert e2["leg_pnl"] == 0.0
    assert e2["total_pnl"] == total_after_first
    conn = sqlite3.connect(db)
    try:
        legs, size_rem = conn.execute(
            "SELECT legs_hit, size_remaining FROM paper_trades WHERE id=?",
            (pid,)).fetchone()
    finally:
        conn.close()
    assert legs == "tp1"
    assert abs(size_rem - 0.5) < 1e-9


# ===========================================================================
# 7. 盲區情境直證：TP 在窄窗外 → 窄窗(n=4)漏、寬窗(since-checkpoint)補回
# ===========================================================================
def test_blindspot_wide_window_catches_tp_that_narrow_window_misses(monkeypatch):
    """TP1 在 15 根序列的第 3 根觸發（早），其後 12 根都在窄窗內但不再觸 TP/SL。
    舊窄窗 n=4 只看末 4 根 → 全盲漏記 TP；新寬窗自檢查點抓回全段 → _check_trade 記到 TP1。"""
    T = FIXED_NOW_MS - 16 * FIVE
    bars = [
        _c(T + 0 * FIVE, 101, 99, 100),
        _c(T + 1 * FIVE, 103, 100, 102),
        _c(T + 2 * FIVE, 106, 102, 104),   # ← TP1(105) 觸發，未破 SL(95)
    ]
    # 其後 12 根：在 101–104 之間徘徊（不再觸 tp1 區無妨；不觸 tp2=110；不破 stop=95）
    for i in range(3, 15):
        bars.append(_c(T + i * FIVE, 104, 101, 103))

    # now 設在最後一根之後一點，讓 gap 涵蓋全段
    now_ms = bars[-1]["ts"] + 60_000
    monkeypatch.setattr(tm.time, "time", lambda: now_ms / 1000.0)
    src = FakeOkxSource(bars)
    since = T - 1                          # 真實成交起點（檢查點為 NULL 的退路語意）
    res = asyncio.run(tm._get_bars_since(src, "BTC", since))
    wide = res["candles"]
    assert len(wide) == 15                 # 全段已確認 bar 都抓回

    trade = _trade("bull", stop=95, tp1=105, tp2=110, tp3=115)
    wide_events = tm._check_trade(trade, wide, SPLIT)
    assert wide_events == [("tp1", 105, 0.5)], wide_events

    # 對照：舊窄窗（末 4 根，皆 101–104）→ _check_trade 什麼都記不到（盲區）
    narrow_events = tm._check_trade(trade, wide[-4:], SPLIT)
    assert narrow_events == [], narrow_events


# ===========================================================================
# 8. monitor_once / _monitor_paper：入帳失敗 → 不前進檢查點（book_failed 旗標）
#    本次對抗稽核抓到的真迴歸：task#77 初版在 record_leg / apply_paper_event 失敗時
#    （如 WAL 鎖 sqlite3.OperationalError），仍【無條件】前進檢查點 → 那根 bar 的腿
#    永久漏記（紅線①實倉帳本最脆弱的一條）。治法＝book_failed 旗標把關檢查點前進；
#    靠監控腿冪等去重，下輪重抓同段重試不會雙記。
# ===========================================================================
def _set_trade_entry_at(db_path, tid, entry_at):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE trades SET entry_at=?, last_checked_ts=NULL WHERE id=?",
                     (entry_at, tid))
        conn.commit()
    finally:
        conn.close()


def _set_paper_entry_at(db_path, pid, entry_at):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE paper_trades SET entry_at=?, last_checked_ts=NULL WHERE id=?",
            (entry_at, pid))
        conn.commit()
    finally:
        conn.close()


def _open_trade_with_tp_bar(monkeypatch, tmp_path):
    """共用：建一筆 open 實倉、entry_at 設為 now−2 根，回 (db, tid, src, E)。
    bar 在 E+1 根觸 tp1（high 106 ≥ 105），不破 stop（low 100 > 95）。"""
    db = str(tmp_path / "trade_journal.db")
    monkeypatch.setattr(tj, "DB_PATH", db)
    monkeypatch.setattr(pj, "DB_PATH", db)   # _monitor_paper 同庫；空 paper 表 → no-op
    tj.init_db()
    pj.init_db()
    monkeypatch.setattr(tm.time, "time", lambda: FIXED_NOW_S)
    tid = tj.record_entry(EntryRecord(
        symbol="BTC", setup="deepdive", direction="bull",
        entry_price=100, stop_price=95, tp1=105, tp2=110, tp3=115),
        initial_status="open")
    E = FIXED_NOW_MS - 2 * FIVE
    _set_trade_entry_at(db, tid, E)
    src = FakeOkxSource([_c(E + FIVE, 106, 100, 104)])
    return db, tid, src, E


def test_monitor_once_advances_checkpoint_on_success(monkeypatch, tmp_path):
    """正常路徑對照：腿入帳成功 → 檢查點前進到最後一根已確認 bar。"""
    db, tid, src, E = _open_trade_with_tp_bar(monkeypatch, tmp_path)
    asyncio.run(tm.monitor_once(src))           # 預設 coach_state=None、tg=None
    assert _read_trade_ckpt(db, tid) == E + FIVE


def test_monitor_once_record_leg_failure_keeps_checkpoint(monkeypatch, tmp_path):
    """迴歸鎖：record_leg 拋例外 → 檢查點【不】前進（仍 NULL）、該腿未入帳。"""
    db, tid, src, E = _open_trade_with_tp_bar(monkeypatch, tmp_path)

    def _boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(tm, "record_leg", _boom)

    asyncio.run(tm.monitor_once(src))
    assert _read_trade_ckpt(db, tid) is None     # 失敗 → 不前進 → 下輪重抓重試
    conn = sqlite3.connect(db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM trade_legs WHERE trade_id=? AND leg_label='tp1'",
            (tid,)).fetchone()[0]
    finally:
        conn.close()
    assert n == 0                                # 沒入帳（下輪靠去重重試補回）


def test_monitor_paper_event_failure_isolated_per_trade(monkeypatch, tmp_path):
    """迴歸鎖：一筆紙上倉 apply_paper_event 失敗 → 該筆檢查點不前進、腿沒入帳；
    其餘紙上倉照常入帳並前進檢查點（同時治『失敗仍前進→永久漏記』與『一筆壞單拖垮整批』）。"""
    db = str(tmp_path / "trade_journal.db")
    monkeypatch.setattr(tj, "DB_PATH", db)
    monkeypatch.setattr(pj, "DB_PATH", db)
    tj.init_db()
    pj.init_db()
    monkeypatch.setattr(tm.time, "time", lambda: FIXED_NOW_S)

    bad = pj.record_paper_entry("AAA", "deepdive", "bull", 100, 95, 105, 110, 115,
                                skip_cooldown=True)
    good = pj.record_paper_entry("BBB", "deepdive", "bull", 100, 95, 105, 110, 115,
                                 skip_cooldown=True)
    assert bad > 0 and good > 0
    E = FIXED_NOW_MS - 2 * FIVE
    _set_paper_entry_at(db, bad, E)              # bad 先被處理（get_open_paper ORDER BY entry_at）
    _set_paper_entry_at(db, good, E + 1)
    src = FakeOkxSource([_c(E + FIVE, 106, 100, 104)])

    real_apply = pj.apply_paper_event

    def _selective(pid, label, size, price):
        if pid == bad:
            raise sqlite3.OperationalError("database is locked")
        return real_apply(pid, label, size, price)
    monkeypatch.setattr(pj, "apply_paper_event", _selective)

    asyncio.run(tm._monitor_paper(src, None, {}))

    assert _read_paper_ckpt(db, bad) is None         # 壞單：不前進
    assert _read_paper_ckpt(db, good) == E + FIVE     # 好單：未被拖垮 → 前進
    conn = sqlite3.connect(db)
    try:
        bad_legs = conn.execute(
            "SELECT legs_hit FROM paper_trades WHERE id=?", (bad,)).fetchone()[0]
        good_legs = conn.execute(
            "SELECT legs_hit FROM paper_trades WHERE id=?", (good,)).fetchone()[0]
    finally:
        conn.close()
    assert (bad_legs or "") == ""                # 壞單無腿入帳
    assert "tp1" in (good_legs or "")            # 好單 tp1 已入帳
