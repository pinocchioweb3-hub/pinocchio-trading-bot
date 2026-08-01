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
_MAX_EXPIRY_H = max(EXPIRY_HOURS.values())        # 進度未知時的保守回掃窗（v193）


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


def _preserve_bad_state(text: str, err: Exception) -> None:
    """壞掉的進度檔留一份鑑識副本——原檔下一次 _save_state 就會被蓋掉。"""
    bad = _STATE.with_suffix(".bad")
    kept = ""
    try:
        if not bad.exists():           # 只留最早那一份（後續覆蓋會沖掉第一現場）
            bad.write_text(text, encoding="utf-8")
        kept = f"；壞檔已留證於 {bad.name}"
    except Exception:  # noqa: BLE001
        kept = "；（留證失敗）"        # 告警層自己的 best-effort——不可反過來壓掉主訊息
    print(f"🚨 [intent_outbox] 進度檔壞了（{type(err).__name__}: {err}）{kept}——"
          "⛔ 不當成『第一次啟動』：那會把上次存檔以來的訊號整批靜默跳過")


def _load_state() -> tuple[dict, str]:
    """讀 outbox 進度。回 (state, status)，status ∈ {"ok","missing","unreadable"}。

    v193（監督員 r87）：同物種第 13 次——**未知被折成確認沒有**。舊版三種情形共用同一個
    回答 `{"last_id": 0}`，而啟動端把 `last_id == 0` 讀成「真·第一次啟動」→ 直接跳到
    MAX(id)。於是進度檔一壞，上次成功存檔到這次重啟之間的**每一筆**訊號都不會被寫成
    intent；消費端連檔都看不到，也沒有任何重試路徑（last_id 已經跳過去了）。

    壞檔正是自己製造的：舊版 _save_state 用非原子 write_text，斷電／當機寫到一半就是
    半截 JSON（本機有實際斷電事件史），下一次啟動再自己誤讀＝自產自誤的閉環。
    ⛔ 勿改回單一 dict 回傳值。"""
    try:
        text = _STATE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"last_id": 0}, "missing"          # 沒有檔＝真的還沒跑過（唯一可跳 MAX 的情形）
    except Exception as e:  # noqa: BLE001 權限／被鎖住／IO 錯——檔可能在，只是讀不到
        print(f"🚨 [intent_outbox] 進度檔讀不到（{type(e).__name__}: {e}）——"
              "⛔ 不當成『第一次啟動』；改走保守復原起點")
        return {"last_id": 0}, "unreadable"
    try:
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError(f"頂層是 {type(raw).__name__} 不是物件")
    except Exception as e:  # noqa: BLE001
        _preserve_bad_state(text, e)
        return {"last_id": 0}, "unreadable"
    return raw, "ok"


def _save_state(st: dict) -> bool:
    """原子寫進度。回 True/False——⛔ 勿改回直接 write_text，也勿改回靜默 pass。

    非原子寫正是上面那個壞檔的來源（同 v166 已治的健康檔）。失敗要出聲：進度存不進去
    代表下次重啟會拿到舊進度／壞進度。"""
    tmp = _STATE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(st), encoding="utf-8")
        tmp.replace(_STATE)                        # 原子改名：讀者永不讀到半寫檔
        return True
    except Exception as e:  # noqa: BLE001
        print(f"🚨 [intent_outbox] 進度存不進去（{type(e).__name__}: {e}）——"
              "下次重啟會拿到過期進度；請查磁碟空間／資料夾權限／同步軟體是否鎖檔")
        return False


def _startup_last_id(status: str) -> Optional[int]:
    """決定起始 id。回 None＝查不出來（⛔ 不得假設 0，也不得假設 MAX）。

    status=="unreadable"（進度未知）時**不可**取 MAX(id)——那等於斷言「這些都處理過了」。
    改取「仍在有效窗內的最舊訊號」之前一格：窗內漏掉的會補上（同 intent_id 檔已存在者
    自動略過＝冪等），窗外的本來就已過期、消費端一律丟棄，回填只會製造垃圾檔。"""
    try:
        conn = sqlite3.connect(f"file:{db_path('trade_journal.db')}?mode=ro", uri=True)
    except Exception as e:  # noqa: BLE001
        print(f"🚨 [intent_outbox] 起始 id 查不出來（{type(e).__name__}: {e}）——"
              "⛔ 不寫下 last_id:0（那會把整部歷史當新訊號回填）；下輪重試")
        return None
    try:
        max_id = int(conn.execute("SELECT IFNULL(MAX(id),0) FROM paper_trades").fetchone()[0])
        if status != "unreadable":
            return max_id            # 真·第一次啟動：不回填歷史（舊訊號價位早已失效）
        cutoff_ms = int((time.time() - _MAX_EXPIRY_H * 3600) * 1000)
        row = conn.execute(
            "SELECT MIN(id) FROM paper_trades WHERE setup IN ('us_breakout','deepdive') "
            "AND entry_at >= ?", (cutoff_ms,)).fetchone()
        oldest = row[0] if row else None
        if oldest is None:
            print("[intent_outbox] 進度未知，但有效窗內沒有任何訊號 → 起點取 MAX(id)")
            return max_id
        start = int(oldest) - 1
        print(f"[intent_outbox] 進度未知 → 保守復原：從 id {start} 起重掃有效窗內訊號"
              "（同 intent_id 已存在者自動略過）")
        return start
    except Exception as e:  # noqa: BLE001
        print(f"🚨 [intent_outbox] 起始 id 查詢失敗（{type(e).__name__}: {e}）——"
              "⛔ 不假設進度；下輪重試")
        return None
    finally:
        conn.close()


def scan_and_write(last_id: int) -> tuple[int, int]:
    """掃新 paper 訊號→原子寫 intent 檔。回 (寫出數, 新 last_id)。唯讀 DB、只寫本地檔。"""
    try:
        conn = sqlite3.connect(f"file:{db_path('trade_journal.db')}?mode=ro", uri=True)
    except Exception as e:  # noqa: BLE001
        print(f"🚨 [intent_outbox] trade_journal.db 打不開（{type(e).__name__}: {e}）——"
              "本輪掃不到訊號；⛔ 這不等於『沒有新訊號』，下輪重試")
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
    st, status = _load_state()
    try:
        last_id = int(st.get("last_id", 0) or 0)
    except Exception:  # noqa: BLE001 型別不對＝進度**未知**，不是 0
        last_id, status = 0, "unreadable"
    # v193：只有「讀得出來且有進度」才算已知。其餘一律交給 _startup_last_id 重新決定，
    # ⛔ 不可再讓 unreadable 與 missing 共用「跳到 MAX(id)」這一條路。
    resolved = status == "ok" and last_id > 0
    while True:
        try:
            if not resolved:
                start = await asyncio.to_thread(_startup_last_id, status)
                if start is None:
                    print("⚠️ [intent_outbox] 起始 id 未定——本輪不掃、不寫進度，下輪重試")
                else:
                    last_id, resolved = start, True
                    _save_state({"last_id": last_id})
            if resolved:
                written, last_id = await asyncio.to_thread(scan_and_write, last_id)
                if written:
                    print(f"[intent_outbox] 寫出 {written} 筆 intent")
                    _save_state({"last_id": last_id})
        except Exception as e:  # noqa: BLE001
            print(f"[intent_outbox] loop 例外（不致命）：{type(e).__name__}: {e}")
        await asyncio.sleep(max(15, int(poll_seconds)))
