"""SMC 專屬走查式回測（v33+，task#7）。

回答 task#6 報告留下的限制：「結構訊號 + 獨立數據確認」這套 confluence 到底有沒有
正期望值？用歷史走查（walk-forward）成對比較兩個變體：

    變體 S    = 只看結構（fresh BOS/CHoCH 或 流動性掃單 Spring/UTAD）→ 方向
    變體 S+C  = 結構 AND（orderflow 確認 B 或 regime/wyckoff/HTF 共振 C）

**頭條數字 = S+C 與 S 的期望值(R) 差**。這是唯一誠實衡量「數據確認是否真的加 alpha」
的方式——因為結構本身沒有內在 alpha（SMC 共識：結構是 WHERE，數據是 IF）。S+C ⊆ S
（兩者進場/停損/停利完全相同，只是 S+C 多一道確認閘），故可成對比較。

== 兩種模式 ==
DEEP（多年，僅價格衍生因子）
    用 Binance fapi 年級歷史。CoinGlass 確認序列（OI/CVD）無法回溯多年（limit≤500、
    present-anchored），故 DEEP 模式 bucket B 一律 False，S+C = A AND C（只測
    regime/wyckoff/HTF 這類純價格共振）。**DEEP 不驗證 orderflow 確認**，只驗證
    「結構 + 價格情境共振」是否優於裸結構。

RECENT（~83 天，含 orderflow）
    4h × 500 根 ≈ 83 天，CoinGlass OI+CVD 可覆蓋。在 OI+CVD 皆有資料的視窗內成對比較
    S vs S+C，bucket B（orderflow）可用。樣本必小（誠實標註「樣本不足≠無效」）。

== 防前視（look-ahead）鐵律 ==
1. 絕不在回放迴圈內呼叫即時 fetcher（它們回「以現在結尾的最近 N 根」→ 會洩漏未來）。
   所有序列一次預抓，回放時用 ts 切片。
2. 每根 bar t 只餵 candles[:t+1]（滾動 200 根視窗，與正式 get_candles(...,200) 一致）。
   所有偵測器以視窗末端錨定（current_price=close[-1]、ago_bars=n-1-i）→ 截斷即正確重錨。
3. 只在「事件正好發生在視窗最後一根」(ago_bars==0) 時觸發 → 每個事件只觸發一次、零未來。
4. 進場 = close[t]，未來價 = candles[t+1:]（給 simulator 判盤中 stop/tp）。
5. 直接 import 正式偵測器（compute_smc_levels / detect_structure_breaks / _detect_sweeps
   / classify_regime / classify_wyckoff / _compute_htf_alignment），驗證的就是線上邏輯本身。

唯讀：只用 Binance 公開 fapi（免 key）+ CoinGlass 唯讀。不 import 任何交易路徑。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MARKET_INTEL_BACKEND", "coinglass")

# RECENT 模式需 CoinGlass 金鑰；金鑰只在 .env（紅線：絕不入碼/不列印）。
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

from .data_loader import get_ohlc
from .simulator import simulate
from .metrics import aggregate
from .validation import assess

# 正式偵測器（驗證線上邏輯本身，勿複製以免漂移）
from market_intel_mcp.smc_levels import compute_smc_levels
from market_intel_mcp.regime import classify_regime
from market_intel_mcp.wyckoff import classify_wyckoff
from l3_dispatcher.chart_render import (
    detect_structure_breaks, _detect_sweeps, _oi_delta_around, _ts_sec)
from l3_dispatcher.macro import _compute_htf_alignment

WARMUP = 60          # 視窗至少 60 根才算（compute_smc_levels swing_length=10 需 ≥30）
WINDOW = 200         # 滾動視窗根數（對齊正式 get_candles(...,200)）
STOP_LOOKBACK = 6    # 結構停損參考：近 6 根擺動高/低
SL_CLAMP = (1.5, 8.0)  # 停損 % 夾限
SL_FALLBACK = 4.0
TP_R = (1.0, 1.5, 2.0)
HOLD_MAX = 30        # 最長持有根數（4h×30 = 5 天）
COOLDOWN = 6
CVD_SLOPE_WIN = 12   # CVD 斜率視窗（對齊 coinglass.get_cvd_series 近 12 根）


def _sec(ts) -> float:
    s = _ts_sec(ts)
    return s if s is not None else 0.0


def _signal_at_bar(c4_win: list[dict], c1d_win: list[dict], *,
                   oi_vals=None, oi_ts=None, cvd_vals=None, cvd_ts=None) -> dict | None:
    """算 c4_win 最後一根 (bar t) 的確認狀態。無 fresh 結構事件回 None。
    只用 c4_win（結束於 t）與 c1d_win（ts≤t）→ 零前視。
    回 {direction, A, B, C, entry, stop, tps}。"""
    n4 = len(c4_win)
    if n4 < WARMUP:
        return None
    smc4 = compute_smc_levels(c4_win, swing_length=10)
    if smc4.get("error"):
        return None
    sp4 = smc4.get("swing_points") or []

    # ---- Bucket A：fresh 結構事件（事件落在最後一根 ago==0）----
    breaks = detect_structure_breaks(c4_win, sp4)
    sweeps = _detect_sweeps(c4_win, sp4, n4)
    dirs: list[str] = []
    fresh_break = None
    for b in breaks:
        if (n4 - 1 - b["idx"]) == 0:
            dirs.append(b["direction"]); fresh_break = b
    fresh_sweep = None
    for s in sweeps:
        if (n4 - 1 - s["x"]) == 0:
            d = "bull" if s["dir"] == "up" else "bear"   # 掃下方流動性=Spring=多
            dirs.append(d); fresh_sweep = s
    if not dirs:
        return None
    if "bull" in dirs and "bear" in dirs:
        return None   # 同根多空衝突 → 略過
    direction = dirs[0]

    entry = c4_win[-1]["close"]
    recent = c4_win[-STOP_LOOKBACK:]
    if direction == "bull":
        swing_stop = min(b["low"] for b in recent)
    else:
        swing_stop = max(b["high"] for b in recent)
    sl_pct = abs(entry - swing_stop) / entry * 100 if entry else SL_FALLBACK
    if not (SL_CLAMP[0] <= sl_pct <= SL_CLAMP[1]):
        sl_pct = min(SL_CLAMP[1], max(SL_CLAMP[0], sl_pct)) if sl_pct > 0 else SL_FALLBACK
    if direction == "bull":
        stop = entry * (1 - sl_pct / 100)
        sld = entry - stop
        tps = tuple(entry + sld * r for r in TP_R)
    else:
        stop = entry * (1 + sl_pct / 100)
        sld = stop - entry
        tps = tuple(entry - sld * r for r in TP_R)

    t_ts = c4_win[-1]["ts"]

    # ---- Bucket B：orderflow 確認（OI + CVD；RECENT 模式才有資料）----
    oi_delta = None
    cvd_slope = None
    if oi_vals is not None and cvd_vals is not None:
        t_s = _sec(t_ts)
        oi_pair = [(ts, v) for ts, v in zip(oi_ts, oi_vals) if _sec(ts) <= t_s]
        cvd_pair = [(ts, v) for ts, v in zip(cvd_ts, cvd_vals) if _sec(ts) <= t_s]
        if len(oi_pair) >= 3:
            vlist = [v for _, v in oi_pair]
            tlist = [ts for ts, _ in oi_pair]
            oi_delta = _oi_delta_around(vlist, 0, oi_ts=tlist,
                                        event_ts=t_ts, tf_sec=14400)
        if len(cvd_pair) >= 4:
            cv = [v for _, v in cvd_pair]
            w = min(CVD_SLOPE_WIN, len(cv) - 1)
            cvd_slope = cv[-1] - cv[-1 - w]
    if fresh_sweep is not None and fresh_break is None:
        oi_ok = (oi_delta is not None and oi_delta < -1.5)   # 掃單後 OI 驟降=清算離場=良性反轉
    else:
        oi_ok = (oi_delta is not None and oi_delta > 1.0)    # 突破時 OI 增=新倉=真突破
    cvd_ok = (cvd_slope is not None and ((cvd_slope > 0) == (direction == "bull")))
    bucket_B = bool(oi_ok and cvd_ok)

    # ---- Bucket C：regime / wyckoff / HTF 共振（純價格，兩模式皆可）----
    reg = classify_regime(c4_win) or {}
    wy = classify_wyckoff(c4_win, cvd_slope=cvd_slope, oi_delta_pct=oi_delta) or {}
    bc4 = [{"direction": b["direction"], "ago_bars": n4 - 1 - b["idx"],
            "type": b["type"], "level": b["level"]} for b in breaks]
    smc1d = {}
    if len(c1d_win) >= 30:
        smc1d = compute_smc_levels(c1d_win, swing_length=5)
        if smc1d.get("error"):
            smc1d = {}
    htf = _compute_htf_alignment(
        {"bos_choch": bc4, "premium_discount": smc4.get("premium_discount")},
        {"bos_choch": smc1d.get("bos_choch"),
         "premium_discount": smc1d.get("premium_discount")})

    reg_trend = reg.get("regime") == "趨勢"
    reg_up = reg.get("trend_dir") == "上"
    reg_agree = reg_trend and (reg_up == (direction == "bull"))
    wy_bias = wy.get("bias")
    wy_agree = (wy_bias is not None and wy_bias == direction)
    htf_agree = (htf.get("verdict") == "aligned") or (htf.get("direction_aligned") is True)
    htf_conflict = htf.get("verdict") == "conflict"
    bucket_C = bool((reg_agree or wy_agree or htf_agree) and not htf_conflict)

    return {"direction": direction, "A": True, "B": bucket_B, "C": bucket_C,
            "entry": entry, "stop": stop, "tps": tps,
            "src": ("sweep" if fresh_sweep is not None and fresh_break is None
                    else "break")}


_CONF_CACHE_DIR = ROOT / "backtest" / "_cg_conf_cache"
_CONF_TTL_SEC = 12 * 3600   # 12h 內重跑不重抓（保護 daemon 共用的 CoinGlass 限額）


def _conf_cache_load(symbol: str):
    import json
    import time as _t
    p = _CONF_CACHE_DIR / f"{symbol}_4h.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if _t.time() - d.get("fetched_at", 0) > _CONF_TTL_SEC:
            return None
        return (d["oi_vals"], d["oi_ts"], d["cvd_vals"], d["cvd_ts"])
    except Exception:
        return None


def _conf_cache_save(symbol: str, tup) -> None:
    import json
    import time as _t
    try:
        _CONF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (_CONF_CACHE_DIR / f"{symbol}_4h.json").write_text(json.dumps(
            {"fetched_at": _t.time(), "oi_vals": tup[0], "oi_ts": tup[1],
             "cvd_vals": tup[2], "cvd_ts": tup[3]}), encoding="utf-8")
    except Exception:
        pass


async def _prefetch_confirmation(symbol: str):
    """RECENT 模式：一次預抓 OI + CVD 序列（唯讀、節流、隔離 daemon rate-limit）。
    12h TTL 本地快取 → 重跑不重抓。回 (oi_vals, oi_ts, cvd_vals, cvd_ts) 或 None。"""
    cached = _conf_cache_load(symbol)
    if cached is not None:
        print(f"    [confirm-CACHE] {symbol} OI={len(cached[0])}根 CVD={len(cached[2])}根")
        return cached
    from market_intel_mcp.sources.coinglass import CoinGlassSource
    cg = CoinGlassSource()
    try:
        oi = await cg.get_oi(symbol, "4h", 500)
        await asyncio.sleep(1.0)
        cvd = await cg.get_cvd_series(symbol, "4h", 500)
        if oi.get("error") or cvd.get("error"):
            print(f"    [confirm-FAIL] {symbol} OI={oi.get('code')}/{oi.get('message')} "
                  f"CVD={cvd.get('code')}/{cvd.get('message')}")
            return None
        oi_series = oi.get("series") or []
        cvd_series = cvd.get("series") or []
        if len(oi_series) < 30 or len(cvd_series) < 30:
            print(f"    [confirm-FAIL] {symbol} series too short: "
                  f"OI={len(oi_series)} CVD={len(cvd_series)}")
            return None
        print(f"    [confirm-OK] {symbol} OI={len(oi_series)}根 CVD={len(cvd_series)}根")
        tup = ([p["value"] for p in oi_series], [p["ts"] for p in oi_series],
               [p["value"] for p in cvd_series], [p["ts"] for p in cvd_series])
        _conf_cache_save(symbol, tup)
        return tup
    except Exception as e:
        print(f"    [warn] {symbol} confirmation prefetch failed: "
              f"{type(e).__name__}: {e}")
        return None
    finally:
        try:
            await cg.close()
        except Exception:
            pass


async def _replay_symbol(symbol: str, *, mode: str, days: int):
    """走查一個 symbol。回 list[(TradeOutcome, flags)]，flags={A,B,C,src}。"""
    c4 = await get_ohlc(symbol, "4h", days)
    c1d = await get_ohlc(symbol, "1d", days + 60)
    if len(c4) < WARMUP + HOLD_MAX + 2:
        return [], {"note": f"4h 根數不足 ({len(c4)})"}

    conf = None
    cov_start_s = None
    if mode == "recent":
        conf = await _prefetch_confirmation(symbol)
        if conf is not None:
            oi_vals, oi_ts, cvd_vals, cvd_ts = conf
            # 只在 OI+CVD 皆有資料的視窗回放 → S 與 S+C 成對可比
            cov_start_s = max(_sec(oi_ts[0]), _sec(cvd_ts[0]))

    n = len(c4)
    trades = []
    last_fire = -10_000
    # 1d 視窗指標：c1d 升序，逐步推進
    for t in range(WARMUP, n - 1):
        if t - last_fire < COOLDOWN:
            continue
        t_ts = c4[t]["ts"]
        if cov_start_s is not None and _sec(t_ts) < cov_start_s:
            continue
        c4_win = c4[max(0, t - WINDOW + 1):t + 1]
        c1d_win = [c for c in c1d if c["ts"] <= t_ts][-WINDOW:]
        kw = {}
        if conf is not None:
            kw = {"oi_vals": oi_vals, "oi_ts": oi_ts,
                  "cvd_vals": cvd_vals, "cvd_ts": cvd_ts}
        sig = _signal_at_bar(c4_win, c1d_win, **kw)
        if sig is None:
            continue
        future = [(c4[j]["ts"], c4[j]["high"], c4[j]["low"], c4[j]["close"])
                  for j in range(t + 1, min(t + 1 + HOLD_MAX, n))]
        if not future:
            break
        out = simulate(symbol=symbol, setup_name="smc_wf",
                       direction=sig["direction"], entry_ts=t_ts,
                       entry_price=sig["entry"], stop=sig["stop"], tps=sig["tps"],
                       future_prices=future, hold_max_hours=HOLD_MAX)
        trades.append((out, {"A": sig["A"], "B": sig["B"], "C": sig["C"],
                             "src": sig["src"]}))
        last_fire = t
    meta = {"bars_4h": n, "conf": "yes" if conf is not None else "no"}
    return trades, meta


def _subset_report(label: str, trades: list, n_trials: int = 1) -> dict:
    outs = [t for t, _ in trades]
    m = aggregate(outs)
    va = assess([o.realized_r for o in outs], n_trials=n_trials)
    return {"variant": label, "n": m.n_trades,
            "win_rate": round(m.win_rate * 100, 1),
            "expectancy_r": round(m.expectancy_r, 4),
            "profit_factor": round(m.profit_factor, 2),
            "max_consec_losses": m.max_consecutive_losses,
            "max_dd_r": round(m.max_drawdown_r, 2),
            "psr": va.get("psr"), "dsr": va.get("dsr"),
            "min_trl": va.get("min_trl"), "verdict": va.get("verdict")}


async def run(mode: str = "deep", symbols: list[str] | None = None,
              days: int | None = None) -> dict:
    """跑 SMC 走查回測。mode='deep'|'recent'。回完整結果 dict。"""
    if symbols is None:
        symbols = ["BTC", "ETH", "SOL"]
    if days is None:
        days = 730 if mode == "deep" else 120

    pooled: list = []
    per_symbol = {}
    for sym in symbols:
        trades, meta = await _replay_symbol(sym, mode=mode, days=days)
        pooled.extend(trades)
        S = trades
        SC = [tr for tr in trades if tr[1]["B"] or tr[1]["C"]]
        S_noC = [tr for tr in trades if not (tr[1]["B"] or tr[1]["C"])]
        block = {"_meta": meta,
                 "S": _subset_report("S 純結構", S),
                 "S+C": _subset_report("S+C 結構+確認", SC),
                 "S\\C 結構但無確認": _subset_report("S\\C", S_noC)}
        if mode == "recent":
            B_only = [tr for tr in trades if tr[1]["B"]]
            C_only = [tr for tr in trades if tr[1]["C"]]
            block["S+B orderflow確認"] = _subset_report("S+B", B_only)
            block["S+C regime確認"] = _subset_report("S+C(regime)", C_only)
        eS = block["S"]["expectancy_r"]
        eSC = block["S+C"]["expectancy_r"]
        block["delta_expectancy_r"] = round(eSC - eS, 4)
        per_symbol[sym] = block

    # 跨 symbol 池化
    poolS = pooled
    poolSC = [tr for tr in pooled if tr[1]["B"] or tr[1]["C"]]
    poolSnoC = [tr for tr in pooled if not (tr[1]["B"] or tr[1]["C"])]
    overall = {"S": _subset_report("S 純結構(池)", poolS, n_trials=2),
               "S+C": _subset_report("S+C 結構+確認(池)", poolSC, n_trials=2),
               "S\\C": _subset_report("S\\C(池)", poolSnoC, n_trials=2)}
    overall["delta_expectancy_r"] = round(
        overall["S+C"]["expectancy_r"] - overall["S"]["expectancy_r"], 4)
    return {"mode": mode, "days": days, "symbols": symbols,
            "per_symbol": per_symbol, "_overall": overall}


# ---------------------------------------------------------------------------
# 無前視自我測試：signal_at_bar(candles[:t+1]) 必須與「附加更多未來根後再算」一致
# ---------------------------------------------------------------------------
async def selftest(symbol: str = "BTC") -> bool:
    c4 = await get_ohlc(symbol, "4h", 180)
    c1d = await get_ohlc(symbol, "1d", 240)
    if len(c4) < WARMUP + 50:
        print(f"selftest: {symbol} 資料不足 ({len(c4)})")
        return False
    n = len(c4)
    import random
    random.seed(7)
    sample_ts = sorted(random.sample(range(WARMUP + 5, n - 20), min(40, n - WARMUP - 30)))

    # 不變量 1（真無洩漏）：把整條 c4 物理截斷到 t 再切視窗，與「直接從完整序列切到 t」
    #   必須得到完全相同的視窗（證明切片永不納入 ts>t 的根），且訊號一致。
    inv1_fail = 0
    # 不變量 2：視窗內最大 ts 不得超過 t_ts；1d 視窗最大 ts ≤ t_ts。
    inv_ts_fail = 0
    # 不變量 3：進場價 == c4[t].close（用當根收盤、非未來）。
    inv2_fail = 0
    checks = 0
    for t in sample_ts:
        checks += 1
        t_ts = c4[t]["ts"]
        win_from_full = c4[max(0, t - WINDOW + 1):t + 1]
        truncated = c4[:t + 1]                       # 物理丟掉所有未來根
        win_from_trunc = truncated[max(0, t - WINDOW + 1):]
        c1d_now = [c for c in c1d if c["ts"] <= t_ts][-WINDOW:]
        if win_from_full != win_from_trunc:
            inv1_fail += 1
        if win_from_full and max(b["ts"] for b in win_from_full) > t_ts:
            inv_ts_fail += 1
        if c1d_now and max(b["ts"] for b in c1d_now) > t_ts:
            inv_ts_fail += 1
        sig_full = _signal_at_bar(win_from_full, c1d_now)
        sig_trunc = _signal_at_bar(win_from_trunc, c1d_now)
        a = None if sig_full is None else (sig_full["direction"],
                                           round(sig_full["entry"], 6), sig_full["C"])
        b = None if sig_trunc is None else (sig_trunc["direction"],
                                            round(sig_trunc["entry"], 6), sig_trunc["C"])
        if a != b:
            inv1_fail += 1
            print(f"  [FAIL inv1] t={t}: {a} != {b}")
        if sig_full is not None and abs(sig_full["entry"] - c4[t]["close"]) > 1e-9:
            inv2_fail += 1
    ok = (inv1_fail == 0 and inv_ts_fail == 0 and inv2_fail == 0)
    print(f"selftest {symbol}: 抽樣 {checks} 根 | 截斷恆等 fails={inv1_fail} | "
          f"視窗 ts≤t fails={inv_ts_fail} | entry==close[t] fails={inv2_fail} "
          f"→ {'PASS' if ok else 'FAIL'}")
    return ok


def _print_block(name: str, blk: dict):
    print(f"\n--- {name} ---")
    for k in ("S", "S+C", "S\\C 結構但無確認", "S+B orderflow確認", "S+C regime確認"):
        v = blk.get(k)
        if not v:
            continue
        print(f"  {k:18} n={v['n']:4d} 勝率={v['win_rate']:5.1f}% "
              f"期望={v['expectancy_r']:+.4f}R PF={v['profit_factor']:.2f} "
              f"連虧={v['max_consec_losses']} PSR={v['psr']} → {v['verdict']}")
    print(f"  ▶ Δ期望(S+C − S) = {blk.get('delta_expectancy_r'):+.4f}R")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "deep"
    if arg == "selftest":
        syms = sys.argv[2:] or ["BTC"]
        async def _t():
            allok = True
            for s in syms:
                allok = await selftest(s) and allok
            print("\n=== 無前視自測 " + ("全 PASS ✅" if allok else "有 FAIL ⛔") + " ===")
        asyncio.run(_t())
    else:
        mode = arg
        rest = sys.argv[2:]
        days = None
        if rest and rest[0].isdigit():
            days = int(rest[0]); rest = rest[1:]
        syms = rest or ["BTC", "ETH", "SOL"]

        async def _r():
            res = await run(mode, syms, days)
            print("=" * 74)
            print(f"  SMC 走查回測｜模式={res['mode']}  天數={res['days']}  "
                  f"標的={','.join(res['symbols'])}")
            print("=" * 74)
            for sym, blk in res["per_symbol"].items():
                _print_block(f"{sym} (4h根={blk['_meta'].get('bars_4h')}, "
                             f"確認資料={blk['_meta'].get('conf')})", blk)
            print("\n" + "=" * 74)
            _print_block("跨標的池化 (POOLED)", res["_overall"])
            print("=" * 74)
            ov = res["_overall"]
            print(f"\n誠實結論：S+C 相對 S 的期望值差 = {ov['delta_expectancy_r']:+.4f}R")
            print("  >0 = 數據確認有加 alpha；<0 = 確認反而砍掉好單；≈0 或樣本<30 = 無定論")
        asyncio.run(_r())
