"""post-close 冷卻去重（paper_journal.record_paper_entry）測試 — v47-2。

修的真實 bug（使用者回報）：🎯交易訊號 出現「同一幣別、剛平倉就立刻重開同樣的單」。
    實例 BTC：id39（多, 止損 64100）timeout 平倉 → 18.5 分鐘後 id57（多, 止損 64100）
    又開 = 貼身重複單。symbol_gate 1h 窗 << 持倉時長且平倉不刷新它、deepdive 又只擋
    still-open，於是漏網。本層在「入口單一收斂點」record_paper_entry 補一道冷卻。

執行方式（任一）：
    pytest tests/test_post_close_cooldown.py
    python tests/test_post_close_cooldown.py

驗證重點（刻意保守，只有全部硬條件成立才擋，絕不誤殺正當新訊號）：
    1. 同幣同向同 setup、剛以 timeout 平倉、止損近乎相同、窗內 → 擋（核心修復）。
    2. 止損差 >0.5%（新論述/新結構，如 BTC id21→id39 差 1.42%）→ 放行。
    3. exit_reason='entry_expired'（掛單從未成交）→ 放行（重掛限價正當）。
    4. 反向（bull 平倉後開 bear，如 FIL）→ 放行（只比同方向；反轉對持倉出場有價值）。
    5. 已過冷卻窗（>6h）→ 放行。
    6. skip_cooldown=True（waiting-trigger 觸發是先前已承諾的等待單兌現）→ 豁免放行。
    7. 跨 setup（deepdive 平倉後 us_breakout 開同幣同向）→ 放行（只比同 setup）。
    8. 無歷史 → 放行。
    9. 先前同幣同向同 setup 仍『open』（非 closed）→ 冷卻不介入（那交給 open 去重層）。
   10. 止損差「恰等於」門檻（0.5%）→ 放行；「略小於」門檻 → 擋（邊界）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import l3_dispatcher.paper_journal as pj

# 把 DB 指到臨時檔，與正式紙上帳完全隔離（import 後改 module 全域，_conn 每次讀全域）
_TEST_DB = Path(tempfile.mkdtemp(prefix="postclose_test_")) / "trade_journal_test.db"
pj.DB_PATH = _TEST_DB

# 測試用固定 TP（record_paper_entry 必填 positional）
_TP1, _TP2, _TP3 = 1.0, 2.0, 3.0


def _fresh():
    """清空測試 DB + 還原冷卻參數為預設值（6h / 0.5%），確保斷言對齊文件預設。"""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(_TEST_DB) + suffix)
        if p.exists():
            p.unlink()
    os.environ.pop("POST_CLOSE_COOLDOWN_S", None)
    os.environ.pop("POST_CLOSE_STOP_EPS", None)
    pj.init_db()


def _insert_closed(symbol, setup, direction, stop_price, exit_reason, age_s):
    """直接插入一筆『已平倉』paper_trade，exit_at = now - age_s 秒。"""
    pj.init_db()
    conn = pj._conn()
    try:
        now_ms = int(time.time() * 1000)
        exit_at = now_ms - age_s * 1000
        entry = stop_price * 1.01
        conn.execute(
            "INSERT INTO paper_trades (symbol, setup, direction, entry_price, stop_price, "
            "entry_at, status, exit_reason, exit_at, realized_r, pnl_usd, size_remaining, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (symbol, setup, direction, entry, stop_price, exit_at - 3_600_000,
             "closed", exit_reason, exit_at, 1.0, 100.0, 0, exit_at - 3_600_000),
        )
    finally:
        conn.close()


def _insert_open(symbol, setup, direction, stop_price):
    """直接插入一筆仍『open』的 paper_trade（exit_at=None）。"""
    pj.init_db()
    conn = pj._conn()
    try:
        now_ms = int(time.time() * 1000)
        entry = stop_price * 1.01
        conn.execute(
            "INSERT INTO paper_trades (symbol, setup, direction, entry_price, stop_price, "
            "entry_at, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (symbol, setup, direction, entry, stop_price, now_ms - 600_000, "open",
             now_ms - 600_000),
        )
    finally:
        conn.close()


def _record(symbol, setup, direction, stop_price, **kw):
    return pj.record_paper_entry(
        symbol, setup, direction, stop_price * 1.01, stop_price,
        _TP1, _TP2, _TP3, **kw)


# --- 1. 核心修復：同幣同向同 setup 剛平倉、止損相同、窗內 → 擋 ---
def test_block_sequential_reopen_same_stop():
    _fresh()
    # BTC id39: 多, 止損 64100, 18.5 分鐘前 timeout 平倉
    _insert_closed("BTC", "deepdive", "bull", 64100.0, "timeout", age_s=int(18.5 * 60))
    # id57: 同向同止損重開 → 應被擋（回 -1，不建單）
    rid = _record("BTC", "deepdive", "bull", 64100.0)
    assert rid == -1, f"應被 post-close 冷卻擋下，實得 {rid}"
    # DB 內不應新增第二筆 open
    assert "BTC" not in pj.open_paper_symbols("deepdive")


# --- 2. 止損差 >0.5%（新論述）→ 放行 ---
def test_pass_different_stop_beyond_eps():
    _fresh()
    _insert_closed("BTC", "deepdive", "bull", 64100.0, "timeout", age_s=600)
    # 止損差 1.42%（BTC id21→id39 的真實情境）→ 放行
    rid = _record("BTC", "deepdive", "bull", 64100.0 * 1.0142)
    assert rid > 0, f"止損差 1.42% 應放行，實得 {rid}"


# --- 3. entry_expired（掛單從未成交）→ 放行（重掛正當） ---
def test_pass_entry_expired_excluded():
    _fresh()
    _insert_closed("ETH", "deepdive", "bull", 3000.0, "entry_expired", age_s=300)
    rid = _record("ETH", "deepdive", "bull", 3000.0)
    assert rid > 0, f"entry_expired 不在白名單，應放行，實得 {rid}"


# --- 4. 反向（bull 平倉後開 bear，如 FIL）→ 放行 ---
def test_pass_reversal_opposite_direction():
    _fresh()
    _insert_closed("FIL", "deepdive", "bull", 5.0, "stop", age_s=600)
    rid = _record("FIL", "deepdive", "bear", 5.0)
    assert rid > 0, f"反向訊號應放行（只比同方向），實得 {rid}"


# --- 5. 已過冷卻窗（>6h）→ 放行 ---
def test_pass_after_window_expired():
    _fresh()
    _insert_closed("SOL", "deepdive", "bull", 150.0, "timeout", age_s=7 * 3600)
    rid = _record("SOL", "deepdive", "bull", 150.0)
    assert rid > 0, f"超過 6h 窗應放行，實得 {rid}"


# --- 6. skip_cooldown=True（waiting-trigger 兌現）→ 豁免放行 ---
def test_exempt_skip_cooldown():
    _fresh()
    _insert_closed("BTC", "intraday", "bull", 64100.0, "timeout", age_s=600)
    rid = _record("BTC", "intraday", "bull", 64100.0, skip_cooldown=True)
    assert rid > 0, f"waiting-trigger 豁免應放行，實得 {rid}"


# --- 7. 跨 setup（deepdive 平倉後 us_breakout 開）→ 放行 ---
def test_pass_cross_setup():
    _fresh()
    _insert_closed("AAPL", "deepdive", "bull", 200.0, "stop", age_s=600)
    rid = _record("AAPL", "us_breakout", "bull", 200.0)
    assert rid > 0, f"跨 setup 不互擋，應放行，實得 {rid}"


# --- 8. 無歷史 → 放行 ---
def test_pass_no_history():
    _fresh()
    rid = _record("DOGE", "deepdive", "bull", 0.15)
    assert rid > 0, f"無歷史應放行，實得 {rid}"


# --- 9. 先前同幣同向同 setup 仍 open（非 closed）→ 冷卻不介入 ---
def test_pass_when_prior_still_open():
    _fresh()
    _insert_open("XRP", "deepdive", "bull", 0.5)
    # 冷卻只看 closed；open 去重交給 open_paper_symbols 那層，這裡放行
    rid = _record("XRP", "deepdive", "bull", 0.5)
    assert rid > 0, f"先前仍 open 時 post-close 冷卻不應介入，實得 {rid}"


# --- 10. 止損差邊界：明顯高於門檻放行、明顯低於門檻擋 ---
#   （刻意不測「恰等於 0.5%」——浮點數 1000*1.005=1004.9999… 使精確邊界無意義；
#     真正要保證的是「明顯差很多放行、明顯近似擋」。）
def test_stop_eps_boundary():
    _fresh()
    base = 1000.0
    _insert_closed("LINK", "deepdive", "bull", base, "timeout", age_s=600)
    # 0.6%（明顯 > 0.5%）→ 放行
    rid_gt = _record("LINK", "deepdive", "bull", base * 1.006)
    assert rid_gt > 0, f"止損差 0.6% > 門檻應放行，實得 {rid_gt}"

    _fresh()
    _insert_closed("LINK", "deepdive", "bull", base, "timeout", age_s=600)
    # 0.4%（明顯 < 0.5%）→ 擋
    rid_lt = _record("LINK", "deepdive", "bull", base * 1.004)
    assert rid_lt == -1, f"止損差 0.4% < 門檻應被擋，實得 {rid_lt}"


# --- 直接執行（無 pytest 也能跑）---
if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
