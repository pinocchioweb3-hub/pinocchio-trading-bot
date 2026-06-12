"""自我審核：直接呼叫 BTC deep dive 把 Claude 真實產出印出來，讓 Claude Code 在 VS Code 對話框內檢視品質。"""
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

from l3_dispatcher.macro import compute_per_symbol_state
from l3_dispatcher.synthesizer import (
    _format_symbol_data, synthesize_per_symbol,
)
from market_intel_mcp.sources import get_source


async def main(symbol: str = "BTC"):
    src = get_source()
    print(f"=== {symbol} per-symbol state 取得中 ===")
    state = await compute_per_symbol_state(src, symbol)

    print()
    print("=" * 70)
    print(f"  RAW DATA 餵給 Claude (前 1500 字):")
    print("=" * 70)
    data_text = _format_symbol_data(symbol, state)
    print(data_text[:1500])
    print()
    print(f"... [total {len(data_text)} chars]")

    print()
    print("=" * 70)
    print("  呼叫 Claude Code Headless（這要 60-180 秒）...")
    print("=" * 70)
    text, meta = await synthesize_per_symbol(symbol, state)
    if text is None:
        print(f"FAILED: {meta.get('error')}")
        return
    print(f"\n[meta] input={meta.get('input_chars')} output={meta.get('output_chars')}\n")
    print("=" * 70)
    print(f"  Claude 實際產出（{symbol} Deep Dive）:")
    print("=" * 70)
    print(text)
    print("=" * 70)


if __name__ == "__main__":
    sym = sys.argv[1] if len(sys.argv) > 1 else "BTC"
    asyncio.run(main(sym))
