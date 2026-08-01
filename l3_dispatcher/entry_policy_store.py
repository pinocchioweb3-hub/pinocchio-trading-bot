"""復盤引擎 step9-b（task#61）── 入場積極度政策的**落地層**：per-(symbol×regime) 覆寫。

定位（承 entry_policy_cc.py 的重放式 champion/challenger，與 auto_param_store.py 同構）：
    entry_policy_cc.compare_entry_policy 對 deepdive 自身的真實凍結計畫，用『真實後續 K 線』
    重放 champion(現行深限價可到期) vs challenger(D 深限價到期轉市價／市價即進)，過 L2 四關
    得到 EntryPolicyVerdict。若 verdict.promote 為真（self-check ∧ L2 ∧ per-proposed 配對更好），
    本模組把該 (symbol, quadrant) 的『入場積極度』寫進活躍覆寫表，模擬盤下一筆同桶進場即生效。
    → 把關靠**統計嚴謹度**（L2），不靠人工逐次點頭；透明在 CEO 報告，可事後 rollback。

三條安全紅線在本模組的落點（務必保持，與 auto_param_store 一致）：
    紅線①（真錢 AI 永不自動下單）：本表**只驅動模擬盤 paper／demo 的入場積極度**。真錢執行層
        完全不讀本表；改的是『限價單到期要不要追市價』這種模擬盤掛單行為，不跨越執行閘。
    紅線②（對外發布逐次人工）：本表是本機檔（data_dir()，非專案目錄），不對外、不進公開 repo。
    紅線③（不捏造）：每次裁決**一律寫稽核**（promote 與 hold 都留痕）；活躍表**只在
        verdict.promote（EV 超越性）或 verdict.coverage_promote（涵蓋率非劣性，task#63 僅 D）
        為真時更新**。今日對齊樣本 <30 → L2 minTRL fail-closed、且涵蓋率晉升 n<30 同樣擋下 →
        0 晉升 → 本表恆為空 → resolve 回 None → 模擬盤維持現行深限價可到期行為（零行為改變）。

設計約束（與 auto_param_store 同規格，便於 paper_journal 在進場當下輕量解析）：
    - 依賴極輕（只用 stdlib + botpaths）；不可反向依賴 entry_policy_cc / l2_stat_gates（避免 import 環）。
    - resolve_entry_policy 每次**重讀檔**且**fail-safe**：任何異常 → 回 None → 呼叫端用現行
      深限價行為（即今日行為）。絕不讓壞檔/競態讓模擬盤進場崩。
    - 活躍表原子寫（temp + os.replace）；稽核 append-only JSONL。
    - verdict 以 duck-typing 讀屬性（不綁 EntryPolicyVerdict 型別）；且因 verdict.challenger 僅為
      『名稱字串』非政策 kind，挑戰者的 kind **必須由呼叫端顯式傳入**（challenger_kind），
      與 auto_param_store 要呼叫端傳 challenger_alloc 同理。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from botpaths import data_dir

_LOG = logging.getLogger("l3_dispatcher.entry_policy_store")

ACTIVE_NAME = "entry_policy_active.json"   # 活躍覆寫表（只在 promote 時更新）
AUDIT_NAME = "entry_policy_audit.jsonl"    # append-only 稽核（promote/hold/rollback 全留痕）

# 與 entry_policy_cc.EntryPolicy.__post_init__ 完全一致的合法 kind 集合（單一真相）。
VALID_KINDS = ("market", "limit_expire", "limit_convert")
# limit_expire ＝現行 champion／預設行為；覆寫成它＝等同無覆寫，故視為「不覆寫」。
DEFAULT_KIND = "limit_expire"

# task#62：池化哨兵。symbol 或 quadrant ＝ POOL("*") 代表「跨此維度聚合」的池化桶。
#   階層（最具體→最一般）：(symbol, quadrant) → (POOL, quadrant) → (POOL, POOL)。
#   為何需要：(symbol×quadrant) 分桶太細 → 單桶到 MIN_BUCKET_N=30 需 ~671 天，優化器
#   結構性 inert ~1.8 年。task#59 已證入場積極度 regime-invariant → 全域池最有據、學最快
#   （~5 天填滿）。部分池化＝有資料就特化(symbol×regime)、沒資料退回池，忠實於使用者
#   「能 per-symbol×per-regime 就 per-symbol×per-regime」的本意，且只 paper/demo（紅線①）。
POOL = "*"


# ── 路徑 ────────────────────────────────────────────────────────────────
def active_path(path: str | Path | None = None) -> Path:
    return Path(path) if path else data_dir() / ACTIVE_NAME


def audit_path(path: str | Path | None = None) -> Path:
    return Path(path) if path else data_dir() / AUDIT_NAME


def bucket_key(symbol: str, quadrant: str) -> str:
    """覆寫桶鍵＝symbol×regime 象限（per-symbol × per-regime 自適應入場積極度）。
    與 auto_param_store.bucket_key／entry_policy_cc.bucket_key 同切面。"""
    return f"{symbol}|{quadrant or 'unknown'}"


# ── kind 驗證 ─────────────────────────────────────────────────────────────
def _valid_kind(kind) -> bool:
    return isinstance(kind, str) and kind in VALID_KINDS


# ── 檔案 I/O（fail-safe + 原子寫；與 auto_param_store 同模式） ─────────────
# 讀取三態（本模組的誠實核心）。⛔ 只有 MISSING 可以當「真·從來沒有任何覆寫」用；
# UNREADABLE ＝檔在、但內容**未知**，任何「所以沒有覆寫」的推論在此都不成立。
LOAD_OK = "ok"
LOAD_MISSING = "missing"
LOAD_UNREADABLE = "unreadable"

# 熱路徑壞檔只出聲一次（依 路徑+mtime+大小 去重），避免每筆進場洗版。
_WARNED_BAD: set = set()


def _warn_unreadable_once(path: str | Path | None = None) -> None:
    """壞檔在進場熱路徑上出聲一次。不寫檔、不擋進場（fail-safe 仍成立）。

    ⚠️ 收「未解析的 path 參數」而非 Path：apply_verdict／rollback 的**參數名就叫 active_path**，
       在那些函式裡模組層的 active_path() 被遮蔽，只有在這裡解析才叫得到。
    """
    p = active_path(path)
    try:
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
    except OSError:
        key = (str(p), None, None)
    if key in _WARNED_BAD:
        return
    _WARNED_BAD.add(key)
    _LOG.warning(
        "🚨 入場積極度活躍表存在但讀不出來（%s）→ 本次進場退回現行深限價行為；"
        "⛔ 這不代表『沒有覆寫』——覆寫內容未知，晉升與回退已自動停手，原檔保留待人工檢視。", p)


def _load_active_status(path: str | Path | None = None) -> tuple[dict, str]:
    """讀活躍表，回 (buckets, status)。status ∈ {ok, missing, unreadable}。

    為何要三態：舊版把「檔不存在」與「檔在但解不開」都折成 {}，而 apply_verdict 會拿這個 {}
    當**整張表**原子覆寫回去 → 一次讀失敗＝其餘所有已晉升桶不可逆消失（連半截檔都不留）。
    """
    p = active_path(path)
    try:
        if not p.exists():
            return {}, LOAD_MISSING
    except OSError:
        return {}, LOAD_UNREADABLE   # 連「在不在」都問不出來 → 未知，不可當沒有
    try:
        with open(p, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except Exception:
        return {}, LOAD_UNREADABLE
    if not isinstance(obj, dict):
        return {}, LOAD_UNREADABLE
    buckets = obj.get("buckets")
    if buckets is None or not isinstance(buckets, dict):
        # 能解析但結構不符（缺 buckets／型別不對）＝仍是「內容未知」，不是「空表」
        return {}, LOAD_UNREADABLE
    return buckets, LOAD_OK


def _load_active(path: str | Path | None = None) -> dict:
    """讀活躍表；任何異常 → {}（fail-safe，等同無覆寫＝用現行深限價行為）。

    ⚠️ 只給**進場熱路徑**用（壞檔絕不可讓進場崩）。寫入端／渲染端一律改用
    _load_active_status，否則「讀不出來」會再次被折成「沒有覆寫」。
    """
    buckets, status = _load_active_status(path)
    if status == LOAD_UNREADABLE:
        _warn_unreadable_once(path)
    return buckets


def _atomic_write_active(buckets: dict, at_ms: int,
                         path: str | Path | None = None) -> None:
    """原子寫活躍表（temp + fsync + os.replace）。失敗向上拋給 apply_verdict 收斂。

    fsync 不可省：只有 os.replace 的話，內容可能還在作業系統快取裡就換名，斷電後目的地
    會留下零長度／半截檔——那正是上面 UNREADABLE 的自產來源（本機有斷電事件史）。
    """
    p = active_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "updated_at_ms": int(at_ms), "buckets": buckets}
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".entry_policy_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _append_audit(rec: dict, path: str | Path | None = None) -> None:
    """append 一行稽核。fail-safe：寫不進去也不擋主流程（只是少一行留痕）。"""
    p = audit_path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ── 對外：進場解析（paper_journal/deepdive 在進場當下呼叫） ────────────────
def _resolution_ladder(symbol: str, quadrant: str) -> list[str]:
    """task#62 階層式部分池化的查找順序（最具體 → 最一般，已去重）。

        (symbol, quadrant)  ＝ per-symbol × per-regime（最具體；使用者本意的特化層）
        (POOL,   quadrant)  ＝ 象限池（跨 symbol、同 regime）
        (POOL,   POOL)      ＝ 全域池（跨一切；task#59 regime-invariant → 最有據、學最快）

    最具體且有有效覆寫者勝。symbol 本身已是 POOL 時，前兩階自然塌陷（去重後不重複查）。
    """
    keys: list[str] = []
    for sk, qk in ((symbol, quadrant), (POOL, quadrant), (POOL, POOL)):
        bk = bucket_key(sk, qk)
        if bk not in keys:
            keys.append(bk)
    return keys


def resolve_entry_policy(symbol: str, quadrant: str, *,
                         active_path: str | Path | None = None) -> str | None:
    """回該 (symbol, quadrant) 生效的入場積極度 kind 覆寫；無/異常/＝預設 → None。

    task#62：採**階層式部分池化** fallback ladder（見 _resolution_ladder）——先找
    per-symbol×regime 桶，沒有就退回象限池 (*,quadrant)，再退回全域池 (*,*)。
    最具體且有有效覆寫者勝；全無 → None（＝今日深限價可到期行為）。

    回傳語意（呼叫端據此決定『如何掛單』，不覆寫 LLM 提的結構區位）：
        None            ＝無覆寫 → 用現行深限價可到期行為（今日行為，champion）。
        "limit_convert" ＝深限價到期未成交 → 改市價追（D，救涵蓋率）。
        "market"        ＝訊號當下市價即進。
        ("limit_expire" 與無覆寫等價，會被正規化成 None。)

    **fail-safe**：每次重讀檔；任何錯誤都回 None，永不讓模擬盤進場崩。
    """
    try:
        buckets = _load_active(active_path)
        for bk in _resolution_ladder(symbol, quadrant):
            rec = buckets.get(bk)
            if not rec:
                continue
            kind = rec.get("kind")
            if not _valid_kind(kind) or kind == DEFAULT_KIND:
                continue   # 壞值或＝預設 → 視同此階無覆寫，續往更一般階找
            return kind
        return None
    except Exception:
        return None


# ── 對外：落地裁決（auto_optimizer 過 L2 後呼叫） ─────────────────────────
def apply_verdict(verdict, *, symbol: str, quadrant: str,
                  challenger_kind: str, champion_kind: str | None = None,
                  at_ms: int, active_path: str | Path | None = None,
                  audit_path: str | Path | None = None) -> dict:
    """消費一個 EntryPolicyVerdict（duck-typed）：**一律寫稽核**；**只在晉升時改活躍表**。

    晉升＝兩條路徑任一（task#63）：
        ① verdict.promote          ＝EV 超越性（self-check ∧ L2 四關 ∧ 配對平均更好）。
        ② verdict.coverage_promote ＝涵蓋率非劣性（僅 D／limit_convert：EV 對 champion 統計
                                     非劣 ∧ 實質回補涵蓋率 ∧ n≥30）。解 D 因 EV-neutral 永過不了
                                     路徑①、導致 live trade_monitor 到期轉市價分支卡死的結構問題。
        兩路皆假 → action=hold（活躍表不動），但稽核仍揭示 coverage_delta_pp＝救回多少涵蓋率。
        晉升依據記入 promote_basis（"ev_superiority"｜"coverage_noninferiority"）＝透明可回溯。

    ⚠️ EntryPolicyVerdict 的 .champion/.challenger 是『名稱字串』非政策 kind（見 entry_policy_cc.py），
       故挑戰者的 kind **必須由呼叫端顯式傳入**：
        challenger_kind（必填，∈ VALID_KINDS）、champion_kind（選填，僅供稽核留痕）。

    Args:
        verdict: 帶 .promote/.bucket_key/.champ_mean_r/.chal_mean_r/.n_aligned/
                 .champ_fill_rate/.chal_fill_rate/.coverage_delta_pp/.l2_summary/
                 .reasons 的物件（容缺）。
        challenger_kind: 挑戰者入場積極度 kind（晉升時寫進活躍表的值）。
        champion_kind: 現行 champion kind（只進稽核，便於回溯對照）。
        at_ms: 事件時戳（毫秒，由呼叫端傳入，本模組不碰 wall-clock）。

    Returns: {action: "promote"|"hold", bucket, to_kind, note}
    """
    bkey = bucket_key(symbol, quadrant)
    promote = bool(getattr(verdict, "promote", False))
    cov_promote = bool(getattr(verdict, "coverage_promote", False))
    # task#63：晉升＝EV 超越性 ∨ 涵蓋率非劣性（僅 D）。依據透明留痕、可事後回滾。
    activate = promote or cov_promote
    promote_basis = ("ev_superiority" if promote
                     else ("coverage_noninferiority" if cov_promote else None))

    chal_ok = _valid_kind(challenger_kind)
    champ_kind_norm = champion_kind if _valid_kind(champion_kind) else None

    buckets, load_status = _load_active_status(active_path)
    unreadable = load_status == LOAD_UNREADABLE
    prev = buckets.get(bkey) or {}
    prev_kind = prev.get("kind")

    action = "hold"
    to_kind = prev_kind
    note = ""

    if unreadable:
        # ⛔ 讀不出來時**絕不寫**：舊碼會拿空 buckets 當整張表覆寫回去，把其餘所有已晉升桶
        # 原子地、乾淨地抹掉（不可逆）。停手＝維持現狀，原檔原封不動留給人工檢視。
        _warn_unreadable_once(active_path)
        action = "blocked_unreadable_active"
        to_kind = None
        note = ("活躍覆寫表存在但讀不出來 → 本次裁決停手不寫（原檔未動）。"
                "⛔ 若照舊寫入會把整張表抹成只剩這一桶，其餘已晉升桶不可逆消失。"
                "請人工檢視該檔後再讓優化器續行。")
    elif activate and chal_ok and challenger_kind != DEFAULT_KIND:
        action = "promote"
        to_kind = challenger_kind
        buckets[bkey] = {
            "kind": challenger_kind,
            "policy_name": getattr(verdict, "challenger", None),
            "promoted_at_ms": int(at_ms),
            "promote_basis": promote_basis,
            "champ_mean_r": getattr(verdict, "champ_mean_r", None),
            "chal_mean_r": getattr(verdict, "chal_mean_r", None),
            "champ_fill_rate": getattr(verdict, "champ_fill_rate", None),
            "chal_fill_rate": getattr(verdict, "chal_fill_rate", None),
            "coverage_delta_pp": getattr(verdict, "coverage_delta_pp", None),
            "ev_noninf_lo": getattr(verdict, "ev_noninf_lo", None),
            "n_aligned": getattr(verdict, "n_aligned", None),
            "l2_bucket_key": getattr(verdict, "bucket_key", bkey),
            "champion_kind": champ_kind_norm,
        }
        try:
            _atomic_write_active(buckets, at_ms, active_path)
        except Exception as e:
            # 寫失敗 → 退回 hold，不假裝已生效（紅線③：不捏造）
            action = "hold"
            to_kind = prev_kind
            note = f"活躍表寫入失敗，未生效（fail-safe）：{type(e).__name__}"
    elif activate and not chal_ok:
        note = f"verdict 晉升為真但 challenger_kind 非法（{challenger_kind!r}）→ 拒寫（fail-safe）"
    elif activate and challenger_kind == DEFAULT_KIND:
        note = "verdict 晉升為真但 challenger＝預設行為（limit_expire）→ 無需覆寫"

    rec = {
        "at_ms": int(at_ms),
        "action": action,
        "bucket": bkey,
        "l2_bucket_key": getattr(verdict, "bucket_key", bkey),
        "promote": promote,
        "coverage_promote": cov_promote,
        "promote_basis": promote_basis,
        "from_kind": prev_kind,
        # ⛔ 讀不出來時 from_kind 的 None 是「未知」不是「本來就沒有」——用旗標把兩者分開，
        # 否則錯誤宣稱會沉進 append-only 稽核軌跡，事後再也分不出來。
        "from_kind_known": not unreadable,
        "active_unreadable": unreadable,
        "to_kind": to_kind,
        "challenger_kind": challenger_kind if chal_ok else None,
        "champion_kind": champ_kind_norm,
        "champion_name": getattr(verdict, "champion", None),
        "challenger_name": getattr(verdict, "challenger", None),
        "champ_mean_r": getattr(verdict, "champ_mean_r", None),
        "chal_mean_r": getattr(verdict, "chal_mean_r", None),
        "champ_fill_rate": getattr(verdict, "champ_fill_rate", None),
        "chal_fill_rate": getattr(verdict, "chal_fill_rate", None),
        "coverage_delta_pp": getattr(verdict, "coverage_delta_pp", None),
        "ev_noninferior": getattr(verdict, "ev_noninferior", None),
        "ev_noninf_lo": getattr(verdict, "ev_noninf_lo", None),
        "n_aligned": getattr(verdict, "n_aligned", None),
        "self_check_ok": getattr(verdict, "self_check_ok", None),
        "l2_passed": getattr(verdict, "l2_passed", None),
        "l2_summary": getattr(verdict, "l2_summary", None),
        "reasons": list(getattr(verdict, "reasons", []) or []),
        "note": note,
    }
    _append_audit(rec, audit_path)
    return {"action": action, "bucket": bkey, "to_kind": rec["to_kind"], "note": note}


def rollback(symbol: str, quadrant: str, *, at_ms: int, reason: str = "",
             active_path: str | Path | None = None,
             audit_path: str | Path | None = None) -> dict:
    """移除某桶覆寫 → 回退現行深限價行為（事後人工/CEO 可逆）。一律寫稽核。

    ⛔ 活躍表讀不出來時**不得回報回退成功**：舊碼會因 buckets={} 判 existed=False，回一句
    「本來就沒有可移除的覆寫」——但檔裡的覆寫可能原封不動還在生效，等於安全閥在最需要它的
    時候假裝自己動過。改為明確擋下並留痕，讓人看得到「回退沒有發生」。
    """
    bkey = bucket_key(symbol, quadrant)
    buckets, load_status = _load_active_status(active_path)
    if load_status == LOAD_UNREADABLE:
        _warn_unreadable_once(active_path)
        note = ("活躍覆寫表存在但讀不出來 → 回退**未執行**（原檔未動）。"
                "⛔ 不可讀成『本來就沒有覆寫』：內容未知，覆寫可能仍在生效。")
        _append_audit({"at_ms": int(at_ms), "action": "rollback_blocked_unreadable",
                       "bucket": bkey, "existed": None, "from_kind": None,
                       "from_kind_known": False, "active_unreadable": True,
                       "to_kind": None, "reason": reason or "manual",
                       "note": note}, audit_path)
        return {"action": "rollback_blocked_unreadable", "bucket": bkey,
                "existed": None, "note": note}
    prev = buckets.get(bkey) or {}
    prev_kind = prev.get("kind")
    existed = bkey in buckets
    if existed:
        del buckets[bkey]
        try:
            _atomic_write_active(buckets, at_ms, active_path)
        except Exception as e:
            _append_audit({"at_ms": int(at_ms), "action": "rollback_failed",
                           "bucket": bkey, "note": f"{type(e).__name__}: {e}"}, audit_path)
            return {"action": "rollback_failed", "bucket": bkey}
    _append_audit({
        "at_ms": int(at_ms), "action": "rollback", "bucket": bkey,
        "existed": existed, "from_kind": prev_kind, "to_kind": None,
        "reason": reason or "manual",
    }, audit_path)
    return {"action": "rollback", "bucket": bkey, "existed": existed}


# ── 繁中渲染（CEO 報告透明化） ────────────────────────────────────────────
_KIND_ZH = {
    "market": "市價即進",
    "limit_convert": "深限價到期轉市價(D)",
    "limit_expire": "深限價可到期(預設)",
}


def _bucket_label(bk: str) -> str:
    """把覆寫桶鍵翻成可讀標籤，明示池化層級（task#62）。
        "*|*"      → 全域池(跨一切)
        "*|<q>"    → 象限池·<q>（跨 symbol）
        "<s>|<q>"  → <s>×<q>（per-symbol×regime）
    """
    s, _, q = bk.partition("|")
    if s == POOL and q == POOL:
        return "全域池(跨一切)"
    if s == POOL:
        return f"象限池·{q}（跨 symbol）"
    return bk


def render_active(active_path: str | Path | None = None) -> str:
    """活躍覆寫表摘要（給 CEO/調參報告）。空表 → 明確說「無覆寫，全用現行行為」。

    ⛔ 讀不出來 ≠ 空表：舊版會把壞檔渲染成「目前 0 桶有覆寫……這是預期的誠實狀態」，
    在報告裡把一個未知狀態講成一句正面保證。
    """
    buckets, load_status = _load_active_status(active_path)
    if load_status == LOAD_UNREADABLE:
        return ("🚨 <b>入場積極度覆寫</b>：活躍表<b>存在但讀不出來</b>（內容未知）。\n"
                "<i>⛔ 這不是「0 桶覆寫」——進場已 fail-safe 退回現行深限價行為，"
                "但晉升與回退已自動停手，原檔保留待人工檢視。</i>")
    if not buckets:
        return ("🎚️ <b>入場積極度覆寫</b>：目前 <b>0</b> 桶有覆寫"
                "（全用現行深限價可到期行為）。\n"
                "<i>對齊樣本未達 L2 統計門檻前不會有任何覆寫 — 這是預期的誠實狀態。</i>")
    lines = [f"🎚️ <b>入場積極度覆寫</b>（模擬盤入場政策，共 {len(buckets)} 桶）"]
    for bk, rec in sorted(buckets.items()):
        kind = rec.get("kind", "?")
        kzh = _KIND_ZH.get(kind, kind)
        n = rec.get("n_aligned")
        cfr, hfr = rec.get("champ_fill_rate"), rec.get("chal_fill_rate")
        cov = (f"｜成交率 {cfr}%→{hfr}%" if isinstance(cfr, (int, float))
               and isinstance(hfr, (int, float)) else "")
        cm, chm = rec.get("champ_mean_r"), rec.get("chal_mean_r")
        delta = (f"｜{cm:+.3f}→{chm:+.3f}R" if isinstance(cm, (int, float))
                 and isinstance(chm, (int, float)) else "")
        lines.append(f"  [{_bucket_label(bk)}] → {kzh}（對齊 n={n}{cov}{delta}）")
    lines.append("<i>來源＝champion/challenger 重放過 L2 四關晉升；只驅動模擬盤（紅線①）。</i>")
    return "\n".join(lines)


def render_audit_tail(n: int = 10, path: str | Path | None = None) -> str:
    """最近 n 筆裁決留痕（promote/hold/rollback）。"""
    p = audit_path(path)
    try:
        if not p.exists():
            return "（尚無入場積極度裁決紀錄）"
        with open(p, "r", encoding="utf-8") as f:
            rows = [json.loads(ln) for ln in f if ln.strip()]
    except Exception:
        return "（入場積極度稽核讀取失敗）"
    if not rows:
        return "（尚無入場積極度裁決紀錄）"
    tail = rows[-n:]
    out = [f"🧾 <b>入場積極度裁決</b>（最近 {len(tail)}/{len(rows)} 筆）"]
    icon = {"promote": "✅晉升", "hold": "⏸️維持", "rollback": "↩️回退",
            "rollback_failed": "⚠️回退失敗"}
    for r in tail:
        act = icon.get(r.get("action", ""), r.get("action", "?"))
        bk = r.get("bucket", "?")
        tk = r.get("to_kind")
        tkzh = _KIND_ZH.get(tk, tk) if tk else "—"
        note = f"｜{r['note']}" if r.get("note") else ""
        out.append(f"  {act} [{_bucket_label(bk)}] →{tkzh}{note}")
    return "\n".join(out)


# ── 自我測試（離線、暫存檔、零真 DB） ────────────────────────────────────
def _selftest() -> bool:
    import types

    with tempfile.TemporaryDirectory() as td:
        ap = Path(td) / ACTIVE_NAME
        au = Path(td) / AUDIT_NAME

        # 1) 空表 → resolve 回 None（＝用現行行為，今日行為）
        assert resolve_entry_policy("BTC", "price_up_oi_up", active_path=ap) is None

        # EntryPolicyVerdict 的 champion/challenger 是『名稱字串』（非 kind）
        def _v(promote, **kw):
            base = dict(promote=promote, bucket_key="BTC|price_up_oi_up",
                        champion="champion(現行深限價可到期)", challenger="D_深限價到期轉市價",
                        champ_mean_r=-0.05, chal_mean_r=-0.01,
                        champ_fill_rate=35.0, chal_fill_rate=94.0, coverage_delta_pp=59.0,
                        n_aligned=40, self_check_ok=True, l2_passed=True,
                        l2_summary="demo", reasons=["x"])
            base.update(kw)
            return types.SimpleNamespace(**base)

        # 2) promote=False → 不寫活躍表，但寫稽核（紅線③留痕）
        r = apply_verdict(_v(False), symbol="BTC", quadrant="price_up_oi_up",
                          challenger_kind="limit_convert", champion_kind="limit_expire",
                          at_ms=1, active_path=ap, audit_path=au)
        assert r["action"] == "hold"
        assert resolve_entry_policy("BTC", "price_up_oi_up", active_path=ap) is None
        assert not ap.exists()           # hold 不該建活躍表
        assert au.exists()               # 但稽核必留痕

        # 3) promote=True → 寫活躍表，resolve 即時生效
        r = apply_verdict(_v(True), symbol="BTC", quadrant="price_up_oi_up",
                          challenger_kind="limit_convert", champion_kind="limit_expire",
                          at_ms=2, active_path=ap, audit_path=au)
        assert r["action"] == "promote"
        assert resolve_entry_policy("BTC", "price_up_oi_up", active_path=ap) == "limit_convert"
        # 別桶不受影響
        assert resolve_entry_policy("ETH", "price_up_oi_up", active_path=ap) is None
        assert resolve_entry_policy("BTC", "price_down_oi_up", active_path=ap) is None

        # 4) promote=True 但 challenger_kind 非法 → fail-safe 拒寫
        r = apply_verdict(_v(True, bucket_key="SOL|unknown"), symbol="SOL", quadrant="unknown",
                          challenger_kind="teleport",   # 非法
                          at_ms=3, active_path=ap, audit_path=au)
        assert r["action"] == "hold"
        assert resolve_entry_policy("SOL", "unknown", active_path=ap) is None

        # 5) promote=True 但 challenger＝預設(limit_expire) → 無需覆寫（hold）
        r = apply_verdict(_v(True, bucket_key="OP|unknown", challenger="champion(現行深限價可到期)"),
                          symbol="OP", quadrant="unknown",
                          challenger_kind="limit_expire",
                          at_ms=4, active_path=ap, audit_path=au)
        assert r["action"] == "hold"
        assert resolve_entry_policy("OP", "unknown", active_path=ap) is None

        # 6) market 政策也能晉升落地
        r = apply_verdict(_v(True, bucket_key="DOGE|price_up_oi_up", challenger="市價即進"),
                          symbol="DOGE", quadrant="price_up_oi_up",
                          challenger_kind="market", champion_kind="limit_expire",
                          at_ms=5, active_path=ap, audit_path=au)
        assert r["action"] == "promote"
        assert resolve_entry_policy("DOGE", "price_up_oi_up", active_path=ap) == "market"

        # 6b) task#63 涵蓋率非劣性晉升：promote=False 但 coverage_promote=True → 仍落地（D）。
        #     依據＝coverage_noninferiority，且 bucket 留痕 promote_basis（紅線③透明）。
        r = apply_verdict(
            _v(False, bucket_key="ADA|price_up_oi_up", challenger="D_深限價到期轉市價",
               coverage_promote=True, ev_noninferior=True, ev_noninf_lo=-0.01),
            symbol="ADA", quadrant="price_up_oi_up",
            challenger_kind="limit_convert", champion_kind="limit_expire",
            at_ms=8, active_path=ap, audit_path=au)
        assert r["action"] == "promote", r
        assert resolve_entry_policy("ADA", "price_up_oi_up", active_path=ap) == "limit_convert"
        _last = json.loads(au.read_text(encoding="utf-8").strip().splitlines()[-1])
        assert _last["promote_basis"] == "coverage_noninferiority", _last
        assert _last["coverage_promote"] is True and _last["promote"] is False
        # 活躍桶也留下晉升依據
        _bkt = json.loads(ap.read_text(encoding="utf-8"))["buckets"]["ADA|price_up_oi_up"]
        assert _bkt["promote_basis"] == "coverage_noninferiority", _bkt

        # 6c) 反例：promote=False 且 coverage_promote=False → hold（活躍表不動）
        r = apply_verdict(
            _v(False, bucket_key="LTC|price_up_oi_up", challenger="D_深限價到期轉市價",
               coverage_promote=False),
            symbol="LTC", quadrant="price_up_oi_up",
            challenger_kind="limit_convert", champion_kind="limit_expire",
            at_ms=9, active_path=ap, audit_path=au)
        assert r["action"] == "hold"
        assert resolve_entry_policy("LTC", "price_up_oi_up", active_path=ap) is None

        # 7) rollback → 回退預設
        r = rollback("BTC", "price_up_oi_up", at_ms=6, reason="selftest",
                     active_path=ap, audit_path=au)
        assert r["action"] == "rollback" and r["existed"] is True
        assert resolve_entry_policy("BTC", "price_up_oi_up", active_path=ap) is None

        # ── task#62 階層式部分池化 ladder ──────────────────────────────
        # ladder 純函式：去重、由具體到一般
        assert _resolution_ladder("BTC", "price_up_oi_up") == [
            "BTC|price_up_oi_up", "*|price_up_oi_up", "*|*"]
        assert _resolution_ladder(POOL, "price_up_oi_up") == ["*|price_up_oi_up", "*|*"]
        assert _resolution_ladder(POOL, POOL) == ["*|*"]

        ap2 = Path(td) / "ladder_active.json"
        au2 = Path(td) / "ladder_audit.jsonl"
        # 9a) 全域池 (*,*) promote market → 任何 symbol/任何 regime 都吃到
        apply_verdict(_v(True, bucket_key="*|*", challenger="市價即進"),
                      symbol=POOL, quadrant=POOL, challenger_kind="market",
                      champion_kind="limit_expire", at_ms=10, active_path=ap2, audit_path=au2)
        assert resolve_entry_policy("ANY", "whatever", active_path=ap2) == "market"
        assert resolve_entry_policy("ANY", "unknown", active_path=ap2) == "market"
        # 9b) 象限池 (*,price_up_oi_up) promote limit_convert → 該象限蓋過全域、別象限仍吃全域
        apply_verdict(_v(True, bucket_key="*|price_up_oi_up"),
                      symbol=POOL, quadrant="price_up_oi_up", challenger_kind="limit_convert",
                      champion_kind="limit_expire", at_ms=11, active_path=ap2, audit_path=au2)
        assert resolve_entry_policy("ZZZ", "price_up_oi_up", active_path=ap2) == "limit_convert"
        assert resolve_entry_policy("ZZZ", "price_down_oi_up", active_path=ap2) == "market"
        # 9c) per-symbol (BTC,price_up_oi_up) promote market → 最具體蓋過象限池與全域；
        #     同象限別 symbol(ETH) 無自有覆寫 → 退回象限池(limit_convert)
        apply_verdict(_v(True, bucket_key="BTC|price_up_oi_up", challenger="市價即進"),
                      symbol="BTC", quadrant="price_up_oi_up", challenger_kind="market",
                      champion_kind="limit_expire", at_ms=12, active_path=ap2, audit_path=au2)
        assert resolve_entry_policy("BTC", "price_up_oi_up", active_path=ap2) == "market"
        assert resolve_entry_policy("ETH", "price_up_oi_up", active_path=ap2) == "limit_convert"
        # 9d) 池化桶標籤可讀
        assert _bucket_label("*|*") == "全域池(跨一切)"
        assert _bucket_label("*|price_up_oi_up").startswith("象限池")
        assert _bucket_label("BTC|price_up_oi_up") == "BTC|price_up_oi_up"

        # 8) 壞檔 → resolve fail-safe 回 None（不崩）
        ap.write_text("{ this is not json", encoding="utf-8")
        assert resolve_entry_policy("DOGE", "price_up_oi_up", active_path=ap) is None

        # 9) 渲染不炸
        assert isinstance(render_active(ap), str)
        assert isinstance(render_audit_tail(5, au), str)

    return True


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        ok = _selftest()
        print("selftest:", "PASS" if ok else "FAIL")
        sys.exit(0 if ok else 1)
    import re as _re
    print(_re.sub(r"<[^>]+>", "", render_active()))
    print()
    print(_re.sub(r"<[^>]+>", "", render_audit_tail()))
