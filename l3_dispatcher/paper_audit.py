"""逐筆重算稽核（v37 / Task #4 的「免金鑰」半）。

目的（回應使用者核心關切「看持倉績效這邊是不是有作假」）：
    用「交易所 API 逐筆重算」獨立查核每一筆**已平倉**紙上單的結果是否屬實。
    這正是 ledger_anchor.py 誠實聲明裡寫明、但尚未實作的另一半：
        「OpenTimestamps 只證明資料未被竄改，【不】證明內容為真；
         內容真實性需另以 OKX 模擬盤對帳 / 交易所 API 逐筆重算佐證。」

    本檔做的是後者，而且**完全不需任何 API key**（OKX 公開 K 線端點）、
    不下任何單、純讀本地帳本 + 公開歷史 K 線 → 安全、可離線驗證。

兩道獨立檢查（都不是「再跑一次 monitor」那種循環自證）：

  (1) 算術自洽（離線、永遠可跑）：
      用帳本「自己記錄的腿」（legs_hit）+ 進出場價 + 分批比例，重算 realized_r，
      與帳本存的 realized_r 比對。對不上 = 算錯或被竄改 → FLAG。
      （含 timeout 腿者其出場價未存，離線無法重算該腿 → 交給 (2) 用 K 線估。）

  (2) 可達性查核（需公開 K 線）：
      抓該筆 [進場, 出場] 窗口的真實 OKX K 線，取窗口 high/low。
      帳本若聲稱「打到某個 TP/SL 價」，但那個價在整個窗口內**從未被觸及** →
      這是不可能成交、疑似捏造 → FLAG。
      （盤中先後次序的樂觀性是 monitor 已知行為，不算造假，只標 INFO/WARN。）

判讀分級：
    FLAG  = 硬傷：聲稱的價位不可達，或 realized_r 與自身腿算不出來。
    WARN  = 需人看：無 K 線可查（窗口太舊/抓不到）、或 R 落在合理帶之外但腿可達。
    OK    = 兩道檢查都過。

用法：
    python -m l3_dispatcher.paper_audit --selftest     # 離線單元測試（無網路）
    python -m l3_dispatcher.paper_audit --run -n 50    # 對最近 50 筆已平倉單跑真稽核
    python -m l3_dispatcher.paper_audit                 # 同 --run，預設參數
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field

from botconfig import CONFIG
from botpaths import db_path

# v56：Windows 主控台預設 cp950，--selftest 印 emoji/繁中會 UnicodeEncodeError → 強制 UTF-8
# （與 champion_challenger.py 同口徑；只影響互動式自測輸出，不影響稽核正確性/daemon）
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

DB_PATH = db_path("trade_journal.db")

# 分批比例與時限 — 與 trade_monitor 同源（botconfig），確保重算規則一致
SPLITS = {"tp1": CONFIG.tp_size_split[0],
          "tp2": CONFIG.tp_size_split[1],
          "tp3": CONFIG.tp_size_split[2]}
R_TOL = 0.02  # realized_r 容差（純算術應到小數點，留一點 rounding 餘裕）

DEFAULT_HOLD_H = int(os.getenv("HOLD_MAX_HOURS", "48"))
HOLD_BY_SETUP = {"us_breakout": 24}  # 與 trade_monitor.HOLD_MAX_BY_SETUP 一致

# 各時框一根的毫秒長度（抓窗口 K 線時的緩衝用）
_BAR_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
}


def _hold_hours(setup: str) -> int:
    return HOLD_BY_SETUP.get(setup, DEFAULT_HOLD_H)


# ── 1. 讀已平倉紙上單 ────────────────────────────────────────────
def load_closed(limit: int = 50, days: int = 120) -> list[dict]:
    """讀最近 N 筆已平倉紙上單（排除 entry_expired：那是掛單從未成交、非真實交易）。"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=8000")
    cutoff = int(time.time() * 1000) - days * 86400 * 1000
    # task#53(step8): 帶上 tp_alloc 供 recompute_r 用該筆凍結分配回算 timeout 腿。
    # 舊庫無此欄 → OperationalError 退回不含 tp_alloc 的查詢（has_alloc=False，沿用預設）。
    _base = ("id, symbol, setup, direction, entry_price, stop_price, "
             "tp1, tp2, tp3, entry_at, exit_at, legs_hit, exit_reason, "
             "realized_r, pnl_usd, entry_filled_pct")
    _where = ("FROM paper_trades "
              "WHERE status='closed' AND IFNULL(exit_reason,'') != 'entry_expired' "
              "AND entry_at >= ? ORDER BY exit_at DESC LIMIT ?")
    has_alloc = True
    try:
        try:
            rows = conn.execute(f"SELECT {_base}, tp_alloc {_where}",
                                (cutoff, limit)).fetchall()
        except sqlite3.OperationalError:
            has_alloc = False
            rows = conn.execute(f"SELECT {_base} {_where}",
                                (cutoff, limit)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        out.append({
            "id": r[0], "symbol": r[1], "setup": r[2], "direction": r[3],
            "entry_price": r[4], "stop_price": r[5],
            "tp1": r[6], "tp2": r[7], "tp3": r[8],
            "entry_at": r[9], "exit_at": r[10],
            "legs_hit": r[11] or "", "exit_reason": r[12],
            "realized_r": r[13] if r[13] is not None else 0.0,
            "pnl_usd": r[14] if r[14] is not None else 0.0,
            "entry_filled_pct": r[15] if r[15] is not None else 1.0,
            "tp_alloc": (r[16] if has_alloc else None),
        })
    return out


# ── 2. 算術自洽重算（離線）──────────────────────────────────────
def _resolve_splits(trade: dict) -> dict:
    """task#53(step8)：取該筆凍結的 TP 分配（進場時 auto_param_store 覆寫值），用於重算。

    回 {"tp1":a,"tp2":b,"tp3":c}；缺失/壞值 → 預設 SPLITS（＝今日行為，完全向後相容）。
    為何重要：timeout 腿的 leg_r 是從帳本 realized_r 反推的，反推時用的分批比例若與
    當初記帳所用不符，會誤判竄改。故必須用「該筆當初凍結的分配」回算。
    """
    raw = trade.get("tp_alloc")
    if not raw:
        return SPLITS
    try:
        vals = json.loads(raw) if isinstance(raw, str) else raw
        if (not isinstance(vals, (list, tuple)) or len(vals) != 3
                or any((v is None or float(v) < 0) for v in vals)
                or abs(sum(float(v) for v in vals) - 1.0) > 1e-3):
            return SPLITS
        return {"tp1": float(vals[0]), "tp2": float(vals[1]), "tp3": float(vals[2])}
    except Exception:
        return SPLITS


def recompute_r(trade: dict, timeout_price: float | None = None) -> dict:
    """用帳本自己記錄的腿重算 realized_r。

    回 {recomputed_r, unverifiable, legs:[(leg,size,leg_r)], remaining_after}。
    unverifiable=True 代表有 timeout 腿且未提供 timeout_price（離線無法算該腿）。
    """
    entry = trade["entry_price"]
    stop = trade["stop_price"]
    sl_dist = abs(entry - stop)
    is_bull = trade["direction"] == "bull"
    filled = trade["entry_filled_pct"] or 1.0
    legs = [x for x in (trade["legs_hit"] or "").split(",") if x]
    px_of = {"tp1": trade["tp1"], "tp2": trade["tp2"], "tp3": trade["tp3"],
             "stop": stop, "timeout": timeout_price}

    if sl_dist <= 0:
        return {"recomputed_r": None, "unverifiable": True,
                "legs": [], "remaining_after": 1.0, "bad_sl_dist": True}

    splits = _resolve_splits(trade)   # task#53: 用該筆凍結分配（缺→預設 SPLITS）
    remaining = 1.0
    r_sum = 0.0            # 所有「價格精確」腿（tp/stop，或有給 timeout_price 時的 timeout）的 R 和
    nt_r_sum = 0.0         # 僅「非 timeout」腿的精確 R 和（供 timeout 隱含出場價回推用）
    timeout_size = 0.0
    used = []
    unverifiable = False
    has_timeout = False
    for leg in legs:
        if leg in ("tp1", "tp2", "tp3"):
            size = splits[leg]
        elif leg in ("stop", "timeout"):
            size = round(remaining, 3)  # 剩餘全平
        else:
            continue
        if leg == "timeout":
            has_timeout = True
            timeout_size = size
        px = px_of.get(leg)
        if px is None:
            unverifiable = True
            remaining -= size
            used.append((leg, size, None))
            continue
        leg_r = ((px - entry) if is_bull else (entry - px)) / sl_dist
        r_sum += size * leg_r
        if leg != "timeout":
            nt_r_sum += size * leg_r
        remaining -= size
        used.append((leg, size, round(leg_r, 4)))

    rec = None if unverifiable else round(r_sum * filled, 3)
    return {"recomputed_r": rec, "unverifiable": unverifiable,
            "legs": used, "remaining_after": round(remaining, 3),
            # 給 timeout 路徑用：非 timeout 腿的精確貢獻（已乘 filled）+ timeout 腿大小
            "nontimeout_r": round(nt_r_sum * filled, 6),
            "has_timeout": has_timeout, "timeout_size": timeout_size,
            "filled": filled, "sl_dist": sl_dist, "is_bull": is_bull,
            "entry": entry}


# ── 3. 可達性查核（需公開 K 線）────────────────────────────────
def _price_at(candles: list[dict], ts: int) -> float | None:
    """回時間 ts 所落 K 棒的收盤價（找 ts 之前最後一根；皆在 ts 之後則取第一根）。"""
    best = None
    for c in candles:
        if c["ts"] <= ts:
            best = c
        else:
            if best is None:
                best = c
            break
    return best["close"] if best else None


def check_timeout_exit(trade: dict, rec: dict, candles: list[dict]) -> dict:
    """timeout 腿專用獨立查核（純函式）。

    timeout 出場價 = 平倉當下市價，帳本未存 → 無法逐分錢重算。
    改從 stored realized_r 回推「隱含的 timeout 出場價」，驗證它是否真的落在
    窗口 K 線 [low, high] 內。不在範圍 = 那個價從未出現過 = 不可能成交 = 造假。

    回 {status, implied_exit, reachable, low, high}。
    status: 'ok'(可查) / 'no_candles' / 'no_timeout'。
    """
    if not rec.get("has_timeout"):
        return {"status": "no_timeout"}
    denom = rec["timeout_size"] * (rec["filled"] or 1.0)
    if denom <= 0:
        return {"status": "no_timeout"}
    residual_r = trade["realized_r"] - rec["nontimeout_r"]
    leg_r_t = residual_r / denom
    implied = (rec["entry"] + leg_r_t * rec["sl_dist"]) if rec["is_bull"] \
        else (rec["entry"] - leg_r_t * rec["sl_dist"])
    if not candles:
        return {"status": "no_candles", "implied_exit": implied}
    hi = max(c["high"] for c in candles)
    lo = min(c["low"] for c in candles)
    eps = 0.003 * abs(implied)  # 容 wick 邊界與「+Nh 才 poll 到」的時點滑移
    return {"status": "ok", "implied_exit": implied,
            "reachable": (lo - eps <= implied <= hi + eps),
            "low": lo, "high": hi}


def reachability(trade: dict, candles: list[dict]) -> dict:
    """檢查帳本聲稱打到的每個 TP/SL 價，是否真的在窗口 K 線 high/low 範圍內被觸及。"""
    if not candles:
        return {"status": "no_candles", "unreachable": []}
    hi = max(c["high"] for c in candles)
    lo = min(c["low"] for c in candles)
    is_bull = trade["direction"] == "bull"
    legs = [x for x in (trade["legs_hit"] or "").split(",") if x]
    bad = []
    for leg in legs:
        if leg in ("tp1", "tp2", "tp3"):
            px = trade[leg]
            if px is None:
                continue
            reached = (hi >= px) if is_bull else (lo <= px)
            if not reached:
                bad.append(f"{leg}={px:g} 窗口內未觸及(high={hi:g}/low={lo:g})")
        elif leg == "stop":
            px = trade["stop_price"]
            reached = (lo <= px) if is_bull else (hi >= px)
            if not reached:
                bad.append(f"stop={px:g} 窗口內未觸及(high={hi:g}/low={lo:g})")
        # timeout 腿：只看時間不看價，不在此查
    return {"status": "ok", "window_high": hi, "window_low": lo,
            "unreachable": bad}


# ── 4. 窗口歷史 K 線抓取（OKX 公開 history-candles，免 key）─────
async def fetch_window(src, symbol: str, start_ms: int, end_ms: int,
                       bar: str = "15m", max_pages: int = 16) -> list[dict]:
    """抓 [start_ms, end_ms] 窗口的 K 線。OKX history-candles 一頁 100 根、降序，
    用 `after` 游標往更舊翻頁，直到覆蓋 start_ms 或達頁數上限。回升序 list。"""
    from market_intel_mcp.sources.okx_candles import OKX_INTERVAL
    inst = src._to_inst(symbol)
    okx_bar = OKX_INTERVAL.get(bar.lower(), bar)
    bar_ms = _BAR_MS.get(bar.lower(), 900_000)
    collected: dict[int, dict] = {}
    after = end_ms + bar_ms  # history-candles：回傳「早於 after」的資料
    for _ in range(max_pages):
        try:
            r = await src.client.get(
                "/api/v5/market/history-candles",
                params={"instId": inst, "bar": okx_bar,
                        "after": str(after), "limit": "100"},
            )
        except Exception:
            break
        if r.status_code != 200:
            break
        try:
            body = r.json()
        except Exception:
            break
        if body.get("code") != "0":
            break
        rows = body.get("data") or []
        if not rows:
            break
        for row in rows:
            try:
                ts = int(row[0])
                collected[ts] = {"ts": ts, "open": float(row[1]),
                                 "high": float(row[2]), "low": float(row[3]),
                                 "close": float(row[4])}
            except (TypeError, ValueError, IndexError):
                continue
        oldest = min(int(row[0]) for row in rows)
        if oldest <= start_ms:
            break
        after = oldest
        await asyncio.sleep(0.12)  # 對端點客氣（公開端點 rate limit 寬鬆）
    # 取窗口（含一根緩衝，因 K 棒 high/low 覆蓋 [ts, ts+bar)）
    lo_b = start_ms - bar_ms
    return [collected[k] for k in sorted(collected)
            if lo_b <= collected[k]["ts"] <= end_ms]


# ── 5. 單筆稽核 ─────────────────────────────────────────────────
@dataclass
class Finding:
    trade_id: int
    symbol: str
    setup: str
    stored_r: float
    recomputed_r: float | None
    verdict: str = "ok"          # ok / warn / flag
    reasons: list[str] = field(default_factory=list)
    window_candles: int = 0


async def audit_one(trade: dict, src, bar: str = "15m") -> Finding:
    f = Finding(trade_id=trade["id"], symbol=trade["symbol"],
                setup=trade["setup"], stored_r=round(trade["realized_r"], 3),
                recomputed_r=None)

    candles = []
    if src is not None:
        candles = await fetch_window(src, trade["symbol"], trade["entry_at"],
                                     trade["exit_at"] or trade["entry_at"], bar)
    f.window_candles = len(candles)

    rec = recompute_r(trade)  # 不餵估計 timeout 價：timeout 改用「回推隱含出場價」查
    f.recomputed_r = rec["recomputed_r"]

    if rec.get("bad_sl_dist"):
        f.verdict = "flag"
        f.reasons.append("entry==stop（SL 距離為 0，資料異常）")
        return f

    if not rec["has_timeout"]:
        # (1a) 純 tp/stop 腿 → 出場價精確 → realized_r 可逐分錢重算，容差極小
        if abs(rec["recomputed_r"] - trade["realized_r"]) > R_TOL:
            f.verdict = "flag"
            f.reasons.append(
                f"realized_r 帳本存 {trade['realized_r']:+.3f}，但由其自身腿重算為 "
                f"{rec['recomputed_r']:+.3f}（差 {rec['recomputed_r']-trade['realized_r']:+.3f}R，疑算錯/竄改）")
    else:
        # (1b) 含 timeout 腿：出場價=當下市價（帳本未存），無法逐分錢重算。
        #      改做更強的獨立查核——回推「隱含 timeout 出場價」，驗證它確實落在
        #      窗口 K 線 [low, high] 內（不可達=不可能成交=造假）。
        to = check_timeout_exit(trade, rec, candles)
        if to["status"] == "ok":
            if not to["reachable"]:
                f.verdict = "flag"
                f.reasons.append(
                    f"timeout 隱含出場價 {to['implied_exit']:.4g} 超出窗口K線範圍 "
                    f"[{to['low']:g}, {to['high']:g}]（不可能成交，疑造假）")
            else:
                f.reasons.append(
                    f"timeout 出場價回推 {to['implied_exit']:.4g} 落在窗口 "
                    f"[{to['low']:g}, {to['high']:g}] 內（合理）")
        elif to["status"] == "no_candles":
            f.verdict = "warn"
            f.reasons.append("含 timeout 腿且窗口無 K 線可查，無法獨立查核出場價")

    # (2) 可達性：所有聲稱打到的 tp/stop 價，必須真的在窗口被觸及
    if candles:
        reach = reachability(trade, candles)
        for bad in reach["unreachable"]:
            f.verdict = "flag"
            f.reasons.append("不可達：" + bad)
    elif f.verdict == "ok":
        f.verdict = "warn"
        f.reasons.append("窗口無 K 線（可能太舊或抓取失敗），僅算術檢查通過")

    if f.verdict == "ok" and not f.reasons:
        f.reasons.append("算術自洽 + 聲稱價位皆可達")
    return f


async def audit_recent(n: int = 50, bar: str = "15m", days: int = 120,
                       fetch: bool = True) -> list[Finding]:
    trades = load_closed(n, days)
    src = None
    if fetch:
        from market_intel_mcp.sources.okx_candles import OkxCandlesSource
        src = OkxCandlesSource()
    findings = []
    try:
        for t in trades:
            findings.append(await audit_one(t, src, bar))
    finally:
        if src is not None:
            await src.close()
    return findings


# ── 6. 報告 ─────────────────────────────────────────────────────
def render_audit_report(findings: list[Finding], html: bool = True) -> str:
    b = (lambda s: f"<b>{s}</b>") if html else (lambda s: s)
    code = (lambda s: f"<code>{s}</code>") if html else (lambda s: s)
    n = len(findings)
    if n == 0:
        return "🔬 逐筆重算稽核：尚無已平倉紙上單可查。"
    n_ok = sum(1 for f in findings if f.verdict == "ok")
    n_warn = sum(1 for f in findings if f.verdict == "warn")
    n_flag = sum(1 for f in findings if f.verdict == "flag")
    lines = [
        b("🔬 持倉績效逐筆重算稽核（免金鑰・OKX 公開K線獨立查核）"),
        f"查核 {code(n)} 筆已平倉紙上單："
        f"✅ 自洽 {code(n_ok)}　⚠️ 待查 {code(n_warn)}　🚩 異常 {code(n_flag)}",
    ]
    flagged = [f for f in findings if f.verdict == "flag"]
    warned = [f for f in findings if f.verdict == "warn"]
    if flagged:
        lines.append("")
        lines.append(b("🚩 異常（疑造假/算錯，需立即查）："))
        for f in flagged[:10]:
            lines.append(f"  #{f.trade_id} {f.symbol}（存 {f.stored_r:+.2f}R）"
                         f"：{'；'.join(f.reasons)}")
    if warned:
        lines.append("")
        lines.append(f"⚠️ 待查 {len(warned)} 筆（多為窗口太舊抓不到K線）：" +
                     "、".join(f"#{f.trade_id}{f.symbol}" for f in warned[:12]))
    if not flagged:
        lines.append("")
        lines.append("✅ 無造假跡象：所有可查窗口內，聲稱的進出價皆真實觸及，"
                     "且 realized_r 與自身記錄的腿一致。")
    return "\n".join(lines)


# ── 7. 離線自測（無網路）+ CLI ─────────────────────────────────
def _selftest() -> int:
    """用合成 K 線與合成交易驗證稽核邏輯（不連網）。回失敗數。"""
    fails = 0

    def chk(name, cond):
        nonlocal fails
        status = "ok " if cond else "FAIL"
        if not cond:
            fails += 1
        print(f"  [{status}] {name}")

    # --- 乾淨的多單：tp1+tp2+stop，價位都可達，R 自洽 ---
    # entry=100 stop=90 (sl=10); tp1=110 tp2=120 tp3=140
    # legs tp1,tp2,stop → r = 0.5*(110-100)/10 + 0.3*(120-100)/10 + 0.2*(90-100)/10
    #     = 0.5*1.0 + 0.3*2.0 + 0.2*(-1.0) = 0.5+0.6-0.2 = 0.9
    clean = {"id": 1, "symbol": "BTC", "setup": "intraday", "direction": "bull",
             "entry_price": 100, "stop_price": 90, "tp1": 110, "tp2": 120,
             "tp3": 140, "entry_at": 0, "exit_at": 10, "legs_hit": "tp1,tp2,stop",
             "exit_reason": "stop", "realized_r": 0.9, "pnl_usd": 90,
             "entry_filled_pct": 1.0}
    rec = recompute_r(clean)
    chk("乾淨單算術重算=0.9", rec["recomputed_r"] == 0.9)

    # 可達性：窗口 high 到 125、low 到 88 → tp1/tp2/stop 皆可達，tp3=140 未聲稱
    candles_ok = [{"ts": 0, "open": 100, "high": 125, "low": 88, "close": 95}]
    reach = reachability(clean, candles_ok)
    chk("乾淨單可達性無異常", reach["unreachable"] == [])

    # --- 竄改 realized_r：腿一樣但 stored 灌水成 2.5R ---
    tampered = dict(clean, id=2, realized_r=2.5, pnl_usd=250)
    rec_t = recompute_r(tampered)
    chk("竄改單重算仍=0.9（與灌水值不符）", rec_t["recomputed_r"] == 0.9)
    chk("竄改單偵測到 R 不符", abs(rec_t["recomputed_r"] - tampered["realized_r"]) > R_TOL)

    # --- 捏造不可達 TP：聲稱 tp3=140 打到，但窗口 high 只到 125 ---
    faketp = dict(clean, id=3, legs_hit="tp1,tp2,tp3", exit_reason="tp3",
                  realized_r=0.5*1.0 + 0.3*2.0 + 0.2*4.0)  # 1.9，算術自洽
    rec_f = recompute_r(faketp)
    chk("捏造TP單算術自洽(1.9)", abs(rec_f["recomputed_r"] - 1.9) < 1e-9)
    reach_f = reachability(faketp, candles_ok)  # high=125 < tp3=140
    chk("捏造TP單可達性抓到不可達", any("tp3" in u for u in reach_f["unreachable"]))

    # --- 空單對稱：entry=100 stop=110 (sl=10); tp1=90 → low 需 <=90 ---
    bear = {"id": 4, "symbol": "ETH", "setup": "ambush", "direction": "bear",
            "entry_price": 100, "stop_price": 110, "tp1": 90, "tp2": 80,
            "tp3": 70, "entry_at": 0, "exit_at": 10, "legs_hit": "tp1,stop",
            "exit_reason": "stop", "realized_r": 0.5*1.0 + 0.5*(-1.0),
            "pnl_usd": 0, "entry_filled_pct": 1.0}
    rec_b = recompute_r(bear)  # 0.5*(100-90)/10 + 0.5*(100-110)/10 = 0.5-0.5 = 0.0
    chk("空單算術重算=0.0", abs(rec_b["recomputed_r"]) < 1e-9)
    reach_b = reachability(bear, [{"ts": 0, "open": 100, "high": 112,
                                   "low": 89, "close": 105}])
    chk("空單可達性無異常", reach_b["unreachable"] == [])
    # 空單捏造：tp1=90 但 low 只到 95 → 不可達
    reach_b2 = reachability(bear, [{"ts": 0, "open": 100, "high": 112,
                                    "low": 95, "close": 105}])
    chk("空單抓到 tp1 不可達", any("tp1" in u for u in reach_b2["unreachable"]))

    # --- 部分進場縮放：filled_pct=0.6 → R 應乘 0.6 ---
    partial = dict(clean, id=5, entry_filled_pct=0.6, realized_r=round(0.9*0.6, 3))
    rec_p = recompute_r(partial)
    chk("部分進場R縮放正確", rec_p["recomputed_r"] == round(0.9 * 0.6, 3))

    # --- timeout 腿：無 K 線→unverifiable；給 timeout_price→可算 ---
    tmo = {"id": 6, "symbol": "SOL", "setup": "intraday", "direction": "bull",
           "entry_price": 100, "stop_price": 90, "tp1": 110, "tp2": 120,
           "tp3": 140, "entry_at": 0, "exit_at": 10, "legs_hit": "tp1,timeout",
           "exit_reason": "timeout", "realized_r": 0.0, "pnl_usd": 0,
           "entry_filled_pct": 1.0}
    rec_tmo = recompute_r(tmo)  # 無 timeout_price
    chk("timeout 腿離線標 unverifiable", rec_tmo["unverifiable"] is True)
    rec_tmo2 = recompute_r(tmo, timeout_price=105)
    # 0.5*(110-100)/10 + 0.5*(105-100)/10 = 0.5 + 0.25 = 0.75
    chk("timeout 給價後可重算=0.75", rec_tmo2["recomputed_r"] == 0.75)

    # --- timeout 隱含出場價回推 + 可達性（修掉假陽性的關鍵）---
    # tmo: tp1 已 hit (0.5)，timeout 平剩 0.5。stored_r=0.75 →
    #   非timeout貢獻 = 0.5*1.0 = 0.5；residual = 0.75-0.5 = 0.25；
    #   timeout leg_r = 0.25/0.5 = 0.5 → 隱含出場價 = 100 + 0.5*10 = 105
    rec_to = recompute_r(tmo)
    chk("timeout 非腿貢獻=0.5", abs(rec_to["nontimeout_r"] - 0.5) < 1e-9)
    tmo_full = dict(tmo, realized_r=0.75)
    cs_reach = [{"ts": 0, "open": 100, "high": 112, "low": 98, "close": 105}]
    to_ok = check_timeout_exit(tmo_full, rec_to, cs_reach)  # 隱含 105，窗口[98,112]
    chk("timeout 隱含價回推=105", abs(to_ok["implied_exit"] - 105) < 1e-9)
    chk("timeout 隱含價在窗口內→可達", to_ok["reachable"] is True)
    # 竄改 stored_r 灌成 3.0 → 隱含出場價 = 100 + (3.0-0.5)/0.5*10 = 100+50 = 150，超窗口
    tmo_fake = dict(tmo, realized_r=3.0)
    to_bad = check_timeout_exit(tmo_fake, recompute_r(tmo_fake), cs_reach)
    chk("竄改 timeout R → 隱含價 150 不可達", to_bad["reachable"] is False)
    chk("無 K 線→ check_timeout_exit 回 no_candles",
        check_timeout_exit(tmo_full, rec_to, [])["status"] == "no_candles")

    # --- _price_at ---
    cs = [{"ts": 0, "close": 1}, {"ts": 10, "close": 2}, {"ts": 20, "close": 3}]
    chk("_price_at 落在區間取前棒", _price_at(cs, 15) == 2)
    chk("_price_at 早於全部取第一", _price_at(cs, -5) == 1)

    print(f"\n自測完成：{'全部通過 ✅' if fails == 0 else str(fails) + ' 項失敗 ❌'}")
    return fails


def main() -> None:
    ap = argparse.ArgumentParser(description="紙上持倉績效逐筆重算稽核（免金鑰）")
    ap.add_argument("--selftest", action="store_true", help="離線單元測試（不連網）")
    ap.add_argument("--run", action="store_true", help="對真實帳本跑稽核")
    ap.add_argument("-n", type=int, default=50, help="查核最近 N 筆已平倉單")
    ap.add_argument("--bar", default="15m", help="重算用 K 線時框（預設 15m，與 monitor 同步）")
    ap.add_argument("--days", type=int, default=120, help="只查近 N 天進場的單")
    ap.add_argument("--no-fetch", action="store_true", help="只跑算術檢查、不抓 K 線")
    a = ap.parse_args()

    if a.selftest:
        raise SystemExit(_selftest())

    # 預設行為（無旗標）= --run
    findings = asyncio.run(audit_recent(a.n, a.bar, a.days, fetch=not a.no_fetch))
    print(render_audit_report(findings, html=False))


if __name__ == "__main__":
    main()
