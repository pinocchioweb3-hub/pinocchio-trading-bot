"""task#61 Step B：入場積極度落地層（D「深限價到期轉市價」）契約測試。

鎖住三件事，避免日後改動把治本重新埋掉：
  1. convert_pending_to_market 的兩道理性閘**嚴格對齊** backtest._limit_variant(convert=True)：
       閘①追價無意義（市價穿越 tp1）→ 放棄；閘②風險≤0（市價過止損）→ 放棄。
  2. 轉換成功＝改寫 entry_price=市價、entry_state='full'、filled_pct=1.0、splits 標 converted，
       **且永不動 entry_at**（優化器以訊號時刻對齊 K 線重放，動 entry_at 會錯位）。
  3. R 數學忠實對映 backtest：apply_paper_event 以改寫後的 entry_price 計 sl_dist。
  4. record_paper_entry 寫 entry_policy_kind（顯式參數優先；否則自 entry_policy_store 解析）；
       get_pending_entries 把欄位帶出供 trade_monitor 到期判斷。
  5. "market" 覆寫尚未落地＝誠實留痕後寫 None（NO silent cap）。
全離線：monkeypatch DB_PATH 到暫存 sqlite；零網路、零真錢、零訊號數學。
"""
import json
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from l3_dispatcher import paper_journal as pj


def _seed_pending(conn, *, direction="bull", entry=95.5, stop=90.0, tp1=110.0,
                  splits=None, e=None):
    """植一張 0% 成交（pending）的限價分批單，回 (id, entry_at)。"""
    e = e if e is not None else int(time.time() * 1000)
    if splits is None:
        splits = [{"price": 96.0, "frac": 0.6, "filled": 0, "filled_at": None},
                  {"price": 95.0, "frac": 0.4, "filled": 0, "filled_at": None}]
    conn.execute(
        "INSERT INTO paper_trades (symbol, setup, direction, entry_price, stop_price, "
        "tp1, entry_at, created_at, status, entry_state, entry_splits, "
        "entry_filled_pct, size_remaining, entry_policy_kind) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("BTC", "deepdive", direction, entry, stop, tp1, e, e, "open", "pending",
         json.dumps(splits), 0.0, 1.0, "limit_convert"))
    pid = conn.execute("SELECT id FROM paper_trades ORDER BY id DESC LIMIT 1").fetchone()[0]
    return pid, e


def test_convert_success_bull_rewrites_entry_not_entry_at(tmp_path, monkeypatch):
    monkeypatch.setattr(pj, "DB_PATH", str(tmp_path / "trade_journal.db"))
    pj.init_db()
    conn = pj._conn()
    try:
        pid, e = _seed_pending(conn, direction="bull", entry=95.5, stop=90.0, tp1=110.0)
    finally:
        conn.close()

    # 市價 100：tp1(110) 之下（閘①過）、risk=|100-90|=10>0（閘②過）→ 轉換。
    res = pj.convert_pending_to_market(pid, 100.0)
    assert res is not None
    assert res["market_px"] == 100.0 and res["risk"] == 10.0 and res["prev_entry"] == 95.5

    conn = pj._conn()
    try:
        row = conn.execute(
            "SELECT entry_price, entry_state, entry_filled_pct, entry_at, entry_splits "
            "FROM paper_trades WHERE id=?", (pid,)).fetchone()
    finally:
        conn.close()
    assert row[0] == 100.0          # entry_price 改寫成市價
    assert row[1] == "full"          # 視同全量成交
    assert row[2] == 1.0
    assert row[3] == e               # ★ entry_at 永不變（優化器重放對齊用）
    splits = json.loads(row[4])
    assert all(s["filled"] == 1 and s.get("converted_market") == 1 for s in splits)


def test_convert_r_math_uses_rewritten_entry(tmp_path, monkeypatch):
    """轉換後 apply_paper_event 以新 entry_price 計 R（忠實對映 backtest risk_c）。"""
    monkeypatch.setattr(pj, "DB_PATH", str(tmp_path / "trade_journal.db"))
    pj.init_db()
    conn = pj._conn()
    try:
        pid, _ = _seed_pending(conn, direction="bull", entry=95.5, stop=90.0, tp1=110.0)
    finally:
        conn.close()
    pj.convert_pending_to_market(pid, 100.0)   # entry→100, stop=90 → sl_dist=10
    ev = pj.apply_paper_event(pid, "tp1", 1.0, 110.0)   # 出場 110
    # leg_r = (110-100)/|100-90| = 1.0R（非用舊掛單價 96 → 那會是 (110-96)/6≈2.33R）
    assert ev["leg_r"] == 1.0


def test_gate1_bull_price_past_tp1_abandons(tmp_path, monkeypatch):
    monkeypatch.setattr(pj, "DB_PATH", str(tmp_path / "trade_journal.db"))
    pj.init_db()
    conn = pj._conn()
    try:
        pid, _ = _seed_pending(conn, direction="bull", stop=90.0, tp1=110.0)
    finally:
        conn.close()
    # 市價已≥tp1 → 追價無意義 → 放棄（None），且狀態維持 pending（交給 expire）。
    assert pj.convert_pending_to_market(pid, 110.0) is None
    assert pj.convert_pending_to_market(pid, 111.0) is None
    conn = pj._conn()
    try:
        st = conn.execute("SELECT entry_state FROM paper_trades WHERE id=?", (pid,)).fetchone()[0]
    finally:
        conn.close()
    assert st == "pending"


def test_gate2_bull_zero_risk_abandons(tmp_path, monkeypatch):
    monkeypatch.setattr(pj, "DB_PATH", str(tmp_path / "trade_journal.db"))
    pj.init_db()
    conn = pj._conn()
    try:
        pid, _ = _seed_pending(conn, direction="bull", stop=90.0, tp1=110.0)
    finally:
        conn.close()
    # 市價==止損 → risk=0 → 放棄（即使 tp1 閘已過）。
    assert pj.convert_pending_to_market(pid, 90.0) is None


def test_gate_bear_semantics(tmp_path, monkeypatch):
    """bear：閘①市價≤tp1（tp1 在下方）放棄；閘②risk=|市價-stop|≤0 放棄；其餘轉換。"""
    monkeypatch.setattr(pj, "DB_PATH", str(tmp_path / "trade_journal.db"))
    pj.init_db()
    # bear 倉：entry 區間在 stop 下方、tp1 更下方。stop=110, tp1=90。
    conn = pj._conn()
    try:
        s = [{"price": 104.0, "frac": 0.6, "filled": 0, "filled_at": None},
             {"price": 105.0, "frac": 0.4, "filled": 0, "filled_at": None}]
        pid_a, _ = _seed_pending(conn, direction="bear", entry=104.5, stop=110.0,
                                 tp1=90.0, splits=list(s))
        pid_b, _ = _seed_pending(conn, direction="bear", entry=104.5, stop=110.0,
                                 tp1=90.0, splits=list(s))
        pid_c, _ = _seed_pending(conn, direction="bear", entry=104.5, stop=110.0,
                                 tp1=90.0, splits=list(s))
    finally:
        conn.close()
    # 閘①：市價≤tp1(90) → 放棄
    assert pj.convert_pending_to_market(pid_a, 90.0) is None
    # 閘②：市價==stop(110) → risk=0 → 放棄
    assert pj.convert_pending_to_market(pid_b, 110.0) is None
    # 正常：市價 100（tp1<100<stop）→ risk=|100-110|=10 → 轉換
    res = pj.convert_pending_to_market(pid_c, 100.0)
    assert res is not None and res["risk"] == 10.0


def test_convert_only_touches_pending(tmp_path, monkeypatch):
    """非 pending（partial/full/closed）一律不轉換（SQL 護欄）。"""
    monkeypatch.setattr(pj, "DB_PATH", str(tmp_path / "trade_journal.db"))
    pj.init_db()
    conn = pj._conn()
    try:
        pid, _ = _seed_pending(conn, direction="bull", stop=90.0, tp1=110.0)
        conn.execute("UPDATE paper_trades SET entry_state='partial', entry_filled_pct=0.6 WHERE id=?", (pid,))
    finally:
        conn.close()
    assert pj.convert_pending_to_market(pid, 100.0) is None


def test_record_explicit_kind_round_trips(tmp_path, monkeypatch):
    """record_paper_entry 顯式 entry_policy_kind 寫入；get_pending_entries 帶出。"""
    monkeypatch.setattr(pj, "DB_PATH", str(tmp_path / "trade_journal.db"))
    pj.init_db()
    pid = pj.record_paper_entry(
        symbol="ETH", setup="deepdive", direction="bull",
        entry_price=95.5, stop_price=90.0, tp1=110.0, tp2=115.0, tp3=120.0,
        zone_lo=95.0, zone_hi=96.0, split_mode=True, skip_cooldown=True,
        entry_policy_kind="limit_convert")
    assert pid > 0
    pend = [p for p in pj.get_pending_entries() if p["id"] == pid]
    assert pend and pend[0]["entry_policy_kind"] == "limit_convert"


def test_record_self_resolves_from_store(tmp_path, monkeypatch):
    """無顯式參數時，從 entry_policy_store 自解析（與 tp_alloc 同模式）。"""
    monkeypatch.setattr(pj, "DB_PATH", str(tmp_path / "trade_journal.db"))
    pj.init_db()
    import l3_dispatcher.entry_policy_store as eps
    monkeypatch.setattr(eps, "resolve_entry_policy", lambda *a, **k: "limit_convert")
    pid = pj.record_paper_entry(
        symbol="SOL", setup="deepdive", direction="bull",
        entry_price=95.5, stop_price=90.0, tp1=110.0, tp2=115.0, tp3=120.0,
        zone_lo=95.0, zone_hi=96.0, split_mode=True, skip_cooldown=True)
    pend = [p for p in pj.get_pending_entries() if p["id"] == pid]
    assert pend and pend[0]["entry_policy_kind"] == "limit_convert"


def test_market_override_not_yet_actionable(tmp_path, monkeypatch, capsys):
    """"market" 覆寫＝誠實留痕後寫 None（NO silent cap），不改現行行為。"""
    monkeypatch.setattr(pj, "DB_PATH", str(tmp_path / "trade_journal.db"))
    pj.init_db()
    import l3_dispatcher.entry_policy_store as eps
    monkeypatch.setattr(eps, "resolve_entry_policy", lambda *a, **k: "market")
    pid = pj.record_paper_entry(
        symbol="OP", setup="deepdive", direction="bull",
        entry_price=95.5, stop_price=90.0, tp1=110.0, tp2=115.0, tp3=120.0,
        zone_lo=95.0, zone_hi=96.0, split_mode=True, skip_cooldown=True)
    out = capsys.readouterr().out
    assert "market 尚未落地" in out          # 留痕，未靜默
    pend = [p for p in pj.get_pending_entries() if p["id"] == pid]
    assert pend and pend[0]["entry_policy_kind"] is None   # 未落地 → 不寫 convert


def test_non_split_market_entry_has_no_policy(tmp_path, monkeypatch):
    """非分批（市價全額）單即使 store 給 limit_convert 也不記（無掛單可轉）。"""
    monkeypatch.setattr(pj, "DB_PATH", str(tmp_path / "trade_journal.db"))
    pj.init_db()
    import l3_dispatcher.entry_policy_store as eps
    monkeypatch.setattr(eps, "resolve_entry_policy", lambda *a, **k: "limit_convert")
    pid = pj.record_paper_entry(
        symbol="APT", setup="us_breakout", direction="bull",
        entry_price=100.0, stop_price=95.0, tp1=110.0, tp2=115.0, tp3=120.0,
        split_mode=False, skip_cooldown=True)
    conn = pj._conn()
    try:
        kind = conn.execute(
            "SELECT entry_policy_kind FROM paper_trades WHERE id=?", (pid,)).fetchone()[0]
    finally:
        conn.close()
    assert kind is None


def test_inert_default_no_override(tmp_path, monkeypatch):
    """store 空（resolve 回 None）→ entry_policy_kind 寫 None＝今日行為（inert-on-ship）。"""
    monkeypatch.setattr(pj, "DB_PATH", str(tmp_path / "trade_journal.db"))
    pj.init_db()
    pid = pj.record_paper_entry(
        symbol="XRP", setup="deepdive", direction="bull",
        entry_price=95.5, stop_price=90.0, tp1=110.0, tp2=115.0, tp3=120.0,
        zone_lo=95.0, zone_hi=96.0, split_mode=True, skip_cooldown=True)
    pend = [p for p in pj.get_pending_entries() if p["id"] == pid]
    assert pend and pend[0]["entry_policy_kind"] is None
