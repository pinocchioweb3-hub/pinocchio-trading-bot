"""task#62：階層式部分池化分桶 契約測試。

鎖住兩件事，避免日後改動把「解樣本餓死」重新埋回去：
  1. **消費端解析階梯**（governs live 模擬盤進場行為）＝ resolve_entry_policy 的
     fallback ladder：(symbol,quadrant) → (POOL,quadrant) → (POOL,POOL)。
       最具體且有有效覆寫者勝；缺則退回象限池、再退回全域池；全無 → None（今日行為）。
       這是 ~671 天餓死 → ~5–22 天可學 的關鍵（task#59 已證 regime-invariant，全域池最有據）。
  2. **編排端分桶**＝ optimize_entry 同時建 per-symbol×regime + 象限池 + 全域池三層，
       由一般到具體處理（讓具體桶 champion 能繼承本輪已晉升的池化覆寫），且小樣本仍 inert。

另外把兩模組的 _selftest 拉進 pytest，確保 CI 會跑到合成 K 線那套整合驗證。
全離線：覆寫表寫進 tmp_path；零網路、零真錢、零訊號數學變更（紅線①/③）。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from l3_dispatcher import entry_policy_store as eps
from l3_dispatcher import entry_policy_optimizer as epo


# ── 工具：用一個 promote=True 的 duck-typed verdict 在指定 (symbol,quadrant) 種覆寫 ──
def _seed(ap, au, symbol, quadrant, kind, *, at_ms=1):
    v = SimpleNamespace(promote=True, bucket_key=eps.bucket_key(symbol, quadrant),
                        challenger=kind, champ_mean_r=-0.01, chal_mean_r=0.05,
                        champ_fill_rate=35.0, chal_fill_rate=94.0, coverage_delta_pp=59.0,
                        n_aligned=40, self_check_ok=True, l2_passed=True,
                        l2_summary="(test seed)", reasons=[])
    res = eps.apply_verdict(v, symbol=symbol, quadrant=quadrant,
                            challenger_kind=kind, champion_kind=eps.DEFAULT_KIND,
                            at_ms=at_ms, active_path=ap, audit_path=au)
    assert res["action"] == "promote", res
    return res


# ════════════════════════════════════════════════════════════════════════
#  module selftests（讓 CI 跑到合成 K 線整合驗證）
# ════════════════════════════════════════════════════════════════════════
def test_store_selftest_passes():
    assert eps._selftest() is True


def test_optimizer_selftest_passes():
    assert epo._selftest() is True


# ════════════════════════════════════════════════════════════════════════
#  解析階梯純度（_resolution_ladder）
# ════════════════════════════════════════════════════════════════════════
def test_ladder_order_and_dedup():
    bk = eps.bucket_key
    # 一般情形：三階皆相異 → 最具體 → 象限池 → 全域池
    assert eps._resolution_ladder("BTC", "price_up_oi_up") == [
        bk("BTC", "price_up_oi_up"), bk(eps.POOL, "price_up_oi_up"), bk(eps.POOL, eps.POOL)]
    # symbol 本身已是 POOL → 前兩階自然塌陷（去重後只剩兩鍵）
    assert eps._resolution_ladder(eps.POOL, "price_up_oi_up") == [
        bk(eps.POOL, "price_up_oi_up"), bk(eps.POOL, eps.POOL)]
    # 全 POOL → 只剩全域池一鍵
    assert eps._resolution_ladder(eps.POOL, eps.POOL) == [bk(eps.POOL, eps.POOL)]


# ════════════════════════════════════════════════════════════════════════
#  消費端解析：最具體勝、階梯退回（這是 live 行為的真相）
# ════════════════════════════════════════════════════════════════════════
def test_global_pool_applies_to_any_symbol_and_regime(tmp_path):
    ap, au = tmp_path / eps.ACTIVE_NAME, tmp_path / eps.AUDIT_NAME
    _seed(ap, au, eps.POOL, eps.POOL, "market")
    # 全域池覆寫 → 任何 symbol/任何 regime 都解析到它
    assert eps.resolve_entry_policy("BTC", "price_up_oi_up", active_path=ap) == "market"
    assert eps.resolve_entry_policy("DOGE", "price_down_oi_down", active_path=ap) == "market"
    assert eps.resolve_entry_policy("ETH", "unknown", active_path=ap) == "market"


def test_quadrant_pool_overrides_global_within_its_quadrant(tmp_path):
    ap, au = tmp_path / eps.ACTIVE_NAME, tmp_path / eps.AUDIT_NAME
    _seed(ap, au, eps.POOL, eps.POOL, "market")                       # 全域＝市價
    _seed(ap, au, eps.POOL, "price_up_oi_up", "limit_convert")        # 該象限＝D
    # 該象限 → 取象限池（比全域具體）
    assert eps.resolve_entry_policy("BTC", "price_up_oi_up", active_path=ap) == "limit_convert"
    assert eps.resolve_entry_policy("SOL", "price_up_oi_up", active_path=ap) == "limit_convert"
    # 其他象限 → 仍退回全域池
    assert eps.resolve_entry_policy("BTC", "price_down_oi_down", active_path=ap) == "market"


def test_per_symbol_overrides_pool_and_other_symbol_falls_back(tmp_path):
    ap, au = tmp_path / eps.ACTIVE_NAME, tmp_path / eps.AUDIT_NAME
    _seed(ap, au, eps.POOL, eps.POOL, "market")                       # 全域＝市價
    _seed(ap, au, eps.POOL, "price_up_oi_up", "limit_convert")        # 象限池＝D
    _seed(ap, au, "BTC", "price_up_oi_up", "limit_convert")           # BTC 該象限特化
    # 最具體勝：BTC 取自身桶
    assert eps.resolve_entry_policy("BTC", "price_up_oi_up", active_path=ap) == "limit_convert"
    # 同象限的別的 symbol（無自身桶）→ 退回象限池
    assert eps.resolve_entry_policy("ETH", "price_up_oi_up", active_path=ap) == "limit_convert"
    # 同 symbol 但別的象限（無自身桶、無該象限池）→ 退回全域池
    assert eps.resolve_entry_policy("BTC", "price_down_oi_down", active_path=ap) == "market"


def test_quadrant_pool_does_not_leak_to_other_quadrant(tmp_path):
    ap, au = tmp_path / eps.ACTIVE_NAME, tmp_path / eps.AUDIT_NAME
    _seed(ap, au, eps.POOL, "price_up_oi_up", "limit_convert")        # 只種一個象限池
    assert eps.resolve_entry_policy("BTC", "price_up_oi_up", active_path=ap) == "limit_convert"
    # 別的象限、無全域池 → None（不外洩、不硬湊）
    assert eps.resolve_entry_policy("BTC", "price_down_oi_down", active_path=ap) is None


def test_empty_store_resolves_none(tmp_path):
    ap = tmp_path / eps.ACTIVE_NAME
    # 空表（inert-on-ship）→ 各層皆 None＝今日深限價可到期行為
    assert eps.resolve_entry_policy("BTC", "price_up_oi_up", active_path=ap) is None
    assert eps.resolve_entry_policy(eps.POOL, eps.POOL, active_path=ap) is None


def test_default_kind_in_store_treated_as_no_override(tmp_path):
    """覆寫值＝預設(limit_expire) → 視同此階無覆寫，續往更一般階找。"""
    ap, au = tmp_path / eps.ACTIVE_NAME, tmp_path / eps.AUDIT_NAME
    _seed(ap, au, eps.POOL, eps.POOL, "market")                       # 全域＝市價
    # 手動把 BTC 桶寫成預設 kind（模擬退化值）→ resolve 應跳過它退回全域
    import json
    buckets = json.loads(ap.read_text(encoding="utf-8"))["buckets"]
    buckets[eps.bucket_key("BTC", "price_up_oi_up")] = {"kind": eps.DEFAULT_KIND}
    ap.write_text(json.dumps({"version": 1, "updated_at_ms": 1, "buckets": buckets},
                             ensure_ascii=False), encoding="utf-8")
    assert eps.resolve_entry_policy("BTC", "price_up_oi_up", active_path=ap) == "market"


# ════════════════════════════════════════════════════════════════════════
#  可讀標籤
# ════════════════════════════════════════════════════════════════════════
def test_bucket_label():
    assert eps._bucket_label(eps.bucket_key(eps.POOL, eps.POOL)) == "全域池(跨一切)"
    assert eps._bucket_label(eps.bucket_key(eps.POOL, "price_up_oi_up")) == \
        "象限池·price_up_oi_up（跨 symbol）"
    # per-symbol 桶＝原鍵（不另翻）
    assert eps._bucket_label(eps.bucket_key("BTC", "price_up_oi_up")) == "BTC|price_up_oi_up"


# ════════════════════════════════════════════════════════════════════════
#  編排端：_level_of / 處理順序（由一般到具體＝池化繼承的前提）
# ════════════════════════════════════════════════════════════════════════
def test_level_of():
    assert epo._level_of(eps.POOL, eps.POOL) == epo._LEVEL_GLOBAL
    assert epo._level_of(eps.POOL, "price_up_oi_up") == epo._LEVEL_QUAD
    assert epo._level_of("BTC", "price_up_oi_up") == epo._LEVEL_SYMBOL
    # rank 嚴格遞增：全域 < 象限 < per-symbol
    assert (epo._LEVEL_RANK[epo._LEVEL_GLOBAL]
            < epo._LEVEL_RANK[epo._LEVEL_QUAD]
            < epo._LEVEL_RANK[epo._LEVEL_SYMBOL])


# ------------------------------------------------- v114 稽核 rank1/rank3 治本
def test_plan_prices_uses_shallowest_split_price():
    """rank1：champion 重放限價須取『實際首格』——bull=最高格、bear=最低格；
    缺 splits 退回 planned_entry（中點）。這是解開已過 L2 晉升被 self-check 卡死的關鍵。"""
    import json as _json
    from l3_dispatcher.entry_policy_optimizer import _plan_prices
    row = {"direction": "bull", "entry_price": 1612.0, "stop_price": 1580.0, "tp1": 1700.0,
           "plan_snapshot": _json.dumps({"planned_entry": 1612.0, "planned_stop": 1580.0,
                                         "planned_tp": {"tp1": 1700.0}}),
           "entry_splits": _json.dumps([{"price": 1608.0, "frac": 0.6},
                                        {"price": 1616.0, "frac": 0.4}])}
    d, limit_px, stop_px, tp_px = _plan_prices(row)
    assert limit_px == 1616.0            # bull 首格=最高格（價下跌先觸），非中點 1612
    row_bear = dict(row, direction="bear",
                    plan_snapshot=_json.dumps({"planned_entry": 1612.0,
                                               "planned_stop": 1650.0,
                                               "planned_tp": {"tp1": 1500.0}}))
    d2, limit2, *_ = _plan_prices(row_bear)
    assert limit2 == 1608.0              # bear 首格=最低格（價上漲先觸）
    row_nosplit = dict(row, entry_splits=None)
    _, limit3, *_ = _plan_prices(row_nosplit)
    assert limit3 == 1612.0              # 無 splits → 退回 planned_entry


def test_loader_filters_to_deepdive_only(tmp_path):
    """rank3：loader 只收加密 deepdive，美股 us_breakout 不得混入池化桶（統計純淨）。"""
    import sqlite3
    import time as _t
    from l3_dispatcher.entry_policy_optimizer import _load_paper_for_entry
    db = tmp_path / "tj.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE paper_trades (id INTEGER PRIMARY KEY, symbol TEXT, "
                 "setup TEXT, direction TEXT, entry_price REAL, stop_price REAL, tp1 REAL, "
                 "entry_at INTEGER, exit_reason TEXT, status TEXT, plan_snapshot TEXT, "
                 "entry_splits TEXT)")
    now = int(_t.time() * 1000)
    conn.execute("INSERT INTO paper_trades (symbol,setup,direction,entry_price,stop_price,"
                 "tp1,entry_at,exit_reason,status) VALUES "
                 "('BTC','deepdive','bull',100,95,110,?,'tp3','closed')", (now,))
    conn.execute("INSERT INTO paper_trades (symbol,setup,direction,entry_price,stop_price,"
                 "tp1,entry_at,exit_reason,status) VALUES "
                 "('SOXL','us_breakout','bull',30,28,35,?,'tp3','closed')", (now,))
    conn.commit(); conn.close()
    rows = _load_paper_for_entry(days=7, db=str(db))
    assert len(rows) == 1 and rows[0]["symbol"] == "BTC"   # 美股被濾掉
