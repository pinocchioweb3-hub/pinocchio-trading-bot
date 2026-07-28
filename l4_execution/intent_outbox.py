# -*- coding: utf-8 -*-
"""intent_outbox.py — trade-intent v1.1 訊號輸出層（v133，ATK 消費鏈的我方半）。

架構（Agent Kit 整合研究定案，路徑 B）：
    我方引擎（大腦）→ 本層把過閘訊號原子寫成 JSON 檔（outbox）→ 使用者側的
    確定性消費腳本（tools/atk_consumer/，使用者審+自跑）讀檔 → okx CLI 下單。
    Pull 架構：消費者來拉，我方永不對交易所發起寫入。

⛔ 鐵則：本模組零網路、零交易所呼叫、零金鑰接觸——只寫本地 JSON 檔。
    execution_policy 只有 "demo_only"｜"human_gated" 兩值，不存在 auto_live（紅線①）。
    美股（us_breakout，已過統計閘 PSRc≥0.95）先行；加密 deepdive 在 demo 帳
    轉正＋過統計閘前一律標 human_gated。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from typing import Optional

from botpaths import data_dir, db_path

OUTBOX_DIR = data_dir() / "intent_outbox"
_STATE = data_dir() / "intent_outbox_state.json"
_POLL_SEC = 60
INTENT_VER = "1.1"
# 訊號有效窗：過期的 intent 消費者一律丟棄（防斷線後補執行過時價位）
EXPIRY_HOURS = {"us_breakout": 6.0, "deepdive": 8.0}


def build_intent(row: dict) -> Optional[dict]:
    """paper 訊號列 → trade-intent v1.1（純函式）。缺關鍵價位→None（不出殘缺單）。

    intent_id＝sha256(engine|symbol|direction|entry_at) 前 16 碼——確定性可重放，
    同一訊號永遠同 ID（消費者冪等去重的錨）。clOrdId 種子=英數 ≤24 碼供 OKX 冪等。"""
    setup = row.get("setup") or ""
    sym = (row.get("symbol") or "").upper()
    direction = row.get("direction")
    entry = row.get("entry_price")
    stop = row.get("stop_price")
    tp1 = row.get("tp1")
    entry_at = row.get("entry_at")
    if (not sym or direction not in ("bull", "bear") or not entry or not stop
            or not tp1 or not entry_at):
        return None
    raw = f"{setup}|{sym}|{direction}|{int(entry_at)}"
    iid = hashlib.sha256(raw.encode()).hexdigest()[:16]
    expiry_h = EXPIRY_HOURS.get(setup, 6.0)
    # 美股引擎已過預註冊統計閘（PSRc≥0.95）→ demo_only（=消費者可自動執行於模擬盤）；
    # 其餘引擎 human_gated（消費者只列印不執行）。真盤永遠是使用者親手切換（紅線①）。
    policy = "demo_only" if setup == "us_breakout" else "human_gated"
    return {
        "ver": INTENT_VER,
        "intent_id": iid,
        "cl_ord_id": f"atk{iid}"[:24],
        "engine": "us" if setup == "us_breakout" else "crypto",
        "setup": setup,
        "symbol": sym,
        "inst_id": f"{sym}-USDT-SWAP",
        "direction": direction,
        "side": "buy" if direction == "bull" else "sell",
        "pos_side": "long" if direction == "bull" else "short",
        "entry_type": "market" if setup == "us_breakout" else "limit",
        "entry": float(entry),
        "stop": float(stop),
        "tp1": float(tp1),
        "tp2": float(row["tp2"]) if row.get("tp2") else None,
        "tp3": float(row["tp3"]) if row.get("tp3") else None,
        "paper_id": row.get("id"),
        "created_at": int(entry_at),
        "expires_at": int(entry_at + expiry_h * 3600_000),
        "execution_policy": policy,
    }


def _load_state() -> dict:
    try:
        return json.loads(_STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"last_id": 0}


def _save_state(st: dict) -> None:
    try:
        _STATE.write_text(json.dumps(st), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def scan_and_write(last_id: int) -> tuple[int, int]:
    """掃新 paper 訊號→原子寫 intent 檔。回 (寫出數, 新 last_id)。唯讀 DB、只寫本地檔。"""
    try:
        conn = sqlite3.connect(f"file:{db_path('trade_journal.db')}?mode=ro", uri=True)
    except Exception:  # noqa: BLE001
        return 0, last_id
    written = 0
    max_id = last_id
    try:
        rows = conn.execute(
            "SELECT id, symbol, setup, direction, entry_price, stop_price, "
            "tp1, tp2, tp3, entry_at FROM paper_trades "
            "WHERE setup IN ('us_breakout','deepdive') AND id > ? ORDER BY id",
            (last_id,)).fetchall()
        cols = ["id", "symbol", "setup", "direction", "entry_price", "stop_price",
                "tp1", "tp2", "tp3", "entry_at"]
        OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
        for r in rows:
            row = dict(zip(cols, r))
            max_id = max(max_id, row["id"])
            intent = build_intent(row)
            if not intent:
                continue
            p = OUTBOX_DIR / f"{intent['intent_id']}.json"
            if p.exists():
                continue                       # 冪等：同訊號永不重寫
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(intent, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(p)                     # 原子改名，消費者永不讀到半寫檔
            written += 1
    except Exception as e:  # noqa: BLE001
        print(f"[intent_outbox] scan error（不致命）：{type(e).__name__}: {e}")
    finally:
        conn.close()
    return written, max_id


async def run_intent_outbox_loop(poll_seconds: int = _POLL_SEC):
    """outbox worker：每輪掃新訊號寫 intent 檔。零網路、零交易所互動。"""
    print(f"[intent_outbox] loop online（trade-intent v{INTENT_VER} → {OUTBOX_DIR}）")
    st = _load_state()
    last_id = int(st.get("last_id", 0))
    if last_id == 0:
        # 首次啟動：從當前最大 id 起算（不回填歷史——舊訊號價位早已失效）
        try:
            conn = sqlite3.connect(f"file:{db_path('trade_journal.db')}?mode=ro", uri=True)
            last_id = conn.execute("SELECT IFNULL(MAX(id),0) FROM paper_trades").fetchone()[0]
            conn.close()
        except Exception:  # noqa: BLE001
            last_id = 0
        _save_state({"last_id": last_id})
    while True:
        try:
            written, last_id = await asyncio.to_thread(scan_and_write, last_id)
            if written:
                print(f"[intent_outbox] 寫出 {written} 筆 intent")
                _save_state({"last_id": last_id})
        except Exception as e:  # noqa: BLE001
            print(f"[intent_outbox] loop 例外（不致命）：{type(e).__name__}: {e}")
        await asyncio.sleep(max(15, int(poll_seconds)))
