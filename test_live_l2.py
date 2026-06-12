"""跑 L2 引擎在真實 CoinGlass 快照上 - 看現在這個時刻 BTC/SUI 系統會說什麼。"""
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")
os.environ["MARKET_INTEL_BACKEND"] = "coinglass"

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from market_intel_mcp.server import mi_get_snapshot
from l2_trigger.configs.intraday import get_intraday_config
from l2_trigger.configs.ambush import get_ambush_config
from l2_trigger.engine import evaluate
from l2_trigger.types import MarketSnapshot


async def main():
    for sym in ("BTC", "SUI"):
        d = await mi_get_snapshot(sym, "1h", 96)
        fn = {f.name for f in MarketSnapshot.__dataclass_fields__.values()}
        kept = {k: v for k, v in d.items() if k in fn and v is not None}
        if "stale_fields" in kept:
            kept["stale_fields"] = tuple(kept["stale_fields"])
        if "sources_used" in kept:
            kept["sources_used"] = tuple(kept["sources_used"])
        for must in ("symbol", "ts", "price"):
            if must not in kept:
                kept[must] = sym if must == "symbol" else 0
        s = MarketSnapshot(**kept)

        print(f"\n{'=' * 70}\n{sym} live snapshot @ ${s.price}")
        print(f"  funding={s.funding}  top_trader={s.top_trader_ratio}  retail={s.ls_ratio}")
        print(f"  oi_d24={s.oi_delta_pct}%  btc_gate={s.btc_gate_open}  regime={s.btc_regime}")
        print(f"  stale ({len(s.stale_fields)} fields): {list(s.stale_fields)}")

        ca = get_intraday_config(sym)
        cb = get_ambush_config(sym)
        da = evaluate(s, ca)
        db = evaluate(s, cb)
        print(f"  [intraday] {da.action.value:5}  {da.reason}")
        print(f"  [ambush]   {db.action.value:5}  {db.reason}")


if __name__ == "__main__":
    asyncio.run(main())
