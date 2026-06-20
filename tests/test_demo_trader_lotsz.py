"""task#72 回歸：OKX demo 51121「Order quantity must be a multiple of the lot size」根因。

根因（活數據實證）：round_contracts_down / split_tp_contracts 舊用 float 乘法 `n_lots*lot_sz`，
對分數 lotSz（如 0.1）產生離格殘渣（7*0.1=0.7000000000000001）。主單 amount 被 ccxt
amountToPrecision 救回，但 attachAlgoOrds 的 TP 腿 sz 經 _fmt_qty 原樣字串化、ccxt 不二次
取整 → OKX 退單 51121。修法：sizing 全程 Decimal 網格 + _fmt_qty 帶 lot_sz denoise。

鐵則：**每一個送進 OKX 的張數（主單 contracts + 每條 attach sz）都必須是該標的 lotSz 的
精確整數倍**（用 Decimal 嚴格檢驗，不容浮點殘渣）。本檔對一片真實 OKX 規格組合掃描驗證。

執行：pytest tests/test_demo_trader_lotsz.py  或  python tests/test_demo_trader_lotsz.py
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from l4_execution.demo_trader import (  # noqa: E402
    build_okx_entry_params,
    build_order_plan,
    round_contracts_down,
    split_tp_contracts,
    _fmt_qty,
)

# (symbol, ctVal, lotSz, minSz) — 取自 live OKX 永續規格（debug 探針實測）。
# 含分數 lotSz=0.1（曾觸 51121）、0.01、整數 1.0；ctVal 跨 0.1 / 1 / 10 / 1000。
SPECS = [
    ("LTC", 1.0, 0.1, 0.1),
    ("AAVE", 0.1, 0.1, 0.1),
    ("AVAX", 1.0, 0.1, 0.1),
    ("BCH", 0.1, 0.1, 0.1),
    ("NEAR", 10.0, 0.1, 0.1),
    ("SOL", 1.0, 0.01, 0.01),
    ("TRX", 1000.0, 0.01, 0.01),
    ("ATOM", 1.0, 1.0, 1.0),
    ("FIL", 0.1, 1.0, 1.0),
    ("XPL", 10.0, 1.0, 1.0),
]

# 一片真實價位 × 止損距離（產生五花八門的 n_lots，逼出浮點殘渣）。
PRICES = [
    (85.0, 83.0), (260.0, 253.0), (22.0, 21.4), (480.0, 470.0), (2.80, 2.73),
    (150.0, 146.0), (0.3215, 0.315), (4.5, 4.4), (3.92, 3.80), (118.0, 115.5),
]


def _is_lot_multiple(q_str: str, lot_sz: float) -> bool:
    """以 OKX 視角嚴格判定：字串化後的張數是否為 lotSz 的精確整數倍。"""
    return (Decimal(q_str) % Decimal(str(lot_sz))) == 0


def test_round_contracts_down_grid_aligned():
    """主單張數：對任意 (qty_base, ctVal, lotSz)，回傳值字串化後必為 lotSz 整數倍。"""
    bad = []
    for _sym, ctv, lot, _mn in SPECS:
        for qb in (0.07692, 0.1538, 2.3077, 0.9091, 7.6923, 0.3030, 19.231):
            c = round_contracts_down(qb, ctv, lot)
            if c <= 0:
                continue
            if not _is_lot_multiple(_fmt_qty(c, lot), lot):
                bad.append((ctv, lot, qb, repr(c)))
    assert not bad, f"round_contracts_down 離格：{bad}"


def test_split_tp_legs_grid_aligned_and_conserved():
    """分批止盈：每腿字串化後為 lotSz 整數倍，且各腿加總 == 總張數（不掉張/不超發）。"""
    bad, broke = [], []
    for _sym, _ctv, lot, _mn in SPECS:
        for total in (lot, 2 * lot, 7 * lot, 13 * lot, 23 * lot, 31 * lot, 187 * lot):
            total = round_contracts_down(total * 1.0001, 1.0, lot)  # 對齊成乾淨總量
            legs = split_tp_contracts(total, lot)
            if abs(sum(legs) - total) > 1e-9:
                broke.append((lot, total, legs))
            for x in legs:
                if x > 0 and not _is_lot_multiple(_fmt_qty(x, lot), lot):
                    bad.append((lot, total, x))
    assert not bad, f"TP 腿離格：{bad}"
    assert not broke, f"TP 腿加總不守恆：{broke}"


def test_entry_params_every_attach_sz_is_lot_multiple():
    """端到端：build_order_plan → build_okx_entry_params，每個 attach sz 必為 lotSz 整數倍。
    這正是會觸發 51121 的那條路徑（attachAlgoOrds.sz 不經 ccxt 二次取整）。"""
    bad = []
    for sym, ctv, lot, mn in SPECS:
        for entry, stop in PRICES:
            if stop >= entry:
                continue
            plan = build_order_plan(sym, "bull", entry, stop, risk_usd=125.0,
                                    ct_val=ctv, lot_sz=lot, min_sz=mn, seq=1)
            if not plan.ok:
                continue
            # 主單張數本身也須對齊
            if not _is_lot_multiple(_fmt_qty(plan.contracts, lot), lot):
                bad.append((sym, lot, entry, "main", repr(plan.contracts)))
            params = build_okx_entry_params(plan)
            for a in params.get("attachAlgoOrds", []):
                if "sz" in a and not _is_lot_multiple(a["sz"], lot):
                    bad.append((sym, lot, entry, a.get("attachAlgoClOrdId"), a["sz"]))
    assert not bad, f"attach/main 張數離格（會觸 51121）：{bad}"


def test_avax_regression_exact_strings():
    """AVAX 型（lot=0.1）精確重現：修前 attach sz 會出 '83.30000000000001'。"""
    plan = build_order_plan("AVAX", "bull", 22.0, 21.4, risk_usd=125.0,
                            ct_val=1.0, lot_sz=0.1, min_sz=0.1, seq=11)
    assert plan.ok, plan.reject_reason
    szs = [a["sz"] for a in build_okx_entry_params(plan).get("attachAlgoOrds", []) if "sz" in a]
    assert szs, "應有 attach TP 腿"
    for s in szs:
        assert "e" not in s.lower(), f"不得科學記號：{s}"
        assert "0000000" not in s, f"不得浮點殘渣：{s}"
        assert _is_lot_multiple(s, 0.1), f"須為 0.1 整數倍：{s}"


def test_fmt_qty_denoise():
    """_fmt_qty 第二道防線：餵浮點殘渣，帶 lot_sz 時 snap 回乾淨格點。"""
    assert _fmt_qty(0.7000000000000001, 0.1) == "0.7"
    assert _fmt_qty(0.30000000000000004, 0.1) == "0.3"
    assert _fmt_qty(62.400000000000006, 0.1) == "62.4"
    assert _fmt_qty(9.370000000000001, 0.01) == "9.37"
    # 不帶 lot_sz → 維持原行為（不 snap），但不得出科學記號
    assert "e" not in _fmt_qty(0.0000001).lower()
    assert _fmt_qty(65000.0) == "65000"
    assert _fmt_qty(7.0, 1.0) == "7"


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
