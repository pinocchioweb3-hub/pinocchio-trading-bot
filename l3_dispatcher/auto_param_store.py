"""復盤引擎 step8（task#53）── 自動參數庫：per-(symbol×regime) TP 分配覆寫的**落地層**。

定位（INTENT #1：自動優化器過 L2 統計閘後「直接寫活鍵讓優化即時生效」）：
    champion/challenger 離線回放（champion_challenger.py）過 L2 四關（l2_stat_gates.py）後，
    得到一個 ChallengerVerdict。若 verdict.promote 為真（統計上顯著更好），本模組把該
    挑戰者的 TP 分配寫進「活躍覆寫表」，模擬盤下一筆同 (symbol, quadrant) 進場即生效。
    → 把關靠**統計嚴謹度**（L2），不靠人工逐次點頭；透明在 CEO 報告，可事後 rollback。

三條安全紅線在本模組的落點（務必保持）：
    紅線①（真錢 AI 永不自動下單）：本表**只驅動模擬盤 paper／demo 的 TP 分配**。真錢
        執行層完全不讀本表；config 只驅動 signal/paper/demo，改參數不跨越執行閘。
    紅線②（對外發布逐次人工）：本表是本機檔，不對外發布、不進公開 repo（資料檔在
        data_dir()，非專案目錄）。
    紅線③（不捏造）：每次評估**一律寫稽核**（promote 與 hold 都留痕）；活躍表**只在
        verdict.promote is True 時更新**。今日樣本 <30 → L2 minTRL fail-closed → 0 晉升
        → 本表恆為空 → 對行為零改變（已由 _selftest 驗證）。

設計約束：
    - 依賴極輕（只用 stdlib + botpaths），因為 paper_journal 在「進場當下」import 本模組
      解析覆寫；不可反向依賴 champion_challenger / l2_stat_gates（避免 import 環）。
    - resolve_tp_alloc 每次**重讀檔**且**fail-safe**：任何異常 → 回 None → 呼叫端用 CONFIG
      預設（即今日行為）。絕不讓壞檔/競態讓模擬盤崩。
    - 活躍表原子寫（temp + os.replace）；稽核 append-only JSONL。
    - verdict 以 duck-typing 讀取屬性（不綁 ChallengerVerdict 型別），方便測試與解耦。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from botpaths import data_dir

_LOG = logging.getLogger("l3_dispatcher.auto_param_store")

ACTIVE_NAME = "auto_params_active.json"   # 活躍覆寫表（只在 promote 時更新）
AUDIT_NAME = "auto_params_audit.jsonl"    # append-only 稽核（promote/hold/rollback 全留痕）
_ALLOC_TOL = 1e-3                          # 分配總和容差（與 champion_challenger 一致）


# ── 路徑 ────────────────────────────────────────────────────────────────
def active_path(path: str | Path | None = None) -> Path:
    return Path(path) if path else data_dir() / ACTIVE_NAME


def audit_path(path: str | Path | None = None) -> Path:
    return Path(path) if path else data_dir() / AUDIT_NAME


POOL = "*"   # task#62 階層池化 sentinel（與 entry_policy_store.POOL 同切面）


def bucket_key(symbol: str, quadrant: str) -> str:
    """覆寫桶鍵＝symbol×regime 象限（per-symbol × per-regime 自適應參數）。"""
    return f"{symbol}|{quadrant or 'unknown'}"


def _resolution_ladder(symbol: str, quadrant: str) -> list[str]:
    """task#62 階層式部分池化查找順序（最具體→最一般，去重）：
        (symbol, quadrant) per-symbol×regime → (POOL, quadrant) 象限池 → (POOL, POOL) 全域池。
    與 entry_policy_store._resolution_ladder 同切面。最具體且有有效覆寫者勝。"""
    keys: list[str] = []
    for sk, qk in ((symbol, quadrant), (POOL, quadrant), (POOL, POOL)):
        bk = bucket_key(sk, qk)
        if bk not in keys:
            keys.append(bk)
    return keys


# ── 分配驗證／正規化 ──────────────────────────────────────────────────
def _valid_alloc(alloc) -> bool:
    """3 段、皆非負、總和 1.0±tol（與 AllocPolicy.__post_init__ 同規則）。"""
    try:
        if alloc is None or len(alloc) != 3:
            return False
        vals = [float(x) for x in alloc]
    except (TypeError, ValueError):
        return False
    if any(v < 0 for v in vals):
        return False
    return abs(sum(vals) - 1.0) <= _ALLOC_TOL


def _norm_alloc(alloc) -> tuple[float, float, float] | None:
    """合法 → 回 (a,b,c) 三元組（round 6）；不合法 → None。"""
    if not _valid_alloc(alloc):
        return None
    return tuple(round(float(x), 6) for x in alloc)  # type: ignore[return-value]


# ── 檔案 I/O（fail-safe + 原子寫） ────────────────────────────────────
# 讀取三態（與 entry_policy_store 同規格）。⛔ 只有 MISSING 可以當「真·從來沒有任何覆寫」用；
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
        "🚨 自動參數活躍表存在但讀不出來（%s）→ 本次進場退回 CONFIG 預設 TP 分配；"
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
    """讀活躍表；任何異常 → {}（fail-safe，等同無覆寫＝用 CONFIG 預設）。

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
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".auto_params_", suffix=".tmp")
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


# ── 對外：進場解析（paper_journal 在進場當下呼叫） ────────────────────
def resolve_tp_alloc(symbol: str, quadrant: str, *,
                     active_path: str | Path | None = None
                     ) -> tuple[float, float, float] | None:
    """回該 (symbol, quadrant) 的活躍 TP 分配覆寫；無或異常 → None（用 CONFIG 預設）。

    **fail-safe**：每次重讀檔；任何錯誤都回 None，永不讓模擬盤進場崩。
    """
    try:
        buckets = _load_active(active_path)
        for bk in _resolution_ladder(symbol, quadrant):   # task#62 階梯：具體→象限池→全域池
            rec = buckets.get(bk)
            if not rec:
                continue
            alloc = _norm_alloc(rec.get("tp_alloc"))       # 壞值→視同此階無覆寫，續找更一般階
            if alloc is not None:
                return alloc
        return None
    except Exception:
        return None


# ── 對外：落地裁決（auto_optimizer 過 L2 後呼叫） ─────────────────────
def apply_verdict(verdict, *, symbol: str, quadrant: str,
                  challenger_alloc, champion_alloc=None, at_ms: int,
                  active_path: str | Path | None = None,
                  audit_path: str | Path | None = None) -> dict:
    """消費一個 ChallengerVerdict（duck-typed）：**一律寫稽核**；**只在 promote 時改活躍表**。

    ⚠️ 真 ChallengerVerdict 的 .champion/.challenger 是「名稱字串」非 AllocPolicy
       （見 champion_challenger.py），故分配元組**必須由呼叫端顯式傳入**：
        challenger_alloc（必填）、champion_alloc（選填，僅供稽核留痕）。

    Args:
        verdict: 帶 .promote/.bucket_key/.champ_mean_r/.chal_mean_r/.n_aligned/
                 .l2_summary/.reasons 的物件（容缺）。
        challenger_alloc: 挑戰者 TP 分配（晉升時寫進活躍表的值）。
        champion_alloc: 現行 champion TP 分配（只進稽核，便於回溯對照）。
        at_ms: 事件時戳（毫秒，由呼叫端傳入，本模組不碰 wall-clock）。

    Returns: {action: "promote"|"hold", bucket, to_alloc, note}
    """
    bkey = bucket_key(symbol, quadrant)
    promote = bool(getattr(verdict, "promote", False))

    norm_chal = _norm_alloc(challenger_alloc)
    norm_champ = _norm_alloc(champion_alloc)

    buckets, load_status = _load_active_status(active_path)
    unreadable = load_status == LOAD_UNREADABLE
    prev = buckets.get(bkey) or {}
    prev_alloc = prev.get("tp_alloc")

    action = "hold"
    to_alloc = prev_alloc
    note = ""

    if unreadable:
        # ⛔ 讀不出來時**絕不寫**：舊碼會拿空 buckets 當整張表覆寫回去，把其餘所有已晉升桶
        # 原子地、乾淨地抹掉（不可逆）。停手＝維持現狀，原檔原封不動留給人工檢視。
        _warn_unreadable_once(active_path)
        action = "blocked_unreadable_active"
        to_alloc = None
        note = ("活躍覆寫表存在但讀不出來 → 本次裁決停手不寫（原檔未動）。"
                "⛔ 若照舊寫入會把整張表抹成只剩這一桶，其餘已晉升桶不可逆消失。"
                "請人工檢視該檔後再讓優化器續行。")
    elif promote and norm_chal is not None:
        action = "promote"
        to_alloc = list(norm_chal)
        buckets[bkey] = {
            "tp_alloc": list(norm_chal),
            "promoted_at_ms": int(at_ms),
            "champ_mean_r": getattr(verdict, "champ_mean_r", None),
            "chal_mean_r": getattr(verdict, "chal_mean_r", None),
            "n_aligned": getattr(verdict, "n_aligned", None),
            "l2_bucket_key": getattr(verdict, "bucket_key", bkey),
            "champion_alloc": list(norm_champ) if norm_champ else None,
        }
        try:
            _atomic_write_active(buckets, at_ms, active_path)
        except Exception as e:
            # 寫失敗 → 退回 hold，不假裝已生效（紅線③：不捏造）
            action = "hold"
            to_alloc = prev_alloc
            note = f"活躍表寫入失敗，未生效（fail-safe）：{type(e).__name__}"
    elif promote and norm_chal is None:
        note = "verdict.promote 為真但 challenger 分配無效 → 拒寫（fail-safe）"

    rec = {
        "at_ms": int(at_ms),
        "action": action,
        "bucket": bkey,
        "l2_bucket_key": getattr(verdict, "bucket_key", bkey),
        "promote": promote,
        "from_alloc": list(prev_alloc) if prev_alloc else None,
        # ⛔ 讀不出來時 from_alloc 的 None 是「未知」不是「本來就沒有」——用旗標把兩者分開，
        # 否則錯誤宣稱會沉進 append-only 稽核軌跡，事後再也分不出來。
        "from_kind_known": not unreadable,
        "active_unreadable": unreadable,
        "to_alloc": list(to_alloc) if to_alloc else None,
        "challenger_alloc": list(norm_chal) if norm_chal else None,
        "champion_alloc": list(norm_champ) if norm_champ else None,
        "champ_mean_r": getattr(verdict, "champ_mean_r", None),
        "chal_mean_r": getattr(verdict, "chal_mean_r", None),
        "n_aligned": getattr(verdict, "n_aligned", None),
        "l2_summary": getattr(verdict, "l2_summary", None),
        "reasons": list(getattr(verdict, "reasons", []) or []),
        "note": note,
    }
    _append_audit(rec, audit_path)
    return {"action": action, "bucket": bkey, "to_alloc": rec["to_alloc"], "note": note}


def rollback(symbol: str, quadrant: str, *, at_ms: int, reason: str = "",
             active_path: str | Path | None = None,
             audit_path: str | Path | None = None) -> dict:
    """移除某桶覆寫 → 回退 CONFIG 預設（事後人工/CEO 可逆）。一律寫稽核。

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
                       "bucket": bkey, "existed": None, "from_alloc": None,
                       "from_kind_known": False, "active_unreadable": True,
                       "to_alloc": None, "reason": reason or "manual",
                       "note": note}, audit_path)
        return {"action": "rollback_blocked_unreadable", "bucket": bkey,
                "existed": None, "note": note}
    prev = buckets.get(bkey) or {}
    prev_alloc = prev.get("tp_alloc")
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
        "existed": existed, "from_alloc": list(prev_alloc) if prev_alloc else None,
        "to_alloc": None, "reason": reason or "manual",
    }, audit_path)
    return {"action": "rollback", "bucket": bkey, "existed": existed}


# ── 繁中渲染（CEO 報告透明化） ────────────────────────────────────────
def render_active(active_path: str | Path | None = None) -> str:
    """活躍覆寫表摘要（給 CEO/調參報告）。空表 → 明確說「無覆寫，全用預設」。

    ⛔ 讀不出來 ≠ 空表：舊版會把壞檔渲染成「目前 0 桶有覆寫……這是預期的誠實狀態」，
    在報告裡把一個未知狀態講成一句正面保證。
    """
    buckets, load_status = _load_active_status(active_path)
    if load_status == LOAD_UNREADABLE:
        return ("🚨 <b>自動參數庫</b>：活躍表<b>存在但讀不出來</b>（內容未知）。\n"
                "<i>⛔ 這不是「0 桶覆寫」——進場已 fail-safe 退回 CONFIG 預設 TP 分配，"
                "但晉升與回退已自動停手，原檔保留待人工檢視。</i>")
    if not buckets:
        return ("🎛️ <b>自動參數庫</b>：目前 <b>0</b> 桶有覆寫（全用 CONFIG 預設 TP 分配）。\n"
                "<i>樣本未達 L2 統計門檻前不會有任何覆寫 — 這是預期的誠實狀態。</i>")
    lines = [f"🎛️ <b>自動參數庫</b>（模擬盤 TP 分配覆寫，共 {len(buckets)} 桶）"]
    for bk, rec in sorted(buckets.items()):
        a = rec.get("tp_alloc") or []
        astr = "/".join(f"{float(x)*100:.0f}%" for x in a) if len(a) == 3 else "?"
        n = rec.get("n_aligned")
        cm, chm = rec.get("champ_mean_r"), rec.get("chal_mean_r")
        delta = (f"（{cm:+.3f}→{chm:+.3f}R）" if isinstance(cm, (int, float))
                 and isinstance(chm, (int, float)) else "")
        lines.append(f"  [{bk}] TP分配 {astr}｜對齊 n={n}{delta}")
    lines.append("<i>來源＝champion/challenger 過 L2 四關晉升；只驅動模擬盤（紅線①）。</i>")
    return "\n".join(lines)


def render_audit_tail(n: int = 10, path: str | Path | None = None) -> str:
    """最近 n 筆裁決留痕（promote/hold/rollback）。"""
    p = audit_path(path)
    try:
        if not p.exists():
            return "（尚無自動參數裁決紀錄）"
        with open(p, "r", encoding="utf-8") as f:
            rows = [json.loads(ln) for ln in f if ln.strip()]
    except Exception:
        return "（自動參數稽核讀取失敗）"
    if not rows:
        return "（尚無自動參數裁決紀錄）"
    tail = rows[-n:]
    out = [f"🧾 <b>自動參數裁決</b>（最近 {len(tail)}/{len(rows)} 筆）"]
    icon = {"promote": "✅晉升", "hold": "⏸️維持", "rollback": "↩️回退",
            "rollback_failed": "⚠️回退失敗"}
    for r in tail:
        act = icon.get(r.get("action", ""), r.get("action", "?"))
        bk = r.get("bucket", "?")
        ta = r.get("to_alloc")
        tastr = "/".join(f"{float(x)*100:.0f}%" for x in ta) if ta and len(ta) == 3 else "—"
        note = f"｜{r['note']}" if r.get("note") else ""
        out.append(f"  {act} [{bk}] →{tastr}{note}")
    return "\n".join(out)


# ── 自我測試（離線、暫存檔、零真 DB） ────────────────────────────────
def _selftest() -> bool:
    import types

    with tempfile.TemporaryDirectory() as td:
        ap = Path(td) / ACTIVE_NAME
        au = Path(td) / AUDIT_NAME

        # 1) 空表 → resolve 回 None（＝用預設，今日行為）
        assert resolve_tp_alloc("BTC", "price_up_oi_up", active_path=ap) is None

        # 真 ChallengerVerdict 的 champion/challenger 是「名稱字串」（非 AllocPolicy）
        def _v(promote, **kw):
            base = dict(promote=promote, bucket_key="BTC|price_up_oi_up",
                        champion="champion(現行)", challenger="c",
                        champ_mean_r=0.10, chal_mean_r=0.20, n_aligned=40,
                        l2_summary="demo", reasons=["x"])
            base.update(kw)
            return types.SimpleNamespace(**base)

        # 2) promote=False → 不寫活躍表，但寫稽核（紅線③留痕）
        r = apply_verdict(_v(False), symbol="BTC", quadrant="price_up_oi_up",
                          challenger_alloc=(0.5, 0.3, 0.2), champion_alloc=(0.4, 0.3, 0.3),
                          at_ms=1, active_path=ap, audit_path=au)
        assert r["action"] == "hold"
        assert resolve_tp_alloc("BTC", "price_up_oi_up", active_path=ap) is None
        assert not ap.exists()           # hold 不該建活躍表
        assert au.exists()               # 但稽核必留痕

        # 3) promote=True → 寫活躍表，resolve 即時生效
        r = apply_verdict(_v(True, chal_mean_r=0.35), symbol="BTC", quadrant="price_up_oi_up",
                          challenger_alloc=(0.5, 0.3, 0.2), champion_alloc=(0.4, 0.3, 0.3),
                          at_ms=2, active_path=ap, audit_path=au)
        assert r["action"] == "promote"
        got = resolve_tp_alloc("BTC", "price_up_oi_up", active_path=ap)
        assert got == (0.5, 0.3, 0.2)
        # 別桶不受影響
        assert resolve_tp_alloc("ETH", "price_up_oi_up", active_path=ap) is None
        assert resolve_tp_alloc("BTC", "price_down_oi_up", active_path=ap) is None

        # 4) promote=True 但 challenger 分配無效 → fail-safe 拒寫
        r = apply_verdict(_v(True, bucket_key="SOL|unknown"), symbol="SOL", quadrant="unknown",
                          challenger_alloc=(0.5, 0.3, 0.5),  # 總和 1.3 → 無效
                          at_ms=3, active_path=ap, audit_path=au)
        assert r["action"] == "hold"
        assert resolve_tp_alloc("SOL", "unknown", active_path=ap) is None

        # 5) rollback → 回退預設
        r = rollback("BTC", "price_up_oi_up", at_ms=4, reason="selftest",
                     active_path=ap, audit_path=au)
        assert r["action"] == "rollback" and r["existed"] is True
        assert resolve_tp_alloc("BTC", "price_up_oi_up", active_path=ap) is None

        # 6) 壞檔 → resolve fail-safe 回 None（不崩）
        ap.write_text("{ this is not json", encoding="utf-8")
        assert resolve_tp_alloc("BTC", "price_up_oi_up", active_path=ap) is None

        # 7) 渲染不炸
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
