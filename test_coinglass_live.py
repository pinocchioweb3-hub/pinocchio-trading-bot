"""真打 CoinGlass API 測試 — 切換 backend=coinglass 跑全部 9 個工具。

執行：
    python test_coinglass_live.py

腳本會：
    1. 從 .env 載入 COINGLASS_API_KEY
    2. 強制 backend=coinglass
    3. 對 BTC + SUI 跑 positioning / oi / funding / liquidations / price / btc_gate
    4. 跑 mi_get_snapshot(SUI) 完整快照
    5. 把缺料端點（cvd, strength_rank, structure）的錯誤訊息也印出來確認 graceful

每個工具的回傳結構截短顯示（避免長 dump）。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# 載 .env，覆寫 backend
from dotenv import load_dotenv
load_dotenv()
os.environ["MARKET_INTEL_BACKEND"] = "coinglass"

# Windows 中文路徑 + emoji 支援
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# === Import AFTER env override（settings 在 import 時讀） ===
from market_intel_mcp.server import (
    mi_get_btc_gate,
    mi_get_cvd,
    mi_get_funding,
    mi_get_liquidations,
    mi_get_oi,
    mi_get_positioning,
    mi_get_snapshot,
    mi_get_strength_rank,
    mi_health,
)


def _fmt_result(name: str, result: dict, expected_keys: list[str] | None = None) -> None:
    if not isinstance(result, dict):
        print(f"  [{name}] ❌ non-dict result: {type(result).__name__}")
        return
    if result.get("error"):
        code = result.get("code")
        msg = result.get("message")
        print(f"  [{name}] ⚠️  error code={code}  msg={msg}")
        if "suggestion" in result:
            print(f"           suggestion: {result['suggestion']}")
        return

    print(f"  [{name}] ✅ source={result.get('source','?')}")
    if expected_keys:
        for k in expected_keys:
            if k in result:
                v = result[k]
                if isinstance(v, list):
                    print(f"           {k}: {len(v)} items")
                elif isinstance(v, dict):
                    print(f"           {k}: dict({list(v.keys())[:5]}...)")
                elif isinstance(v, float):
                    print(f"           {k}: {v}")
                else:
                    print(f"           {k}: {v}")


async def main():
    print("=" * 72)
    print("  CoinGlass live API test  (backend=coinglass, real network calls)")
    print("=" * 72)

    # === Health ===
    print("\n[mi_health]")
    h = await mi_health()
    print(f"  backend={h.get('backend')}  source={h.get('source_name')}  ok={h.get('ok')}")
    print(f"  details: {h.get('details')}")
    if not h.get("ok"):
        print("\n❌ Health check failed; aborting. Check .env and CoinGlass key.")
        return 1

    # === 各端點：BTC + SUI ===
    for sym in ("BTC", "SUI"):
        print(f"\n{'─' * 72}\n  ● {sym}\n{'─' * 72}")

        print("\n  Positioning (top_trader_position, 4h, 24 bars)")
        r = await mi_get_positioning(sym, "top_trader_position", "4h", 24)
        _fmt_result("positioning", r, ["latest", "delta_pct", "series", "ratio_type"])

        print("\n  Positioning (account, 4h, 24 bars)")
        r = await mi_get_positioning(sym, "account", "4h", 24)
        _fmt_result("positioning_retail", r, ["latest", "delta_pct"])

        print("\n  OI (aggregated, 1h, 24 bars)")
        r = await mi_get_oi(sym, "1h", 24)
        _fmt_result("oi", r, ["latest", "delta_pct_24h", "series"])

        print("\n  Funding")
        r = await mi_get_funding(sym)
        _fmt_result("funding", r, ["funding", "funding_predicted"])

        print("\n  Liquidations (24h aggregated)")
        r = await mi_get_liquidations(sym, "24h")
        _fmt_result("liquidations", r, ["liq_long", "liq_short"])

    # === BTC 閘（compute from CoinGlass price OHLC）===
    print(f"\n{'─' * 72}\n  ● BTC GATE (computed from 4h OHLC 200MA)\n{'─' * 72}")
    g = await mi_get_btc_gate()
    _fmt_result("btc_gate", g, ["btc_gate_open", "btc_regime", "evidence"])

    # === 缺料端點：確認 graceful failure ===
    print(f"\n{'─' * 72}\n  ● Expected stale endpoints (graceful errors)\n{'─' * 72}")
    print("\n  CVD (should be WRONG_BACKEND — needs LocalSource)")
    _fmt_result("cvd", await mi_get_cvd("SUI", "1h", 24))

    print("\n  Strength rank (should be NOT_IMPLEMENTED_YET in CG mode)")
    _fmt_result("strength_rank", await mi_get_strength_rank(50, 5))

    # === 完整快照（含 stale 容錯）===
    print(f"\n{'─' * 72}\n  ● mi_get_snapshot('SUI')  full pipeline\n{'─' * 72}")
    snap = await mi_get_snapshot("SUI", "1h", 96)
    print(f"  price={snap.get('price')}  ts={snap.get('ts')}")
    print(f"  oi={snap.get('oi')}  funding={snap.get('funding')}")
    print(f"  top_trader={snap.get('top_trader_ratio')}  retail={snap.get('ls_ratio')}")
    print(f"  liq long/short = {snap.get('liq_long')} / {snap.get('liq_short')}")
    print(f"  btc_gate_open={snap.get('btc_gate_open')}  regime={snap.get('btc_regime')}")
    print(f"  is_hot={snap.get('is_hot')}  strength={snap.get('strength_score')}")
    print(f"  sources_used: {snap.get('sources_used')}")
    print(f"  stale_fields ({len(snap.get('stale_fields',[]))}): {snap.get('stale_fields')}")

    print(f"\n{'=' * 72}")
    print("  CoinGlass live test complete.")
    print(f"{'=' * 72}\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
