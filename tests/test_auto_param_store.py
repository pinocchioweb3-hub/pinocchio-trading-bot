"""復盤引擎 step8（task#53）── auto_param_store 測試。

覆蓋：resolve fail-safe、promote 才寫活躍表（hold 不寫但留稽核＝紅線③）、無效分配 fail-safe
拒寫、rollback 回退、壞檔不崩、桶隔離、_valid_alloc 規則、稽核必留痕。
全離線、暫存檔、零真 DB / 零網路。verdict 以 SimpleNamespace 仿真 ChallengerVerdict
（champion/challenger 是名稱字串，分配元組由呼叫端顯式傳入）。
"""
import json
import sys
import tempfile
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from l3_dispatcher import auto_param_store as aps


def _verdict(promote, *, bucket_key="BTC|price_up_oi_up", champ_mean_r=0.10,
            chal_mean_r=0.30, n_aligned=40):
    """仿真 ChallengerVerdict（champion/challenger＝名稱字串，非 AllocPolicy）。"""
    return types.SimpleNamespace(
        promote=promote, bucket_key=bucket_key, champion="champion(現行)",
        challenger="c", champ_mean_r=champ_mean_r, chal_mean_r=chal_mean_r,
        n_aligned=n_aligned, l2_summary="demo", reasons=["r"])


def _paths(td):
    return Path(td) / "active.json", Path(td) / "audit.jsonl"


def test_module_selftest_passes():
    assert aps._selftest() is True


def test_bucket_key_format():
    assert aps.bucket_key("BTC", "price_up_oi_up") == "BTC|price_up_oi_up"
    assert aps.bucket_key("ETH", "") == "ETH|unknown"      # 空象限 → unknown
    assert aps.bucket_key("SOL", None) == "SOL|unknown"


def test_resolve_empty_is_none():
    with tempfile.TemporaryDirectory() as td:
        ap, _ = _paths(td)
        assert aps.resolve_tp_alloc("BTC", "price_up_oi_up", active_path=ap) is None


def test_promote_false_holds_no_active_write_but_audits():
    with tempfile.TemporaryDirectory() as td:
        ap, au = _paths(td)
        r = aps.apply_verdict(_verdict(False), symbol="BTC", quadrant="price_up_oi_up",
                              challenger_alloc=(0.5, 0.3, 0.2), champion_alloc=(0.4, 0.3, 0.3),
                              at_ms=1, active_path=ap, audit_path=au)
        assert r["action"] == "hold"
        assert not ap.exists()                       # 活躍表未建
        assert au.exists()                           # 稽核留痕（紅線③）
        rows = [json.loads(x) for x in au.read_text(encoding="utf-8").splitlines() if x.strip()]
        assert len(rows) == 1 and rows[0]["action"] == "hold" and rows[0]["promote"] is False
        assert aps.resolve_tp_alloc("BTC", "price_up_oi_up", active_path=ap) is None


def test_promote_true_writes_and_resolves():
    with tempfile.TemporaryDirectory() as td:
        ap, au = _paths(td)
        r = aps.apply_verdict(_verdict(True), symbol="BTC", quadrant="price_up_oi_up",
                              challenger_alloc=(0.6, 0.25, 0.15), champion_alloc=(0.5, 0.3, 0.2),
                              at_ms=2, active_path=ap, audit_path=au)
        assert r["action"] == "promote"
        assert aps.resolve_tp_alloc("BTC", "price_up_oi_up", active_path=ap) == (0.6, 0.25, 0.15)
        # 稽核含 from/to 分配
        rows = [json.loads(x) for x in au.read_text(encoding="utf-8").splitlines() if x.strip()]
        assert rows[-1]["to_alloc"] == [0.6, 0.25, 0.15]
        assert rows[-1]["champion_alloc"] == [0.5, 0.3, 0.2]


def test_invalid_alloc_on_promote_fails_safe():
    with tempfile.TemporaryDirectory() as td:
        ap, au = _paths(td)
        # 總和 1.3（無效）→ 即使 promote 也拒寫
        r = aps.apply_verdict(_verdict(True), symbol="SOL", quadrant="unknown",
                              challenger_alloc=(0.5, 0.3, 0.5), at_ms=3,
                              active_path=ap, audit_path=au)
        assert r["action"] == "hold"
        assert aps.resolve_tp_alloc("SOL", "unknown", active_path=ap) is None


def test_rollback_reverts_to_default():
    with tempfile.TemporaryDirectory() as td:
        ap, au = _paths(td)
        aps.apply_verdict(_verdict(True), symbol="BTC", quadrant="price_up_oi_up",
                          challenger_alloc=(0.6, 0.25, 0.15), at_ms=1,
                          active_path=ap, audit_path=au)
        assert aps.resolve_tp_alloc("BTC", "price_up_oi_up", active_path=ap) is not None
        r = aps.rollback("BTC", "price_up_oi_up", at_ms=2, reason="test",
                         active_path=ap, audit_path=au)
        assert r["action"] == "rollback" and r["existed"] is True
        assert aps.resolve_tp_alloc("BTC", "price_up_oi_up", active_path=ap) is None
        # rollback 不存在的桶 → existed False，不炸
        r2 = aps.rollback("NOPE", "x", at_ms=3, active_path=ap, audit_path=au)
        assert r2["existed"] is False


def test_buckets_isolated():
    with tempfile.TemporaryDirectory() as td:
        ap, au = _paths(td)
        aps.apply_verdict(_verdict(True, bucket_key="BTC|price_up_oi_up"),
                          symbol="BTC", quadrant="price_up_oi_up",
                          challenger_alloc=(0.6, 0.25, 0.15), at_ms=1,
                          active_path=ap, audit_path=au)
        assert aps.resolve_tp_alloc("BTC", "price_up_oi_up", active_path=ap) == (0.6, 0.25, 0.15)
        assert aps.resolve_tp_alloc("ETH", "price_up_oi_up", active_path=ap) is None
        assert aps.resolve_tp_alloc("BTC", "price_down_oi_up", active_path=ap) is None


def test_corrupt_active_file_failsafe_none():
    with tempfile.TemporaryDirectory() as td:
        ap, _ = _paths(td)
        ap.write_text("{ not valid json", encoding="utf-8")
        assert aps.resolve_tp_alloc("BTC", "price_up_oi_up", active_path=ap) is None


def test_valid_alloc_rules():
    assert aps._valid_alloc((0.5, 0.3, 0.2)) is True
    assert aps._valid_alloc((0.34, 0.33, 0.33)) is True
    assert aps._valid_alloc((0.5, 0.3)) is False          # 段數錯
    assert aps._valid_alloc((0.5, 0.3, 0.3)) is False     # 總和 1.1
    assert aps._valid_alloc((-0.1, 0.6, 0.5)) is False    # 負值
    assert aps._valid_alloc(None) is False
    assert aps._valid_alloc("nope") is False


def test_renders_do_not_raise():
    with tempfile.TemporaryDirectory() as td:
        ap, au = _paths(td)
        # 空表
        assert "0" in aps.render_active(ap)
        assert isinstance(aps.render_audit_tail(5, au), str)
        # 有覆寫後
        aps.apply_verdict(_verdict(True), symbol="BTC", quadrant="price_up_oi_up",
                          challenger_alloc=(0.6, 0.25, 0.15), at_ms=1,
                          active_path=ap, audit_path=au)
        assert "BTC|price_up_oi_up" in aps.render_active(ap)
        assert "✅晉升" in aps.render_audit_tail(5, au)
