"""task#20 回測閘：Binance 自算 taker CVD vs CoinGlass 聚合 CVD 忠實度驗證（離線、唯讀）。

背景（紅線判定）：strength.py 的 cvd_slope_7d（20% 權重）與 signals.py 的 cvd_slope
（CVD 背離票 + 靜默吸籌票）目前唯一來源＝CoinGlass
/api/futures/aggregated-taker-buy-sell-volume/history；universe 排名路徑更糟＝
coinglass.py:666 回填常數 0.0 stub（z-score 恆 0、對相對排名零貢獻＝裝飾欄）。
Option A＝用 Binance 公開 klines（免 API key）自算 CVD 當備援 / 補 stub。

但 cvd_slope 餵訊號數學 → 改來源＝改訊號 → **必須先過回測閘**。本工具＝那道閘的「證據層」：
- 不接任何 live 路徑、不改任何 daemon 模組（純新增離線 CLI）。
- 不下任何單（紅線①）、不發任何對外訊息（紅線②）、不捏造（紅線③：誠實判定、預先登錄門檻）。
- 只比對「Binance 自算」對「CoinGlass 聚合」的：①每根淨主動買賣 delta 的時序相關性（黃金標準）
  ②實際餵進系統的純量 cvd_slope / cvd_slope_7d 的同號率與量級比。

數學同構（這是 Option A 可行的根基）：
  CoinGlass 每根 delta_pct = (buy_usd − sell_usd)/(buy_usd + sell_usd) * 100。
  Binance klines 每根：quoteVol = row[7]、takerBuyQuote = row[10]
   → takerSellQuote = quoteVol − takerBuyQuote
   → delta = takerBuyQuote − takerSellQuote = 2*takerBuyQuote − quoteVol、total = quoteVol
   → delta_pct 同式。cvd_slope = 最後 12 根均值、cvd_slope_7d = 最後 168 根均值（與 coinglass 一致）。

用法：
  python -m backtest.binance_cvd_validate                 # 用預設流動性樣本
  python -m backtest.binance_cvd_validate BTC ETH SOL ... # 指定樣本
  python -m backtest.binance_cvd_validate --selftest      # 純函式單元自測（離線、零網路）
"""
from __future__ import annotations

import asyncio
import sys
from statistics import mean, median

# ── 預設流動性樣本（夠多 symbol 讓相關性/同號率有統計意義；皆 Binance USDⓈ-M 永續）──
DEFAULT_SYMBOLS = [
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE",
    "ADA", "AVAX", "LINK", "SUI", "LTC", "DOT",
]

# 預先登錄（pre-registered）判定門檻 — 跑之前就定，避免事後挑門檻（紅線③）
PASS_PERBAR_R = 0.70        # 每根 delta 時序相關性中位數 ≥ 0.70 → 時序忠實
PASS_SIGN_AGREE = 0.70      # 純量 cvd_slope_7d 同號率 ≥ 0.70 → 方向忠實
MIN_OK_FOR_VERDICT = 3      # 成功比對 < 此數 → 判定資料不足（不得render忠實/不忠實，紅線③）
_EPS = 1e-9


# ════════════════════════════════════════════════════════════════════════
#  純函式：由 Binance 原始 klines 列推導 CVD（與 coinglass.get_cvd_series 同構）
# ════════════════════════════════════════════════════════════════════════
def cvd_slopes_from_klines(rows, n_short: int = 12, n_long: int = 168) -> dict:
    """rows = Binance /fapi/v1/klines 原始列。

    每列：[openTime, o, h, l, c, volume(base), closeTime, quoteAssetVolume,
           nTrades, takerBuyBaseVol, takerBuyQuoteVol, ignore]
    回傳形狀刻意對齊 coinglass.get_cvd_series：{cvd, cvd_slope, cvd_slope_7d, series, deltas}。
    deltas = [(ts, delta_usd)] 每根淨主動買賣量（給時序相關性用）。
    """
    cumsum = 0.0
    series: list[dict] = []
    delta_pcts: list[float] = []
    deltas: list[tuple[int, float]] = []
    for row in rows:
        try:
            ts = int(row[0])
            quote_vol = float(row[7])          # 總成交額（USD/quote）
            taker_buy_quote = float(row[10])   # 主動買進額（USD/quote）
        except (TypeError, ValueError, IndexError):
            continue
        taker_sell_quote = quote_vol - taker_buy_quote
        delta = taker_buy_quote - taker_sell_quote   # = 2*taker_buy_quote − quote_vol
        total = quote_vol
        cumsum += delta
        series.append({"ts": ts, "value": cumsum})
        deltas.append((ts, delta))
        if total > 0:
            delta_pcts.append(delta / total * 100)

    n_recent = min(n_short, len(delta_pcts))
    cvd_slope = (sum(delta_pcts[-n_recent:]) / n_recent) if n_recent > 0 else 0.0
    n_7d = min(n_long, len(delta_pcts))
    cvd_slope_7d = (sum(delta_pcts[-n_7d:]) / n_7d) if n_7d > 0 else 0.0
    return {
        "cvd": round(series[-1]["value"], 2) if series else 0.0,
        "cvd_slope": round(cvd_slope, 4),
        "cvd_slope_7d": round(cvd_slope_7d, 4),
        "series": series,
        "deltas": deltas,
    }


def _norm_ts_to_hour(ts: int | float) -> int:
    """把 ts（ms 或 s）正規化成「小時桶」整數，供兩源對齊（避免 ms/s 單位混淆）。"""
    t = float(ts)
    if t > 1e12:       # 毫秒
        t /= 1000.0
    return int(t // 3600)


def _deltas_from_cumsum(series: list[dict]) -> list[tuple[int, float]]:
    """CoinGlass get_cvd_series 只回 cumsum series → 差分還原每根 delta（USD）。"""
    out: list[tuple[int, float]] = []
    prev = 0.0
    for i, pt in enumerate(series):
        try:
            v = float(pt["value"])
            ts = int(pt["ts"])
        except (TypeError, ValueError, KeyError):
            continue
        out.append((ts, v - prev if i > 0 else v))
        prev = v
    return out


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson r，純函式；n<2 或任一序列零變異 → None。"""
    n = len(xs)
    if n < 2 or len(ys) != n:
        return None
    mx, my = mean(xs), mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= _EPS or syy <= _EPS:
        return None
    sxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    return sxy / (sxx ** 0.5 * syy ** 0.5)


def _perbar_correlation(bn_deltas, cg_deltas) -> tuple[float | None, int]:
    """把兩源每根 delta 依小時桶對齊後算 Pearson r。回 (r, n_aligned)。"""
    bn_map = {_norm_ts_to_hour(ts): d for ts, d in bn_deltas}
    cg_map = {_norm_ts_to_hour(ts): d for ts, d in cg_deltas}
    keys = sorted(set(bn_map) & set(cg_map))
    if len(keys) < 2:
        return (None, len(keys))
    xs = [bn_map[k] for k in keys]
    ys = [cg_map[k] for k in keys]
    return (_pearson(xs, ys), len(keys))


def _sign(x: float) -> int:
    if x > _EPS:
        return 1
    if x < -_EPS:
        return -1
    return 0


# ════════════════════════════════════════════════════════════════════════
#  Live 取數（唯讀；Binance 免 key、CoinGlass key 留在 .env 不外洩）
# ════════════════════════════════════════════════════════════════════════
async def _binance_cvd(bn, symbol: str, interval: str = "1h", limit: int = 168) -> dict | None:
    """直接取 Binance 原始 klines（含 row[10] taker buy quote，get_candles 會丟此欄）。"""
    sym = bn._sym(symbol)
    body = await bn._get("/fapi/v1/klines",
                         {"symbol": sym, "interval": interval,
                          "limit": min(max(limit, 1), 1500)},
                         symbol, "cvd_validate")
    if isinstance(body, dict) and body.get("error"):
        return None
    if not isinstance(body, list) or not body:
        return None
    return cvd_slopes_from_klines(body)


async def _coinglass_cvd(cg, symbol: str, interval: str = "1h", limit: int = 168) -> dict | None:
    res = await cg.get_cvd_series(symbol, interval, limit)
    if not isinstance(res, dict) or res.get("error"):
        return None
    return res


async def _eval_symbol(bn, cg, symbol: str) -> dict | None:
    b, c = await asyncio.gather(_binance_cvd(bn, symbol), _coinglass_cvd(cg, symbol))
    if b is None or c is None:
        return {"symbol": symbol, "ok": False,
                "reason": f"fetch failed (bn={'ok' if b else 'X'}/cg={'ok' if c else 'X'})"}
    r, n_al = _perbar_correlation(b["deltas"], _deltas_from_cumsum(c.get("series", [])))
    return {
        "symbol": symbol, "ok": True,
        "bn_slope": b["cvd_slope"], "cg_slope": c.get("cvd_slope"),
        "bn_slope7d": b["cvd_slope_7d"], "cg_slope7d": c.get("cvd_slope_7d"),
        "perbar_r": r, "n_aligned": n_al,
    }


async def main(symbols: list[str]) -> int:
    # 延後 import：純函式自測路徑不需要這些 daemon 來源
    from pathlib import Path
    from dotenv import load_dotenv

    # ⚠️ 必須在 import 任何 market_intel_mcp 模組之前載入 .env：
    # settings.py 的 SETTINGS = Settings.load() 在 import 當下就凍結讀取 COINGLASS_API_KEY，
    # 晚於此 import 才 load_dotenv 會拿到空金鑰（daemon 由 run_bot.py 先 load_dotenv 才 import）。
    # 金鑰只從 gitignored .env 進環境變數，永不入聊天室/不印出值。
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    from market_intel_mcp.sources.binance_perp import BinancePerpSource
    from market_intel_mcp.sources.coinglass import CoinGlassSource

    bn, cg = BinancePerpSource(), CoinGlassSource()
    print(f"task#20 回測閘：Binance 自算 CVD vs CoinGlass 聚合 CVD（n={len(symbols)} symbols，1h×168 根）")
    print(f"預先登錄門檻：每根 delta 時序 r 中位數 ≥ {PASS_PERBAR_R}、cvd_slope_7d 同號率 ≥ {PASS_SIGN_AGREE}\n")
    try:
        rows = []
        for sym in symbols:   # 序列化，尊重兩源各自的限速 semaphore
            rows.append(await _eval_symbol(bn, cg, sym))
    finally:
        await bn.close()
        await cg.close()

    ok_rows = [r for r in rows if r and r.get("ok")]
    print(f"{'幣':<6}{'bn_7d':>10}{'cg_7d':>10}{'同號':>6}{'每根r':>9}{'對齊根':>8}")
    print("-" * 49)
    for r in rows:
        if not r.get("ok"):
            print(f"{r['symbol']:<6}  ⚠️ {r.get('reason','')}")
            continue
        s7_bn, s7_cg = r["bn_slope7d"], r["cg_slope7d"]
        same = "✓" if (s7_cg is not None and _sign(s7_bn) == _sign(s7_cg) and _sign(s7_bn) != 0) else \
               ("0" if (s7_cg is not None and (_sign(s7_bn) == 0 or _sign(s7_cg) == 0)) else "✗")
        rr = f"{r['perbar_r']:.3f}" if r["perbar_r"] is not None else "  n/a"
        cg7 = f"{s7_cg:.4f}" if s7_cg is not None else "n/a"
        print(f"{r['symbol']:<6}{s7_bn:>10.4f}{cg7:>10}{same:>6}{rr:>9}{r['n_aligned']:>8}")

    # ── 彙總判定 ──
    rs = [r["perbar_r"] for r in ok_rows if r["perbar_r"] is not None]
    # 同號率：兩邊皆非零且同號 / 兩邊皆非零的樣本
    nonzero = [r for r in ok_rows if r["cg_slope7d"] is not None
               and _sign(r["bn_slope7d"]) != 0 and _sign(r["cg_slope7d"]) != 0]
    sign_agree = (sum(1 for r in nonzero
                      if _sign(r["bn_slope7d"]) == _sign(r["cg_slope7d"])) / len(nonzero)) \
        if nonzero else None
    # 純量 cvd_slope_7d 跨幣截面相關（noisy，僅輔證）
    xsec = _pearson([r["bn_slope7d"] for r in nonzero],
                    [r["cg_slope7d"] for r in nonzero]) if len(nonzero) >= 2 else None
    # 量級比 |bn|/|cg|：>1 = Binance 單一場所比三所聚合「更燙」（誠實揭露，紅線③）
    mag_ratios = [abs(r["bn_slope7d"]) / abs(r["cg_slope7d"]) for r in nonzero
                  if abs(r["cg_slope7d"]) > _EPS]

    print("\n── 彙總 ──")
    print(f"成功比對：{len(ok_rows)}/{len(symbols)} 幣")
    if rs:
        print(f"每根 delta 時序相關性 r：中位 {median(rs):.3f}　平均 {mean(rs):.3f}　"
              f"（r≥0.7 的幣：{sum(1 for r in rs if r >= PASS_PERBAR_R)}/{len(rs)}）")
    else:
        print("每根 delta 時序相關性：無足夠對齊樣本")
    if sign_agree is not None:
        print(f"cvd_slope_7d 同號率：{sign_agree:.0%}（{len(nonzero)} 幣皆非零）")
    if xsec is not None:
        print(f"cvd_slope_7d 跨幣截面相關（輔證，noisy）：{xsec:.3f}")
    if mag_ratios:
        print(f"量級比 |bn|/|cg|：中位 {median(mag_ratios):.2f}　"
              f"（>1＝單一場所較燙；z-score/同號型消費者對此免疫，絕對門檻型須留意）")

    # 預先登錄門檻判定
    med_r = median(rs) if rs else None
    pass_r = med_r is not None and med_r >= PASS_PERBAR_R
    pass_sign = sign_agree is not None and sign_agree >= PASS_SIGN_AGREE

    # 資料充足性閘：成功比對太少 / 兩指標皆算不出來 → 判定資料不足，不得 render 忠實/不忠實（紅線③）
    data_sufficient = (len(ok_rows) >= MIN_OK_FOR_VERDICT
                       and med_r is not None and sign_agree is not None)
    if not data_sufficient:
        print("\n── 判定 ──")
        print(f"⚪ 結論＝資料不足（成功比對 {len(ok_rows)}/{len(symbols)} 幣、"
              f"門檻需 ≥{MIN_OK_FOR_VERDICT}）→ 無法判定忠實度。")
        if not ok_rows:
            bad = next((r for r in rows if not r.get("ok")), None)
            print(f"   主因＝取數失敗（例：{bad.get('reason','?') if bad else '?'}）。"
                  "請先排除取數問題（如 CoinGlass 金鑰/端點權益）再重跑，"
                  "切勿把『比對源不可用』誤判為『代理不忠實』（紅線③）。")
        return 0

    print("\n── 判定（預先登錄門檻）──")
    print(f"  時序 r 中位 ≥ {PASS_PERBAR_R}：{'✅ PASS' if pass_r else '❌'}"
          f"（{med_r:.3f}）" if med_r is not None else "  時序 r：資料不足")
    print(f"  同號率 ≥ {PASS_SIGN_AGREE}：{'✅ PASS' if pass_sign else '❌'}"
          f"（{sign_agree:.0%}）" if sign_agree is not None else "  同號率：資料不足")
    if pass_r and pass_sign:
        print("\n🟢 結論＝代理忠實：Binance 自算 CVD 可當 CoinGlass 的免 key 備援/補 stub。")
        print("   形狀(每根r)與方向(同號)達標；量級系統性偏燙→宜先補 z-score 型 universe stub")
        print("   （量級被截面標準化吸收）與 None/stale 備援，絕對門檻型votes 再驗。")
        print("   下一步＝設計 live 接線（仍走 RUNBOOK #26，僅補 None/stale/stub，不取代健康 CoinGlass）。")
    elif (pass_r or pass_sign):
        print("\n🟡 結論＝部分忠實：方向或時序其一達標、另一未達 → 僅可做「方向性備援」或需更大樣本複驗，"
              "勿逕接 strength 主路徑。")
    else:
        print("\n🔴 結論＝代理不夠忠實：Binance 單一場所 CVD 偏離 CoinGlass 三所聚合過大 →"
              "勿落地當備援（紅線③：不為了補欄而引入失真訊號）。")
    return 0


# ════════════════════════════════════════════════════════════════════════
#  純函式自測（離線、零網路）
# ════════════════════════════════════════════════════════════════════════
def _row(ts, quote_vol, taker_buy_quote):
    """造一根最小 klines（只填用到的欄位 0/7/10）。"""
    r = [0] * 12
    r[0] = ts
    r[7] = quote_vol
    r[10] = taker_buy_quote
    return r


def _selftest() -> int:
    # 1) 全主動買進（taker_buy = total）→ delta_pct = +100、斜率 = +100
    rows = [_row(i * 3600_000, 1000.0, 1000.0) for i in range(20)]
    out = cvd_slopes_from_klines(rows)
    assert abs(out["cvd_slope"] - 100.0) < 1e-6, out["cvd_slope"]
    assert abs(out["cvd_slope_7d"] - 100.0) < 1e-6
    print("  ✅ 全主動買進 → cvd_slope=+100")

    # 2) 全主動賣出（taker_buy = 0）→ delta_pct = −100
    rows = [_row(i * 3600_000, 1000.0, 0.0) for i in range(20)]
    out = cvd_slopes_from_klines(rows)
    assert abs(out["cvd_slope"] + 100.0) < 1e-6, out["cvd_slope"]
    print("  ✅ 全主動賣出 → cvd_slope=−100")

    # 3) 完美平衡（taker_buy = 半）→ delta_pct = 0
    rows = [_row(i * 3600_000, 1000.0, 500.0) for i in range(20)]
    out = cvd_slopes_from_klines(rows)
    assert abs(out["cvd_slope"]) < 1e-6 and abs(out["cvd"]) < 1e-6
    print("  ✅ 買賣平衡 → cvd_slope=0、cumsum=0")

    # 4) 短窗只看最後 12 根：前 8 根平衡、後 12 根全買 → cvd_slope=+100、7d 被前段拉低
    rows = ([_row(i * 3600_000, 1000.0, 500.0) for i in range(8)]
            + [_row((8 + i) * 3600_000, 1000.0, 1000.0) for i in range(12)])
    out = cvd_slopes_from_klines(rows)
    assert abs(out["cvd_slope"] - 100.0) < 1e-6, out["cvd_slope"]
    assert out["cvd_slope_7d"] < 100.0 - 1e-6, out["cvd_slope_7d"]
    print("  ✅ 短窗(12根)與7d窗(168根)分離正確")

    # 5) total=0 的壞根被跳過、不汙染斜率
    rows = [_row(0, 0.0, 0.0)] + [_row(i * 3600_000, 1000.0, 1000.0) for i in range(1, 13)]
    out = cvd_slopes_from_klines(rows)
    assert abs(out["cvd_slope"] - 100.0) < 1e-6
    print("  ✅ total=0 壞根跳過")

    # 6) cumsum 差分還原 delta 與原 deltas 一致（時序相關性基礎）
    rows = [_row(i * 3600_000, 1000.0, 600.0 + i * 10) for i in range(10)]
    out = cvd_slopes_from_klines(rows)
    recon = _deltas_from_cumsum(out["series"])
    assert len(recon) == len(out["deltas"])
    for (t1, d1), (t2, d2) in zip(recon, out["deltas"]):
        assert t1 == t2 and abs(d1 - d2) < 1e-6
    print("  ✅ cumsum 差分還原 delta 一致")

    # 7) Pearson：完全相同序列 r=1、反向 r=−1、常數 → None
    assert abs(_pearson([1, 2, 3, 4], [1, 2, 3, 4]) - 1.0) < 1e-9
    assert abs(_pearson([1, 2, 3, 4], [4, 3, 2, 1]) + 1.0) < 1e-9
    assert _pearson([1, 1, 1], [1, 2, 3]) is None
    print("  ✅ Pearson r（同向/反向/零變異）")

    # 8) 小時桶對齊：ms 與 s 混單位仍對齊同一根
    #    用真實量級時戳（2024-ish）才會觸發 >1e12 的 ms 偵測；小數值會被當秒處理（與 live 一致）
    _h0_s = 480000 * 3600          # = 1,728,000,000 s ≈ 2024，<1e12 → 偵測為秒
    _h1_s = _h0_s + 3600
    bn = [(_h0_s * 1000, 5.0), (_h1_s * 1000, -3.0)]   # ms（>1e12 → 偵測為毫秒）
    cg = [(_h0_s, 10.0), (_h1_s, -6.0)]                # s（同兩根）
    r, n = _perbar_correlation(bn, cg)
    assert n == 2 and r is not None and abs(r - 1.0) < 1e-9
    print("  ✅ ms/s 單位混用仍對齊（小時桶）")

    print("自測通過：CVD 純函式(同構推導/短窗7d窗分離/壞根跳過) + 差分還原 + Pearson + 跨單位對齊 ✅")
    return 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] == "--selftest":
        raise SystemExit(_selftest())
    syms = [s.upper() for s in argv] if argv else DEFAULT_SYMBOLS
    raise SystemExit(asyncio.run(main(syms)))
