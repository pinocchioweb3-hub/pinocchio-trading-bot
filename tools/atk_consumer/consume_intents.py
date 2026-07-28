# -*- coding: utf-8 -*-
"""consume_intents.py — trade-intent → OKX Agent Trade Kit(CLI) 消費腳本【使用者側範本】。

角色：這支腳本是「使用者自有執行器」——由使用者審閱後自行排程執行。
    讀 intent_outbox 的 JSON 訊號 → 冪等去重 → 張數換算 → 呼叫 `okx` CLI 下單。
    金鑰只存在使用者的 ~/.okx/config.toml（Agent Kit 本地簽名），本腳本不碰金鑰值。

⛔ 安全鐵則（程式層硬寫死，不可用參數繞過）：
    1. PROFILE 常數 = "demo"，且會先跑 `okx config show` 驗證該 profile 是 demo=true，
       驗不到就拒絕執行任何下單——不存在 live 模式的程式路徑。
       （要上真盤＝使用者親手複製此腳本、自行修改、自行承擔——原檔永遠是 demo。）
    2. 只執行 execution_policy == "demo_only" 的 intent；human_gated 只列印。
    3. 冪等雙鎖：本地 state 檔記已處理 intent_id ＋ OKX clOrdId 去重（同 ID 重送會被拒）。
    4. 過期即棄：now > expires_at 的 intent 直接標 skip（防斷線後補執行過時價位）。
    5. 單筆風險上限 RISK_USD_CAP、名義值上限 NOTIONAL_CAP_USD 雙夾層。

用法（使用者手動首跑，確認無誤後再排程）：
    python consume_intents.py --once      # 消費一輪後退出
    python consume_intents.py --dry-run   # 只列印將執行的指令，不真的下單
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Windows 排程器/cp950 環境下 emoji 輸出防呆（本機環境陷阱：cp950 吃不下 UTF-8 符號）
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

# ── 使用者可調（demo 安全值） ─────────────────────────────────────────
PROFILE = "demo"                 # ⛔硬寫死。原檔永遠 demo；真盤=使用者自建副本自改自擔。
RISK_USD = 100.0                 # 每筆風險預算（1R）
RISK_USD_CAP = 150.0             # 風險絕對上限
NOTIONAL_CAP_USD = 3000.0        # 單筆名義值上限（防張數換算出錯爆倉）
LEVERAGE = 5                     # 保守槓桿（美股代幣永續上限 25x，取遠低於上限）
OUTBOX = Path(os.path.expandvars(r"%LOCALAPPDATA%\TradingBot\intent_outbox"))
STATE = Path(os.path.expandvars(r"%LOCALAPPDATA%\TradingBot\atk_consumer_state.json"))


# Windows 陷阱：npm 全域裝的 okx 是 okx.cmd shim，subprocess 不走 shell 找不到裸名
# → 用 shutil.which 解析完整路徑（會依 PATHEXT 找到 .cmd）
import shutil
_OKX_BIN = shutil.which("okx")


def _okx(args: list[str], timeout: int = 30) -> tuple[int, str]:
    """呼叫 okx CLI（--json 輸出）。回 (exit_code, stdout)。"""
    if not _OKX_BIN:
        return 127, "okx CLI 未安裝（npm install -g @okx_ai/okx-trade-cli）"
    cmd = [_OKX_BIN, "--profile", PROFILE, *args, "--json"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, (r.stdout or r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "okx CLI timeout"


def verify_demo_profile() -> bool:
    """開單前硬驗證：直接讀 ~/.okx/config.toml，PROFILE 段必須 demo=true，
    否則拒絕一切下單（不信任 CLI 輸出格式，讀設定檔本身最可靠）。"""
    try:
        import tomllib
        cfg_path = Path.home() / ".okx" / "config.toml"
        cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
        prof = (cfg.get("profiles") or {}).get(PROFILE) or {}
        if prof.get("demo") is True:
            return True
        print(f"⛔ profile '{PROFILE}' 不是模擬盤（demo≠true）——本腳本永不對非 demo 帳戶下單")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"⛔ 無法驗證 profile（{type(e).__name__}: {e}）——拒絕執行")
        return False


def contracts_for(inst_id: str, entry: float, stop: float, ct_val_cache: dict) -> float | None:
    """風險預算→張數。sz=風險USD÷|entry−stop|÷ctVal，向下取整到 lotSz。錯→None 不下單。"""
    spec = ct_val_cache.get(inst_id)
    if spec is None:
        code, out = _okx(["market", "instruments", "--instType", "SWAP",
                          "--instId", inst_id])
        if code != 0:
            return None
        try:
            raw = json.loads(out)
            # CLI --json 頂層可能是 list（實測 v1.4.2）或 {"data":[...]}，兩者都容
            items = raw.get("data") if isinstance(raw, dict) else raw
            item = items[0]
            spec = {"ctVal": float(item["ctVal"]), "lotSz": float(item["lotSz"]),
                    "minSz": float(item["minSz"])}
            ct_val_cache[inst_id] = spec
        except Exception:  # noqa: BLE001
            return None
    risk = min(RISK_USD, RISK_USD_CAP)
    dist = abs(entry - stop)
    if dist <= 0 or spec["ctVal"] <= 0:
        return None
    units = risk / dist                      # 標的單位數
    sz = units / spec["ctVal"]               # 合約張數
    lot = spec["lotSz"]
    sz = int(sz / lot) * lot                 # 向下取整到 lotSz
    if sz < spec["minSz"]:
        return None
    if sz * spec["ctVal"] * entry > NOTIONAL_CAP_USD:   # 名義值夾層
        sz = int(NOTIONAL_CAP_USD / (spec["ctVal"] * entry) / lot) * lot
        if sz < spec["minSz"]:
            return None
    return round(sz, 8)


def place(intent: dict, sz: float, dry: bool) -> bool:
    """市價開倉＋附掛 TP1/SL（attach 式，避開官方 #15 計畫委託 bug）。"""
    args = ["swap", "place",
            "--instId", intent["inst_id"],
            "--side", intent["side"],
            "--posSide", intent["pos_side"],
            "--ordType", "market",
            "--sz", str(sz),
            "--tdMode", "isolated",
            "--clOrdId", intent["cl_ord_id"],
            "--tpTriggerPx", str(intent["tp1"]), "--tpOrdPx", "-1",
            "--slTriggerPx", str(intent["stop"]), "--slOrdPx", "-1"]
    if dry:
        print("DRY-RUN:", "okx --profile demo " + " ".join(args))
        return True
    code, out = _okx(args)
    ok = code == 0
    print(("✅" if ok else "❌") + f" {intent['inst_id']} {intent['side']} sz={sz} "
          f"→ {out[:200]}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not a.dry_run and not verify_demo_profile():
        return 1
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        state = {"done": []}
    done = set(state.get("done", []))
    ct_cache: dict = {}

    while True:
        now_ms = time.time() * 1000
        for p in sorted(OUTBOX.glob("*.json")):
            try:
                intent = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            iid = intent.get("intent_id")
            if not iid or iid in done:
                continue
            if intent.get("execution_policy") != "demo_only":
                print(f"⏸ {iid} {intent.get('symbol')} human_gated——僅列印不執行")
                done.add(iid)
                continue
            if now_ms > intent.get("expires_at", 0):
                print(f"⏭ {iid} {intent.get('symbol')} 已過期——跳過")
                done.add(iid)
                continue
            sz = contracts_for(intent["inst_id"], intent["entry"], intent["stop"], ct_cache)
            if sz is None:
                print(f"❌ {iid} 張數換算失敗——跳過（不猜）")
                done.add(iid)
                continue
            place(intent, sz, a.dry_run)
            done.add(iid)
        state["done"] = sorted(done)[-500:]
        try:
            STATE.write_text(json.dumps(state), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        if a.once or a.dry_run:
            return 0
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
