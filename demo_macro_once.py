"""Demo：一次性完整宏觀分析。
預設用 Claude Sonnet 4.6 做敘事化合成。
缺 ANTHROPIC_API_KEY → fallback 回原始 template 渲染。

用法：
    python demo_macro_once.py                    # Sonnet 4.6 (預設)
    python demo_macro_once.py --model opus       # Opus 4.8（深度，貴 5 倍）
    python demo_macro_once.py --model haiku      # Haiku 4.5（輕量，便宜）
    python demo_macro_once.py --no-llm           # 不用 LLM，純 template
    python demo_macro_once.py --no-tradfi        # 跳過 Yahoo 拉取
    python demo_macro_once.py --auto-upgrade     # 重大事件自動升級 Opus
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass

os.environ["MARKET_INTEL_BACKEND"] = "coinglass"
sys.path.insert(0, str(ROOT))

from l3_dispatcher.macro import compute_macro_state
from l3_dispatcher.synthesizer import (
    MODEL_HAIKU, MODEL_OPUS, MODEL_SONNET,
    should_use_opus, synthesize_macro, synthesize_via_claude_code,
)
from l3_dispatcher.watchlist import WatchlistManager
from market_intel_mcp.sources import get_source
from telegram_bot.client import TelegramClient
from telegram_bot.message_format import render_macro_report


MODEL_MAP = {"opus": MODEL_OPUS, "sonnet": MODEL_SONNET, "haiku": MODEL_HAIKU}


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["claude-code", "api", "template"],
                   default="claude-code",
                   help="claude-code=Max 訂閱包月免費 / api=Anthropic 計費 / template=純結構化")
    p.add_argument("--model", choices=["opus", "sonnet", "haiku"], default="sonnet",
                   help="僅 --mode=api 時生效")
    p.add_argument("--no-tradfi", action="store_true")
    p.add_argument("--auto-upgrade", action="store_true",
                   help="僅 --mode=api：重大事件升級 Opus")
    args = p.parse_args()

    mode_label = {
        "claude-code": "Claude Code Headless (Max 訂閱)",
        "api": f"Anthropic API ({MODEL_MAP[args.model]})",
        "template": "純 template renderer",
    }[args.mode]

    print("=" * 60)
    print("  Demo: 宏觀分析推送 → Telegram")
    print(f"  模式：{mode_label}")
    print(f"  TradFi：{'skip' if args.no_tradfi else 'fetch'}")
    print("=" * 60)

    tg = TelegramClient()
    if not tg.configured():
        print("ERROR: Telegram env missing"); return 1

    watchlist = WatchlistManager()
    watchlist.trading = ["BTC", "SUI", "ARB", "DOGE", "LINK", "ADA", "AVAX", "XRP"]
    source = get_source()

    # === 1. CoinGlass + OKX news ===
    print("\n[1/4] 拉 CoinGlass + OKX 數據（並行 15 項）...")
    state = await compute_macro_state(source, watchlist)
    print(f"      OK")

    # === 2. TradFi ===
    tradfi = None
    if not args.no_tradfi:
        print("\n[2/4] 拉 Yahoo Finance 跨資產（11 個 ticker 並行）...")
        from market_intel_mcp.sources.tradfi import get_tradfi
        tradfi = await get_tradfi().get_full_snapshot()
        ok_count = sum(1 for v in tradfi.get("items", {}).values()
                       if not v.get("error"))
        total = len(tradfi.get("items", {}))
        print(f"      OK  {ok_count}/{total} ticker 成功")
    else:
        print("\n[2/4] 跳過 TradFi")

    # === 3. 合成（依 mode 分流）===
    if args.mode == "template":
        print(f"\n[3/4] template renderer（純結構化）...")
        text = render_macro_report(state, watchlist)
        meta = {"mode": "template"}
    elif args.mode == "claude-code":
        print(f"\n[3/4] Claude Code Headless 合成中...")
        text, meta = await synthesize_via_claude_code(state, tradfi, watchlist)
        if text is None:
            print(f"      FAILED: {meta.get('error')}")
            print(f"      → fallback to template")
            text = render_macro_report(state, watchlist)
            meta = {"mode": "template_fallback", "error": meta.get("error")}
        else:
            print(f"      OK ({meta.get('input_chars')} in → {meta.get('output_chars')} out)")
            print(f"      成本: 包月免費 (Max)")
    elif args.mode == "api":
        has_key = bool(os.getenv("ANTHROPIC_API_KEY"))
        if not has_key:
            print(f"\n[3/4] ❌ --mode=api 但 ANTHROPIC_API_KEY 未設 → fallback template")
            text = render_macro_report(state, watchlist)
            meta = {"mode": "template_fallback", "error": "no api key"}
        else:
            model = MODEL_OPUS if (args.auto_upgrade and should_use_opus(state)) \
                    else MODEL_MAP[args.model]
            print(f"\n[3/4] Anthropic API 合成中（{model}）...")
            text, meta = await synthesize_macro(state, tradfi, watchlist, model=model)
            if text is None:
                print(f"      FAILED: {meta.get('error')}")
                text = render_macro_report(state, watchlist)
                meta = {"mode": "template_fallback", "error": meta.get("error")}
            else:
                print(f"      OK  in={meta['input_tokens']} out={meta['output_tokens']} ${meta['estimated_cost_usd']}")

    # === 4. Push ===
    print(f"\n[4/4] 推送 Telegram（{len(text)} chars）...")
    if len(text) > 4096:
        parts = []
        cur = ""
        for line in text.split("\n"):
            if len(cur) + len(line) + 1 > 3900:
                parts.append(cur); cur = line
            else:
                cur += ("\n" if cur else "") + line
        if cur: parts.append(cur)
        for i, part in enumerate(parts, 1):
            await tg.send_message(f"<b>[{i}/{len(parts)}]</b>\n{part}",
                                  parse_mode="HTML")
            print(f"      推送第 {i}/{len(parts)} 段")
            await asyncio.sleep(0.6)
    else:
        await tg.send_message(text, parse_mode="HTML")
    print("      OK 推送完成")

    print(f"\n{'=' * 60}\n  完成 — 看你 Telegram\n{'=' * 60}\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
