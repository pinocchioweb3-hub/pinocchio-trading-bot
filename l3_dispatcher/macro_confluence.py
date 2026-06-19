"""Session C：綜合宏觀指標合成（影子層，永不影響下單）。

把多個彼此獨立的宏觀分量（funding / OI / 清算 / 巨鯨 / ETF / DXY / 市場廣度
breadth …）用「確定性規則」合成成單一 macro_confluence_score（-100..+100，
+ 偏多/risk-on、- 偏空/risk-off）+ 各分量明細，每小時寫一行 JSONL 到獨立
sink：data_dir()/macro_confluence.jsonl。另以 SQLite 持續累積 OI/CVD/funding
近 500 根快照（補「從今天起往前累積」的未來歷史；補不回過去，誠實標明）。

════════════════════════════════════════════════════════════════════════════
影子鐵則（最高優先，絕不可違反；仿 convergence.py 明文鐵則）：
    1. macro_confluence_score 與其任何分量是 **SHADOW 專用**。
       **永不** 乘進/加進 strength_score、**永不** 寫 snapshot、
       **永不** 進 fire / 進場 / symbol_gate / 任何下單路徑。
       它只進獨立影子 sink（jsonl + macro_history.db）供日後 A/B 回測評估。
    2. 本模組 **不得 import market_intel_mcp.strength**、不得改 strength.py /
       eval_cvd_divergence，不寫 fire_queue / paper / trade 任何帳。
    3. 純讀：零下單路徑（紅線①）；資料蒐集失敗一律中性化（不臆測方向），
       吞例外續跑，絕不拖垮 daemon（外層另有 supervise() 崩潰隔離）。
    4. 不發 Telegram（純背景觀測）；顯示層函式只「回字串」供 daily macro 卡取用，
       由呼叫端決定是否顯示，本模組自身不推播。
    5. 誠實（紅線③）：無績效/勝率/年化字眼；分數是「盤面氛圍描述」非交易訊號，
       輸出帶誠實標註「影子觀測／非進場訊號」。
════════════════════════════════════════════════════════════════════════════

複用 Core 既有資料源（不重複打 CoinGlass）：
    * funding / OI / 清算 / 巨鯨 / ETF：經 daemon 主 source（CoinGlassSource，
      共用限流器 + TTL 快取）。同 (path,params) 在 TTL 內回上次成功值，吃掉重複。
    * DXY：tradfi（Yahoo Finance 免費，與 daily macro 同源）。
    * breadth：market_scanner.get_latest_breadth()（純讀 scanner.db）。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import sqlite3
import time

# JSONL sink 軟上限（位元組）；超過先輪替 .1 再重開，避免無限長（仿 convergence_shadow）
_SINK_MAX_BYTES = 5_000_000

# 分量權重（總和 = 1.0）。確定性、可調但目前固定；shadow 不影響任何下單故可自由校準。
_WEIGHTS = {
    "etf": 0.22,        # ETF 機構淨流（最強的趨勢資金訊號）
    "dxy": 0.18,        # 美元指數（升→風險資產逆風）
    "breadth": 0.16,    # 全市場廣度（risk-on/off 旗標來源）
    "funding": 0.14,    # 資金費率（過熱→偏空燃料）
    "oi": 0.12,         # 未平倉量趨勢
    "liquidation": 0.10,  # 清算失衡（空清算多→軋空燃料）
    "whales": 0.08,     # HL 巨鯨淨倉
}

# breadth<這個門檻 → 掛 risk_off 旗標
_BREADTH_RISKOFF = 35


# ===========================================================================
# 確定性規則：把每個分量原始值映射到 [-1, +1] 的「對多方燃料方向強度」。
# 全為純函式：零 I/O、零隨機、不改輸入。語意統一：+1 偏多/risk-on、-1 偏空。
# ===========================================================================
def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def score_etf(cum_7d_flow_usd) -> float:
    """ETF 近 7d 累積淨流 → [-1,+1]。淨流入加分、淨流出扣分。
    ±$2B（7d）視為滿格（近年 BTC 現貨 ETF 強週的量級）。
    """
    if not isinstance(cum_7d_flow_usd, (int, float)):
        return 0.0
    return _clamp(cum_7d_flow_usd / 2_000_000_000.0)


def score_dxy(dxy_change_pct) -> float:
    """美元指數變化% → [-1,+1]。DXY 升＝風險資產逆風（扣分，故反號）。
    ±2%（區間內變動）視為滿格。
    """
    if not isinstance(dxy_change_pct, (int, float)):
        return 0.0
    return _clamp(-dxy_change_pct / 2.0)


def score_breadth(b: dict | None) -> tuple[float, bool]:
    """全市場廣度 → ([-1,+1], risk_off_flag)。
    用 24h 漲跌家數淨佔比當方向；n_total<門檻或缺料 → (0.0, False) 不臆測。
    risk_off：1h 下跌家數佔比過半且 n_total 足夠 → True（風險趨避旗標）。
    """
    if not isinstance(b, dict):
        return 0.0, False
    n_total = b.get("n_total") or 0
    if n_total < 30:                      # 廣度樣本太少 → 中性，不掛旗標
        return 0.0, False
    up24 = b.get("n_up24h") or 0
    dn24 = b.get("n_down24h") or 0
    denom24 = up24 + dn24
    direction = ((up24 - dn24) / denom24) if denom24 > 0 else 0.0

    up1 = b.get("n_up1h") or 0
    dn1 = b.get("n_down1h") or 0
    denom1 = up1 + dn1
    dn_ratio_1h = (dn1 / denom1) if denom1 > 0 else 0.0
    # risk_off：1h 明顯偏空（下跌佔比≥65%）或廣度本身已低於門檻意義的 n_total
    risk_off = (dn_ratio_1h >= 0.65 and dn1 >= 15)
    return _clamp(direction), bool(risk_off)


def score_funding(avg_funding_8h) -> float:
    """平均資金費率（8h 小數，0.0009=0.09%）→ [-1,+1]。
    funding 翻正過熱＝多頭付錢＝過熱／回調風險 → 扣分（反號，與 convergence 一致）；
    funding 偏負＝空方付錢＝軋空燃料 → 加分。±0.05%(8h) 視為滿格。
    """
    if not isinstance(avg_funding_8h, (int, float)):
        return 0.0
    return _clamp(-avg_funding_8h / 0.0005)


def score_oi(oi_delta_pct) -> float:
    """OI 近期變化% → [-1,+1]。增倉視為趨勢動能（順方向加分的『絕對動能』分量；
    方向由其他分量決定，這裡只給『有沒有新資金進場』的溫和正權重）。
    純增倉 +、去槓桿 -。±10% 視為滿格。
    """
    if not isinstance(oi_delta_pct, (int, float)):
        return 0.0
    return _clamp(oi_delta_pct / 10.0)


def score_liquidation(long_liq_usd, short_liq_usd) -> float:
    """清算失衡 → [-1,+1]。空單清算遠大於多單＝軋空燃料（偏多 +）；
    多單清算遠大於空單＝多殺多（偏空 -）。用 (short-long)/(short+long)。
    """
    sl = short_liq_usd if isinstance(short_liq_usd, (int, float)) else 0.0
    ll = long_liq_usd if isinstance(long_liq_usd, (int, float)) else 0.0
    total = sl + ll
    if total <= 0:
        return 0.0
    return _clamp((sl - ll) / total)


def score_whales(net_long_pct) -> float:
    """巨鯨淨多比%（+100 全多 / -100 全空）→ [-1,+1]。直接線性映射。"""
    if not isinstance(net_long_pct, (int, float)):
        return 0.0
    return _clamp(net_long_pct / 100.0)


def compute_confluence(components: dict) -> dict:
    """把各分量原始輸入用確定性規則合成 macro_confluence_score + 明細。**純函式**。

    參數 components（各鍵皆可缺，缺則該分量中性化）：
        etf_cum_7d_flow_usd, dxy_change_pct, breadth(dict), avg_funding_8h,
        oi_delta_pct, liq_long_usd, liq_short_usd, whale_net_long_pct
    回傳
        macro_confluence_score: float ∈ [-100,+100]（+偏多/risk-on，-偏空/risk-off）
        components: {name: {raw, sub_score∈[-1,1], weight, contribution}}
        risk_off: bool（breadth 風險趨避旗標）
        n_present: 有明確（非中性 0）分量數
        bias: 'risk_on' | 'risk_off' | 'neutral'（依分數帶）

    ⚠️ 此分數為 SHADOW 專用：永不乘進/加進 strength_score、永不進 fire/下單。
    """
    c = components or {}
    breadth_score, risk_off = score_breadth(c.get("breadth"))

    subs = {
        "etf": (score_etf(c.get("etf_cum_7d_flow_usd")),
                c.get("etf_cum_7d_flow_usd")),
        "dxy": (score_dxy(c.get("dxy_change_pct")), c.get("dxy_change_pct")),
        "breadth": (breadth_score,
                    (c.get("breadth") or {}).get("n_total")
                    if isinstance(c.get("breadth"), dict) else None),
        "funding": (score_funding(c.get("avg_funding_8h")),
                    c.get("avg_funding_8h")),
        "oi": (score_oi(c.get("oi_delta_pct")), c.get("oi_delta_pct")),
        "liquidation": (score_liquidation(c.get("liq_long_usd"),
                                          c.get("liq_short_usd")),
                        {"long": c.get("liq_long_usd"),
                         "short": c.get("liq_short_usd")}),
        "whales": (score_whales(c.get("whale_net_long_pct")),
                   c.get("whale_net_long_pct")),
    }

    detail: dict[str, dict] = {}
    weighted_sum = 0.0
    n_present = 0
    for name, (sub, raw) in subs.items():
        w = _WEIGHTS.get(name, 0.0)
        contrib = sub * w
        weighted_sum += contrib
        if abs(sub) > 1e-9:
            n_present += 1
        detail[name] = {
            "raw": raw,
            "sub_score": round(sub, 4),
            "weight": w,
            "contribution": round(contrib, 4),
        }

    score = round(_clamp(weighted_sum, -1.0, 1.0) * 100, 2)
    if risk_off:
        bias = "risk_off"
    elif score >= 20:
        bias = "risk_on"
    elif score <= -20:
        bias = "risk_off"
    else:
        bias = "neutral"

    return {
        "macro_confluence_score": score,   # SHADOW only — 永不施用於 strength/fire
        "components": detail,
        "risk_off": bool(risk_off),
        "n_present": n_present,
        "bias": bias,
    }


# ===========================================================================
# 顯示層（純顯示）：把一輪結果組成「綜合分數儀表板」文字供 daily macro 卡顯示。
# 不過 LLM、不推播；任何缺料/錯誤回安全字串。帶誠實標註（紅線③）。
# ===========================================================================
def render_dashboard(summary: dict | None) -> str:
    """回「綜合宏觀儀表板」純文字。summary = compute_confluence(...) 之回傳
    （另可含 'ts'）。缺料/壞 dict → 回安全提示字串，永不 raise。
    """
    try:
        s = summary or {}
        score = s.get("macro_confluence_score")
        if not isinstance(score, (int, float)):
            return "📊 綜合宏觀儀表板：累積數據中…（影子觀測）"
        bias_zh = {"risk_on": "🟢 偏多 / Risk-On",
                   "risk_off": "🔴 偏空 / Risk-Off",
                   "neutral": "⚪ 中性"}.get(s.get("bias"), "⚪ 中性")
        comps = s.get("components") or {}
        name_zh = {"etf": "ETF淨流", "dxy": "美元DXY", "breadth": "市場廣度",
                   "funding": "資金費率", "oi": "未平倉OI",
                   "liquidation": "清算失衡", "whales": "巨鯨淨倉"}
        # 取貢獻度絕對值前 4 大分量列出（方向＋/−）
        ranked = sorted(
            ((name_zh.get(k, k), v.get("sub_score", 0.0))
             for k, v in comps.items() if isinstance(v, dict)),
            key=lambda kv: abs(kv[1]), reverse=True)
        parts = []
        for label, sub in ranked[:4]:
            if abs(sub) < 1e-9:
                continue
            arrow = "▲" if sub > 0 else "▼"
            parts.append(f"{label}{arrow}{abs(sub):.2f}")
        drivers = "　".join(parts) if parts else "各分量皆中性"
        riskoff_tag = "　⚠️廣度risk-off旗標" if s.get("risk_off") else ""
        n_present = s.get("n_present", 0)
        return (
            f"📊 <b>綜合宏觀儀表板（影子觀測，非進場訊號）</b>\n"
            f"　綜合分數 <code>{score:+.1f}</code>／100　{bias_zh}"
            f"（{n_present} 個分量在線）{riskoff_tag}\n"
            f"　主導分量：{drivers}\n"
            f"　<i>※ 確定性規則合成；永不影響訊號/下單，僅供盤面氛圍參考。</i>"
        )
    except Exception:
        return "📊 綜合宏觀儀表板：暫時無法顯示（影子觀測）"


# ===========================================================================
# 影子 JSONL sink
# ===========================================================================
def _sink_path():
    from botpaths import data_dir
    return data_dir() / "macro_confluence.jsonl"


def _append_jsonl(record: dict) -> None:
    """把一輪觀測寫一行 JSONL（純本地檔；超過軟上限就輪替一次）。失敗吞掉。"""
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
        pass


# ===========================================================================
# history-logger：每小時把當下 OI/CVD/funding 最近 500 根快照持續累積寫 SQLite。
# ---------------------------------------------------------------------------
# 誠實標明：這是「從今天起往前累積」的未來歷史。CoinGlass history 端點硬卡 500
# 根、present-anchored、無時間分頁 → 補不回過去；本表只負責「從現在開始，每小時
# 落一次盤，日積月累出跨年綜合歷史」。每筆標 captured_at（落盤時刻）。
# 用獨立 DB 檔（macro_history.db），不碰 trade_journal.db（影子資料不入帳本）。
# ===========================================================================
def _history_db_path():
    from botpaths import db_path as _db_path
    return _db_path("macro_history.db")


def _hist_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_history_db_path(), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_history_db() -> None:
    """建影子歷史表（冪等）。metric ∈ {oi, cvd, funding}；bar_ts=該根原始時間戳。
    主鍵 (symbol,metric,bar_ts) → 同一根重抓 INSERT OR IGNORE 不重複累積。
    captured_at＝本機落盤毫秒（誠實標『何時開始累積』）。
    """
    conn = _hist_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS macro_metric_history (
                symbol TEXT NOT NULL,
                metric TEXT NOT NULL,        -- 'oi' | 'cvd' | 'funding'
                bar_ts INTEGER NOT NULL,     -- 該根原始時間戳（ms/s 依源；原樣保存）
                value REAL,
                interval TEXT,               -- 抓取視窗（如 '1h'）
                captured_at INTEGER NOT NULL,  -- 本機落盤毫秒（未來歷史起算點）
                PRIMARY KEY (symbol, metric, bar_ts)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mmh_metric_ts "
            "ON macro_metric_history(metric, bar_ts)")
    finally:
        conn.close()


def _persist_snapshot(symbol: str, metric: str,
                      series: list[dict], interval: str = "1h") -> int:
    """把一條 {ts,value} 序列（最多 500 根）INSERT OR IGNORE 累積。回實際新增筆數。
    series 元素需含 'ts' + ('value' 或 cvd 用 'value')；缺值 row 跳過。失敗回 0。
    """
    if not series:
        return 0
    now_ms = int(time.time() * 1000)
    rows = []
    for pt in series[-500:]:
        if not isinstance(pt, dict):
            continue
        ts = pt.get("ts")
        val = pt.get("value")
        if ts is None or not isinstance(val, (int, float)):
            continue
        rows.append((symbol, metric, int(ts), float(val), interval, now_ms))
    if not rows:
        return 0
    conn = _hist_conn()
    try:
        cur = conn.executemany(
            "INSERT OR IGNORE INTO macro_metric_history "
            "(symbol, metric, bar_ts, value, interval, captured_at) "
            "VALUES (?,?,?,?,?,?)", rows)
        return cur.rowcount if cur.rowcount is not None else 0
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


async def _log_history(source, symbols=("BTC", "ETH", "SOL")) -> dict:
    """對 symbols 抓 OI/CVD/funding 各 ≤500 根並累積進 macro_history.db。
    複用 daemon 主 source（共用限流器 + TTL 快取）。任何源失敗該項跳過、不 raise。
    回 {"inserted": n, "errors": [...]}（觀測用統計）。
    """
    init_history_db()
    inserted = 0
    errors: list[str] = []
    if source is None:
        return {"inserted": 0, "errors": ["no source"]}
    for sym in symbols:
        # OI（1h，多拉以盡量補滿 500 根）
        try:
            r = await source.get_oi(sym, "1h", 500)
            if isinstance(r, dict) and not r.get("error"):
                inserted += _persist_snapshot(sym, "oi", r.get("series") or [], "1h")
        except Exception as e:
            errors.append(f"{sym}/oi:{type(e).__name__}")
        # CVD（1h）
        try:
            r = await source.get_cvd_series(sym, "1h", 500)
            if isinstance(r, dict) and not r.get("error"):
                inserted += _persist_snapshot(sym, "cvd", r.get("series") or [], "1h")
        except Exception as e:
            errors.append(f"{sym}/cvd:{type(e).__name__}")
        # funding（1h 序列）
        try:
            r = await source.get_funding_series(sym, "1h", 500)
            if isinstance(r, dict) and not r.get("error"):
                inserted += _persist_snapshot(sym, "funding",
                                              r.get("series") or [], "1h")
        except Exception as e:
            errors.append(f"{sym}/funding:{type(e).__name__}")
    return {"inserted": inserted, "errors": errors}


# ===========================================================================
# 蒐料（I/O 薄層）：複用 Core 既有源，缺料一律 None / 中性化，絕不 raise。
# ===========================================================================
async def _collect_components(source) -> dict:
    """蒐集合成所需的各分量原始值。任何單項失敗 → 該鍵缺/None（compute 端中性化）。

    複用既有源（不重複打 CoinGlass）：
        ETF：source.get_etf_flows('BTC',7)（共用限流器 + TTL 快取）
        funding/OI/清算：source（BTC 代理整體加密 risk 氛圍）
        巨鯨：source.get_hyperliquid_whales()
        DXY：tradfi（Yahoo Finance）
        breadth：market_scanner.get_latest_breadth()（純讀 scanner.db）
    """
    out: dict = {}
    if source is None:
        try:
            from market_intel_mcp.sources import get_source
            source = get_source()
        except Exception:
            source = None

    # --- breadth（純讀本地 scanner.db，最便宜，先拿）---
    try:
        from l3_dispatcher.market_scanner import get_latest_breadth
        b = get_latest_breadth()
        if isinstance(b, dict):
            out["breadth"] = b
            af = b.get("avg_funding")
            if isinstance(af, (int, float)):
                out["avg_funding_8h"] = af   # breadth 已含全市場均資費，免再打
    except Exception:
        pass

    # --- DXY（tradfi）---
    try:
        from market_intel_mcp.sources.tradfi import get_tradfi
        # DX=F 24h 期貨優先（全天候），缺則 DX-Y.NYB
        for tk in ("DX=F", "DX-Y.NYB"):
            r = await get_tradfi().get_ticker(tk)
            if isinstance(r, dict) and not r.get("error"):
                out["dxy_change_pct"] = r.get("change_1d_pct")
                break
    except Exception:
        pass

    if source is None:
        return out

    # --- ETF 7d 累積淨流（BTC 為主流代理）---
    try:
        r = await source.get_etf_flows("BTC", 7)
        if isinstance(r, dict) and not r.get("error"):
            out["etf_cum_7d_flow_usd"] = r.get("cumulative_7d_flow_usd")
    except Exception:
        pass

    # --- funding（若 breadth 沒給均資費，退而用 BTC funding 代理）---
    if "avg_funding_8h" not in out:
        try:
            r = await source.get_funding("BTC")
            if isinstance(r, dict) and not r.get("error"):
                out["avg_funding_8h"] = r.get("funding")
        except Exception:
            pass

    # --- OI 24h 變化%（BTC 代理整體槓桿動能）---
    try:
        r = await source.get_oi("BTC", "1h", 24)
        if isinstance(r, dict) and not r.get("error"):
            out["oi_delta_pct"] = r.get("delta_pct_24h")
    except Exception:
        pass

    # --- 清算失衡（BTC 近 24h 多/空清算 USD）---
    try:
        r = await source.get_liquidations("BTC", "24h")
        if isinstance(r, dict) and not r.get("error"):
            out["liq_long_usd"] = r.get("liq_long")
            out["liq_short_usd"] = r.get("liq_short")
    except Exception:
        pass

    # --- 巨鯨（HL）BTC 淨多比 ---
    try:
        r = await source.get_hyperliquid_whales(50)
        if isinstance(r, dict) and not r.get("error"):
            for it in (r.get("per_symbol_aggregate") or []):
                if (it.get("symbol") or "").upper() in ("BTC", "BTCUSDT", "XBT"):
                    out["whale_net_long_pct"] = it.get("net_long_pct")
                    break
    except Exception:
        pass

    return out


async def _run_cycle(source=None) -> dict:
    """跑一輪：蒐料 → 確定性合成 → 順手累積一次歷史快照 → 回可序列化摘要 dict。

    ⚠️ 全程影子：回傳/落盤的分數從不施用於 strength/fire/下單。
    """
    components = await _collect_components(source)
    summary = compute_confluence(components)
    summary["ts"] = dt.datetime.now(tz=dt.timezone.utc).isoformat()
    summary["note"] = ("shadow-only: macro_confluence_score 從不施用於 "
                       "strength_score/fire/下單；確定性規則合成，僅供 A/B 觀測。")

    # 順手累積一次歷史快照（未來歷史；補不回過去，誠實標 captured_at）
    try:
        hist = await _log_history(source)
        summary["history_inserted"] = hist.get("inserted", 0)
    except Exception as e:
        summary["history_inserted"] = 0
        summary["history_error"] = f"{type(e).__name__}: {e}"

    return summary


async def run_macro_confluence_loop(source=None, interval_seconds: int = 3600):
    """Session C 綜合宏觀合成常駐迴圈（每 interval 跑一輪，純觀測寫 JSONL + SQLite）。

    `source`＝daemon 主 source（與其他 worker 簽名一致），複用其限流器/TTL 快取；
    None → 延遲 get_source()（供一次性測試）。

    影子鐵則：永不影響 strength/fire/下單、不發 Telegram、整輪包 try/except 續跑。
    用 asyncio.sleep 讓出事件迴圈，不吞例外成 busy-loop。
    """
    # 啟動稍緩，避開開機尖峰（讓 daily macro / 掃描先跑）
    await asyncio.sleep(90)
    while True:
        try:
            summary = await _run_cycle(source)
            _append_jsonl(summary)
            print(f"[macro_confluence] score={summary.get('macro_confluence_score')} "
                  f"bias={summary.get('bias')} "
                  f"n_present={summary.get('n_present')} "
                  f"risk_off={summary.get('risk_off')} "
                  f"hist+{summary.get('history_inserted', 0)}")
        except Exception as e:  # 整輪保護：任何意外吞掉續跑，不拖垮 daemon
            print(f"[macro_confluence] cycle error: {type(e).__name__}: {e}")
        # 確定性間隔睡眠（>=60s），讓出事件迴圈，非 busy-loop
        await asyncio.sleep(max(60, int(interval_seconds)))
