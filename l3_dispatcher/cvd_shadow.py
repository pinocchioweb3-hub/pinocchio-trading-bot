"""task#20 因子回補治本 · 第二步「影子接線」（純觀測、零訊號數學變更）。

第一步（v61，已 SHIP）＝忠實度回測閘：證明免 key Binance 自算 CVD 是 CoinGlass
聚合 CVD 的忠實代理（每根 delta 時序 r 中位 0.928、cvd_slope_7d 同號率 100%）。
v64（已 SHIP）＝其餘三個壞因子的離線回補純函式層（backtest.factor_backfill）。

本檔＝第二步：開始「回補缺的數據」。資料缺口在 universe 排名路徑——coinglass.py
get_strength_universe 對多個因子塞死值/反符號：cvd_slope_7d=0.0、top_trader_dev=0.05、
btc_corr_30d=0.70（三者 z-score 退化＝死權重，合計 35%）、vol_24h_vs_30d 反符號（15%）。
本影子 worker 每小時：
    1. 取 universe 因子快照（**逐幣節流**呼叫 source.get_strength_universe，避開
       「burst N 筆被 CoinGlass 上游 429 截斷成 ~2–5 筆」的缺陷；重用 daemon 共用
       限流器 + TTL 快取）——這是 strength 排名器「實際吃到」的因子（含死值/反符號缺口）。
    2. 對同一批幣用免 key Binance 自算「該補的正確值」（零額度、同 T 橫斷面）：
       cvd_slope_7d/cvd_slope（1h klines）、top_trader_dev（大戶比−1）、
       btc_corr_30d（日報酬 Pearson vs BTC）、vol_24h_vs_30d（24h/30d 日均，修正反符號）。
    3. 把「實際因子（含死值/反符號缺口）＋ 該補的 Binance 正確值」併成一筆 JSONL 寫進
       獨立 sink：data_dir()/cvd_shadow.jsonl（binance_* 獨立鍵，從不回寫）。

日後的「EV 閘」（離線、另案）可讀這份 JSONL 做**聯合反事實重排序**：
「若把這幾個壞因子（cvd_slope_7d / top_trader_dev / btc_corr_30d / vol_24h_vs_30d）
同時用 Binance 正確值取代死值/反符號，universe 排名/EV 是否改善？」過閘才晉升去真填
coinglass.py get_strength_universe（走 RUNBOOK #26、task#64）。離線 EV 閘可以
import strength.py；但本 worker 不行（見影子鐵則）。同 T 橫斷面一次補齊全部因子，是為了
讓數週的累積同時覆蓋所有因子、且能做「同一快照」的聯合反事實檢定（資料累積才是長瓶頸）。

════════════════════════════════════════════════════════════════════════════
影子鐵則（與 convergence_shadow 同一套，絕不可違反）：
    * 本 worker **永不** 把任何 binance_* 回補值（cvd / top_trader_dev / btc_corr / vol）
      寫回 universe items / strength_score；
      **永不** 寫 fire_queue / snapshot / symbol_gate / 任何下單路徑；
      **永不** import market_intel_mcp.strength 或 l2_trigger.signals。
    * 唯一輸出 = 自己的 cvd_shadow.jsonl（觀測用，給日後 EV 閘離線反事實重排序）。
    * 因子數學唯一來源 = backtest.binance_cvd_validate（CVD，v61 回測閘認證）
      + backtest.factor_backfill（top_trader_dev / btc_corr / vol，v64 已認證純函式）；
      本檔只重用、不另寫一份數學。
    * 整個迴圈體包 try/except；任何源失敗都吞掉續跑，絕不拖垮 daemon
      （外層另有 supervise() 崩潰隔離 + 退避重啟）。
    * 不發 Telegram（純背景觀測，不打擾使用者）。
════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import time

# universe 因子快照取樣數（對它們逐幣免費自算 Binance CVD；上限保守省 CoinGlass 額度）
_UNIVERSE_N = 20
# Binance CVD 窗口（與回測閘一致：1h × 168 根 = 7d）
_CVD_INTERVAL = "1h"
_CVD_LIMIT = 168
# JSONL sink 軟上限（位元組）；超過就先改名 .1 再重開，避免無限長
_SINK_MAX_BYTES = 5_000_000
# universe 逐幣取數的節流間隔（秒）。探針實證：對 CoinGlass Startup tier 的
# /pairs-markets，一次 burst N 筆會被上游 429 截斷成 ~2–5 筆；間隔逐筆送大幅提升成功率。
# 取 3.0s：20 幣 × 3.0s ≈ 60s 在「一輪內」跑完整個橫斷面快照（單筆 JSONL 含全幣同一時刻
# 因子，供離線反事實重排序須同 T 橫斷面），且 60s 略大於 90s TTL/60s 滑窗——讓多數第二趟
# 補抓能吃到 live 暖快取命中。刻意不把採樣攤到整個小時（會把同 T 橫斷面打散成 20 筆單幣記錄）。
# 影子側自己 pace（純讀、零訊號影響），不碰 live get_strength_universe 本身——
# 改 live 方法的節流＝改變訊號路徑的幣覆蓋（→ trading tier → FIRE），須過回測閘屬另案。
_UNIVERSE_PACE_SECONDS = 3.0


def _sink_path():
    from botpaths import data_dir
    return data_dir() / "cvd_shadow.jsonl"


def _append_jsonl(record: dict) -> None:
    """把一輪觀測寫一行 JSONL（純本地檔；超過軟上限就輪替一次）。"""
    path = _sink_path()
    try:
        if path.exists() and path.stat().st_size > _SINK_MAX_BYTES:
            backup = path.with_suffix(".jsonl.1")
            try:
                if backup.exists():
                    backup.unlink()
                path.rename(backup)
            except OSError:
                pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 觀測 sink 寫失敗不致命，吞掉續跑


# ════════════════════════════════════════════════════════════════════════
#  純函式：把「universe 因子 + Binance CVD」併成一筆觀測記錄（離線可測）
# ════════════════════════════════════════════════════════════════════════
# universe item 裡要保留的因子鍵（strength 排名器實際吃到的；cvd_slope_7d 是 0.0 缺口）
_FACTOR_KEYS = (
    "return_7d_pct", "vol_24h_usd", "vol_24h_vs_30d",
    "oi_delta_7d_pct", "cvd_slope_7d", "top_trader_dev",
    "btc_corr_30d", "funding",
)


def _build_item(symbol: str, factors: dict, cvd: dict | None,
                captured_ts: str | None = None,
                backfill: dict | None = None) -> dict:
    """把一個 universe 因子 dict + Binance 回補值（CVD + 三因子）併成觀測 item。

    純函式（無 I/O）：factors=get_strength_universe 的單筆 item；cvd=Binance 自算
    （None＝Binance 取數失敗，誠實記 None 不捏造，紅線③）；backfill=_backfill_factors
    輸出（None＝未取，四個回補鍵誠實 None）。
    刻意把「實際因子（含死值/反符號缺口）」與「該補的 Binance 正確值」並排，供離線反事實重排。
    captured_ts＝該幣因子實際取到的 UTC 時刻（None＝未知，誠實不捏造）；離線閘可據此
    驗證整輪是否落在同一橫斷面窗口（見 span_sec）。
    影子鐵則：所有 binance_* 值活在獨立鍵，**從不**寫回 factors_live（strength 排名器吃的）。
    """
    factors_live = {k: factors.get(k) for k in _FACTOR_KEYS}
    stub_cvd = factors_live.get("cvd_slope_7d")
    item = {
        "symbol": symbol,
        "captured_ts": captured_ts,          # 該幣因子實際取到的 UTC 時刻（同 T 橫斷面驗證用）
        "factors_live": factors_live,        # strength 排名器實際吃到的（含死值/反符號缺口）
        "stub_cvd_slope_7d": stub_cvd,       # 明確標出 universe 路徑現用的 stub（通常 0.0）
        "binance_cvd_slope_7d": None,
        "binance_cvd_slope": None,
        "binance_cvd": None,
        "binance_bars": 0,
        "binance_ok": False,
        # —— v65 因子回補：top_trader_dev / btc_corr_30d / vol_24h_vs_30d 並排記錄 ——
        # 獨立 binance_* 鍵，從不汙染 factors_live；缺料誠實 None（紅線③）。
        "binance_top_trader_ratio": None,
        "binance_top_trader_dev": None,
        "binance_btc_corr_30d": None,
        "binance_vol_24h_vs_30d": None,
        "binance_daily_bars": 0,
    }
    if isinstance(cvd, dict):
        item["binance_cvd_slope_7d"] = cvd.get("cvd_slope_7d")
        item["binance_cvd_slope"] = cvd.get("cvd_slope")
        item["binance_cvd"] = cvd.get("cvd")
        item["binance_bars"] = len(cvd.get("series") or [])
        item["binance_ok"] = True
    if isinstance(backfill, dict):
        item["binance_top_trader_ratio"] = backfill.get("top_trader_ratio")
        item["binance_top_trader_dev"] = backfill.get("top_trader_dev")
        item["binance_btc_corr_30d"] = backfill.get("btc_corr_30d")
        item["binance_vol_24h_vs_30d"] = backfill.get("vol_24h_vs_30d")
        item["binance_daily_bars"] = backfill.get("daily_bars") or 0
    return item


def _span_seconds(ts_list) -> float | None:
    """一輪內「最早→最晚」因子取到時刻的時間跨度（秒）。

    純函式（無 I/O）：ts_list＝各幣 captured_ts（ISO 字串，None 略過）。少於 2 個
    可解析時刻回 None（誠實不捏造，紅線③）。離線閘用它驗證整輪是否落在同一橫斷面窗口
    （span 太大＝因子散在不同時刻，同 T 反事實重排序的前提被破壞，該筆記錄須降權/剔除）。
    """
    parsed = []
    for ts in ts_list or []:
        if not ts:
            continue
        try:
            parsed.append(dt.datetime.fromisoformat(ts))
        except (ValueError, TypeError):
            continue
    if len(parsed) < 2:
        return None
    return round((max(parsed) - min(parsed)).total_seconds(), 1)


# ════════════════════════════════════════════════════════════════════════
#  Live 取數（唯讀；Binance 免 key、CoinGlass 重用 daemon 共用 source）
# ════════════════════════════════════════════════════════════════════════
async def _binance_cvd(bn, symbol: str) -> dict | None:
    """免 key 取 Binance 原始 klines（含 row[10] taker buy quote）→ 認證純函式自算 CVD。

    用 bn._get 直取原始 klines（get_candles 會丟掉 row[10]，無法算 CVD），與 v61
    回測閘 _binance_cvd 同一條取數路徑。失敗回 None（不 raise）。
    """
    from backtest.binance_cvd_validate import cvd_slopes_from_klines
    try:
        sym = bn._sym(symbol)
        body = await bn._get(
            "/fapi/v1/klines",
            {"symbol": sym, "interval": _CVD_INTERVAL,
             "limit": min(max(_CVD_LIMIT, 1), 1500)},
            symbol, "cvd_shadow",
        )
        if isinstance(body, dict) and body.get("error"):
            return None
        if not isinstance(body, list) or not body:
            return None
        return cvd_slopes_from_klines(body)
    except Exception:
        return None


async def _binance_daily(bn, symbol: str, limit: int = 35):
    """免 key 取 Binance 1d klines → (closes, volumes_usd) 兩個升序 list。

    給 btc_corr_30d（日報酬 Pearson）與 vol_24h_vs_30d（24h/30d 日均量）用。
    失敗回 (None, None)（不 raise；誠實，紅線③）。
    """
    try:
        kl = await bn.get_candles(symbol, "1d", limit)
        if not isinstance(kl, dict) or kl.get("error"):
            return None, None
        candles = kl.get("candles") or []
        closes = [c.get("close") for c in candles]
        vols = [c.get("volume_usd") for c in candles]
        return closes, vols
    except Exception:
        return None, None


async def _binance_positioning(bn, symbol: str):
    """免 key 取大戶帳戶多空比最新值（topLongShortAccountRatio['latest']）。

    給 top_trader_dev（ratio − 1）用。失敗回 None（不 raise；誠實，紅線③）。
    """
    try:
        pos = await bn.get_positioning(symbol, "1d", 30)
        if isinstance(pos, dict) and not pos.get("error"):
            return pos.get("latest")
        return None
    except Exception:
        return None


def _backfill_factors(symbol: str, top_ratio, sym_closes, sym_vols,
                      btc_closes) -> dict:
    """純函式：用 v64 回補純函式把三個壞因子算成「該補的正確值」（誠實 None，不捏造）。

    重用 backtest.factor_backfill（v64 已 SHIP + 16 測鎖契約）的純函式，本檔不另寫數學：
        top_trader_dev ← ratio − 1（死權重 stub=0.05 的治本）
        btc_corr_30d   ← 日報酬 Pearson vs BTC（死權重 stub=0.70 的治本；BTC 對自己=1.0）
        vol_24h_vs_30d ← 24h 量 / 30d 日均量（修正 stub 反符號 bug）
    分子用最近日線量（vols[-1]，可能是當日進行中未完整 bar）；因整輪同 T 橫斷面同時刻取樣，
    「當日進行中比例」對全幣近似同一乘數 → z-score 截面正規化會吸收，相對排名不受偏。
    純函式（無 I/O）：所有 live 取數由呼叫端先做好再餵進來，方便離線單測。
    """
    from backtest.factor_backfill import (
        top_trader_dev_from_ratio, btc_corr_from_closes, vol_ratio_24h_vs_30d,
    )
    dev = top_trader_dev_from_ratio(top_ratio)
    if symbol == "BTC":
        corr = 1.0                       # BTC 對自己定義為 1.0（與 factor_backfill 一致）
    elif sym_closes and btc_closes:
        corr = btc_corr_from_closes(sym_closes, btc_closes)
    else:
        corr = None
    vol_ratio = None
    if sym_vols:
        vol_ratio = vol_ratio_24h_vs_30d(sym_vols[-1], sym_vols[:-1])
    n_bars = len([c for c in (sym_closes or []) if c is not None])
    return {
        "top_trader_ratio": top_ratio,
        "top_trader_dev": dev,
        "btc_corr_30d": corr,
        "vol_24h_vs_30d": vol_ratio,
        "daily_bars": n_bars,
    }


# 取數補抓回合數（pass 1 之後再補抓 miss 的幣；吸收瞬時 429 競爭，盡量湊滿 universe）。
# 取 2：最壞 20 幣 ×（1+2）趟 = 60 次呼叫，仍遠低於共用限流器 75/min 上限；補抓只送
# 「上一趟 miss 的幣」（通常遠少於 20），換手前指數退避讓 60s 滑窗釋出額度，攤平瞬時 429。
_UNIVERSE_RETRIES = 2


def _universe_miss_reason(r) -> str:
    """這一幣為什麼沒抓到（v244）。成功時呼叫端不會用到本函式。

    ⛔ 不得回空字串／"unknown"／"error"：四種成因的處置完全不同——
        API 錯誤（401）→ 續訂／換源
        回應成功但 items 空 → 查資料契約（幣不在宇宙裡？還是聚合掉了？）
        回應型別不對 → 查端點契約
        例外 → 查網路／限流
    講不清楚等於沒說，而這正是這個 worker 空轉 26 天沒人看出來的原因。
    """
    if isinstance(r, BaseException):
        return f"例外 {type(r).__name__}: {r}"[:160]
    if not isinstance(r, dict):
        return f"回應型別非 dict（{type(r).__name__}）"
    if r.get("error"):
        code = r.get("code") or "ERROR"
        msg = str(r.get("message") or r.get("msg") or "").strip()
        return f"{code}: {msg}"[:160] if msg else f"{code}（無訊息）"
    # v245：源自己說得出成因就**轉述**，⛔ 不得覆蓋成下面那句自己的推測。
    # v244 上線後的線上實證就卡在這裡：源回 401，影子卻講「該幣未進宇宙」，
    # 把讀的人指去查資料契約——說得出話但說錯方向，比沉默更貴。
    up = r.get("unavailable")
    if isinstance(up, dict) and up:
        return "上游：" + "；".join(str(k) for k in up)[:150]
    if not (r.get("items") or []):
        return "回應成功但 items 為空（該幣未進宇宙／上游聚合掉了）"
    return "未知形狀的回應"


async def _universe_factors(source, n: int,
                            pace: float = _UNIVERSE_PACE_SECONDS,
                            retries: int = _UNIVERSE_RETRIES,
                            reasons: dict | None = None) -> list[dict]:
    """取 universe 因子快照——**逐幣節流 + miss 補抓**呼叫 get_strength_universe。

    重用 live get_strength_universe 的「每幣聚合」邏輯（candidate_symbols=[sym]、
    limit=1 → 恰 1 筆 /pairs-markets 上游呼叫，與 burst 版逐幣完全等價），但影子側
    **自己排程**、每筆間隔 `pace` 秒。這避開「同步 burst N 筆被上游 429 截斷成
    ~2–5 筆」的缺陷，又**完全不碰 live 方法本身**（改 live＝改變訊號路徑的幣覆蓋
    → trading tier → FIRE，須過回測閘，屬另案；影子側純讀、零訊號影響）。

    缺料回補：429 競爭下 _get 退避 3 次仍可能空手（get_strength_universe 對該幣
    `continue` → items=[]）。故再做 `retries` 回合補抓**只剩 miss 的幣**，攤平把
    瞬時節流吃掉，盡量湊滿整個 universe（hourly cadence、TTL 快取會幫忙，成本低）。
    上游呼叫量仍受共用限流器封頂，不增 CoinGlass 額度上限風險。

    universe 幣＝live 路徑同一份（get_strength_universe 無 candidate_symbols 時即用
    TRADING_CANDIDATES[:limit]）→ 逐幣重現完全相同的 universe。回每幣 item（依
    canonical 順序）；個別幣最終仍失敗就誠實略過（不捏造，紅線③）。
    """
    # v244：`reasons` 是給呼叫端收「每一幣為什麼沒抓到」的外參（回傳型別刻意不動，
    # 避免動到既有 8 個測試的簽章）。⛔ 每一條 return [] 都要先留痕——接線斷了
    # 靜默回空，在輸出上跟「宇宙裡本來就沒幣」一模一樣。
    if reasons is None:
        reasons = {}
    if source is None:
        reasons["*"] = "daemon 沒給 source（get_source() 失敗或未接線）"
        return []
    if not hasattr(source, "get_strength_universe"):
        reasons["*"] = f"source（{type(source).__name__}）沒有 get_strength_universe"
        return []
    try:
        from market_intel_mcp.symbol_mapping import TRADING_CANDIDATES
        candidates = list(TRADING_CANDIDATES)[:n]
    except Exception as e:  # noqa: BLE001
        reasons["*"] = f"取候選幣清單失敗：{type(e).__name__}: {e}"[:160]
        return []

    captured: dict[str, dict] = {}
    remaining = list(candidates)
    attempt = 0
    while remaining and attempt <= retries:
        misses: list[str] = []
        last = len(remaining) - 1
        for i, sym in enumerate(remaining):
            ok = False
            r = None
            try:
                r = await source.get_strength_universe(limit=1, candidate_symbols=[sym])
                if isinstance(r, dict) and not r.get("error"):
                    its = r.get("items") or []
                    if its:
                        item0 = its[0]
                        # 把該幣「實際取到」的 UTC 時刻釘在 item 上（離線閘據此驗同 T 橫斷面）。
                        # 用私有鍵 _captured_ts 暫存於 item（不改本函式回傳型別 list[dict]，
                        # 避免動到既有測試簽章）；_run_cycle 會抽出來餵給 _build_item。
                        try:
                            item0["_captured_ts"] = dt.datetime.now(
                                tz=dt.timezone.utc).isoformat()
                        except (TypeError, AttributeError):
                            pass
                        captured[sym] = item0
                        ok = True
            except Exception as e:  # noqa: BLE001
                ok = False
                r = e                # v244：例外本身就是成因，別在這裡丟掉
            if ok:
                reasons.pop(sym, None)   # 補抓成功 → 撤掉上一趟的成因（不留假故障）
            else:
                misses.append(sym)   # 429/空手/例外都進補抓清單
                reasons[sym] = _universe_miss_reason(r)  # …但成因要留下來
            if pace and i < last:
                await asyncio.sleep(pace)
        remaining = misses
        attempt += 1
        if remaining and attempt <= retries and pace:
            # 換手前指數退避（pace*2^attempt，封頂 8s），讓 60s 滑窗釋出更多額度再補抓
            await asyncio.sleep(min(pace * (2 ** attempt), 8.0))
    # 依 canonical 順序回（與 live universe 排序一致）
    return [captured[s] for s in candidates if s in captured]


async def _run_cycle(source=None, n: int = _UNIVERSE_N,
                     pace: float | None = None,
                     retries: int | None = None) -> dict:
    """跑一輪 CVD 影子觀測，回一個可序列化摘要 dict。

    `source`＝daemon 主 source（backend=coinglass 時即 CoinGlassSource）；
    None → 延遲 get_source()（供一次性測試）。
    `pace`／`retries`＝節流參數（None＝用模組預設；離線測試傳 0 才不用真的睡 3 秒 ×N 趟）。
    """
    from market_intel_mcp.sources.binance_perp import get_binance_perp

    src_reasons: dict[str, str] = {}
    if source is None:
        try:
            from market_intel_mcp.sources import get_source
            source = get_source()
        except Exception as e:  # noqa: BLE001
            source = None
            src_reasons["*"] = f"get_source() 失敗：{type(e).__name__}: {e}"[:160]

    bn = get_binance_perp()
    t0 = time.monotonic()

    # 1) universe 因子快照（strength 排名器實際吃到的；含死值/反符號缺口）
    factor_items = await _universe_factors(
        source, n,
        pace=_UNIVERSE_PACE_SECONDS if pace is None else pace,
        retries=_UNIVERSE_RETRIES if retries is None else retries,
        reasons=src_reasons,
    )
    syms = [it.get("symbol") for it in factor_items if it.get("symbol")]

    # 2) BTC 日線收盤當 btc_corr 參考序列（取一次；BTC 對自己 corr=1.0）。
    #    即便 BTC 不在本輪 universe 也要有參考，故獨立取（免 key、零額度）。
    btc_closes, _btc_vols = await _binance_daily(bn, "BTC")

    # 3) 對同一批幣免費自算 Binance：CVD（1h×168）+ 日線（corr/vol）+ 大戶比（dev）。
    #    三組全並行（Binance 免 key、零 CoinGlass 額度）；同 T 橫斷面一次補齊全部因子。
    cvd_results, daily_results, pos_results = await asyncio.gather(
        asyncio.gather(*[_binance_cvd(bn, s) for s in syms], return_exceptions=True),
        asyncio.gather(*[_binance_daily(bn, s) for s in syms], return_exceptions=True),
        asyncio.gather(*[_binance_positioning(bn, s) for s in syms], return_exceptions=True),
    )
    cvd_map: dict[str, dict | None] = {}
    daily_map: dict[str, tuple] = {}
    pos_map: dict = {}
    for idx, s in enumerate(syms):
        cr = cvd_results[idx]
        cvd_map[s] = cr if isinstance(cr, dict) else None
        dr = daily_results[idx]
        daily_map[s] = dr if isinstance(dr, tuple) else (None, None)
        pr = pos_results[idx]
        pos_map[s] = pr if not isinstance(pr, BaseException) else None

    # 4) 併記錄（純函式 _build_item + _backfill_factors），抽出每幣 captured_ts 算橫斷面跨度
    items = []
    cap_ts_list = []
    for it in factor_items:
        sym = it.get("symbol")
        if not sym:
            continue
        cts = it.get("_captured_ts")   # _universe_factors 暫存的取到時刻（可能 None）
        cap_ts_list.append(cts)
        sclose, svol = daily_map.get(sym, (None, None))
        backfill = _backfill_factors(sym, pos_map.get(sym), sclose, svol, btc_closes)
        items.append(_build_item(sym, it, cvd_map.get(sym),
                                 captured_ts=cts, backfill=backfill))

    n_binance_ok = sum(1 for it in items if it.get("binance_ok"))
    n_backfill_ok = sum(1 for it in items
                        if it.get("binance_btc_corr_30d") is not None
                        or it.get("binance_top_trader_dev") is not None
                        or it.get("binance_vol_24h_vs_30d") is not None)
    summary = {
        "ts": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "universe_n": n,
        "captured": len(items),
        # v244：syms 空 ⇒ 一次 Binance 都沒呼叫。少了這一欄，`binance_ok=0` 就同時
        # 代表「20 幣全失敗」和「從沒問過」——用「我量到是零」冒充「我沒去量」。
        "binance_attempted": len(syms),
        "binance_ok": n_binance_ok,
        "backfill_ok": n_backfill_ok,             # 至少補到一個因子（top_trader_dev/btc_corr/vol）的幣數
        "span_sec": _span_seconds(cap_ts_list),   # 首→末因子取到的時間跨度（同 T 橫斷面驗證；None=不足2筆）
        "elapsed_sec": round(time.monotonic() - t0, 1),  # 整輪耗時（含補抓退避；接近 interval 即告警）
        "items": items,
        "note": "shadow-only: 任何 binance_* 回補值（cvd/top_trader_dev/btc_corr/vol）"
                "從不寫回 universe/strength/fire；補捉 universe 路徑死值/反符號缺口"
                "供日後 EV 閘離線『聯合反事實重排序』；因子數學＝v61+v64 回測閘認證純函式",
    }
    # v244：沒抓到的幣要帶成因進 sink。⛔ 去重成 {成因: [幣]}——20 幣同一句 401 時
    #       逐幣重複會讓每小時一列的 sink 白白膨脹一倍。
    # ⛔ 反向側：全成功時完全不加這個鍵（不製造 ⚠️ 雜訊）。
    if src_reasons:
        grouped: dict[str, list[str]] = {}
        for sym, why in src_reasons.items():
            grouped.setdefault(why, []).append(sym)
        summary["unavailable"] = grouped
    return summary


async def run_cvd_shadow_loop(source=None, interval_seconds: int = 3600):
    """task#20 CVD 影子觀測常駐迴圈（每 interval 跑一輪，純觀測寫 cvd_shadow.jsonl）。

    `source`＝daemon 主 source（用其 get_strength_universe 取 universe 因子，bounded
    + 共用 TTL 快取）。Binance CVD 走 binance_perp 單例（免 key、零額度）。
    """
    # 啟動延遲 300s（5min）避開開機尖峰：watchlist.refresh 開機那一刻會無節流 burst 掃
    # 數十幣 /pairs-markets，與影子逐幣搶同一個共用限流器→開機首輪 capture 必爛（前車之鑑
    # 6/20）。等 5min 讓開機 burst 退潮、共用 TTL 快取暖起來，首輪才打在穩態而非尖峰。
    await asyncio.sleep(300)
    while True:
        try:
            summary = await _run_cycle(source)
            _append_jsonl(summary)
            span = summary.get("span_sec")
            elapsed = summary.get("elapsed_sec")
            # v244：`captured=0 binance_ok=0` 讀起來像一個安靜的正常輪——實際上這個
            # worker 從 2026-07-08（CG 方案到期）起空轉了 574 輪、26 天，每小時印一次
            # 這行，沒有一次被讀成故障。成因要印在同一行上，否則等於沒印。
            unavail = summary.get("unavailable") or {}
            why = ""
            if unavail:
                why = "  ⚠️未取得：" + "；".join(
                    f"{w}×{len(syms)}" for w, syms in unavail.items())
            print(f"[cvd_shadow] universe_n={summary['universe_n']} "
                  f"captured={summary['captured']} "
                  f"binance_ok={summary['binance_ok']}"
                  f"/{summary.get('binance_attempted')}attempted "
                  f"backfill_ok={summary.get('backfill_ok')} "
                  f"span_sec={span} elapsed_sec={elapsed}{why}")
            # 整輪耗時逼近 interval＝補抓退避吃太久（橫斷面被拉長/下一輪要遲到），留痕示警
            if isinstance(elapsed, (int, float)) and elapsed > int(interval_seconds) * 0.9:
                print(f"[cvd_shadow] WARN cycle elapsed={elapsed}s 逼近 interval="
                      f"{int(interval_seconds)}s（補抓退避過久；橫斷面窗口被拉長）")
        except Exception as e:  # 整輪保護：任何意外吞掉續跑，不拖垮 daemon
            print(f"[cvd_shadow] cycle error: {e}")
        await asyncio.sleep(max(60, int(interval_seconds)))


# ════════════════════════════════════════════════════════════════════════
#  純函式自測（離線、零網路）
# ════════════════════════════════════════════════════════════════════════
def _selftest() -> int:
    # 1) 正常併記：factors 帶 0.0 stub、cvd 有值 → item 並排兩者、binance_ok=True
    factors = {
        "return_7d_pct": 5.0, "vol_24h_usd": 1e9, "vol_24h_vs_30d": 1.2,
        "oi_delta_7d_pct": 3.0, "cvd_slope_7d": 0.0, "top_trader_dev": 0.05,
        "btc_corr_30d": 0.7, "funding": 0.0001,
    }
    cvd = {"cvd": 1234.5, "cvd_slope": 8.9, "cvd_slope_7d": 12.34,
           "series": [{"ts": 1, "value": 1.0}] * 168, "deltas": []}
    item = _build_item("BTC", factors, cvd)
    assert item["symbol"] == "BTC"
    assert item["stub_cvd_slope_7d"] == 0.0
    assert item["factors_live"]["cvd_slope_7d"] == 0.0      # 缺口被忠實保留
    assert item["binance_cvd_slope_7d"] == 12.34
    assert item["binance_cvd_slope"] == 8.9
    assert item["binance_bars"] == 168
    assert item["binance_ok"] is True
    assert item["captured_ts"] is None                      # 未傳＝誠實 None（不捏造，紅線③）
    print("  ✅ 正常併記：0.0 stub 與 Binance 值並排、binance_ok=True、captured_ts 預設 None")

    # 2) Binance 取數失敗（cvd=None）→ 誠實記 None、binance_ok=False（不捏造，紅線③）
    item2 = _build_item("ETH", factors, None)
    assert item2["binance_cvd_slope_7d"] is None
    assert item2["binance_ok"] is False
    assert item2["binance_bars"] == 0
    assert item2["factors_live"]["return_7d_pct"] == 5.0    # 因子仍完整保留
    print("  ✅ Binance 缺料 → 誠實 None、binance_ok=False")

    # 3) 只保留白名單因子鍵，雜鍵不外漏（避免 sink 膨脹/夾帶非預期欄）
    noisy = dict(factors, secret="x", _internal=1)
    item3 = _build_item("SOL", noisy, cvd)
    assert "secret" not in item3["factors_live"] and "_internal" not in item3["factors_live"]
    assert set(item3["factors_live"].keys()) == set(_FACTOR_KEYS)
    print("  ✅ 因子鍵白名單：雜鍵不外漏")

    # 4) 記錄可 JSON 序列化（sink 寫得出去）
    rec = {"ts": "2026-01-01T00:00:00+00:00", "universe_n": 1, "captured": 1,
           "binance_ok": 1, "items": [item], "note": "x"}
    s = json.dumps(rec, ensure_ascii=False)
    assert "BTC" in s and "binance_cvd_slope_7d" in s
    print("  ✅ 記錄可 JSON 序列化")

    # 5) captured_ts 有傳就忠實帶進 item（同 T 橫斷面驗證需要）
    item_ts = _build_item("BTC", factors, cvd, captured_ts="2026-01-01T00:00:05+00:00")
    assert item_ts["captured_ts"] == "2026-01-01T00:00:05+00:00"
    print("  ✅ captured_ts 忠實帶入 item")

    # 6) _span_seconds：≥2 筆算首→末跨度；<2 筆或全 None → 誠實 None（不捏造，紅線③）
    span = _span_seconds([
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T00:00:42+00:00",
        None,
        "2026-01-01T00:00:18+00:00",
    ])
    assert span == 42.0, span                               # max(42) - min(0)
    assert _span_seconds([]) is None
    assert _span_seconds(["2026-01-01T00:00:00+00:00"]) is None   # 僅 1 筆
    assert _span_seconds([None, None]) is None
    assert _span_seconds(["bad-ts", "also-bad"]) is None    # 解析不出 → None，不丟例外
    print("  ✅ _span_seconds：首末跨度正確、不足2筆/壞值誠實 None")

    # 7) 因子回補並排（v65）：backfill 值活在獨立 binance_* 鍵，不汙染 factors_live
    bf = {"top_trader_ratio": 1.93, "top_trader_dev": 0.93,
          "btc_corr_30d": 0.42, "vol_24h_vs_30d": 1.8, "daily_bars": 34}
    item_bf = _build_item("BTC", factors, cvd, backfill=bf)
    assert item_bf["binance_top_trader_ratio"] == 1.93
    assert item_bf["binance_top_trader_dev"] == 0.93
    assert item_bf["binance_btc_corr_30d"] == 0.42
    assert item_bf["binance_vol_24h_vs_30d"] == 1.8
    assert item_bf["binance_daily_bars"] == 34
    # 影子鐵則：backfill 絕不寫回 factors_live（仍是含死值缺口的原值）
    assert item_bf["factors_live"]["top_trader_dev"] == 0.05
    assert item_bf["factors_live"]["btc_corr_30d"] == 0.7
    assert item_bf["factors_live"]["vol_24h_vs_30d"] == 1.2
    print("  ✅ 因子回補並排：binance_* 獨立鍵、不汙染 factors_live")

    # 8) 沒傳 backfill → 四個回補鍵誠實 None、daily_bars=0（不捏造，紅線③）
    item_nobf = _build_item("ETH", factors, cvd)
    assert item_nobf["binance_top_trader_dev"] is None
    assert item_nobf["binance_btc_corr_30d"] is None
    assert item_nobf["binance_vol_24h_vs_30d"] is None
    assert item_nobf["binance_daily_bars"] == 0
    print("  ✅ 無 backfill → 回補鍵誠實 None")

    # 9) _backfill_factors 純函式：BTC 對自己 corr=1.0、dev=ratio−1、資料不足誠實 None
    bf_btc = _backfill_factors("BTC", 1.5, [100, 101, 102], [10, 20, 30],
                               [100, 101, 102])
    assert bf_btc["btc_corr_30d"] == 1.0           # BTC 對自己定義為 1.0
    assert bf_btc["top_trader_dev"] == 0.5         # 1.5 − 1
    assert bf_btc["vol_24h_vs_30d"] is None        # 之前日數 < min_days=7 → 誠實 None
    bf_alt = _backfill_factors("ETH", None, None, None, [100, 101])
    assert bf_alt["top_trader_dev"] is None and bf_alt["btc_corr_30d"] is None
    assert bf_alt["daily_bars"] == 0
    print("  ✅ _backfill_factors：BTC自corr=1 / dev=ratio−1 / 資料不足誠實 None")

    print("自測通過：影子 item 併記（死值缺口並排/誠實 None/因子白名單/captured_ts）"
          "+ 因子回補（top_trader_dev/btc_corr/vol）+ span 跨度 + 可序列化 ✅")
    return 0


if __name__ == "__main__":
    import sys
    from pathlib import Path
    # 直接以路徑執行本檔時 sys.path[0] 是 l3_dispatcher/，專案根不在路徑上 →
    # import backtest / market_intel_mcp 會失敗；先補上專案根，讓 --selftest（會 import
    # backtest.factor_backfill）與 live 試跑（import market_intel_mcp）皆可「python
    # l3_dispatcher\cvd_shadow.py」直跑。pytest 另由 test 檔自插 root，故那條路徑不受影響。
    _root = str(Path(__file__).resolve().parent.parent)
    if _root not in sys.path:
        sys.path.insert(0, _root)
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        raise SystemExit(_selftest())
    # 一次性 live 試跑（唯讀；需 .env 的 CoinGlass 金鑰才取得到 universe 因子）
    import os
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    # ⚠️ 環境陷阱：MARKET_INTEL_BACKEND 預設 "mock"（settings.py:19），且 daemon 是用
    # run_bot 程式內設成 "coinglass"、非寫在 .env，故獨立 CLI 不設就會悄悄打到 MockSource
    # 拿到「假的非零 cvd_slope_7d」誤判缺口已補。這裡 setdefault 成 coinglass 讓 dev 一次性
    # 試跑打到真源（須在第一次 import market_intel_mcp.settings 凍結 SETTINGS 之前設）。
    os.environ.setdefault("MARKET_INTEL_BACKEND", "coinglass")

    async def _once():
        summary = await _run_cycle()
        print(json.dumps(summary, ensure_ascii=False, indent=2)[:2000])
        return 0
    raise SystemExit(asyncio.run(_once()))
