# -*- coding: utf-8 -*-
"""v252：名義值上限把單筆風險砍掉時，必須出聲。

背景（2026-08-03 使用者實測發現）：他預期每單風險 = 帳戶 2%，實際看到的止損金額
只有 1/6。逐格量測後成因不是風險預算設錯，而是 `NOTIONAL_CAP_USD` 這道**防呆夾層**
先咬住了：INTC 那筆風險換算出 14.6 張、名義值 1286U > 上限 600U ⇒ 砍到 6.8 張，
有效風險從 20U 掉到 9.3U。交易所端的保證金 29.95U = 600/20x 是最硬的旁證。

夾層本身是對的（它擋的是張數換算爆掉那種事故），錯的是**它砍完不說話**：
`contracts_for` 只在「砍完連最小張數都不到」時回報，砍到一半＝靜音。於是
「這筆的 1R 是多少」在帳上永遠是設定值，實際卻是另一個數——同一物種
（把量到的降級折成正常）。

⛔ 本輪一個字都不動張數換算的數學，只補**可觀測性**。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "atk_consumer"))
import consume_intents as ci  # noqa: E402

# 2026-08-03 線上真實值（read-only 查回，非杜撰）
INTC_SPEC = {"ctVal": 1.0, "lotSz": 0.1, "minSz": 0.1, "tickSz": 0.01, "maxLever": 25.0}
INTC_ENTRY, INTC_STOP = 88.09, 89.4576783682294


def _expected(entry: float, stop: float, spec: dict) -> tuple[float, float, float]:
    """用模組自己的常數重算一份預期值（模板 100U/3000U 與線上副本 20U/600U 都適用）。"""
    risk = min(ci.RISK_USD, ci.RISK_USD_CAP)
    dist = abs(entry - stop)
    lot = spec["lotSz"]
    raw = int((risk / dist) / spec["ctVal"] / lot) * lot
    capped = int(ci.NOTIONAL_CAP_USD / (spec["ctVal"] * entry) / lot) * lot
    return risk, raw, capped


def test_cap_truncation_is_reported_not_silent():
    """夾層咬住時，呼叫端必須拿得到「本來幾張、實際幾張、有效風險多少」。"""
    risk, raw, capped = _expected(INTC_ENTRY, INTC_STOP, INTC_SPEC)
    assert raw > capped, "測試前提不成立：這組數字沒有觸發名義值夾層"

    out: dict = {}
    sz = ci.contracts_for("INTC-USDT-SWAP", INTC_ENTRY, INTC_STOP,
                          {"INTC-USDT-SWAP": dict(INTC_SPEC)}, out=out)
    assert sz == capped                      # ⛔ 數學不變：張數必須與舊碼逐位相同
    assert out.get("capped") is True
    assert abs(out.get("sz_uncapped") - raw) < 1e-6   # 回報值有 round(,8)
    assert out.get("risk_intended") == risk
    eff = capped * INTC_SPEC["ctVal"] * abs(INTC_ENTRY - INTC_STOP)
    assert abs(out["risk_effective"] - eff) < 1e-9
    assert out["risk_effective"] < risk      # 這正是使用者看到的落差
    assert abs(out.get("notional_cap") - ci.NOTIONAL_CAP_USD) < 1e-9


def test_uncapped_sizing_reports_no_cut():
    """反向側：夾層沒咬住就不得長出 capped 欄位（否則帳上每筆都像被砍過）。"""
    # 止損拉到很寬 ⇒ 風險換算出的張數本來就小，名義值遠低於上限
    entry, stop = 88.09, 88.09 * 1.30
    out: dict = {}
    sz = ci.contracts_for("INTC-USDT-SWAP", entry, stop,
                          {"INTC-USDT-SWAP": dict(INTC_SPEC)}, out=out)
    assert sz is not None and sz > 0
    assert sz * INTC_SPEC["ctVal"] * entry <= ci.NOTIONAL_CAP_USD
    assert not out.get("capped")
    assert "risk_effective" not in out


def test_cut_lands_in_health_and_survives_an_idle_round():
    """記帳側：缺口要進健康檔，而且必須寫在「空轉輪提早 return」之前。

    砍張數發生在接新單那一刻，那一輪很可能一次成功呼叫都沒有（下單回應算 ok 之前
    就先算好張數）——v169/v170/v249 三次都栽在這條路徑上。"""
    cut = {"intent_id": "abc", "inst_id": "INTC-USDT-SWAP", "symbol": "INTC",
           "pos_side": "short", "risk_intended": 20.0, "risk_effective": 9.3,
           "sz_uncapped": 14.6, "sz": 6.8, "notional_cap": 600.0}
    h = ci.update_health({}, {}, 1000.0, oks=0, risk_cuts=[cut])
    assert h.get("idle_rounds") == 1                     # 確實走了空轉輪分支
    assert h.get("risk_capped_total") == 1
    assert h.get("risk_capped_last_ts") == 1000.0
    rec = (h.get("risk_capped_recent") or [])[-1]
    assert rec["inst_id"] == "INTC-USDT-SWAP"
    assert rec["risk_effective"] == 9.3 and rec["risk_intended"] == 20.0

    # 沒缺口就不生欄位（帳本不長出一排 0）
    h2 = ci.update_health({}, {}, 1001.0, oks=0, risk_cuts=[])
    assert "risk_capped_total" not in h2

    # 累加而非覆蓋，且明細有上限
    h3 = ci.update_health(h, {}, 1002.0, oks=0,
                          risk_cuts=[dict(cut) for _ in range(ci.CAP_CUT_RECENT_MAX + 5)])
    assert h3["risk_capped_total"] == 1 + ci.CAP_CUT_RECENT_MAX + 5
    assert len(h3["risk_capped_recent"]) == ci.CAP_CUT_RECENT_MAX


def test_finish_round_accepts_risk_cuts():
    """迴圈級：收尾函式要收得下這條通道，否則主迴圈算了也送不出去。"""
    import inspect
    assert "risk_cuts" in inspect.signature(ci.finish_round).parameters
