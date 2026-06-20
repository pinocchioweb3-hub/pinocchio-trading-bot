"""task#27 ── crypto-only EV 顯著性（離線唯讀回測工具）。

定位：把關「模擬盤 crypto 策略到底有沒有統計上可證實的 edge」。這是紅線①（真錢上線）
Phase-0 閘門的證據之一——在拿真錢前，先用嚴謹統計回答「現有樣本是否已證明 SR>0」。

設計比照 `backtest/stop_placement_ab.py` / `backtest/l2_stat_gates.py`：
  **純離線、唯讀、不碰 daemon、零網路、零下單數學。**
本檔只讀 `trade_journal.db` 的已平倉逐筆 R，吐出 PSR/minTRL 判定；不寫任何 DB。

────────────────────────────────────────────────────────────────────────
方法學（經 5 鏡頭對抗稽核 wf_16963335-557 後校正，task#27）：
  • **主結論用「僅真成交」切口**：限價未觸發（entry_expired，realized_r=0）是
    「沒發生的交易」(non-event)，不是「打平 0R 的已實現結果」。把它計入報酬向量
    是樣本定義的類別錯誤（膨脹分母、扭曲 minTRL/勝率）。故主切口＝P(R|filled)；
    含未成交者降為「敏感度」對照；entry_expired 另以成交率單獨呈現。
  • **獨立性校正 n_eff**：逐筆 R 高度非獨立（crypto 部位同向重疊、時間叢聚）。以
    entry_at 按 UTC 日分群、單因子 ANOVA 估 ICC → Kish design effect → n_eff＝
    n/deff。顯著性門檻（MIN_N）改以 n_eff 把關；PSR 另報「叢聚校正版」psr_clustered。
  • **不做跨假設族 DSR deflate**：ledger 的試驗數是 auto_optimizer/champion_
    challenger 在「不同 per-symbol×regime 假設族」灌的，與「證明這個 crypto EV
    樣本」無關。誤用全域 n_trials 去 deflate 不可辯護 → 本工具 DSR=PSR（n_trials=1），
    ledger 規模僅列為脈絡。
  • **minTRL 僅作量級**：minTRL ∝ 1/SR²，SR≈0 時對樣本組成極敏感（自助 p5–p95 可
    橫跨數量級）。報「量級 ~10^k」而非精確筆數。它不是厚尾扭曲（skew/kurt 在 SR≈0
    時對其貢獻 <2%），而是「SR≈0 → 需天量樣本」這個正確事實。

紅線③（不臆造）：
  • 不寫死任何勝率／報酬；數字皆由真實已平倉樣本即時算出。
  • 模擬盤（paper/demo）樣本 **不是真錢**；真錢帳 `trades` 表筆數另列（Phase-0 看真錢）。
  • 勝率／R 僅作敘述統計，**不得當對外績效宣稱**；n<門檻切口（如美股對照）的
    PSR/minTRL **不予判讀**（避免「接近顯著」誤讀）。
  • 「未證實有 edge」≠「已證明無 edge」：現有樣本對方向無區辨力。
────────────────────────────────────────────────────────────────────────

crypto / 非 crypto 邊界（離線近似）：
  乾淨 instCategory=='1' 判定需 OKX live /public/instruments（離線工具不連網）。故採
  US_WHITELIST（美股獨立引擎標的）硬排除＋KNOWN_NONCRYPTO_TOKENS（代幣化商品/股權）
  敏感度切口；主結論對其去留不敏感（一併報出）。

執行：
    python -m backtest.crypto_ev_significance            # 印繁中報告
    python -m backtest.crypto_ev_significance --json     # 機器可讀
    python -m backtest.crypto_ev_significance --selftest # 合成自測（無需 DB）
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from math import log10, sqrt
from statistics import NormalDist, mean, pstdev

# Windows 主控台預設 cp950，印 emoji/繁中會 UnicodeEncodeError → 強制 UTF-8
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from backtest.validation import _moments, min_trl, psr, sharpe
# 獨立性校正原語抽至共用葉模組（單一真相源；live L2 閘 l2_stat_gates.py 亦 import
# 同一套，見 task#80）。此處 re-export 保留既有呼叫名（_psr_with_n）與測試。
from backtest.independence import (  # noqa: F401  (re-export)
    _icc_oneway,
    _utc_day_key,
    effective_n,
    psr_with_n as _psr_with_n,
)

_NORM = NormalDist()

# ── 分類常數 ──────────────────────────────────────────────────────────────
# 美股獨立引擎白名單（隔離；絕非 crypto SMC 射程）。見記憶 us-vs-crypto-engine。
US_WHITELIST = {"MU", "SNDK", "SOXL", "MRVL", "NVDA", "INTC", "ORCL", "QQQ"}
# 已知代幣化非加密（商品／股權 token）——保守已知集，僅供敏感度切口。
KNOWN_NONCRYPTO_TOKENS = {"XAU", "XAG", "CL", "SPCX", "BRENT", "WTI", "NG", "HG", "SPX", "NAS"}

# exit_reason → 是否「真成交」。entry_expired＝限價未觸發（R 記 0，未成交）。
UNFILLED_REASONS = {"entry_expired", "entry_cancelled", "unfilled"}

DSR_SIG = 0.95   # 顯著門檻（與 l2_stat_gates DSR_MIN 一致；僅判讀用，不放寬）
MIN_N = 30       # 與 l2_stat_gates MIN_BUCKET_N 一致：<30 不足以證明（fail-closed）。
                 # 改以 n_eff（叢聚校正後）把關，較名目 n 嚴格。


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
    entry_ms: int | None = None   # entry_at（epoch 毫秒）；供叢聚 n_eff 用


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
    has_entry = "entry_at" in cols
    q = (f"SELECT symbol, realized_r, "
         f"{'exit_reason' if has_reason else 'NULL'} ex, "
         f"{'direction' if has_dir else 'NULL'} dir, "
         f"{'regime' if has_regime else 'NULL'} rg, "
         f"{'entry_at' if has_entry else 'NULL'} eat "
         f"FROM {table} WHERE status='closed' AND realized_r IS NOT NULL")
    for sym, rr, ex, dr, rg, eat in con.execute(q):
        b = _base(sym)
        reason = (ex or "").strip().lower()
        try:
            ems = int(eat) if eat is not None else None
        except (TypeError, ValueError):
            ems = None
        out.append(Trade(
            symbol=sym, base=b, klass=classify(b), realized_r=float(rr),
            exit_reason=reason, filled=reason not in UNFILLED_REASONS,
            direction=(dr or "").lower(), regime=(rg or "").lower(),
            table="paper" if table == "paper_trades" else "demo",
            entry_ms=ems))
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


# ── 獨立性校正（n_eff）── 見上方 re-export（backtest.independence）。 ──────────


# ── 統計 ──────────────────────────────────────────────────────────────────
@dataclass
class EVStat:
    label: str
    role: str = "sensitivity"             # primary / sensitivity / control
    n: int = 0
    n_eff: float | None = None            # 叢聚校正後有效獨立筆數
    icc: float | None = None
    design_effect: float | None = None
    mean_r: float | None = None
    sd: float | None = None
    t_stat: float | None = None
    win_rate: float | None = None
    sum_r: float | None = None
    profit_factor: float | None = None
    sharpe_per_trade: float | None = None
    psr: float | None = None              # P(真實 SR>0)，i.i.d. 假設
    psr_clustered: float | None = None    # 同上但以 n_eff（叢聚校正）
    min_trl: float | None = None          # 證明 SR>0 所需最少筆數（None=∞/不適用）
    judgeable: bool = False               # n≥MIN_N 且 n_eff≥MIN_N（達判讀門檻）
    significant: bool = False             # judgeable 且 PSR/psr_clustered 皆≥0.95
    verdict: str = ""


def ev_stats(returns: list[float], label: str, *,
             day_keys: list[str | None] | None = None,
             role: str = "sensitivity") -> EVStat:
    n = len(returns)
    st = EVStat(label=label, role=role, n=n)
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
    # 獨立性校正
    if day_keys is not None:
        st.n_eff, st.icc, st.design_effect, _ = effective_n(returns, day_keys)
    if n >= 3:
        st.psr = round(psr(returns, 0.0), 4)
        if st.n_eff is not None:
            st.psr_clustered = _psr_with_n(returns, st.n_eff, 0.0)
        mtrl = min_trl(returns, 0.0, 0.95)
        st.min_trl = None if mtrl == float("inf") else round(mtrl, 0)
    # 判讀門檻：名目 n≥30 且（若有 n_eff）n_eff≥30。fail-closed。
    neff_ok = (st.n_eff is None) or (st.n_eff >= MIN_N)
    st.judgeable = bool(n >= MIN_N and neff_ok)
    # 顯著＝可判讀 且 PSR≥0.95 且（若有叢聚校正）psr_clustered≥0.95
    pc_ok = (st.psr_clustered is None) or (st.psr_clustered >= DSR_SIG)
    st.significant = bool(st.judgeable and st.psr is not None
                          and st.psr >= DSR_SIG and pc_ok)
    st.verdict = _verdict(st)
    return st


def _mag(x: float | None) -> str:
    """量級字串：minTRL 對 SR≈0 極不穩，只報 ~10^k 不報精確值。"""
    if x is None:
        return "∞"
    if x <= 0:
        return "—"
    return f"~10^{int(round(log10(x)))}"


def _verdict(st: EVStat) -> str:
    if st.n == 0:
        return "無樣本"
    neff_s = "" if st.n_eff is None else f"，n_eff≈{st.n_eff:.0f}"
    if not st.judgeable:
        return (f"樣本不足（n={st.n}{neff_s}<{MIN_N}）→ 未證實 edge"
                f"（fail-closed；未證實≠已證明無）")
    if st.significant:
        return f"顯著（PSR≥95% 且 n_eff≥{MIN_N}）"
    if st.min_trl is None:
        return "SR≤0：現有樣本證不出正 edge（minTRL=∞；未證實≠已證明無）"
    conf = 0 if st.psr is None else st.psr * 100
    return (f"未達顯著：PSR={st.psr:.2f}（真實 SR>0 信心僅 {conf:.0f}%，≈無法區分於零）；"
            f"所需樣本量級 {_mag(st.min_trl)} 筆，遠超現有 {st.n}（minTRL 僅供量級）")


def _ledger_size() -> int:
    """讀真 trial_ledger 規模（唯讀）——僅作脈絡呈現，不用於跨族 DSR deflate。"""
    try:
        from backtest.l2_stat_gates import TrialLedger
        return max(0, TrialLedger().count_trials())
    except Exception:
        return 0


@dataclass
class Report:
    real_money_closed: int
    ledger_size: int
    cuts: list[EVStat] = field(default_factory=list)
    by_reason: dict = field(default_factory=dict)
    fill_stats: dict = field(default_factory=dict)
    note: str = ""


def _rr_days(trades: list[Trade]):
    return ([t.realized_r for t in trades],
            [_utc_day_key(t.entry_ms) for t in trades])


def build_report(db_path: str, include_demo: bool = True) -> Report:
    rows = load_closed(db_path, include_demo=include_demo)
    paper = [t for t in rows if t.table == "paper"]
    demo = [t for t in rows if t.table == "demo"]
    crypto_paper = [t for t in paper if t.klass == "crypto"]
    crypto_filled = [t for t in crypto_paper if t.filled]
    crypto_loose = [t for t in paper if t.klass in ("crypto", "noncrypto_token")]
    us_paper = [t for t in paper if t.klass == "us"]
    crypto_demo = [t for t in demo if t.klass == "crypto"]

    cuts: list[EVStat] = []
    # 1) 主結論：paper crypto 僅真成交（P(R|filled)，排除 entry_expired 非事件）
    rr, dk = _rr_days(crypto_filled)
    cuts.append(ev_stats(rr, "paper｜crypto 僅真成交〔主結論〕",
                         day_keys=dk, role="primary"))
    # 2) 敏感度A：含未成交 R=0（膨脹分母；僅供穩健性對照，非獨立證據）
    rr, dk = _rr_days(crypto_paper)
    cuts.append(ev_stats(rr, "paper｜crypto 含未成交R=0〔敏感度〕",
                         day_keys=dk, role="sensitivity"))
    # 3) 敏感度B：＋代幣化商品/股權（寬鬆邊界；同源敏感度）
    rr, dk = _rr_days(crypto_loose)
    cuts.append(ev_stats(rr, "paper｜crypto＋代幣化〔敏感度〕",
                         day_keys=dk, role="sensitivity"))
    # 4) 對照：paper US 美股（隔離引擎；n<門檻不予判讀，不可宣稱）
    rr, dk = _rr_days(us_paper)
    cuts.append(ev_stats(rr, "paper｜US 美股〔隔離對照〕",
                         day_keys=dk, role="control"))
    # 5) demo crypto（OKX 模擬盤獨立執行）
    rr, dk = _rr_days(crypto_demo)
    cuts.append(ev_stats(rr, "demo｜crypto（OKX 模擬盤）",
                         day_keys=dk, role="sensitivity"))

    # 成交率（entry_expired 另計，不混入報酬統計）
    n_total = len(crypto_paper)
    n_filled = len(crypto_filled)
    fill_stats = {
        "crypto_total": n_total, "crypto_filled": n_filled,
        "unfilled": n_total - n_filled,
        "fill_rate": round(n_filled / n_total, 3) if n_total else None,
    }
    # by exit_reason（crypto paper 全體）
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
        ledger_size=_ledger_size(), cuts=cuts, by_reason=by_reason,
        fill_stats=fill_stats,
        note=("代幣化非加密採保守已知集近似（乾淨 instCategory 需 OKX live，離線不連網）；"
              "主結論對其去留不敏感。n_eff＝按 UTC 日叢聚的有效獨立筆數（部位同向重疊）。"))


# ── 渲染 ──────────────────────────────────────────────────────────────────
def render(rep: Report) -> str:
    L = []
    L.append("═" * 74)
    L.append("crypto-only EV 顯著性（task#27｜離線唯讀｜PSR/minTRL／n_eff 叢聚校正）")
    L.append("═" * 74)
    L.append("⚠️ 模擬盤（paper/demo）樣本，非真錢；數字皆即時由真實已平倉樣本算出。")
    L.append(f"   真錢帳 trades 已平倉 = {rep.real_money_closed} 筆"
             f"（Phase-0 紅線①閘門看這個，非下方 paper 樣本）")
    L.append(f"   ledger 規模 = {rep.ledger_size}（他處 per-bucket 參數搜尋；"
             f"屬不同假設族，本工具不對其跨族 deflate，故 DSR=PSR）")
    fs = rep.fill_stats
    if fs.get("crypto_total"):
        fr = fs.get("fill_rate")
        L.append(f"   crypto paper 成交率 = {fs['crypto_filled']}/{fs['crypto_total']}"
                 f" = {fr:.0%}（未成交 entry_expired={fs['unfilled']} 另計，"
                 f"不混入報酬統計）" if fr is not None else "")
    L.append("")
    hdr = (f"{'切口':<32}{'n':>4}{'n_eff':>7}{'meanR':>9}{'win%':>7}"
           f"{'PSR':>7}{'PSRc':>7}{'minTRL':>8}")
    L.append(hdr)
    L.append("─" * 74)
    for c in rep.cuts:
        if c.n == 0:
            L.append(f"{c.label:<32}{c.n:>4}{'—':>7}{'—':>9}{'—':>7}"
                     f"{'—':>7}{'—':>7}{'—':>8}")
            continue
        neff = "—" if c.n_eff is None else f"{c.n_eff:.0f}"
        # n<門檻不予判讀 → 隱藏 PSR/minTRL（避免「接近顯著」誤讀＝紅線③）
        if not c.judgeable:
            psr_s = pc_s = mtrl = "不判讀"
        else:
            psr_s = "—" if c.psr is None else f"{c.psr:.2f}"
            pc_s = "—" if c.psr_clustered is None else f"{c.psr_clustered:.2f}"
            mtrl = "∞" if c.min_trl is None else _mag(c.min_trl)
        L.append(f"{c.label:<32}{c.n:>4}{neff:>7}{c.mean_r:>+9.4f}"
                 f"{c.win_rate:>6.1f}%{psr_s:>7}{pc_s:>7}{mtrl:>8}")
    L.append("")
    L.append("【crypto paper 各 exit_reason】")
    for k, d in sorted(rep.by_reason.items(), key=lambda kv: -kv[1]["n"]):
        L.append(f"  {k:<16} n={d['n']:>3}  meanR={d['mean_r']:+.4f}  ΣR={d['sum_r']:+.2f}")
    L.append("")
    L.append("【判讀（每切口）】")
    for c in rep.cuts:
        if c.n:
            tag = {"primary": "★主結論", "control": "·對照",
                   "sensitivity": "·敏感度"}.get(c.role, "")
            L.append(f"  • {c.label}{tag}：{c.verdict}")
    L.append("")
    L.append("【方法學備註】")
    L.append("  · PSRc＝叢聚校正版 PSR（以 n_eff 取代 n）；部位同向重疊使 n_eff 約為 n 半。")
    L.append("  · PSR 低多因每筆 SR≈0（與 n 大小無關），非「樣本夠大才可信」。")
    L.append("  · minTRL 僅作量級（∝1/SR²，SR≈0 時自助 p5–p95 可橫跨數量級）；非精確目標筆數。")
    L.append("  · 敏感度切口與主結論同源（重疊子集），非獨立證據；顯著性只引主結論。")
    L.append("  · 未證實有 edge ≠ 已證明無 edge：現有樣本對方向無區辨力。")
    L.append("")
    L.append(f"註：{rep.note}")
    L.append("═" * 74)
    return "\n".join(L)


# ── 自測（合成資料，無需 DB） ───────────────────────────────────────────────
def _selftest() -> bool:
    import random
    # 分類
    assert classify("BTC") == "crypto"
    assert classify("NVDA") == "us"
    assert classify("XAU") == "noncrypto_token"
    assert _base("BTC-USDT-SWAP") == "BTC"
    assert _base("ETHUSDT") == "ETH"
    # 強正 edge（無 day_keys → n_eff=None，只看 i.i.d. PSR）→ 顯著
    rng = random.Random(7)
    pos = [rng.gauss(0.4, 1.0) for _ in range(120)]
    s = ev_stats(pos, "pos", role="primary")
    assert s.psr is not None and s.psr > 0.95, f"強 edge PSR 應>0.95，得 {s.psr}"
    assert s.judgeable and s.significant, "強 edge 應 judgeable+significant"
    assert s.min_trl is not None
    # 確定性零 edge → 不顯著、minTRL=∞
    flat = [1.0, -1.0] * 60
    s2 = ev_stats(flat, "flat")
    assert not s2.significant and s2.min_trl is None
    # 小樣本 fail-closed
    s3 = ev_stats([0.5] * 10, "small")
    assert not s3.judgeable and "不足" in s3.verdict
    # n_eff：每日一群、群內高度同向 → ICC 高 → n_eff < n
    rng2 = random.Random(3)
    days = []
    rr = []
    for di in range(6):                       # 6 天，每天 20 筆同向（強叢聚）
        base = 0.5 if di % 2 == 0 else -0.5
        for _ in range(20):
            rr.append(base + rng2.gauss(0, 0.05))
            days.append(f"2026-06-{10+di:02d}")
    neff, icc, deff, cov = effective_n(rr, days)
    assert neff is not None and neff < len(rr), f"叢聚應使 n_eff<n，得 {neff}/{len(rr)}"
    assert icc > 0.5, f"強同向應高 ICC，得 {icc}"
    # 獨立資料（每筆不同天）→ n_eff≈n
    ind_days = [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(60)]
    ind_rr = [rng2.gauss(0.0, 1.0) for _ in range(60)]
    neff2, icc2, _, _ = effective_n(ind_rr, ind_days)
    assert neff2 is not None and neff2 >= 0.8 * len(ind_rr), f"獨立應 n_eff≈n，得 {neff2}"
    # n_eff 把關：名目 n≥30 但叢聚使 n_eff<30 → 不可判讀
    s4 = ev_stats(rr, "clustered", day_keys=days, role="primary")
    assert s4.n >= MIN_N and s4.n_eff is not None
    if s4.n_eff < MIN_N:
        assert not s4.judgeable, "n_eff<30 應 fail-closed"
    # psr_clustered ≤ psr（n_eff≤n → z 較小 → PSR 較低）對正 edge 成立
    sp = ev_stats(pos, "pos2", day_keys=["2026-06-10"] * 60 + ["2026-06-11"] * 60,
                  role="primary")
    if sp.psr_clustered is not None and sp.psr is not None:
        assert sp.psr_clustered <= sp.psr + 1e-9, "叢聚校正 PSR 不應高於 i.i.d. PSR"
    # 未成交旗標
    t = Trade("BTC", "BTC", "crypto", 0.0, "entry_expired", False, "long", "x", "paper")
    assert not t.filled
    # 量級
    assert _mag(10000) == "~10^4" and _mag(None) == "∞"
    print("  crypto_ev_significance 自測通過："
          "分類/PSR/minTRL/n_eff 叢聚校正/judgeable 把關/量級 ✅")
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
        def _dump(c: EVStat) -> dict:
            d = asdict(c)
            # n<門檻不予判讀 → JSON 也抹去 PSR/minTRL（紅線③，避免「接近顯著」誤讀）
            if not c.judgeable:
                for kk in ("psr", "psr_clustered", "min_trl"):
                    d[kk] = None
                d["suppressed_reason"] = "n<MIN_N 不予判讀"
            return d
        print(json.dumps({
            "real_money_closed": rep.real_money_closed,
            "ledger_size": rep.ledger_size,
            "fill_stats": rep.fill_stats,
            "cuts": [_dump(c) for c in rep.cuts],
            "by_reason": rep.by_reason, "note": rep.note,
        }, ensure_ascii=False, indent=2))
    else:
        print(render(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
