"""同物種第 18 次 ── 兩支「活躍覆寫表」的讀取端誠實化（auto_param_store / entry_policy_store）。

物種：把「檔案存在但讀不出來（壞檔／半刷入）」折成「從來沒有任何覆寫」（{}）。

為何這兩支比前幾次嚴重——**讀失敗會被寫成不可逆的抹除**：
    apply_verdict 的寫法是 buckets = _load_active() → buckets[新桶] = ... → 原子寫**整張表**。
    讀不出來時 _load_active 回 {}，於是「整張表」＝只剩剛晉升的那一桶，其餘**所有**已晉升
    的桶被原子地、乾淨地抹掉（連半截檔都不留，事後無從還原）；稽核那行還會記
    from_kind=None＝「這桶本來就沒有覆寫」，錯誤宣稱一路傳進稽核軌跡。
    rollback 同源：讀不出來 → existed=False → 回報「本來就沒有可移除的覆寫」（等於宣稱已回退
    成功），但檔裡的覆寫其實原封不動還在生效——安全閥在最需要它的時候假裝自己動過。

resolve 路徑的 fail-safe（壞檔 → None → 用預設行為）是**刻意設計**（不可讓進場崩），本檔不動它，
只補「不可讓讀失敗變成寫抹除」與「不可謊報回退成功」。

另補一條自產壞檔的來源：原子寫只有 os.replace 沒有 fsync——內容尚未落盤就斷電（本機有斷電
事件史）可留下零長度/半截檔於目的地，下次啟動再自己誤讀＝自產自誤閉環。

⛔ 紅線①：這兩張表只驅動 paper／demo 的 TP 分配與入場積極度，真錢執行層完全不讀 → 本次修補
   與真錢無關，不得宣稱真錢實證。
"""
import json
import os
import sys
import tempfile
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from l3_dispatcher import auto_param_store as aps
from l3_dispatcher import entry_policy_store as eps

# 壞檔內容：模擬「寫到一半就斷電」——前半段是合法 JSON 的開頭，含真實桶資料。
# 重點是它**讀不出來**卻**明顯不是空表**（人眼看得到裡面有東西）。
_HALF_WRITTEN = (
    '{\n  "version": 1,\n  "updated_at_ms": 1700000000000,\n  "buckets": {\n'
    '    "ETH|price_up_oi_up": {"kind": "market", "alloc": [0.5, 0.3, 0.2]},\n'
    '    "SOL|price_dn_oi_up"'
)


def _paths(td):
    return Path(td) / "active.json", Path(td) / "audit.jsonl"


def _aps_verdict(promote=True, bucket_key="BTC|price_up_oi_up"):
    return types.SimpleNamespace(
        promote=promote, bucket_key=bucket_key, champion="champion(現行)",
        challenger="c", champ_mean_r=0.10, chal_mean_r=0.30,
        n_aligned=40, l2_summary="demo", reasons=["r"])


def _eps_verdict(promote=True, bucket_key="BTC|price_up_oi_up"):
    return types.SimpleNamespace(
        promote=promote, coverage_promote=False, bucket_key=bucket_key,
        champion="champion(現行)", challenger="c", champ_mean_r=0.10,
        chal_mean_r=0.30, champ_fill_rate=0.35, chal_fill_rate=0.94,
        coverage_delta_pp=59.0, n_aligned=40, l2_summary="demo", reasons=["r"])


def _audit_rows(au):
    if not au.exists():
        return []
    return [json.loads(x) for x in au.read_text(encoding="utf-8").splitlines() if x.strip()]


# ═══════════════════════════════════════════════════════════════════════
# 主症狀①：讀不出來時，晉升不得把整張表原子抹成「只剩這一桶」
# ═══════════════════════════════════════════════════════════════════════
def test_aps_unreadable_active_must_not_overwrite_file():
    with tempfile.TemporaryDirectory() as td:
        ap, au = _paths(td)
        ap.write_text(_HALF_WRITTEN, encoding="utf-8")
        before = ap.read_bytes()
        r = aps.apply_verdict(_aps_verdict(True), symbol="BTC", quadrant="price_up_oi_up",
                              challenger_alloc=(0.6, 0.25, 0.15), champion_alloc=(0.5, 0.3, 0.2),
                              at_ms=1, active_path=ap, audit_path=au)
        # 原檔逐位元不變＝沒有把「讀失敗」變成「寫抹除」（⛔ 不刪不改原檔）
        assert ap.read_bytes() == before
        assert r["action"] != "promote"          # 不得宣稱已生效（紅線③）


def test_eps_unreadable_active_must_not_overwrite_file():
    with tempfile.TemporaryDirectory() as td:
        ap, au = _paths(td)
        ap.write_text(_HALF_WRITTEN, encoding="utf-8")
        before = ap.read_bytes()
        r = eps.apply_verdict(_eps_verdict(True), symbol="BTC", quadrant="price_up_oi_up",
                              challenger_kind="limit_convert", champion_kind="limit_expire",
                              at_ms=1, active_path=ap, audit_path=au)
        assert ap.read_bytes() == before
        assert r["action"] != "promote"


# ═══════════════════════════════════════════════════════════════════════
# 主症狀②：讀不出來要在稽核裡「說出來」，不可靜默 hold（否則與 L2 沒過同形）
# ═══════════════════════════════════════════════════════════════════════
def test_aps_unreadable_is_audited_as_blocked_not_silent_hold():
    with tempfile.TemporaryDirectory() as td:
        ap, au = _paths(td)
        ap.write_text(_HALF_WRITTEN, encoding="utf-8")
        aps.apply_verdict(_aps_verdict(True), symbol="BTC", quadrant="price_up_oi_up",
                          challenger_alloc=(0.6, 0.25, 0.15), champion_alloc=None,
                          at_ms=2, active_path=ap, audit_path=au)
        rows = _audit_rows(au)
        assert len(rows) == 1
        assert rows[0].get("active_unreadable") is True
        # 「這桶原本沒有覆寫」是未知，不可當事實寫進稽核
        assert rows[0].get("from_kind_known") is False


def test_eps_unreadable_is_audited_as_blocked_not_silent_hold():
    with tempfile.TemporaryDirectory() as td:
        ap, au = _paths(td)
        ap.write_text(_HALF_WRITTEN, encoding="utf-8")
        eps.apply_verdict(_eps_verdict(True), symbol="BTC", quadrant="price_up_oi_up",
                          challenger_kind="limit_convert", at_ms=2,
                          active_path=ap, audit_path=au)
        rows = _audit_rows(au)
        assert len(rows) == 1
        assert rows[0].get("active_unreadable") is True
        assert rows[0].get("from_kind_known") is False


# ═══════════════════════════════════════════════════════════════════════
# 主症狀③：安全閥不可謊報——讀不出來時 rollback 不得回報「已回退／本來就沒有」
# ═══════════════════════════════════════════════════════════════════════
def test_aps_rollback_on_unreadable_must_not_claim_success():
    with tempfile.TemporaryDirectory() as td:
        ap, au = _paths(td)
        ap.write_text(_HALF_WRITTEN, encoding="utf-8")
        before = ap.read_bytes()
        r = aps.rollback("BTC", "price_up_oi_up", at_ms=3, reason="t",
                         active_path=ap, audit_path=au)
        assert r["action"] != "rollback"          # 不可宣稱回退成功
        assert r.get("existed") is not False      # 「本來就沒有」是未知，不可當事實
        assert ap.read_bytes() == before


def test_eps_rollback_on_unreadable_must_not_claim_success():
    with tempfile.TemporaryDirectory() as td:
        ap, au = _paths(td)
        ap.write_text(_HALF_WRITTEN, encoding="utf-8")
        before = ap.read_bytes()
        r = eps.rollback("BTC", "price_up_oi_up", at_ms=3, reason="t",
                         active_path=ap, audit_path=au)
        assert r["action"] != "rollback"
        assert r.get("existed") is not False
        assert ap.read_bytes() == before


# ═══════════════════════════════════════════════════════════════════════
# 主症狀④：原子寫要 fsync（否則斷電留下的半截檔就是上面壞檔的來源）
# ═══════════════════════════════════════════════════════════════════════
def _assert_fsync_called(mod, write_call):
    calls = []
    real = os.fsync

    def _spy(fd):
        calls.append(fd)
        return real(fd)

    os.fsync = _spy
    try:
        write_call()
    finally:
        os.fsync = real
    assert calls, "原子寫未 fsync：內容可能還沒落盤就 replace，斷電即得半截檔"


def test_aps_atomic_write_fsyncs():
    with tempfile.TemporaryDirectory() as td:
        ap, _ = _paths(td)
        _assert_fsync_called(aps, lambda: aps._atomic_write_active(
            {"BTC|q": {"alloc": [0.5, 0.3, 0.2]}}, 1, ap))
        assert json.loads(ap.read_text(encoding="utf-8"))["buckets"]


def test_eps_atomic_write_fsyncs():
    with tempfile.TemporaryDirectory() as td:
        ap, _ = _paths(td)
        _assert_fsync_called(eps, lambda: eps._atomic_write_active(
            {"BTC|q": {"kind": "market"}}, 1, ap))
        assert json.loads(ap.read_text(encoding="utf-8"))["buckets"]


# ═══════════════════════════════════════════════════════════════════════
# 反向護欄（在**改動前的碼上就該綠**）：正常路徑一律不得被這次修補改掉
# ═══════════════════════════════════════════════════════════════════════
def test_missing_file_is_still_a_normal_first_write():
    """真·從來沒有（檔不存在）≠ 讀不出來：前者照常晉升寫入。"""
    with tempfile.TemporaryDirectory() as td:
        ap, au = _paths(td)
        r = aps.apply_verdict(_aps_verdict(True), symbol="BTC", quadrant="price_up_oi_up",
                              challenger_alloc=(0.6, 0.25, 0.15), champion_alloc=None,
                              at_ms=4, active_path=ap, audit_path=au)
        assert r["action"] == "promote" and ap.exists()
        assert aps.resolve_tp_alloc("BTC", "price_up_oi_up", active_path=ap) == (0.6, 0.25, 0.15)

    with tempfile.TemporaryDirectory() as td:
        ap, au = _paths(td)
        r = eps.apply_verdict(_eps_verdict(True), symbol="BTC", quadrant="price_up_oi_up",
                              challenger_kind="limit_convert", at_ms=4,
                              active_path=ap, audit_path=au)
        assert r["action"] == "promote" and ap.exists()
        assert eps.resolve_entry_policy("BTC", "price_up_oi_up", active_path=ap) == "limit_convert"


def test_resolve_on_unreadable_still_failsafe_none():
    """進場熱路徑的 fail-safe 是刻意設計：壞檔仍回 None（用預設行為），永不拋。"""
    with tempfile.TemporaryDirectory() as td:
        ap, _ = _paths(td)
        ap.write_text(_HALF_WRITTEN, encoding="utf-8")
        assert aps.resolve_tp_alloc("BTC", "price_up_oi_up", active_path=ap) is None
        assert eps.resolve_entry_policy("BTC", "price_up_oi_up", active_path=ap) is None


def test_missing_file_rollback_still_reports_not_existed():
    """檔不存在＝真·沒有覆寫可移除：仍照舊回報 existed=False（不可被本次修補污染）。"""
    with tempfile.TemporaryDirectory() as td:
        ap, au = _paths(td)
        r = aps.rollback("BTC", "price_up_oi_up", at_ms=5, active_path=ap, audit_path=au)
        assert r["action"] == "rollback" and r["existed"] is False
    with tempfile.TemporaryDirectory() as td:
        ap, au = _paths(td)
        r = eps.rollback("BTC", "price_up_oi_up", at_ms=5, active_path=ap, audit_path=au)
        assert r["action"] == "rollback" and r["existed"] is False


def test_module_selftests_still_pass():
    assert aps._selftest() is True
    assert eps._selftest() is True
