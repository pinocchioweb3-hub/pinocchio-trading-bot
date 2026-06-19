"""主入口：分層 watchlist + 多 worker 並行交易機器人。

架構：
    指標層 BTC/ETH/SOL        → 規範市場 regime（不交易）
    現貨層 SUI/WLFI           → 監控（不交易）
    交易層 動態 Top 7-10       → Setup A/B 在此 FIRE

Workers（asyncio.gather）：
    🔄 scheduler       每 N 秒掃交易層 → 排入 fire_queue
    📬 dispatcher      Poll queue → Telegram
    📊 macro_report    每 N 秒推宏觀分析（指標+現貨+regime 建議）
    🔁 watchlist_ref   每日 00:00 UTC 重排交易層

模式：
    python run_bot.py                            # 真實 CoinGlass，連續
    python run_bot.py --backend mock             # 模擬
    python run_bot.py --once                     # 單輪 + 退出
    python run_bot.py --scan-interval 900        # 15 min 掃描
    python run_bot.py --macro-interval 3600      # 1 hr 宏觀報告
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
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


_SUPERVISE_ALERT_LAST: dict[str, float] = {}


async def supervise(name: str, factory, alert_tg=None):
    """v14: worker 隔離包裝 — 崩潰不再殺全 daemon，指數退避自動重啟 + TG 警報。
    v15: 警報節流 — 同一 worker 30 分鐘最多警報一次（防反覆崩潰轟炸群組）。

    factory 是 zero-arg callable，每次重啟產生新的 coroutine。
    """
    import time as _t
    backoff = 5
    while True:
        try:
            await factory()
            print(f"[supervise:{name}] exited normally")
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)[:200]}"
            print(f"[supervise:{name}] CRASHED: {msg} -> restart in {backoff}s")
            now = _t.monotonic()
            if alert_tg is not None and now - _SUPERVISE_ALERT_LAST.get(name, 0) > 1800:
                _SUPERVISE_ALERT_LAST[name] = now
                try:
                    await alert_tg.send_message(
                        f"⚠️ <b>Worker 崩潰，自動重啟中</b>\n"
                        f"Worker：<code>{name}</code>\n"
                        f"錯誤：<code>{msg}</code>\n"
                        f"{backoff} 秒後重啟（其他 worker 不受影響；"
                        f"同 worker 警報 30 分鐘內不重複）",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 600)


async def amain(args: argparse.Namespace) -> int:
    os.environ["MARKET_INTEL_BACKEND"] = args.backend

    # 延遲 import 確保 env 已設
    from l3_dispatcher.dispatcher import dispatch_once, run_dispatcher
    from l3_dispatcher.fire_queue import stats
    from l3_dispatcher.macro import compute_macro_state
    from l3_dispatcher import liveness
    from l3_dispatcher.scheduler import run_scheduler, scan_once
    from l3_dispatcher.watchlist import WatchlistManager, run_refresh_loop
    from market_intel_mcp.sources import get_source
    from telegram_bot.client import TelegramClient
    from telegram_bot.message_format import (
        render_macro_report,
        render_refresh_summary,
        render_shutdown,
        render_startup,
    )

    tg = TelegramClient()
    if not tg.configured():
        print("ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
        return 1

    me = await tg.get_me()
    if not me.get("ok"):
        print(f"ERROR: Telegram getMe failed: {me}")
        return 1
    print(f"[startup] bot @{me['result']['username']}  backend={args.backend}")

    # === v15: Forum Topics 路由（6 主題；未設定時全部 fallback 單一 chat）===
    from telegram_bot.topics import TopicRouter
    router = TopicRouter(tg)
    tg_trade = router.client("trade")          # 🎯 FIRE 進場訊號 + 熔斷警報（高價值低頻）
    tg_positions = router.client("positions")  # 📈 TP/SL 事件 / 持倉快照 / 績效 / 風控阻擋
    tg_intel = router.client("intel")          # 📊 宏觀 / pulse / deepdive / watchlist
    tg_news = router.client("news")            # 📰 Trump / X（已過濾+翻譯）
    tg_sys = router.client("system")           # 🛠 開關機 / worker 警報 / supervisor

    # === 初始化 watchlist manager + 立即 refresh 交易層 ===
    source = get_source()
    # v27: 訊號層大小走 botconfig（全市場 Top N，可 /settings 調）；CLI 明確指定才覆蓋
    from botconfig import CONFIG as _BC
    _tsize = args.trading_size if args.trading_size else _BC.trading_size
    watchlist = WatchlistManager(trading_size=_tsize)
    print(f"[startup] refreshing trading tier...")
    refresh_result = await watchlist.refresh(source)
    print(f"[startup] trading tier: {refresh_result['chosen']}")

    # === 開機訊息（v23-6: 30 分鐘內重啟不重推，避免部署期洗版系統主題）===
    if not args.no_startup_msg:
        import json as _json
        import time as _t
        from botpaths import data_dir as _dd
        _su = _dd() / "startup_state.json"
        _recent = False
        try:
            if _su.exists():
                _last = _json.loads(_su.read_text(encoding="utf-8")).get("ts", 0)
                _recent = (_t.time() - _last) < 1800
        except Exception:
            pass
        if _recent:
            print("[startup] 30 分鐘內已發過開機訊息，本次靜默（避免洗版）")
        else:
            startup_text = render_startup(
                backend=args.backend,
                watchlist=watchlist.all_symbols,
                interval_s=args.scan_interval,
            )
            await tg_sys.send_message(startup_text, parse_mode="HTML")
            try:
                _su.write_text(_json.dumps({"ts": _t.time()}), encoding="utf-8")
            except Exception:
                pass
        # v14.1: 交易層名單只推一次 — run_refresh_loop 的 initial refresh
        # 會經 on_refresh 推 summary，這裡不再重複推

    # === --once 模式：跑一輪掃 + 推宏觀 + drain queue + 退出 ===
    if args.once:
        print("[mode] --once")
        summary = await scan_once(watchlist, cooldown_seconds=args.cooldown)
        print(f"[scan] fire_scan={summary.scanned}  fires={summary.fires_enqueued}")

        # Drain（v14.1: 走主題路由，與連續模式一致）
        drained = 0
        while await dispatch_once(tg_trade, tg_positions):
            drained += 1
        print(f"[drain] dispatched {drained}")

        # 跑一次宏觀
        try:
            state = await compute_macro_state(source, watchlist)
            await tg_intel.send_message(render_macro_report(state, watchlist), parse_mode="HTML")
            print(f"[macro] sent (regime={state['regime']})")
        except Exception as e:
            print(f"[macro] error: {e}")

        await tg_sys.send_message(render_shutdown(stats()), parse_mode="HTML")
        return 0

    # === 連續模式 ===
    from l3_dispatcher.supervisor import SupervisorState, run_supervisor_loop
    print(f"[mode] continuous: scan({args.scan_interval}s) + macro({args.macro_interval}s) "
          f"+ supervisor({args.supervisor_interval}s) + refresh(daily)")

    sup_state = SupervisorState()

    # === v49: 啟動斷層偵測 — 若上次存活戳記距今過久，daemon 曾斷線 → 推一則告警 ===
    # 純本地、只在真有斷層時出聲（高訊號），正常重啟靜默。
    try:
        _gap = liveness.check_gap()
        if _gap["gap"]:
            liveness.record_gap(_gap["last_ts"], _gap["gap_sec"])  # v50: 記入缺口帳本供 CEO 日報回顧
            await tg_sys.send_message(
                liveness.render_gap_alert(_gap["last_ts"], _gap["gap_sec"]),
                parse_mode="HTML",
            )
            print(f"[liveness] offline-gap alert sent (gap={_gap['gap_sec']:.0f}s)")
        else:
            print(f"[liveness] no gap ({_gap['reason']})")
    except Exception as e:
        print(f"[liveness] gap check error: {type(e).__name__}: {e}")

    # v52: 啟動即寫一筆存活戳記（不必等首輪 scan_once 完成，深度掃描可能耗時數分鐘）。
    # 修正外部 watchdog 冷啟誤判：首輪掃描期間心跳會顯得過舊，watchdog 可能在 grace
    # 後誤殺暖機中的健康 daemon。startup 戳記讓心跳從 t=0 起即新鮮。
    # 註：必須在上面 check_gap 之後 —— check_gap 要先讀到「舊」戳記才能算出斷層。
    try:
        liveness.stamp({"startup": True, "scanned": 0, "fires": 0})
        print("[liveness] startup heartbeat stamped")
    except Exception as e:
        print(f"[liveness] startup stamp error: {type(e).__name__}: {e}")

    _refresh_seen = {"first": True}
    async def on_refresh(result):
        # v23-6: 開機訊息已顯示觀察清單 → 跳過啟動時的首次 refresh 摘要（重啟不洗版）；
        # 只在之後的每日重排推（那才是真正的名單變動）
        if _refresh_seen["first"]:
            _refresh_seen["first"] = False
            print("[refresh-notify] 啟動首次 refresh 靜默（開機訊息已含名單）")
            return
        try:
            await tg_intel.send_message(render_refresh_summary(result, watchlist), parse_mode="HTML")
        except Exception as e:
            print(f"[refresh-notify] {e}")

    def on_scan_summary(summary):
        """scheduler 結束每輪就把 summary 給 supervisor 用"""
        import time as _t
        sup_state.last_scan_ts = _t.time()
        sup_state.last_scan_summary = {"snapshots": summary.snapshots}
        # v49: 每輪掃描寫存活戳記（純本地、不發 Telegram）。下次啟動用它偵測斷層。
        liveness.stamp({"scanned": summary.scanned, "fires": summary.fires_enqueued})

    from l3_dispatcher.macro import (
        run_daily_macro_loop,
        run_hourly_pulse_loop,
        run_per_symbol_loop,
        run_performance_loop,
        run_position_tracker_loop,
    )
    from l3_dispatcher.trade_monitor import run_trade_monitor_loop
    from news_feed.truth_social_rss import run_truth_social_loop
    from news_feed.twitter_apify import run_twitter_apify_loop
    # v15: 互動 listener import
    from telegram_bot.callbacks import run_interactive_listener as _run_interactive
    # v16: 美股 pulse + 經濟日曆 import
    from l3_dispatcher.us_stocks import run_us_stock_pulse_loop as _run_us_stocks
    from news_feed.econ_calendar import run_econ_calendar_loop as _run_econ
    from l3_dispatcher.us_stock_signals import run_us_signal_loop as _run_us_sig
    from l3_dispatcher.market_scanner import run_market_scanner_loop as _run_scanner
    from news_feed.unlock_calendar import run_unlock_calendar_loop as _run_unlock
    # v20: Threads 建造日誌自動發布（無 token 時優雅待命）
    from threads_publisher import run_threads_publisher_loop as _run_threads
    # v22-4: 美股第一線快訊（TradingView 隱藏 API，零成本）
    from news_feed.us_news import run_us_news_loop as _run_us_news
    # v52: CoinGlass 加密新聞快訊（/api/article/list，$79 Startup，AI 過濾+繁中翻譯）
    from news_feed.coinglass_news import run_coinglass_news_loop as _run_cg_news
    # v24: 訊息稽核 Session（路由/重複/明確性自我檢測）
    from telegram_bot.message_auditor import run_audit_loop as _run_audit
    # v25: 事件脈絡敘事引擎（跨時窗事件聚類 + 因果鏈）
    from news_feed.narrative_engine import run_narrative_loop as _run_narrative
    # v31: 調參 Session（task #27，自動分析紙上帳產參數建議）
    from l3_dispatcher.auto_tuner import run_auto_tuner_loop as _run_tuner
    # v32: 回測 Session（每週真實歷史回放，驗證啟用策略期望值 → 系統主題 + 供 auto_tuner 參照）
    from backtest.backtest_session import run_backtest_loop as _run_backtest
    # v56: 覆盤/驗屍 Session（task #41-A）— 每 6h 掃新平倉逐筆驗屍 + 每週一彙整賠錢模式 →
    #      系統主題（頂部誠實橫幅）。100% 純讀（trade_journal/scanner/ohlc_cache 皆 mode=ro），
    #      無下單路徑、不碰 strength/eval_cvd；寫 postmortem_notes.jsonl + digest 於 data_dir()。
    from l3_dispatcher.postmortem import run_postmortem_loop as _run_postmortem
    # v35: 帳本防竄改錨定（每週 trade_journal 快照 → OpenTimestamps 錨定比特幣，純讀不下單）
    from l3_dispatcher.ledger_anchor import run_anchor_loop as _run_anchor
    # v40: CEO 監督 Session（每日彙整所有 Session 輸出 → 單一兩段式簡報，解決「埋在細節忘全局」）
    from l3_dispatcher.ceo_session import run_ceo_loop as _run_ceo
    # v54-3: #33 跨源匯流影子觀測（OKX∧Binance∧CoinGlass 存在度 + funding 共振；
    #        純觀測寫 convergence_shadow.jsonl，永不影響 strength/fire/下單）
    from l3_dispatcher.convergence_shadow import (
        run_convergence_shadow_loop as _run_convergence)
    # v56: 綜合宏觀指標合成影子層（task #41-C）— 每小時把 funding+OI+清算+巨鯨+ETF+DXY+breadth
    #      用確定性規則合成 macro_confluence_score（影子鐵則：永不乘/加進 strength、不進 fire/
    #      symbol_gate、不發 TG）。純讀，寫 macro_confluence.jsonl + 獨立 macro_history.db。
    from l3_dispatcher.macro_confluence import (
        run_macro_confluence_loop as _run_macro_confluence)
    # v55: OKX 模擬盤自動操盤手（task #4/#39）— 鏡像新紙上加密訊號到 OKX 模擬盤實單。
    #      預設「雙鑰待命」：DEMO_OPERATOR_ACTIVE 未開時整個 worker 完全閒置、零 OKX 互動
    #      （連 ex 都不建）→ 接進 daemon 本身是安全的；真要開單須另外把旗標設 1。
    #      模擬盤＝demo_guard 正向證明 x-simulated-trading=1，零真錢，不踩紅線①。
    from l3_dispatcher.demo_operator import run_demo_operator_loop as _run_demo_operator
    # v55: 監督員 Layer 1（task #40）— CEO 之上的純讀守望者：盤點進度寫 oversight_ledger.json，
    #      真停滯且過冷卻才發私人提醒。純讀，不下單、不改參數、不碰 daemon。
    from l3_dispatcher.ceo_oversight import run_oversight_loop as _run_oversight

    # v14: 每個 worker 用 supervise() 隔離 — 單一 worker 崩潰自動重啟，不再全滅
    # v14.1: run_on_startup 只在首次啟動為 True — supervise 崩潰重啟不重推開機報告
    _startup_once = {"performance": True, "daily_macro": True, "us_stocks": True}

    def _consume_startup(key: str) -> bool:
        val = _startup_once[key]
        _startup_once[key] = False
        return val

    workers = [
        ("scheduler", lambda: run_scheduler(watchlist, args.scan_interval, args.cooldown,
                                            summary_callback=on_scan_summary)),
        ("dispatcher", lambda: run_dispatcher(tg_trade, tg_positions)),
        # 三層時間架構 → 市場情報主題
        ("daily_macro", lambda: run_daily_macro_loop(tg_intel, source, watchlist,
                                                     target_hour_utc=args.daily_macro_hour_utc,
                                                     run_on_startup=_consume_startup("daily_macro"))),
        # v36: 群組精簡 12→6，pulse 併回 📊市場情報（與 Daily Macro/DeepDive/掃描/經濟同群）
        ("hourly_pulse", lambda: run_hourly_pulse_loop(router.client("intel"),
                                                       source, watchlist,
                                                       args.pulse_interval)),
        # v16: deepdive 是具體交易計畫（報單）→ 改推交易訊號主題（使用者明確要求）
        ("deepdive", lambda: run_per_symbol_loop(tg_trade, source, watchlist,
                                                 args.deepdive_interval,
                                                 max_symbols_per_run=args.deepdive_top_n)),
        # v15: 績效 / 監控 → 持倉與績效主題（FIRE 主題不再被高頻訊息淹沒）
        ("performance", lambda: run_performance_loop(tg_positions,
                                                     target_hour_utc=args.daily_macro_hour_utc,
                                                     run_on_startup=_consume_startup("performance"))),
        ("trade_monitor", lambda: run_trade_monitor_loop(tg_positions, source,
                                                         args.monitor_interval,
                                                         tg_alert=tg_trade,
                                                         tg_us=router.client("positions"))),  # v36: 美股持倉併入 📈持倉與績效
        ("position_tracker", lambda: run_position_tracker_loop(tg_positions, source,
                                                               args.position_tracker_interval)),
        # 新聞（已過濾 + 繁中翻譯）→ 新聞快訊主題
        ("truth_social", lambda: run_truth_social_loop(tg_news, args.truth_social_interval)),
        ("twitter_apify", lambda: run_twitter_apify_loop(
            tg_news, args.twitter_interval,
            tg_us=router.client("news"))),   # v36: 美股類帳號併入 📰新聞快訊
        ("watchlist_refresh", lambda: run_refresh_loop(watchlist, source, callback=on_refresh)),
        ("supervisor", lambda: run_supervisor_loop(tg_sys, source, sup_state,
                                                   args.supervisor_interval)),
        # v15: 互動 listener（FIRE 按鈕 + /status /stats 指令；唯一 getUpdates consumer）
        ("interactive", lambda: _run_interactive(tg)),
        # v16: 美股永續行情（開盤前瞻 13:25 UTC + 收盤總結 20:05 UTC）→ v36 併入 📰新聞快訊
        ("us_stocks", lambda: _run_us_stocks(router.client("news"),
                                             run_on_startup=_consume_startup("us_stocks"))),
        # v16: 經濟數據日曆 → v36 併入 📊市場情報
        ("econ_calendar", lambda: _run_econ(router.client("intel"))),
        # v17: 美股永續突破訊號（實驗性，僅紙上帳）→ v36 併入 🎯交易訊號（加密+美股 FIRE 同群）
        ("us_signals", lambda: _run_us_sig(router.client("trade"))),
        # v18-A: 全市場異常掃描器 → v36 併入 📊市場情報
        ("market_scanner", lambda: _run_scanner(router.client("intel"))),
        # v18-C: 代幣解鎖日曆 → v36 併入 📊市場情報
        ("unlock_calendar", lambda: _run_unlock(router.client("intel"))),
        # v20: Threads 自動發布（token 未設定時 no-op 待命）
        ("threads_publisher", lambda: _run_threads(tg_sys)),
        # v22-4: 美股快訊（DJ 終端 flash 優先，AI 過濾+繁中）→ v36 併入 📰新聞快訊
        ("us_news", lambda: _run_us_news(router.client("news"))),
        # v52: CoinGlass 加密快訊（AI 過濾+繁中）→ 📰新聞快訊；共用 source 限流器
        ("cg_news", lambda: _run_cg_news(router.client("news"), source)),
        # v24: 稽核 Session 報告（每小時彙整路由/重複/明確性警示 → 系統主題）
        ("auditor", lambda: _run_audit(tg_sys)),
        # v25: 敘事引擎（每日聚類事件因果脈絡 → 市場情報主題）
        ("narrative", lambda: _run_narrative(tg_intel)),
        # v31: 調參 Session（每日分析紙上帳 → 參數建議到系統主題，僅建議）
        ("auto_tuner", lambda: _run_tuner(tg_sys)),
        # v32: 回測 Session（每週歷史回放驗證期望值 → 系統主題，純讀不下單）
        ("backtest", lambda: _run_backtest(tg_sys)),
        # v56: 覆盤/驗屍 Session（每 6h 逐筆驗屍 + 每週一彙整賠錢模式 → 系統主題，純讀不下單）
        ("postmortem", lambda: _run_postmortem(tg_sys)),
        # v35: 帳本錨定 Session（每週快照 → OpenTimestamps 比特幣防竄改，只送 32B 雜湊）
        ("ledger_anchor", lambda: _run_anchor(tg_sys)),
        # v40: CEO 監督 Session（每日 09:00 台北彙整簡報 → 系統主題，純讀不下單不發外）
        ("ceo_session", lambda: _run_ceo(tg_sys)),
        # v54-3: #33 跨源匯流影子觀測（每 30 分一輪 → convergence_shadow.jsonl；
        #        純背景觀測，不發 Telegram、不影響任何訊號/下單）
        ("convergence_shadow", lambda: _run_convergence(source)),
        # v56: 綜合宏觀指標合成影子層（每小時一輪 → macro_confluence.jsonl + macro_history.db；
        #       純影子觀測，不發 Telegram、永不影響 strength/fire/symbol_gate/下單）
        ("macro_confluence", lambda: _run_macro_confluence(source)),
        # v55: OKX 模擬盤操盤手（DEMO_OPERATOR_ACTIVE 未開＝完全閒置、零 OKX 互動）。
        #      開啟後鏡像新紙上加密訊號到模擬盤實單，純驗證持倉真實性（零真錢）。
        ("okx_demo_operator", lambda: _run_demo_operator(
            interval_s=args.demo_operator_interval, tg=tg_sys)),
        # v55: 監督員 Layer 1（純讀守望 → oversight_ledger.json + 停滯時私人提醒）。
        ("ceo_oversight", lambda: _run_oversight(tg_sys, args.oversight_interval)),
    ]
    try:
        await asyncio.gather(*[
            supervise(name, factory, alert_tg=tg_sys) for name, factory in workers
        ])
    except (KeyboardInterrupt, asyncio.CancelledError):
        # v14.1: Ctrl+C 在 asyncio 內實際拋 CancelledError，原本只接 KeyboardInterrupt 是死碼
        print("\n[shutdown] interrupted")
    finally:
        liveness.stamp({"shutdown": True})  # v49: 乾淨關閉也補一筆戳記
        try:
            await tg_sys.send_message(render_shutdown(stats()), parse_mode="HTML")
        except Exception:
            pass
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--backend", default="coinglass",
                   choices=["mock", "coinglass", "local"])
    p.add_argument("--scan-interval", type=int, default=900,
                   help="交易層 FIRE 掃描間隔秒（預設 900=15min）")
    p.add_argument("--macro-interval", type=int, default=3600,
                   help="（已棄用，留為相容）舊宏觀推播間隔")
    p.add_argument("--daily-macro-hour-utc", type=int, default=0,
                   help="每日宏觀分析推送的 UTC 小時（預設 0 = 08:00 台北）")
    p.add_argument("--pulse-interval", type=int, default=3600,
                   help="每小時 pulse 即時動態間隔秒（預設 3600=1hr）")
    p.add_argument("--deepdive-interval", type=int, default=21600,
                   help="每幣 deep dive 交易計畫間隔秒（預設 21600=6hr）")
    p.add_argument("--deepdive-top-n", type=int, default=3,
                   help="每次 deep dive 分析交易層的前 N 個強勢幣（預設 3）")
    p.add_argument("--cooldown", type=int, default=14400,
                   help="per-(sym,setup,direction) 冷卻秒（v12: 1hr → 4hr 降低重複推播）")
    p.add_argument("--monitor-interval", type=int, default=900,
                   help="v12: trade_monitor 掃 open trades 的間隔秒（預設 900=15min）")
    p.add_argument("--position-tracker-interval", type=int, default=3600,
                   help="v12: 每小時持倉快照推送間隔秒")
    p.add_argument("--truth-social-interval", type=int, default=300,
                   help="v13: Trump Truth Social RSS 抓取間隔秒（預設 300=5min）")
    p.add_argument("--twitter-interval", type=int, default=1800,
                   help="v15.1: Apify Twitter 抓取間隔秒（預設 1800=30min；"
                        "配合 since_time 增量抓取，月成本從 $150+ 降到 <$1）")
    p.add_argument("--trading-size", type=int, default=0,   # 0=用 botconfig TRADING_SIZE
                   help="交易層大小（7-10 區間）")
    p.add_argument("--supervisor-interval", type=int, default=300,
                   help="健康監督檢查間隔秒（預設 300=5min）")
    p.add_argument("--demo-operator-interval", type=int, default=180,
                   help="v55: OKX 模擬盤操盤手巡檢間隔秒（預設 180=3min；"
                        "DEMO_OPERATOR_ACTIVE 未開時完全閒置）")
    p.add_argument("--oversight-interval", type=int, default=1800,
                   help="v55: 監督員 Layer 1 盤點間隔秒（預設 1800=30min）")
    p.add_argument("--once", action="store_true")
    p.add_argument("--no-startup-msg", action="store_true")
    return asyncio.run(amain(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
