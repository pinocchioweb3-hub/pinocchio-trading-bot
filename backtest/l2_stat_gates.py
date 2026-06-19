"""復盤引擎 step5（task#50）── L2 統計嚴謹度閘門（離線唯讀工具）。

定位（使用者 INTENT #1）：自動優化器（#52 auto_tuner）想「直接調整模擬盤的槓桿／風險／
策略參數」，把關的不是「每次人工點頭」，而是**統計嚴謹度**。本檔就是那道把關閘——
唯有候選參數的真實樣本通過下列四關，#52 才被允許寫入一筆參數變更。

設計比照 `backtest/stop_placement_ab.py`：**純離線、唯讀、不碰 daemon、零網路、零下單數學**。
本檔不 import 任何常駐 worker / 行情抓取 / 交易模組；它只吃「已實現的逐筆 R 報酬序列」與
「同期多配置報酬矩陣」，吐出 PASS/FAIL 判定。

────────────────────────────────────────────────────────────────────────
紅線③（不臆造）：本檔不寫死任何勝率／報酬；所有顯著性都由傳入的真實樣本即時算出。
門檻只能調嚴（CI 斷言 `assert_thresholds_only_stricter` 守住），不可悄悄放寬騙過閘門。
────────────────────────────────────────────────────────────────────────

四關（全部 AND；任一不過 = 候選參數被擋下）：

  1. minTRL  — 樣本夠不夠長到能證明 SR>0？且 **MIN_BUCKET_N=30 fail-closed**
               （bucket 內不足 30 筆 → 直接擋，不准用小樣本自欺）。
  2. DSR     — Deflated Sharpe（Bailey & López de Prado）。用「試了幾組參數」去通膨
               所需門檻。**n_trials 一律從 ledger 累計、禁止呼叫端手傳**（防止謊報試驗數
               來稀釋多重檢定懲罰）；DSR 的跨試驗 SR 變異數用 ledger 內**實測**值，
               不是 1/(n-1) 近似。
  3. PBO/CSCV— Probability of Backtest Overfitting（Bailey, Borwein, López de Prado,
               Zhu 2017 的 Combinatorially-Symmetric Cross-Validation）。量化「挑出
               樣本內最佳配置」這個動作本身的過擬合機率。需要≥2 個同期配置才能算；
               算不出來 → fail-closed（你連『有沒有挑過頭』都證不了，就不准過）。
  4. BHY-FDR — Benjamini–Yekutieli (2001) 在任意相依下控制偽發現率。把本候選的 p 值
               放進「ledger 內所有試驗 p 值」一起做 FDR 校正（含 file-drawer：失敗的
               試驗也記進 ledger、也算進分母）→ 唯有存活者才算真發現。

防 HARKing（Hypothesizing After Results are Known）：
  `register_hypothesis()` 把「假設＋凍結時刻」寫進 append-only 雜湊鏈（凍結時刻因此
  無法事後竄改）。評估時只採計**凍結後**成交的單做 out-of-time holdout
  （`out_of_time_holdout()`）——先射箭再畫靶的事後合理化被結構性擋死。

ledger（`trial_ledger.jsonl`）：append-only、逐行 JSON、鏈式 sha256
  （每筆 hash = sha256(prev_hash + 本筆正規化內容)），任何竄改都會被 `verify_chain()`
  抓出。file-drawer 原則：通過與**沒通過**的試驗都 append，n_trials 與 p 值族群都從
  整本 ledger 累計——你無法只記漂亮的、藏起難看的來騙統計。

執行：
    python -m backtest.l2_stat_gates --selftest        # 合成資料自測（無需網路/ledger）
    python -m backtest.l2_stat_gates --verify-ledger   # 驗真 ledger 雜湊鏈完整性
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from dataclasses import dataclass, field
from itertools import combinations
from math import sqrt
from pathlib import Path
from statistics import mean, pvariance

# v49：Windows 主控台預設 cp950，印 emoji/繁中報告會 UnicodeEncodeError → 強制 UTF-8
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from backtest.validation import deflated_sharpe, min_trl, psr, sharpe

# ─── 門檻（只能調嚴；改動須同步通過 assert_thresholds_only_stricter）─────────
MIN_BUCKET_N = 30        # bucket（如 幣×regime）內最少筆數，不足 fail-closed（只能調高）
DSR_MIN = 0.95           # Deflated Sharpe 顯著門檻（只能調高）
PBO_MAX = 0.50           # 過擬合機率上限（只能調低）
FDR_Q = 0.05             # BHY-FDR 控制水準（只能調低）
CSCV_SPLITS = 16         # CSCV 連續分塊數（偶數；C(16,8)=12870 組，離線可承受）
PBO_MIN_CONFIGS = 2      # PBO 至少要這麼多同期配置才有意義，否則 fail-closed

# CI 凍結基準：方向標記哪邊算「更嚴」。assert_thresholds_only_stricter 比對之。
_FROZEN_THRESHOLDS = {
    # key: (baseline_value, direction)  direction="ge" 表只能 ≥（調高更嚴）；"le" 只能 ≤
    "MIN_BUCKET_N": (30, "ge"),
    "DSR_MIN": (0.95, "ge"),
    "PBO_MAX": (0.50, "le"),
    "FDR_Q": (0.05, "le"),
}

_GENESIS = "0" * 64


# ════════════════════════════════════════════════════════════════════════
#  純統計核心（無 I/O、無相依 daemon）
# ════════════════════════════════════════════════════════════════════════
def trial_p_value(returns: list[float]) -> float:
    """單一試驗的 standalone p 值 = P(真實 SR ≤ 0) = 1 − PSR(0)。越小＝edge 越強。"""
    n = len(returns)
    if n < 3:
        return 1.0
    return max(0.0, min(1.0, 1.0 - psr(returns, 0.0)))


# ── PBO / CSCV ───────────────────────────────────────────────────────────
def _block_moments(matrix: list[list[float]], blocks: list[list[int]]):
    """預算每塊×每配置的 (Σr, Σr², count)，讓任意塊聯集的 Sharpe 變成 O(塊數) 累加。

    matrix：T×N（T 期觀測 × N 個配置的逐期報酬）。回 (bsum, bsq, bcnt)：
      bsum[k][s] / bsq[k][s] = 第 k 塊、第 s 配置的報酬和／平方和；bcnt[k] = 該塊期數。
    """
    n_cfg = len(matrix[0])
    bsum = [[0.0] * n_cfg for _ in blocks]
    bsq = [[0.0] * n_cfg for _ in blocks]
    bcnt = [0] * len(blocks)
    for k, rows in enumerate(blocks):
        bcnt[k] = len(rows)
        for t in rows:
            row = matrix[t]
            for s in range(n_cfg):
                v = row[s]
                bsum[k][s] += v
                bsq[k][s] += v * v
    return bsum, bsq, bcnt


def _sharpe_from_moments(ssum: float, ssq: float, cnt: int) -> float:
    """由 (Σr, Σr², n) 還原每筆 Sharpe = mean/std(母體)。std=0 或 n<2 → 0。"""
    if cnt < 2:
        return 0.0
    m = ssum / cnt
    var = ssq / cnt - m * m
    if var <= 0:
        return 0.0
    return m / sqrt(var)


def cscv_pbo(matrix: list[list[float]], n_splits: int = CSCV_SPLITS):
    """CSCV → PBO（Bailey et al. 2017）。

    matrix：T×N 逐期報酬（同一條時間軸上，N 個配置各自的每期報酬）。
    流程：把 T 期連續切 S 塊（保留序列結構）→ 列出所有「一半當 IS、另一半當 OOS」的
    C(S,S/2) 組合 → 每組挑 IS 內 Sharpe 最高的配置 n*，看它在 OOS 的相對名次 → 取 logit；
    PBO = P(λ<0) = 「IS 冠軍在 OOS 落到中位數以下」的比率。PBO 高 = 選擇程序在過擬合。

    回 (pbo, n_combos, n_cfg, S)；無法計算（配置<2 / T 太短 / S<2）回 (None, 0, n_cfg, 0)。
    """
    T = len(matrix)
    n_cfg = len(matrix[0]) if T else 0
    if n_cfg < PBO_MIN_CONFIGS or T < 4:
        return None, 0, n_cfg, 0
    # S = 不超過 n_splits 且 ≤T 的最大偶數，且每塊至少 1 期
    S = min(n_splits, T)
    S -= S % 2
    if S < 2:
        return None, 0, n_cfg, 0
    bounds = [round(k * T / S) for k in range(S + 1)]
    blocks = [list(range(bounds[k], bounds[k + 1])) for k in range(S)]
    if any(len(b) == 0 for b in blocks):       # 罕見：T 與 S 互質致空塊 → 降 S
        S -= 2
        if S < 2:
            return None, 0, n_cfg, 0
        bounds = [round(k * T / S) for k in range(S + 1)]
        blocks = [list(range(bounds[k], bounds[k + 1])) for k in range(S)]
        if any(len(b) == 0 for b in blocks):
            return None, 0, n_cfg, 0

    bsum, bsq, bcnt = _block_moments(matrix, blocks)
    half = S // 2
    all_blocks = set(range(S))
    n_below = 0
    n_combos = 0
    for combo in combinations(range(S), half):
        is_set = set(combo)
        oos_set = all_blocks - is_set
        # IS 各配置 Sharpe → 取冠軍
        best_s, best_sr = 0, float("-inf")
        for s in range(n_cfg):
            ssum = sum(bsum[k][s] for k in is_set)
            ssq = sum(bsq[k][s] for k in is_set)
            cnt = sum(bcnt[k] for k in is_set)
            sr = _sharpe_from_moments(ssum, ssq, cnt)
            if sr > best_sr:
                best_sr, best_s = sr, s
        # OOS 各配置 Sharpe → 冠軍的相對名次
        oos_sr = []
        for s in range(n_cfg):
            ssum = sum(bsum[k][s] for k in oos_set)
            ssq = sum(bsq[k][s] for k in oos_set)
            cnt = sum(bcnt[k] for k in oos_set)
            oos_sr.append(_sharpe_from_moments(ssum, ssq, cnt))
        # 名次 r ∈ {1..N}（1 = OOS 最差）；同分用「嚴格小於」計，平手不罰
        r = 1 + sum(1 for v in oos_sr if v < oos_sr[best_s])
        omega = r / (n_cfg + 1)                # ∈ (0,1)
        # logit λ；λ<0 ⟺ omega<0.5 ⟺ 冠軍落在 OOS 中位數以下 = 過擬合徵兆
        if omega <= 0.5:
            n_below += 1
        n_combos += 1
    pbo = n_below / n_combos if n_combos else None
    return pbo, n_combos, n_cfg, S


# ── BHY-FDR ──────────────────────────────────────────────────────────────
def bhy_fdr_rejected(p_values: list[float], q: float = FDR_Q) -> set[int]:
    """Benjamini–Yekutieli (2001)：任意相依下控制 FDR≤q。回被拒虛無（=真發現）的索引集合。

    p(1)≤…≤p(m)；c(m)=Σ_{i=1..m} 1/i（Yekutieli 對任意相依的修正項）；
    找最大 k 使 p(k) ≤ k/(m·c(m))·q，拒絕前 k 名。
    """
    m = len(p_values)
    if m == 0:
        return set()
    c_m = sum(1.0 / i for i in range(1, m + 1))
    order = sorted(range(m), key=lambda i: p_values[i])
    k_max = 0
    for rank, idx in enumerate(order, start=1):
        if p_values[idx] <= (rank / (m * c_m)) * q:
            k_max = rank
    return set(order[:k_max])


def bhy_survives(candidate_p: float, other_p: list[float], q: float = FDR_Q) -> bool:
    """本候選 p 值放進「其餘所有試驗 p 值」一起做 BHY-FDR，候選是否存活（=真發現）。"""
    p_all = list(other_p) + [candidate_p]
    cand_idx = len(p_all) - 1
    return cand_idx in bhy_fdr_rejected(p_all, q)


# ════════════════════════════════════════════════════════════════════════
#  append-only 鏈式雜湊 ledger（file-drawer：成敗都記）
# ════════════════════════════════════════════════════════════════════════
def _canonical(rec: dict) -> str:
    """正規化（排序鍵、無空白、保留非 ASCII）→ 雜湊用的穩定字串。"""
    return json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash_entry(prev_hash: str, rec: dict) -> str:
    body = {k: v for k, v in rec.items() if k != "hash"}
    return hashlib.sha256((prev_hash + _canonical(body)).encode("utf-8")).hexdigest()


def _returns_digest(returns: list[float]) -> str:
    """報酬序列的 sha256（存指紋而非全序列：ledger 精簡又可驗證據未被換掉）。"""
    payload = ",".join(f"{x:.10g}" for x in returns)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def default_ledger_path() -> Path:
    from botpaths import data_dir
    return data_dir() / "trial_ledger.jsonl"


class TrialLedger:
    """append-only 試驗帳本：逐行 JSON、鏈式雜湊、file-drawer 累計。"""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else default_ledger_path()

    # ── 讀 ──
    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def verify_chain(self) -> tuple[bool, str]:
        """重算整條鏈：seq 連續、prev_hash 接續、每筆 hash 吻合。回 (ok, detail)。"""
        entries = self.load()
        prev = _GENESIS
        for i, e in enumerate(entries):
            if e.get("seq") != i:
                return False, f"seq 不連續 @ index {i}（記為 {e.get('seq')}）"
            if e.get("prev_hash") != prev:
                return False, f"prev_hash 斷鏈 @ seq {i}"
            if e.get("hash") != _hash_entry(prev, e):
                return False, f"hash 不符（內容被竄改）@ seq {i}"
            prev = e["hash"]
        return True, f"鏈完整（{len(entries)} 筆）"

    def distinct_trials(self, bucket_key: str | None = None) -> dict[str, dict]:
        """bucket 內每個 trial_id 的最新一筆 evaluation（{trial_id: {sharpe,p_value,n,passed}}）。

        file-drawer：成敗都算。同一 trial_id 多次重估只保留最新（同假設不重複計入族群）。
        """
        out: dict[str, dict] = {}
        for e in self.load():
            if e.get("kind") != "evaluation":
                continue
            if bucket_key is not None and e.get("bucket_key") != bucket_key:
                continue
            out[e["trial_id"]] = {
                "sharpe": e.get("sharpe", 0.0),
                "p_value": e.get("p_value", 1.0),
                "n": e.get("n", 0),
                "passed": e.get("passed"),
            }
        return out

    def count_trials(self, bucket_key: str | None = None) -> int:
        """族群試驗數（distinct 已評估 trial_id；含失敗）。**這是 n_trials 的唯一來源。**"""
        return len(self.distinct_trials(bucket_key))

    # ── 寫 ──
    def _append(self, rec: dict, ts_ms: int | None) -> dict:
        entries = self.load()
        seq = len(entries)
        prev = entries[-1]["hash"] if entries else _GENESIS
        rec = dict(rec)
        rec["seq"] = seq
        rec["ts_ms"] = int(ts_ms if ts_ms is not None else time.time() * 1000)
        rec["prev_hash"] = prev
        rec["hash"] = _hash_entry(prev, rec)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def append_register(self, *, bucket_key: str, hypothesis: str,
                        registered_at_ms: int, trial_id: str | None = None,
                        ts_ms: int | None = None) -> dict:
        tid = trial_id or _derive_trial_id(bucket_key, hypothesis, registered_at_ms)
        return self._append({
            "kind": "register", "trial_id": tid, "bucket_key": bucket_key,
            "hypothesis": hypothesis, "registered_at_ms": int(registered_at_ms),
        }, ts_ms)

    def append_evaluation(self, *, bucket_key: str, trial_id: str, hypothesis: str,
                          returns: list[float], sharpe_val: float, p_value: float,
                          passed: bool, registered_at_ms: int | None = None,
                          ts_ms: int | None = None) -> dict:
        return self._append({
            "kind": "evaluation", "trial_id": trial_id, "bucket_key": bucket_key,
            "hypothesis": hypothesis, "registered_at_ms": registered_at_ms,
            "n": len(returns), "sharpe": round(sharpe_val, 6),
            "p_value": round(p_value, 6),
            "returns_digest": _returns_digest(returns), "passed": bool(passed),
        }, ts_ms)


def _derive_trial_id(bucket_key: str, hypothesis: str, registered_at_ms: int) -> str:
    """由 (bucket, 假設, 凍結時刻) 推導確定性 trial_id（同假設同凍結→同 id，可冪等重估）。"""
    raw = f"{bucket_key}|{hypothesis}|{registered_at_ms}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def register_hypothesis(ledger: TrialLedger, *, bucket_key: str, hypothesis: str,
                        registered_at_ms: int, trial_id: str | None = None,
                        ts_ms: int | None = None) -> str:
    """凍結一個假設（寫進雜湊鏈，凍結時刻從此不可竄改）。回 trial_id。

    之後的 holdout 只採計 registered_at_ms 之後成交的單（out_of_time_holdout）→ 防 HARKing。
    """
    rec = ledger.append_register(bucket_key=bucket_key, hypothesis=hypothesis,
                                 registered_at_ms=registered_at_ms,
                                 trial_id=trial_id, ts_ms=ts_ms)
    return rec["trial_id"]


def out_of_time_holdout(trades: list[dict], registered_at_ms: int, *,
                        ts_key: str = "exit_ts_ms", r_key: str = "realized_r") -> list[float]:
    """只取凍結後（成交時刻 > registered_at_ms）已平倉單的 R 序列。防 HARKing 的結構性閘。

    trades：dict 列；需含成交/平倉時刻欄（ts_key）與已實現 R 欄（r_key）。
    缺欄或非凍結後者一律排除（fail-closed 偏保守）。
    """
    out = []
    for t in trades:
        try:
            ts = t.get(ts_key)
            r = t.get(r_key)
            if ts is None or r is None:
                continue
            if int(ts) > int(registered_at_ms):
                out.append(float(r))
        except (TypeError, ValueError):
            continue
    return out


# ════════════════════════════════════════════════════════════════════════
#  四關閘門 + 總判定
# ════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    stat: float | None
    threshold: float | None
    detail: str


@dataclass(frozen=True)
class L2Verdict:
    passed: bool
    gates: tuple[GateResult, ...]
    n_trials: int
    bucket_key: str
    summary: str = ""


def gate_min_trl(returns: list[float]) -> GateResult:
    n = len(returns)
    if n < MIN_BUCKET_N:
        return GateResult("minTRL", False, float(n), float(MIN_BUCKET_N),
                          f"樣本 {n}<{MIN_BUCKET_N} → fail-closed（小樣本不准自欺）")
    mtrl = min_trl(returns, 0.0, 0.95)
    if mtrl == float("inf"):
        return GateResult("minTRL", False, None, float(MIN_BUCKET_N),
                          "SR≤0 或無離散 → minTRL=∞（證不出 edge）")
    passed = n >= mtrl
    return GateResult("minTRL", passed, round(mtrl, 1), float(n),
                      f"需 {mtrl:.0f} 筆才足以證明 SR>0；現有 {n} 筆"
                      f"{'（足）' if passed else '（不足）'}")


def gate_dsr(returns: list[float], n_trials: int, sr_variance: float | None) -> GateResult:
    dsr = deflated_sharpe(returns, n_trials, sr_variance)
    passed = dsr >= DSR_MIN
    sv = "—" if sr_variance is None else f"{sr_variance:.4f}"
    return GateResult("DSR", passed, round(dsr, 4), DSR_MIN,
                      f"DSR={dsr:.3f}（n_trials={n_trials}、實測跨試驗SR變異={sv}）"
                      f" {'≥' if passed else '<'}{DSR_MIN}")


def gate_pbo(matrix: list[list[float]] | None) -> GateResult:
    if not matrix or len(matrix) == 0 or len(matrix[0]) < PBO_MIN_CONFIGS:
        return GateResult("PBO/CSCV", False, None, PBO_MAX,
                          f"同期配置<{PBO_MIN_CONFIGS} → 無法評估選擇性過擬合 → fail-closed")
    pbo, n_combos, n_cfg, S = cscv_pbo(matrix)
    if pbo is None:
        return GateResult("PBO/CSCV", False, None, PBO_MAX,
                          "資料不足以做 CSCV（T 太短）→ fail-closed")
    passed = pbo <= PBO_MAX
    return GateResult("PBO/CSCV", passed, round(pbo, 4), PBO_MAX,
                      f"PBO={pbo:.3f}（{n_cfg}配置×{S}塊×{n_combos}組合）"
                      f" {'≤' if passed else '>'}{PBO_MAX}")


def gate_bhy_fdr(candidate_p: float, other_p: list[float], q: float = FDR_Q) -> GateResult:
    survives = bhy_survives(candidate_p, other_p, q)
    m = len(other_p) + 1
    return GateResult("BHY-FDR", survives, round(candidate_p, 4), q,
                      f"候選 p={candidate_p:.4f}；族群 m={m}（含 file-drawer）"
                      f" → {'存活=真發現' if survives else '被多重檢定刷掉'}（q={q}）")


def evaluate_candidate(ledger: TrialLedger, *, bucket_key: str,
                       candidate_returns: list[float],
                       matrix: list[list[float]] | None = None,
                       hypothesis: str = "", registered_at_ms: int | None = None,
                       trial_id: str | None = None, ts_ms: int | None = None,
                       append: bool = True) -> L2Verdict:
    """跑四關 → 總判定。**n_trials / 跨試驗 SR 變異 / FDR 族群全部從 ledger 取，禁手傳。**

    candidate_returns：必須是「凍結後」的 out-of-time holdout R 序列（防 HARKing；
        呼叫端用 out_of_time_holdout() 產生）。
    matrix：T×N 同期多配置逐期報酬（給 PBO/CSCV）。None → PBO fail-closed。
    append=True：無論成敗都 append（file-drawer），讓下次評估看得到、分母累計。
    """
    # 1) 從 ledger 取族群（評估候選之前的既有試驗）
    prior = ledger.distinct_trials(bucket_key)
    n_trials = len(prior) + 1                    # 禁手傳：候選 = +1
    other_p = [v["p_value"] for v in prior.values()]

    # 2) 候選自身統計
    cand_sr = sharpe(candidate_returns)
    cand_p = trial_p_value(candidate_returns)

    # 3) 實測跨試驗 SR 變異數（含候選）
    sr_list = [v["sharpe"] for v in prior.values()] + [cand_sr]
    sr_var = pvariance(sr_list) if len(sr_list) >= 2 else None

    # 4) 四關
    gates = (
        gate_min_trl(candidate_returns),
        gate_dsr(candidate_returns, n_trials, sr_var),
        gate_pbo(matrix),
        gate_bhy_fdr(cand_p, other_p),
    )
    passed = all(g.passed for g in gates)

    # 5) file-drawer：成敗都 append
    if append:
        tid = trial_id or _derive_trial_id(
            bucket_key, hypothesis or "(unnamed)",
            registered_at_ms if registered_at_ms is not None else 0)
        ledger.append_evaluation(
            bucket_key=bucket_key, trial_id=tid, hypothesis=hypothesis,
            returns=candidate_returns, sharpe_val=cand_sr, p_value=cand_p,
            passed=passed, registered_at_ms=registered_at_ms, ts_ms=ts_ms)

    head = "✅ 通過四關" if passed else "❌ 未過閘"
    detail = "；".join(f"{g.name}{'✓' if g.passed else '✗'}" for g in gates)
    summary = f"{head}（bucket={bucket_key}, n_trials={n_trials}）：{detail}"
    return L2Verdict(passed, gates, n_trials, bucket_key, summary)


# ════════════════════════════════════════════════════════════════════════
#  CI 守門：門檻只能調嚴
# ════════════════════════════════════════════════════════════════════════
def assert_thresholds_only_stricter() -> None:
    """CI 斷言：現行門檻相對凍結基準只能更嚴，不可放寬。放寬 = 自欺，直接 raise。"""
    current = {"MIN_BUCKET_N": MIN_BUCKET_N, "DSR_MIN": DSR_MIN,
               "PBO_MAX": PBO_MAX, "FDR_Q": FDR_Q}
    for key, (base, direction) in _FROZEN_THRESHOLDS.items():
        cur = current[key]
        if direction == "ge" and cur < base:
            raise AssertionError(
                f"門檻 {key}={cur} 比凍結基準 {base} 鬆（應 ≥）— 門檻只能調嚴！")
        if direction == "le" and cur > base:
            raise AssertionError(
                f"門檻 {key}={cur} 比凍結基準 {base} 鬆（應 ≤）— 門檻只能調嚴！")


# ════════════════════════════════════════════════════════════════════════
#  渲染（繁中，含誠實橫幅）
# ════════════════════════════════════════════════════════════════════════
def render_verdict(v: L2Verdict) -> str:
    L = []
    L.append("═" * 66)
    L.append("L2 統計嚴謹度閘門（復盤引擎 step5｜參數變更前的把關）")
    L.append("═" * 66)
    L.append("⚠️ 把關靠統計嚴謹度而非逐次人工點頭；數字皆由傳入真實樣本即時算出。")
    L.append(f"   bucket={v.bucket_key}｜n_trials（ledger 累計，含 file-drawer）={v.n_trials}")
    L.append("")
    for g in v.gates:
        mark = "✅" if g.passed else "❌"
        L.append(f"  {mark} {g.name:<9} {g.detail}")
    L.append("")
    L.append("【總判定（四關全過才放行參數變更）】")
    L.append(f"  → {'✅ 通過：允許 #52 寫入此參數變更' if v.passed else '❌ 擋下：統計上未證實，不准寫入'}")
    L.append("═" * 66)
    return "\n".join(L)


# ════════════════════════════════════════════════════════════════════════
#  離線自測（合成資料，無需網路/真 ledger）
# ════════════════════════════════════════════════════════════════════════
def _selftest() -> bool:
    """合成資料跑通四關 + ledger 鏈 + 防竄改 + n_trials 累計；只驗『不爆、行為正確』。"""
    import math
    import random
    import tempfile

    # 門檻不可被偷放寬
    assert_thresholds_only_stricter()

    rng = random.Random(42)

    # — BHY-FDR 手算對照（小例）—
    # m=5、q=0.05；c(5)=1+1/2+1/3+1/4+1/5=2.283…；門檻序列 p(k)≤k/(5·2.283)·0.05
    rej = bhy_fdr_rejected([0.001, 0.2, 0.5, 0.7, 0.9], 0.05)
    assert 0 in rej, "極小 p 值應存活"
    assert 4 not in rej, "極大 p 值應被刷掉"

    # — PBO：純雜訊矩陣（無真 edge）選擇程序不該泛化 → PBO 偏高 —
    T, N = 240, 8
    noise = [[rng.gauss(0, 1) for _ in range(N)] for _ in range(T)]
    pbo_noise, _, _, _ = cscv_pbo(noise)
    assert pbo_noise is not None and pbo_noise >= 0.30, f"純雜訊 PBO 應偏高，得 {pbo_noise}"

    # — PBO：放一個全期真 edge 的配置（其餘雜訊）→ 該配置 IS/OOS 都贏 → PBO 偏低 —
    edge = [[rng.gauss(0, 1) for _ in range(N - 1)] + [rng.gauss(0.45, 1)]
            for _ in range(T)]
    pbo_edge, _, _, _ = cscv_pbo(edge)
    assert pbo_edge is not None and pbo_edge <= pbo_noise, \
        f"真 edge 的 PBO({pbo_edge}) 應 ≤ 純雜訊({pbo_noise})"

    # — minTRL fail-closed：n<30 必擋 —
    assert not gate_min_trl([0.1] * 10).passed, "n<30 應 fail-closed"

    # — ledger：append → 驗鏈 → 竄改偵測 → n_trials 累計 —
    with tempfile.TemporaryDirectory() as td:
        led = TrialLedger(Path(td) / "trial_ledger.jsonl")
        ok, _ = led.verify_chain()
        assert ok, "空 ledger 應視為完整"

        # 強 edge 候選（正期望、夠分散、>30 筆）
        cand = [rng.gauss(0.30, 1.0) for _ in range(120)]
        v1 = evaluate_candidate(led, bucket_key="BTC|bull", candidate_returns=cand,
                                matrix=edge, hypothesis="h1", ts_ms=1)
        assert v1.n_trials == 1, "首次評估 n_trials 應為 1"
        ok, detail = led.verify_chain()
        assert ok, f"append 後鏈應完整：{detail}"

        # 第二個不同假設 → n_trials 由 ledger 累計成 2（禁手傳的證明）
        v2 = evaluate_candidate(led, bucket_key="BTC|bull", candidate_returns=cand,
                                matrix=edge, hypothesis="h2", ts_ms=2)
        assert v2.n_trials == 2, f"第二假設 n_trials 應為 2（ledger 累計），得 {v2.n_trials}"

        # file-drawer：失敗試驗也記、也算 — 灌一個爛假設後 n_trials 再 +1
        bad = [rng.gauss(-0.2, 1.0) for _ in range(40)]
        v3 = evaluate_candidate(led, bucket_key="BTC|bull", candidate_returns=bad,
                                matrix=noise, hypothesis="h3_bad", ts_ms=3)
        assert not v3.passed, "負期望+純雜訊矩陣應被擋"
        v4 = evaluate_candidate(led, bucket_key="BTC|bull", candidate_returns=cand,
                                matrix=edge, hypothesis="h4", ts_ms=4)
        assert v4.n_trials == 4, f"含失敗試驗 n_trials 應累計到 4，得 {v4.n_trials}"

        # 竄改偵測：手改最後一行的 sharpe → 驗鏈必失敗
        lines = (Path(td) / "trial_ledger.jsonl").read_text(encoding="utf-8").splitlines()
        rec = json.loads(lines[-1]); rec["sharpe"] = 9.99
        lines[-1] = json.dumps(rec, ensure_ascii=False)
        (Path(td) / "trial_ledger.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        ok, _ = led.verify_chain()
        assert not ok, "竄改後驗鏈必須失敗"

    # — out-of-time holdout：只取凍結後的單 —
    trades = [{"exit_ts_ms": 100, "realized_r": 1.0},
              {"exit_ts_ms": 200, "realized_r": 2.0},
              {"exit_ts_ms": 50, "realized_r": -9.0}]
    ho = out_of_time_holdout(trades, registered_at_ms=150)
    assert ho == [2.0], f"凍結後只該留 ts>150 的單，得 {ho}"

    print("  自測通過：PBO/CSCV + BHY-FDR + minTRL fail-closed + ledger 鏈/防竄改"
          " + n_trials 累計 + HARKing holdout 皆正常 ✅")
    return True


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if _selftest() else 1)
    if "--verify-ledger" in sys.argv:
        led = TrialLedger()
        ok, detail = led.verify_chain()
        print(f"[l2_stat_gates] ledger={led.path}")
        print(f"[l2_stat_gates] verify_chain: {'✅' if ok else '❌'} {detail}")
        print(f"[l2_stat_gates] 試驗數（distinct evaluated）= {led.count_trials()}")
        sys.exit(0 if ok else 1)
    print(__doc__)
    print("用法：--selftest（合成自測） | --verify-ledger（驗真 ledger 鏈）")
