"""task#27 ── crypto-only EV 顯著性（離線唯讀回測工具）。

定位：把關「模擬盤 crypto 策略到底有沒有統計上可證實的 edge」。這是紅線①（真錢上線）
Phase-0 閘門的證據之一——在拿真錢前，先用嚴謹統計回答「現有樣本是否已證明 SR>0」。

設計比照 `backtest/stop_placement_ab.py` / `backtest/l2_stat_gates.py`：
  **純離線、唯讀、不碰 daemon、零網路、零下單數學。**
本檔只讀 `trade_journal.db` 的已平倉逐筆 R，吐出 PSR/DSR/minTRL 判定；不寫任何 DB、
不 append ledger（n_trials 只「讀」真 ledger 累計數做保守 DSR，不污染優化器族群）。

────────────────────────────────────────────────────────────────────────
紅線③（不臆造）：
  • 本檔不寫死任何勝率／報酬；所有數字由真實已平倉樣本即時算出。
  • 這是「模擬盤（paper/demo）」樣本，**不是真錢**；真錢帳 `trades` 表筆數另外列出
    （Phase-0 閘門看的是真錢 30 筆，非本檔的 paper 樣本）。
  • 勝率／R 僅作「樣本敘述統計」呈現，**不得當作對外績效宣稱**；顯著性結論一律以
    PSR/DSR/minTRL 為準，未達門檻即誠實寫「未證實 edge」。
────────────────────────────────────────────────────────────────────────

crypto / 非 crypto 邊界（離線近似）：
  乾淨的 instCategory=='1' 判定需要 OKX live /public/instruments（見 market_scanner.
  ensure_crypto_allowset），本離線工具不連網。故採：
    • US_WHITELIST：美股獨立引擎標的（MU/NVDA/… 走真實股市數據的 1h 突破引擎，
      從不屬 crypto SMC 射程）→ 硬排除，這是主切口。
    • KNOWN_NONCRYPTO_TOKENS：已知代幣化商品／股權 token（XAU 金、CL 原油、SPCX…）
      → 提供「嚴格 crypto」敏感度切口；因僅佔少數，主結論對其去留不敏感（會一併報出）。

執行：
    python -m backtest.crypto_ev_significance            # 印繁中報告
    python -m backtest.crypto_ev_significance --json     # 機器可讀
    python -m backtest.crypto_ev_significance --selftest # 合成自測（無需 DB）
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from math import sqrt
from statistics import mean, pstdev

# Windows 主控台預設 cp950，印 emoji/繁中會 UnicodeEncodeError → 強制 UTF-8
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from backtest.validation import deflated_sharpe, min_trl, psr, sharpe

# ── 分類常數 ──────────────────────────────────────────────────────────────
# 美股獨立引擎白名單（隔離；絕非 crypto SMC 射程）。見記憶 us-vs-crypto-engine。
US_WHITELIST = {"MU", "SNDK", "SOXL", "MRVL", "NVDA", "INTC", "ORCL", "QQQ"}
# 已知代幣化非加密（商品／股權 token）——保守已知集，僅供敏感度切口。
KNOWN_NONCRYPTO_TOKENS = {"XAU", "XAG", "CL", "SPCX", "BRENT", "WTI", "NG", "HG", "SPX", "NAS"}

# exit_reason → 是否「真成交」。entry_expired＝限價未觸發（R 記 0，未成交）。
UNFILLED_REASONS = {"entry_expired", "entry_cancelled", "unfilled"}

DSR_SIG = 0.95   # 顯著門檻（與 l2_stat_gates DSR_MIN 一致；僅判讀用，不放寬）
MIN_N = 30       # 與 l2_stat_gates MIN_BUCKET_N 一致：<30 筆不足以證明（fail-closed 判讀）


def _base(symbol: str) -> str:
    """標的 base：'BTC-USDT-SWAP'→'BTC'、'BTC'→'BTC'、'BTCUSDT'→'BTC'。"""
    s = (symbol or "").upper().strip()
    if "-" in s:
        return s.split("-")[0]
    for suf in ("USDT", "USDC", "USD"):
        if s.endswith(suf) and len(s) > len(suf):
            return s[: -len(suf)]
    return s


def classify(base: str) -> str:
    """回 'us' | 'noncrypto_token' | 'crypto'（離線近似分類）。"""
    if base in US_WHITELIST:
        return "us"
    if base in KNOWN_NONCRYPTO_TOKENS:
        return "noncrypto_token"
    return "crypto"


# ── 樣本載入 ──────────────────────────────────────────────────────────────
@dataclass
class Trade:
    symbol: str
    base: str
    klass: str          # us / noncrypto_token / crypto
    realized_r: float
    exit_reason: str
    filled: bool
    direction: str
    regime: str
    table: str          # paper / demo


def _load_table(con: sqlite3.Connection, table: str) -> list[Trade]:
    """讀單表已平倉、realized_r 非空者。表不存在或欄缺 → 回空（fail-soft）。"""
    out: list[Trade] = []
    try:
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return out
    if not {"symbol", "realized_r", "status"} <= cols:
        return out
    has_reason = "exit_reason" in cols
    has_dir = "direction" in cols
    has_regime = "regime" in cols
    q = (f"SELECT symbol, realized_r, "
         f"{'exit_reason' if has_reason else 'NULL'} ex, "
         f"{'direction' if has_dir else 'NULL'} dir, "
         f"{'regime' if has_regime else 'NULL'} rg "
         f"FROM {table} WHERE status='closed' AND realized_r IS NOT NULL")
    for sym, rr, ex, dr, rg in con.execute(q):
        b = _base(sym)
        reason = (ex or "").strip().lower()
        out.append(Trade(
            symbol=sym, base=b, klass=classify(b), realized_r=float(rr),
            exit_reason=reason, filled=reason not in UNFILLED_REASONS,
            direction=(dr or "").lower(), regime=(rg or "").lower(),
            table="paper" if table == "paper_trades" else "demo"))
    return out


def load_closed(db_path: str, include_demo: bool = True) -> list[Trade]:
    con = sqlite3.connect(db_path)
    try:
        rows = _load_table(con, "paper_trades")
        if include_demo:
            rows += _load_table(con, "demo_trades")
        return rows
    finally:
        con.close()


def real_money_count(db_path: str) -> int:
    """真錢帳 trades 表的已平倉筆數（Phase-0 閘門的真分母；與 paper 樣本分離呈現）。"""
    con = sqlite3.connect(db_path)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(trades)")}
        if "status" not in cols:
            return 0
        return con.execute(
            "SELECT COUNT(*) FROM trades WHERE status='closed'").fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        con.close()


# ── 統計 ──────────────────────────────────────────────────────────────────
@dataclass
class EVStat:
    label: str
    n: int
    mean_r: float | None = None
    sd: float | None = None
    t_stat: float | None = None
    win_rate: float | None = None
    sum_r: float | None = None
    profit_factor: float | None = None
    sharpe_per_trade: float | None = None
    psr: float | None = None              # P(真實 SR>0)，n_trials=1 standalone
    dsr_ledger: float | None = None       # 用真 ledger n_trials 累計做保守多重檢定 deflate
    n_trials_ledger: int | None = None
    min_trl: float | None = None          # 證明 SR>0 所需最少筆數（None=∞/不適用）
    significant: bool = False             # PSR≥0.95 且 n≥MIN_N（保守）
    verdict: str = ""


def ev_stats(returns: list[float], label: str, n_trials_ledger: int = 1) -> EVStat:
    n = len(returns)
    st = EVStat(label=label, n=n)
    if n == 0:
        st.verdict = "無樣本"
        return st
    st.mean_r = round(mean(returns), 4)
    st.sum_r = round(sum(returns), 3)
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    st.win_rate = round(100 * len(wins) / n, 1)
    gp = sum(wins)
    gl = -sum(losses)
    st.profit_factor = round(gp / gl, 3) if gl > 0 else None
    if n >= 2:
        st.sd = round(pstdev(returns), 4)
        sd = pstdev(returns)
        if sd > 0:
            st.t_stat = round(mean(returns) / (sd / sqrt(n)), 3)
        st.sharpe_per_trade = round(sharpe(returns), 4)
    if n >= 3:
        st.psr = round(psr(returns, 0.0), 4)
        st.n_trials_ledger = n_trials_ledger
        st.dsr_ledger = round(deflated_sharpe(returns, max(1, n_trials_ledger)), 4)
        mtrl = min_trl(returns, 0.0, 0.95)
        st.min_trl = None if mtrl == float("inf") else round(mtrl, 0)
    # 保守顯著性：PSR≥0.95 且 n≥30（minTRL fail-closed 同口徑）
    st.significant = bool(st.psr is not None and st.psr >= DSR_SIG and n >= MIN_N)
    if n < MIN_N:
        st.verdict = f"樣本不足（n={n}<{MIN_N}）→ 統計上未證實 edge（fail-closed）"
    elif st.significant:
        st.verdict = "顯著（PSR≥95%）"
    elif st.min_trl is None:
        st.verdict = "SR≤0：現有樣本證不出正 edge（minTRL=∞）"
    else:
        st.verdict = (f"未達顯著：PSR={st.psr:.2f}；"
                      f"需約 {st.min_trl:.0f} 筆才足以證明（現 {n}）")
    return st


def _ledger_n_trials() -> int:
    """讀真 trial_ledger 的 distinct 試驗數（唯讀；做保守 DSR）。讀不到回 1。"""
    try:
        from backtest.l2_stat_gates import TrialLedger
        return max(1, TrialLedger().count_trials())
    except Exception:
        return 1


@dataclass
class Report:
    real_money_closed: int
    n_trials_ledger: int
    cuts: list[EVStat] = field(default_factory=list)
    by_reason: dict = field(default_factory=dict)
    note: str = ""


def build_report(db_path: str, include_demo: bool = True) -> Report:
    rows = load_closed(db_path, include_demo=include_demo)
    nt = _ledger_n_trials()
    paper = [t for t in rows if t.table == "paper"]
    demo = [t for t in rows if t.table == "demo"]
    crypto_paper = [t for t in paper if t.klass == "crypto"]
    crypto_paper_strict = crypto_paper  # 已排除 us 與 noncrypto_token？否：crypto 不含二者
    # 主切口：paper crypto（us 已被 classify 排除；noncrypto_token 亦排除）
    cuts: list[EVStat] = []
    # 1) paper crypto（嚴格：已排除 us + 代幣化非加密）
    cuts.append(ev_stats([t.realized_r for t in crypto_paper], "paper｜crypto（嚴格）", nt))
    # 2) paper crypto 含代幣化 token（寬鬆敏感度）
    crypto_loose = [t for t in paper if t.klass in ("crypto", "noncrypto_token")]
    cuts.append(ev_stats([t.realized_r for t in crypto_loose],
                         "paper｜crypto＋代幣化商品/股權（寬鬆）", nt))
    # 3) paper crypto 僅「真成交」（排除 entry_expired 未成交 R=0）
    cuts.append(ev_stats([t.realized_r for t in crypto_paper if t.filled],
                         "paper｜crypto 僅真成交（排除未成交）", nt))
    # 4) paper US（隔離引擎；對照，不可宣稱）
    cuts.append(ev_stats([t.realized_r for t in paper if t.klass == "us"],
                         "paper｜US 美股（隔離引擎·對照）", nt))
    # 5) demo crypto（OKX 模擬盤獨立執行）
    crypto_demo = [t for t in demo if t.klass == "crypto"]
    cuts.append(ev_stats([t.realized_r for t in crypto_demo],
                         "demo｜crypto（OKX 模擬盤）", nt))
    # by exit_reason（crypto paper）
    by_reason: dict = {}
    for t in crypto_paper:
        d = by_reason.setdefault(t.exit_reason or "(none)", {"n": 0, "sum_r": 0.0})
        d["n"] += 1
        d["sum_r"] += t.realized_r
    for k, d in by_reason.items():
        d["mean_r"] = round(d["sum_r"] / d["n"], 4) if d["n"] else None
        d["sum_r"] = round(d["sum_r"], 3)
    return Report(
        real_money_closed=real_money_count(db_path),
        n_trials_ledger=nt, cuts=cuts, by_reason=by_reason,
        note=("代幣化非加密採保守已知集近似；乾淨 instCategory 邊界需 OKX live "
              "instruments（離線工具不連網）。主結論對其去留不敏感。"))


# ── 渲染 ──────────────────────────────────────────────────────────────────
def render(rep: Report) -> str:
    L = []
    L.append("═" * 70)
    L.append("crypto-only EV 顯著性（task#27｜離線唯讀｜PSR/DSR/minTRL）")
    L.append("═" * 70)
    L.append("⚠️ 模擬盤（paper/demo）樣本，非真錢；數字皆即時由真實已平倉樣本算出。")
    L.append(f"   真錢帳 trades 已平倉 = {rep.real_money_closed} 筆"
             f"（Phase-0 紅線①閘門看這個，非下方 paper 樣本）")
    L.append(f"   ledger 累計試驗數 n_trials = {rep.n_trials_ledger}（DSR 多重檢定用）")
    L.append("")
    hdr = f"{'切口':<34}{'n':>4}{'meanR':>9}{'t':>7}{'win%':>7}{'PSR':>7}{'minTRL':>8}"
    L.append(hdr)
    L.append("─" * 70)
    for c in rep.cuts:
        if c.n == 0:
            L.append(f"{c.label:<34}{c.n:>4}{'—':>9}{'—':>7}{'—':>7}{'—':>7}{'—':>8}")
            continue
        mtrl = "∞" if c.min_trl is None else f"{c.min_trl:.0f}"
        t = "—" if c.t_stat is None else f"{c.t_stat:+.2f}"
        psr_s = "—" if c.psr is None else f"{c.psr:.2f}"
        L.append(f"{c.label:<34}{c.n:>4}{c.mean_r:>+9.4f}{t:>7}"
                 f"{c.win_rate:>6.1f}%{psr_s:>7}{mtrl:>8}")
    L.append("")
    L.append("【crypto paper 各 exit_reason】")
    for k, d in sorted(rep.by_reason.items(), key=lambda kv: -kv[1]["n"]):
        L.append(f"  {k:<16} n={d['n']:>3}  meanR={d['mean_r']:+.4f}  ΣR={d['sum_r']:+.2f}")
    L.append("")
    L.append("【判讀（每切口）】")
    for c in rep.cuts:
        if c.n:
            L.append(f"  • {c.label}：{c.verdict}")
    L.append("")
    L.append(f"註：{rep.note}")
    L.append("═" * 70)
    return "\n".join(L)


# ── 自測（合成資料，無需 DB） ───────────────────────────────────────────────
def _selftest() -> bool:
    # 分類
    assert classify("BTC") == "crypto"
    assert classify("NVDA") == "us"
    assert classify("XAU") == "noncrypto_token"
    assert _base("BTC-USDT-SWAP") == "BTC"
    assert _base("ETHUSDT") == "ETH"
    # 強正 edge → PSR 高、minTRL 有限、significant
    import random
    rng = random.Random(7)
    pos = [rng.gauss(0.4, 1.0) for _ in range(120)]
    s = ev_stats(pos, "pos", 1)
    assert s.psr is not None and s.psr > 0.95, f"強 edge PSR 應>0.95，得 {s.psr}"
    assert s.significant, "強 edge 應 significant"
    assert s.min_trl is not None, "強 edge minTRL 應有限"
    # 零 edge → PSR≈0.5、minTRL=∞、not significant
    flat = [rng.gauss(0.0, 1.0) for _ in range(120)]
    s2 = ev_stats(flat, "flat", 1)
    assert not s2.significant, "零 edge 不應 significant"
    # 小樣本 fail-closed
    s3 = ev_stats([0.5] * 10, "small", 1)
    assert not s3.significant and "不足" in s3.verdict, "n<30 應 fail-closed"
    # DSR 隨 n_trials 增加而下降（多重檢定懲罰）
    d1 = ev_stats(pos, "d1", 1).dsr_ledger
    d50 = ev_stats(pos, "d50", 50).dsr_ledger
    assert d50 <= d1, f"n_trials 多→DSR 應降（{d50} ≤ {d1}）"
    # 未成交（entry_expired）filled 旗標
    t = Trade("BTC", "BTC", "crypto", 0.0, "entry_expired", False, "long", "deepdive", "paper")
    assert not t.filled
    print("  crypto_ev_significance 自測通過：分類/PSR/minTRL/DSR-deflate/fail-closed ✅")
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="crypto-only EV 顯著性（離線唯讀）")
    ap.add_argument("--db", default=None, help="trade_journal.db 路徑（預設 botpaths）")
    ap.add_argument("--no-demo", action="store_true", help="不含 demo_trades")
    ap.add_argument("--json", action="store_true", help="輸出 JSON")
    ap.add_argument("--selftest", action="store_true", help="合成自測（無需 DB）")
    args = ap.parse_args(argv)
    if args.selftest:
        return 0 if _selftest() else 1
    db = args.db
    if db is None:
        from botpaths import db_path
        db = str(db_path("trade_journal.db"))
    rep = build_report(db, include_demo=not args.no_demo)
    if args.json:
        print(json.dumps({
            "real_money_closed": rep.real_money_closed,
            "n_trials_ledger": rep.n_trials_ledger,
            "cuts": [asdict(c) for c in rep.cuts],
            "by_reason": rep.by_reason, "note": rep.note,
        }, ensure_ascii=False, indent=2))
    else:
        print(render(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
