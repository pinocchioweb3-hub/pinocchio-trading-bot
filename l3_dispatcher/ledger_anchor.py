"""帳本防竄改錨定（OpenTimestamps → 比特幣）— v35。

目的（本專案「誠實」核心資產之一）：
    把交易帳本（trade_journal.db：含 27 筆紙上實單 paper_trades）做成
    不可事後竄改的快照，用 OpenTimestamps【免費】錨定到比特幣區塊鏈。
    任何人日後都能用公開的 `ots verify` 獨立驗證：
        「這份戰績在某個比特幣區塊時間點之前就已存在，且之後未被改動。」
    → 防止有人（包含我們自己）日後偷改/補登/回填假歷史來美化績效。

⚠️ 誠實邊界（對外務必如實說明，見 [[trading-bot-token-strategy-verdict]]）：
    OpenTimestamps 只證明「這份資料在某時點已存在且之後未竄改」，
    【不】證明資料內容為真、不證明這些交易真的在交易所成交過。
    內容真實性需另靠 OKX 模擬盤對帳 / 交易所 API 逐筆重算（另一項工作）。

界線（符合使用者紅線）：
    - 只送出 32-byte SHA-256 雜湊到公開 calendar — 不可逆、不洩漏任何帳本內容。
    - 不需帳號、不需 gas、不上實彈、純讀帳本。
    - 比特幣最終確認需數小時 → .ots 先存 pending，之後 upgrade 升級為區塊證明。

驗證方式（任何第三方）：
    pip install opentimestamps-client
    ots verify <snapshot>.json.ots         # .ots 承諾 sha256(snapshot 檔位元組)
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

from botpaths import data_dir, db_path

from opentimestamps.core.timestamp import Timestamp, DetachedTimestampFile
from opentimestamps.core.op import OpSHA256, OpAppend
from opentimestamps.core.serialize import (StreamSerializationContext,
                                           BytesSerializationContext)
from opentimestamps.core.notary import (PendingAttestation,
                                        BitcoinBlockHeaderAttestation)
from opentimestamps.calendar import RemoteCalendar, CommitmentNotFoundError

SCHEMA_VERSION = 1
LEDGER_DB = "trade_journal.db"

# 公開 OpenTimestamps aggregator（可用 env OTS_CALENDARS 逗號覆寫）
DEFAULT_CALENDARS = (
    "https://a.pool.opentimestamps.org",
    "https://b.pool.opentimestamps.org",
    "https://a.pool.eternitywall.com",
    "https://ots.btc.catallaxy.com",
)
SUBMIT_TIMEOUT = int(os.getenv("OTS_SUBMIT_TIMEOUT_SEC", "20"))

HONESTY_NOTE = (
    "此快照經 OpenTimestamps 錨定僅證明『本資料於某比特幣區塊時間前已存在、"
    "且之後未被竄改』。【不】證明內容為真、不證明交易真的在交易所成交。"
    "內容真實性需另以 OKX 模擬盤對帳 / 交易所 API 逐筆重算佐證。"
)


def _calendars() -> list[str]:
    env = (os.getenv("OTS_CALENDARS") or "").strip()
    if env:
        return [u.strip() for u in env.split(",") if u.strip()]
    return list(DEFAULT_CALENDARS)


def anchor_dir() -> Path:
    d = data_dir() / "anchors"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _manifest_path() -> Path:
    return anchor_dir() / "anchor_manifest.jsonl"


# ── 1. 帳本快照（canonical、位元組穩定）──────────────────────────
def _data_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    return [r[0] for r in rows
            if not r[0].startswith("sqlite_")]


def _pk_cols(conn: sqlite3.Connection, table: str) -> list[str]:
    info = list(conn.execute("PRAGMA table_info(%s)" % table))
    pks = sorted([r for r in info if r[5]], key=lambda r: r[5])  # r[5]=pk order
    return [r[1] for r in pks]  # r[1]=name


def _norm(v):
    """BLOB → hex 字串；其餘原樣（JSON 可序列化）。"""
    if isinstance(v, (bytes, bytearray)):
        return {"__blob_hex__": v.hex()}
    return v


def _dump_table(conn: sqlite3.Connection, table: str) -> list[dict]:
    cur = conn.execute("SELECT * FROM %s" % table)
    cols = [d[0] for d in cur.description]
    rows = [{c: _norm(v) for c, v in zip(cols, r)} for r in cur.fetchall()]
    pks = _pk_cols(conn, table)
    if pks:
        rows.sort(key=lambda row: json.dumps([row.get(k) for k in pks],
                                             sort_keys=True, ensure_ascii=True))
    else:
        rows.sort(key=lambda row: json.dumps(row, sort_keys=True,
                                             ensure_ascii=True))
    return rows


def build_snapshot() -> tuple[dict, bytes]:
    """讀 trade_journal.db 全資料表 → 回 (snapshot dict, 要被雜湊/寫檔的位元組)。

    位元組即寫進 .json 檔的內容，.ots 承諾 sha256(這些位元組)。
    """
    p = db_path(LEDGER_DB)
    conn = sqlite3.connect(p)
    conn.execute("PRAGMA busy_timeout=8000")
    try:
        tables = _data_tables(conn)
        data = {t: _dump_table(conn, t) for t in tables}
        counts = {t: len(rows) for t, rows in data.items()}
    finally:
        conn.close()

    now = dt.datetime.now(tz=dt.timezone.utc)
    snap = {
        "_meta": {
            "artifact": "pinocchio_trade_ledger_snapshot",
            "schema_version": SCHEMA_VERSION,
            "generated_at_ms": int(now.timestamp() * 1000),
            "generated_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source_db": LEDGER_DB,
            "honesty_note": HONESTY_NOTE,
            "row_counts": counts,
        },
        "tables": data,
    }
    # 位元組穩定：sort_keys + ensure_ascii（不受平台編碼影響）
    body = json.dumps(snap, sort_keys=True, ensure_ascii=True,
                      indent=2).encode("utf-8")
    return snap, body


# ── 2. OpenTimestamps 蓋章 ──────────────────────────────────────
def _stamp_digest(file_digest: bytes, calendars: list[str]) -> tuple[
        DetachedTimestampFile, list[str], list[str]]:
    """對 file_digest（sha256，32B）做 OTS 蓋章。

    流程與官方 `ots stamp` 一致：file_digest → append 隨機 nonce → sha256
    → 送各 calendar 取 pending 承諾 → merge。回 (DetachedTimestampFile, ok, fail)。
    """
    if len(file_digest) != 32:
        raise ValueError("file_digest 必須是 32-byte sha256")
    file_ts = DetachedTimestampFile(OpSHA256(), Timestamp(file_digest))
    # nonce：保護隱私（calendar 看不到真 digest）+ 構成 per-file merkle leaf
    nonce_added = file_ts.timestamp.ops.add(OpAppend(os.urandom(16)))
    merkle_root = nonce_added.ops.add(OpSHA256())

    ok, fail = [], []
    for url in calendars:
        try:
            cal = RemoteCalendar(url)
            cal_ts = cal.submit(merkle_root.msg, timeout=SUBMIT_TIMEOUT)
            merkle_root.merge(cal_ts)
            ok.append(url)
        except Exception as e:  # noqa: BLE001 — 單一 calendar 失敗不致命
            fail.append(f"{url} ({type(e).__name__})")
    if not ok:
        raise RuntimeError("所有 calendar 均無回應：%s" % "; ".join(fail))
    return file_ts, ok, fail


def _serialize_ots(file_ts: DetachedTimestampFile, path: Path) -> None:
    with open(path, "wb") as f:
        file_ts.serialize(StreamSerializationContext(f))


def _append_manifest(record: dict) -> None:
    with open(_manifest_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def anchor_once(reason: str = "manual") -> dict:
    """完整錨定一次：快照 → 蓋章 → 寫 .json/.ots → 記 manifest。回 record。"""
    snap, body = build_snapshot()
    file_digest = hashlib.sha256(body).digest()
    digest_hex = file_digest.hex()

    ts = snap["_meta"]["generated_at_ms"]
    stem = f"ledger_{ts}"
    json_path = anchor_dir() / f"{stem}.json"
    ots_path = anchor_dir() / f"{stem}.json.ots"

    json_path.write_bytes(body)  # 必須與被雜湊的位元組完全相同

    file_ts, ok, fail = _stamp_digest(file_digest, _calendars())
    _serialize_ots(file_ts, ots_path)

    record = {
        "ts_ms": ts,
        "ts_utc": snap["_meta"]["generated_at_utc"],
        "reason": reason,
        "snapshot_file": json_path.name,
        "ots_file": ots_path.name,
        "sha256": digest_hex,
        "row_counts": snap["_meta"]["row_counts"],
        "calendars_ok": ok,
        "calendars_failed": fail,
        "status": "pending",            # 待比特幣區塊確認後 upgrade
        "bitcoin_height": None,
    }
    _append_manifest(record)
    return record


# ── 3. 升級 pending → 比特幣區塊證明 ────────────────────────────
def _find_node(ts: Timestamp, target_msg: bytes) -> Timestamp | None:
    if ts.msg == target_msg:
        return ts
    for sub in ts.ops.values():
        r = _find_node(sub, target_msg)
        if r is not None:
            return r
    return None


def _bitcoin_height(file_ts: DetachedTimestampFile) -> int | None:
    for _msg, att in file_ts.timestamp.all_attestations():
        if isinstance(att, BitcoinBlockHeaderAttestation):
            return att.height
    return None


def upgrade_pending() -> list[dict]:
    """嘗試把所有 pending 的 .ots 升級為比特幣區塊證明（與 `ots upgrade` 同義）。

    比特幣確認通常需數小時～1 天；尚未進塊則維持 pending、下次再試。
    回 [{snapshot_file, status, bitcoin_height}]。
    """
    if not _manifest_path().exists():
        return []
    lines = _manifest_path().read_text(encoding="utf-8").splitlines()
    records = [json.loads(ln) for ln in lines if ln.strip()]
    changed = False
    out = []
    for rec in records:
        if rec.get("status") == "confirmed":
            continue
        ots_path = anchor_dir() / rec["ots_file"]
        if not ots_path.exists():
            continue
        try:
            from opentimestamps.core.serialize import StreamDeserializationContext
            with open(ots_path, "rb") as f:
                file_ts = DetachedTimestampFile.deserialize(
                    StreamDeserializationContext(f))
        except Exception:
            continue

        pend = [(msg, att) for msg, att
                in file_ts.timestamp.all_attestations()
                if isinstance(att, PendingAttestation)]
        upgraded = False
        for msg, att in pend:
            node = _find_node(file_ts.timestamp, msg)
            if node is None:
                continue
            try:
                cal = RemoteCalendar(att.uri)
                up = cal.get_timestamp(msg, timeout=SUBMIT_TIMEOUT)
                node.merge(up)
                upgraded = True
            except CommitmentNotFoundError:
                pass  # 尚未進比特幣，下次再試
            except Exception:
                pass
        height = _bitcoin_height(file_ts)
        if upgraded:
            _serialize_ots(file_ts, ots_path)
        if height is not None and rec.get("status") != "confirmed":
            rec["status"] = "confirmed"
            rec["bitcoin_height"] = height
            changed = True
        out.append({"snapshot_file": rec["snapshot_file"],
                    "status": rec["status"],
                    "bitcoin_height": rec.get("bitcoin_height")})
    if changed:
        with open(_manifest_path(), "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return out


# ── 4. 本地竄改自檢（不靠網路）──────────────────────────────────
def verify_local() -> dict:
    """重算每個已存快照檔的 sha256，對照 manifest 紀錄 → 偵測本地檔被改。

    （真正的防竄改靠比特幣錨定；這層只快速抓「快照檔被事後動過」。）
    """
    if not _manifest_path().exists():
        return {"checked": 0, "ok": 0, "mismatch": [], "missing": []}
    records = [json.loads(ln) for ln
               in _manifest_path().read_text(encoding="utf-8").splitlines()
               if ln.strip()]
    ok = 0
    mismatch, missing = [], []
    for rec in records:
        jp = anchor_dir() / rec["snapshot_file"]
        if not jp.exists():
            missing.append(rec["snapshot_file"])
            continue
        h = hashlib.sha256(jp.read_bytes()).hexdigest()
        if h == rec["sha256"]:
            ok += 1
        else:
            mismatch.append(rec["snapshot_file"])
    return {"checked": len(records), "ok": ok,
            "mismatch": mismatch, "missing": missing}


def latest_anchor() -> dict | None:
    if not _manifest_path().exists():
        return None
    records = [json.loads(ln) for ln
               in _manifest_path().read_text(encoding="utf-8").splitlines()
               if ln.strip()]
    return records[-1] if records else None


# ── 5. 報告（系統主題）+ 每週常駐 loop ─────────────────────────
def render_anchor_report(rec: dict, html: bool = True) -> str:
    from telegram_bot.rebate_tiers import render_tiers
    b = (lambda s: f"<b>{s}</b>") if html else (lambda s: s)
    counts = rec.get("row_counts", {})
    n_paper = counts.get("paper_trades", 0)
    lines = [
        b("🔗 帳本防竄改錨定（OpenTimestamps → 比特幣）"),
        f"時間：{rec['ts_utc']}",
        f"快照：{rec['snapshot_file']}",
        f"SHA-256：<code>{rec['sha256'][:32]}…</code>" if html
        else f"SHA-256：{rec['sha256'][:32]}…",
        f"涵蓋：紙上實單 {n_paper} 筆 / 各表 {counts}",
        f"狀態：{rec['status']}（calendar {len(rec.get('calendars_ok', []))} 個已收）",
        "",
        "✅ 任何人可用公開 `ots verify` 獨立查核此戰績存在時間。",
        "⚠️ " + HONESTY_NOTE,
        "",
        render_tiers(html=html),
    ]
    return "\n".join(lines)


async def run_anchor_loop(tg, target_dow_utc: int = 0, target_hour_utc: int = 4):
    """每週帳本錨定 session（預設週一 12:00 台北 = 04:00 UTC）。

    啟動後睡 10 分鐘避開開機高峰；若距上次錨定 > 6 天（或從未），先補錨一次。
    每週：upgrade 舊 pending → 錨定本週 → 推系統主題報告。純讀帳本、不下單。
    """
    print("[anchor] loop online（每週帳本 OpenTimestamps 錨定）")
    await asyncio.sleep(600)

    # 冷啟動補錨（避免每次重啟都錨 → 只在超過 6 天或從未時）
    try:
        last = latest_anchor()
        stale = (last is None or
                 (time.time() * 1000 - last.get("ts_ms", 0)) > 6 * 86400 * 1000)
        if stale:
            rec = await asyncio.to_thread(anchor_once, "startup")
            print(f"[anchor] startup anchored: {rec['snapshot_file']} "
                  f"({len(rec['calendars_ok'])} calendars)")
            if tg is not None:
                await tg.send_message(render_anchor_report(rec), parse_mode="HTML")
    except Exception as e:  # noqa: BLE001
        print(f"[anchor] startup anchor error: {type(e).__name__}: {e}")

    while True:
        now = dt.datetime.now(tz=dt.timezone.utc)
        days_ahead = (target_dow_utc - now.weekday()) % 7
        nxt = now.replace(hour=target_hour_utc, minute=0, second=0,
                          microsecond=0) + dt.timedelta(days=days_ahead)
        if nxt <= now:
            nxt += dt.timedelta(days=7)
        wait = (nxt - now).total_seconds()
        print(f"[anchor] next at {nxt.strftime('%Y-%m-%d %H:%M UTC')} "
              f"(in {wait/3600:.1f}h)")
        await asyncio.sleep(wait)
        try:
            up = await asyncio.to_thread(upgrade_pending)
            confirmed = [u for u in up if u["status"] == "confirmed"]
            if confirmed:
                print(f"[anchor] {len(confirmed)} proof(s) confirmed on Bitcoin")
            rec = await asyncio.to_thread(anchor_once, "weekly")
            print(f"[anchor] weekly anchored: {rec['snapshot_file']}")
            if tg is not None:
                await tg.send_message(render_anchor_report(rec), parse_mode="HTML")
        except Exception as e:  # noqa: BLE001
            print(f"[anchor] weekly run error: {type(e).__name__}: {e}")


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    if cmd == "anchor":
        r = anchor_once("cli")
        print("ANCHORED", r["snapshot_file"], "sha256=" + r["sha256"][:16],
              "calendars_ok=%d" % len(r["calendars_ok"]),
              "failed=%d" % len(r["calendars_failed"]))
        if r["calendars_failed"]:
            print("  failed:", r["calendars_failed"])
    elif cmd == "upgrade":
        for u in upgrade_pending():
            print(u)
    elif cmd == "verify":
        print(verify_local())
    elif cmd == "snapshot":
        _snap, body = build_snapshot()
        print("snapshot bytes=%d sha256=%s"
              % (len(body), hashlib.sha256(body).hexdigest()))
        print("row_counts:", _snap["_meta"]["row_counts"])
    else:
        last = latest_anchor()
        print("latest:", last)
        print("verify_local:", verify_local())
