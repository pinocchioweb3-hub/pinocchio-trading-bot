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
import os
import tempfile
from pathlib import Path

from botpaths import data_dir

ACTIVE_NAME = "auto_params_active.json"   # 活躍覆寫表（只在 promote 時更新）
AUDIT_NAME = "auto_params_audit.jsonl"    # append-only 稽核（promote/hold/rollback 全留痕）
_ALLOC_TOL = 1e-3                          # 分配總和容差（與 champion_challenger 一致）


# ── 路徑 ────────────────────────────────────────────────────────────────
def active_path(path: str | Path | None = None) -> Path:
    return Path(path) if path else data_dir() / ACTIVE_NAME


def audit_path(path: str | Path | None = None) -> Path:
    return Path(path) if path else data_dir() / AUDIT_NAME


def bucket_key(symbol: str, quadrant: str) -> str:
    """覆寫桶鍵＝symbol×regime 象限（per-symbol × per-regime 自適應參數）。"""
    return f"{symbol}|{quadrant or 'unknown'}"


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
def _load_active(path: str | Path | None = None) -> dict:
    """讀活躍表；任何異常 → {}（fail-safe，等同無覆寫＝用 CONFIG 預設）。"""
    p = active_path(path)
    try:
        if not p.exists():
            return {}
        with open(p, "r", encoding="utf-8") as f:
            obj = json.load(f)
        buckets = obj.get("buckets")
        return buckets if isinstance(buckets, dict) else {}
    except Exception:
        return {}


def _atomic_write_active(buckets: dict, at_ms: int,
                         path: str | Path | None = None) -> None:
    """原子寫活躍表（temp + os.replace）。失敗向上拋給 apply_verdict 收斂。"""
    p = active_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "updated_at_ms": int(at_ms), "buckets": buckets}
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".auto_params_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
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
        rec = buckets.get(bucket_key(symbol, quadrant))
        if not rec:
            return None
        return _norm_alloc(rec.get("tp_alloc"))   # 壞值 → None → 用預設
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

    buckets = _load_active(active_path)
    prev = buckets.get(bkey) or {}
    prev_alloc = prev.get("tp_alloc")

    action = "hold"
    to_alloc = prev_alloc
    note = ""

    if promote and norm_chal is not None:
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
    """移除某桶覆寫 → 回退 CONFIG 預設（事後人工/CEO 可逆）。一律寫稽核。"""
    bkey = bucket_key(symbol, quadrant)
    buckets = _load_active(active_path)
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
    """活躍覆寫表摘要（給 CEO/調參報告）。空表 → 明確說「無覆寫，全用預設」。"""
    buckets = _load_active(active_path)
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
