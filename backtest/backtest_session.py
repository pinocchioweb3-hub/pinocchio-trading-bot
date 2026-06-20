"""🔬 回測 Session（v32，常駐 worker）— 每週用真實歷史驗證啟用中策略的期望值。

界線（使用者決定）：
    回測屬「紙上+回測+稽核全自動」範疇，純讀歷史、不下任何單。
    結果只用於 (1) 推系統主題給人看 (2) 存檔供 auto_tuner 參照，不自動改參數。

做什麼：
    - 取掃描器全市場流動性池前 N 檔（預設 12，控管 CoinGlass 速率）
    - 每檔拉一次真實歷史（OKX 1h candles + CoinGlass funding/OI/positioning）
    - 對每個「啟用中且有 config_factory」的策略跑 replay → simulate → aggregate
    - 跨 symbol 合併成單策略的真實期望值/勝率/PF/連虧
    - 寫 backtest_results.db（run 歷史 + 每策略最新），推系統主題報告

與 auto_tuner 的分工：
    auto_tuner 看「紙上實單帳」（真實觸發後的逐筆結果）；
    回測 Session 看「歷史回放」（同策略在過去 N 天的全市場表現）。
    兩者互補：紙上樣本不足時，回測提供期望值的歷史錨點。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from botpaths import db_path as _db_path

DB_PATH = _db_path("backtest_results.db")

DEFAULT_DAYS = 30          # OKX 1h × 300 ≈ 12.5 天上限，days 只當上限傳入
DEFAULT_SYMBOL_CAP = 12    # 每次回測檔數（控管 CoinGlass 速率：每檔 ~4 CG 呼叫）


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=8000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_schema() -> None:
    conn = _conn()
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS backtest_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_ts INTEGER NOT NULL,
                strategy TEXT NOT NULL,
                days INTEGER NOT NULL,
                n_symbols INTEGER NOT NULL,
                n_trades INTEGER NOT NULL,
                win_rate REAL,
                expectancy_r REAL,
                profit_factor REAL,
                max_consec_losses INTEGER,
                symbols_json TEXT
            )""")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_bt_strat_ts ON backtest_runs(strategy, run_ts DESC)")
        conn.commit()
    finally:
        conn.close()


def _liquid_symbols(cap: int = DEFAULT_SYMBOL_CAP) -> list[str]:
    """取掃描器全市場流動性池前 cap 檔（依量）。冷啟動 fallback 主流幣。"""
    try:
        from l3_dispatcher.watchlist import _market_candidates
        pool = _market_candidates(min_vol_usd=30_000_000, cap=cap)
        if pool:
            # 來源邊界（task#73-C parity）：濾掉 scanner.db 的代幣化美股/商品
            # （instCategory!=1）；fail-open：分類快取空→不過濾（is_crypto_base 回 True）
            try:
                from l3_dispatcher.market_scanner import is_crypto_base
                pool = [s for s in pool if is_crypto_base(s)]
            except Exception:
                pass
            if pool:
                return pool[:cap]
    except Exception:
        pass
    return ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX"][:cap]


def latest_backtest(setup: str) -> dict | None:
    """回某策略最近一次回測結果（給 auto_tuner / 系統狀態參照）。無資料回 None。"""
    try:
        _ensure_schema()
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT run_ts, days, n_symbols, n_trades, win_rate, expectancy_r, "
                "profit_factor, max_consec_losses FROM backtest_runs "
                "WHERE strategy=? ORDER BY run_ts DESC LIMIT 1", (setup,)).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if not row:
        return None
    return {"run_ts": row[0], "days": row[1], "n_symbols": row[2], "n_trades": row[3],
            "win_rate": row[4], "expectancy_r": row[5], "profit_factor": row[6],
            "max_consec_losses": row[7]}


async def run_backtest_once(days: int = DEFAULT_DAYS,
                            symbol_cap: int = DEFAULT_SYMBOL_CAP) -> dict:
    """跑一次完整回測：每檔拉一次歷史，對所有啟用策略 replay 合併。
    回 {strategy: metrics_dict}；同時寫入 DB。"""
    from l2_trigger.registry import scheduler_strategies
    from market_intel_mcp.sources.coinglass import CoinGlassSource
    from .real_historical import fetch_real_history, replay_real
    from .metrics import aggregate
    from .validation import assess   # v48: 逐筆 R 序列的統計顯著性（PSR/DSR/minTRL）

    _ensure_schema()
    strategies = scheduler_strategies()
    if not strategies:
        return {}

    symbols = _liquid_symbols(symbol_cap)
    cg = CoinGlassSource()
    # strategy.id -> {"meta": meta, "trades": [...], "symbols": set()}
    acc: dict[str, dict] = {
        m.id: {"meta": m, "trades": [], "symbols": set()} for m in strategies}

    try:
        for sym in symbols:
            try:
                history = await fetch_real_history(sym, days, cg)
            except Exception as e:
                print(f"[backtest] {sym} fetch error: {type(e).__name__}: {e}")
                continue
            if len(history) < 168:
                continue
            for m in strategies:
                if m.config_factory is None:
                    continue
                try:
                    cfg = m.config_factory(sym)
                    trades, _fires = await replay_real(history, cfg)
                    if trades:
                        acc[m.id]["trades"].extend(trades)
                        acc[m.id]["symbols"].add(sym)
                except Exception as e:
                    print(f"[backtest] {sym}/{m.id} replay error: {type(e).__name__}: {e}")
    finally:
        await cg.close()

    run_ts = int(time.time() * 1000)
    results: dict[str, dict] = {}
    conn = _conn()
    try:
        for sid, a in acc.items():
            metr = aggregate(a["trades"])
            syms = sorted(a["symbols"])
            # v48: 算逐筆 R 序列的統計顯著性（樣本<3 → None；render 端據此誠實標註）
            _rs = [t.realized_r for t in a["trades"]]
            _val = assess(_rs) if len(_rs) >= 3 else {}
            results[sid] = {
                "display": a["meta"].display_name_zh,
                "n_symbols": len(syms), "symbols": syms,
                "n_trades": metr.n_trades, "win_rate": metr.win_rate,
                "expectancy_r": metr.expectancy_r, "profit_factor": metr.profit_factor,
                "max_consec_losses": metr.max_consecutive_losses,
                "psr": _val.get("psr"), "dsr": _val.get("dsr"),
                "min_trl": _val.get("min_trl"),
            }
            conn.execute(
                "INSERT INTO backtest_runs(run_ts, strategy, days, n_symbols, n_trades, "
                "win_rate, expectancy_r, profit_factor, max_consec_losses, symbols_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (run_ts, sid, days, len(syms), metr.n_trades,
                 round(metr.win_rate, 4), round(metr.expectancy_r, 4),
                 round(metr.profit_factor, 4), metr.max_consecutive_losses,
                 json.dumps(syms)))
        conn.commit()
    finally:
        conn.close()
    return results


def render_backtest_report(results: dict, days: int) -> str | None:
    """把 run_backtest_once 的結果組成 HTML 報告。全 0 筆回 None。"""
    if not results or all(r["n_trades"] == 0 for r in results.values()):
        return None
    lines = ["🔬 <b>回測 Session 報告</b>（真實歷史回放，純讀不下單）",
             f"━━━━━━━━━━━━━━━━",
             f"<i>近 {days} 天全市場流動性池回放，驗證啟用中策略期望值</i>",
             # v48: 誠實輸入揭露橫幅（紅線③不誇大）— 簡化假設先講清楚，數字才不會被誤讀為實盤
             "⚠️ <i>此回測使用簡化輸入：固定 SL/TP 結構、未計入滑點與資金費、以歷史 1h K 線"
             "近似觸發；數字僅供期望值錨定，非實盤績效保證。</i>", ""]
    for sid, r in results.items():
        if r["n_trades"] == 0:
            lines.append(f"<b>{r['display']}</b>（<code>{sid}</code>）："
                         f"{r['n_symbols']} 檔回放，0 次觸發")
            continue
        wr = r["win_rate"] * 100
        exp = r["expectancy_r"]
        n = r["n_trades"]
        psr = r.get("psr")
        dsr = r.get("dsr")
        mtrl = r.get("min_trl")
        # v48: 措辭由「樣本數 + 統計顯著性」決定，不再用與樣本無關的「穩健」。
        #   n<30          → 樣本不足（任何方向都不下結論，只給歷史錨點）
        #   負期望        → 直接標負期望
        #   PSR≥95%       → 才敢稱統計顯著
        #   其餘          → 持平觀察（未達顯著，可能是運氣）
        if n < 30:
            verdict = "⚠️樣本不足（n&lt;30，僅供錨定）"  # &lt; 防 Telegram HTML 把 <30 當標籤
        elif exp < 0:
            verdict = "⚠️負期望"
        elif psr is not None and psr >= 0.95:
            verdict = "✅統計顯著（PSR≥95%）"
        else:
            verdict = "持平觀察（未達統計顯著）"
        lines.append(
            f"<b>{r['display']}</b>（<code>{sid}</code>）{verdict}\n"
            f"  {n} 筆／{r['n_symbols']} 檔｜勝率 {wr:.1f}%｜"
            f"期望值 <code>{exp:+.2f}R</code>｜PF {r['profit_factor']:.2f}｜"
            f"最大連虧 {r['max_consec_losses']}")
        # v48: 顯著性細節（算得到才顯示）；min_trl 指出「還差多少筆才能下結論」
        if psr is not None:
            need = ""
            if mtrl and n < mtrl:
                need = f"｜還需≥{int(mtrl)}筆達顯著"
            lines.append(f"  📊 PSR {psr * 100:.0f}%｜DSR {(dsr or 0) * 100:.0f}%{need}")
    lines.append("\n<i>用途：與紙上實單帳互相印證；樣本不足時提供歷史錨點。"
                 "不自動改參數，調整權保留給你（/settings 或 .env）。</i>")
    return "\n".join(lines)


async def run_backtest_loop(tg, interval_days: int = 7,
                            target_dow_utc: int = 0, target_hour_utc: int = 3,
                            days: int = DEFAULT_DAYS,
                            symbol_cap: int = DEFAULT_SYMBOL_CAP):
    """每週回測 session（預設週一 11:00 台北 = 03:00 UTC）。
    啟動後先睡 6 分鐘（避開開機高峰與 refresh），再對齊到下一個目標時點。"""
    print("[backtest] loop online（每週歷史回放驗證）")
    await asyncio.sleep(360)
    while True:
        now = dt.datetime.now(tz=dt.timezone.utc)
        # 算到下一個 target_dow 的 target_hour
        days_ahead = (target_dow_utc - now.weekday()) % 7
        nxt = now.replace(hour=target_hour_utc, minute=0, second=0, microsecond=0) \
                 + dt.timedelta(days=days_ahead)
        if nxt <= now:
            nxt += dt.timedelta(days=7)
        wait = (nxt - now).total_seconds()
        print(f"[backtest] next at {nxt.strftime('%Y-%m-%d %H:%M UTC')} (in {wait/3600:.1f}h)")
        await asyncio.sleep(wait)
        try:
            results = await run_backtest_once(days=days, symbol_cap=symbol_cap)
            rep = render_backtest_report(results, days)
            if rep and tg is not None:
                await tg.send_message(rep, parse_mode="HTML")
                print("[backtest] report sent")
            else:
                print("[backtest] 無足夠回放資料，略過推播")
        except Exception as e:
            print(f"[backtest] run error: {type(e).__name__}: {e}")


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    os.environ.setdefault("MARKET_INTEL_BACKEND", "coinglass")
    import sys
    d = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DAYS
    cap = int(sys.argv[2]) if len(sys.argv) > 2 else 4  # 手動測試只跑 4 檔
    res = asyncio.run(run_backtest_once(days=d, symbol_cap=cap))
    import re
    rep = render_backtest_report(res, d)
    print(re.sub(r"<[^>]+>", "", rep) if rep else "（無回放資料）")
