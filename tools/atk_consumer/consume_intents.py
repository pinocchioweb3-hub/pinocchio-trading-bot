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
import math
import os
import re
import subprocess
import sys
import tempfile
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
TIMEOUT_HOURS = 24.0             # 持倉逾時強制平倉（對齊紙上 us_breakout 24h 口徑）
# ── TP1 後保本移損（v249；使用者 2026-08-03 明確指示） ────────────────
# 開關預設 ON＝使用者要的行為。⛔ 誠實揭露：自家 1366 訊號的止損管理 A/B（task#13，
# memory/stop-management-ab-verdict.md）判定「TP1 後保本」在**加密突破訊號**上
# 淨 R 期望 −0.027（A_fixed −0.000），配對 PSR P=0% 顯著劣於不搬；勝率從 41.5%
# 抬到 52.0% 是砍掉右尾換來的幻覺。該 A/B **沒有**涵蓋美股引擎、也沒用 deepdive
# 真訊號 ⇒ 對這條路徑只是先驗、不是定論。使用者已知此結論仍要求落地（保住已浮盈
# 的部位優先於期望值最大化），照做並留下量測欄位，日後可用真樣本回頭裁決。
BE_ENABLED = True                # TP1（任一 TP 腿）成交後，把剩餘腿的止損搬到保本
BE_BUFFER_R = 0.1                # 保本緩衝（R）：⛔ 不設 0——剛好成本價扣完雙邊手續費
                                 #   仍是淨虧，而且貼著均價＝噪音磁鐵（A/B 同此設定）
BE_GAP_RECENT_MAX = 20           # 健康檔內保留的「該保本卻沒搬成」明細筆數上限
DAILY_STOP_USD = 300.0           # 日虧熔斷（≈3R）：當日已實現虧損達此值→今日不再接新單
OUTBOX = Path(os.path.expandvars(r"%LOCALAPPDATA%\TradingBot\intent_outbox"))
STATE = Path(os.path.expandvars(r"%LOCALAPPDATA%\TradingBot\atk_consumer_state.json"))
POS_STATE = Path(os.path.expandvars(r"%LOCALAPPDATA%\TradingBot\atk_positions.json"))
HEALTH = Path(os.path.expandvars(r"%LOCALAPPDATA%\TradingBot\atk_consumer_health.json"))

# ── 連續 fail-closed 告警（v143；2026-07-30 教訓） ─────────────────────
# fail-closed 本身是對的（認證失敗就不下單），錯的是「沒有出口」：
# 那晚 OKX 因浮動 IP 換掉回 401 共 121 次、整盤零成交，卻只寫在 log 裡沒人知道，
# 靠肉眼撞見才發現——與 halt 殘閂 19 天、週報斷檔 16.4 天同一物種（無聲失敗）。
FAIL_ALERT_AFTER = 3            # 連續幾輪有故障才告警（單輪抖動不吵）
FAIL_ALERT_REPEAT_SEC = 3600.0  # 同類故障持續時的重複提醒間隔
ENV_FILE = Path(r"C:\Users\user\OneDrive\桌面\交易機器人\.env")  # 只讀 TG 憑證，永不列印值

# 良性回應：查無此單是「冪等查詢」的正常答案，不是故障
_BENIGN_MARKERS = ("51603", "doesn't exist", "does not exist")
# 故障嚴重度排序（同輪多類時取最前者當代表）
# orphan_position 排最前：它是唯一代表「真錢部位在交易所上、但已脫離本地帳」的類別，
# 而且只有在「account positions 查詢成功」時才可能被記到（＝不可能發生在斷流輪，
# 不會像 leverage_fail 那樣有洗掉斷流主因的疑慮）。見 r47。
#
# v163（r54）：pos_state_* 兩類排在傳輸類之前——本地帳壞掉會同時讓熔斷口徑與同幣同向
# 閘失去資料（不是「這輪送不出去」，是「風險上限暫時不存在」），且與網路無關、
# 不可能被斷流洗掉；修法也完全不同（要人去看那個檔案，不是等網路好）。
#
# v165（r56）：done_state_* 兩類緊接在 pos_state_* 之後——同樣是本地檔壞掉、與網路無關、
# 不可能被斷流洗掉，且後果同樣是「風險閘暫時不存在」（分不出哪些單下過 ⇒ 全面停接新單）；
# 排在 pos_state_* 之後是因為部位帳壞掉會直接讓熔斷消失，比冪等清單壞掉更前面一步。
# v170（r68）：pnl_unaccounted 緊接在 orphan_position 之後——它是唯一「已經發生且
# 不可逆」的類別（已了結的真錢損益永遠不會再進 day_pnl ⇒ 日/週熔斷從此低估）。
# 其餘 pos_state_*／done_state_* 雖然也會讓風險閘失效，但都是**暫時**的（下輪重試
# 就回來）；傳輸類更只是「這輪送不出去」。代表類別若被下游的 query_fail 蓋掉，
# 使用者看到的處置建議會變成「等網路好」，而這一類等再久也不會好。
# 與 orphan_position 同理：它只可能在 account positions 查詢成功的輪被記到，
# 不可能發生在斷流輪 ⇒ 不會洗掉斷流主因。
_CLASS_PRIORITY = ("orphan_position", "pnl_unaccounted",
                   # v171（r69）：同上兩類的性質——只可能在 account positions 查詢
                   # 成功的輪被記到 ⇒ 不會洗掉斷流主因；⛔ 但不得擠掉前兩位（r68 令）
                   "lev_mismatch",
                   "pos_state_unreadable", "pos_state_write_fail",
                   "done_state_unreadable", "done_state_write_fail",
                   "cli_missing", "auth_ip_whitelist", "auth",
                   "rate_limit", "timeout", "leverage_fail", "query_fail",
                   # v208（r103）：查詢通了、回應形狀認不得。排在 query_fail **之後**：
                   # 兩者處置相近，但連線類故障（401／限流／逾時）若同輪也發生，那才是
                   # 使用者該先去動的主因，⛔ 不可被這一類擠掉。排在 intent_unreadable
                   # 之前：它是對外回應的問題，比純本地檔的問題更接近斷流主因。
                   "exchange_rows_unreadable",
                   # v209（r104）：合約規格（market instruments）這輪讀不出來。緊接在
                   # exchange_rows_unreadable 之後：同樣是「對外查詢的回應讀不到」，
                   # 同樣不可擠掉連線類主因；後果比它輕（只有那一筆 intent 本輪不接，
                   # 既有倉照常管理），故排它之後。
                   "instrument_spec_unreadable",
                   # v192（r86）：讀本地 intent 檔失敗——⛔ 必須排在連線類**之後**。
                   # 理由同本表開頭：它是純本地失敗，斷流輪照樣會被記到；排前面會把
                   # 「對外連不上」這個真正主因從代表類別擠掉，使用者拿到的處置建議
                   # 就會變成「去刪一個檔」，而不是「去補白名單」。
                   "intent_unreadable",
                   # v207（r102）：已確認手動倉檔壞掉——排在此處的理由同 intent_unreadable
                   # （純本地失敗,不可把斷流主因擠掉）,且**必須**在 orphan_position 之後：
                   # 它談的正是孤兒倉,若反過來當代表,使用者拿到的處置建議會從
                   # 「去 OKX 看那個真錢倉」變成「去修一個檔」。它讀壞時走的是比較嚴格的
                   # 那一邊（全部當孤兒）,不會讓任何風險閘失效 ⇒ 不需排到前段。
                   "acked_state_unreadable",
                   # v164/v166：健康檔壞掉（讀不到／寫不進去）排在最後——它是告警層
                   # 自己的內傷，永遠不該蓋掉同輪真正的交易故障當代表（否則使用者
                   # 看到的主因會被換掉）
                   "health_state_unreadable", "health_state_write_fail", "other")
_ROUND_FAILS: dict[str, str] = {}   # 本輪故障 {類別: 樣本}；每輪開頭清空
# v151：本輪「成功呼叫數」；每輪開頭歸零。沒有它就無法分辨「全部呼叫都成功」
# 與「這輪根本沒呼叫」——後者被當成乾淨輪會蓋出假痊癒（見 update_health）。
_ROUND_OKS: dict[str, int] = {"ok": 0}
# v169：本輪「因過期而永久丟棄」的 intent；每輪開頭清空。過了 expires_at 就不會
# 再重送，這是斷流唯一會造成的實質損失，必須是數字而不是一行 log（見 _account_expiry）。
_ROUND_EXPIRED: list[dict] = []
EXPIRED_RECENT_MAX = 20             # 健康檔內保留的丟棄明細筆數上限
# v170（監督員 r68）：本輪「已了結、但已實現損益查不到而放棄」的部位；每輪開頭清空。
# 那筆損益是日/週熔斷**唯一**的輸入，漏記＝風險上限被低估，而且不可逆（紀錄一從
# 本地帳移出，就再也沒有任何東西知道要去查它）。必須是數字，不能只是一行 log。
_ROUND_PNL_GAPS: list[dict] = []
PNL_GAP_RECENT_MAX = 20             # 健康檔內保留的漏記明細筆數上限
# 放棄前先重試幾輪。⛔ 用「輪數」不用牆鐘：每輪都是獨立行程（schtasks 每分鐘一次），
# 牆鐘門檻會在排程漏跑／休眠時把重試窗白白吃掉，反而更容易走到不可逆那一步。
PNL_RETRY_MAX = 5
# v249：本輪「已吃到 TP1、但剩餘腿的止損沒能搬到保本」的部位；每輪開頭清空。
# ⛔ 刻意**不**走 _note_fail：那是「輪級故障」的通道，而這是**部位狀態**。
#   v171 的 lev_mismatch 正是踩了這個坑——倉活著就每輪記一次故障 ⇒
#   consecutive_fail_rounds 永不歸零、last_ok_ts 凍住、蓋掉其他類別，
#   帳本因此把「執行器照跑」誤報成「管線實質停擺」。這裡比照 _ROUND_PNL_GAPS
#   走獨立結構化通道：計數、明細、時間戳都進健康檔，但不污染故障連續輪。
_ROUND_BE_GAPS: list[dict] = []


# Windows 陷阱：npm 全域裝的 okx 是 okx.cmd shim，subprocess 不走 shell 找不到裸名
# → 用 shutil.which 解析完整路徑（會依 PATHEXT 找到 .cmd）
import shutil
_OKX_BIN = shutil.which("okx")


def redact_secrets(text: str) -> str:
    """遮蔽 API key 識別碼（UUID）。
    ⚠️ IP 不遮——那正是使用者要拿去補白名單的唯一有用資訊；key id 對他無診斷價值。"""
    return re.sub(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                  r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                  "<key-id-redacted>", text or "")


def classify_failure(code: int, out: str) -> str | None:
    """把 okx CLI 失敗分流成「可行動的類別」（純函式）。回 None＝良性非故障。

    分流的意義：401 白名單要人去後台補、cli_missing 要重裝、rate_limit 會自癒、
    query_fail 是下游症狀——混在一起就只剩「有錯」這種沒人會動作的資訊。"""
    t = out or ""
    low = t.lower()
    if any(s in t for s in _BENIGN_MARKERS):
        return None                      # 查無此單＝冪等查詢的正常答案
    if code == 127 or "未安裝" in t:
        return "cli_missing"
    if "401" in t and "not included in" in low:
        return "auth_ip_whitelist"       # 浮動 IP 換掉→白名單失效（會復發）
    if ("401" in t or "invalid sign" in low or "50111" in t or "50113" in t
            or "50102" in t):
        return "auth"
    if code == 124 or "timeout" in low:
        return "timeout"
    if "50011" in t or "429" in t or "too many requests" in low:
        return "rate_limit"
    return "other"


def _note_fail(cls: str | None, sample: str) -> None:
    """記一筆本輪故障（同類只留第一個樣本＝class_counts 以「輪」為單位不重複計）。"""
    if cls and cls not in _ROUND_FAILS:
        _ROUND_FAILS[cls] = redact_secrets(sample)[:300]


def _okx(args: list[str], timeout: int = 30) -> tuple[int, str]:
    """呼叫 okx CLI（--json 輸出）。回 (exit_code, stdout)。

    失敗一律登記到 _ROUND_FAILS、成功一律計入 _ROUND_OKS（單一掛鉤攔到槓桿/查單/
    下單/對帳全部路徑）——只加副作用，回傳值與交易邏輯完全不變。"""
    if not _OKX_BIN:
        out = "okx CLI 未安裝（npm install -g @okx_ai/okx-trade-cli）"
        _note_fail("cli_missing", out)
        return 127, out
    cmd = [_OKX_BIN, "--profile", PROFILE, *args, "--json"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        out = (r.stdout or r.stderr or "")
        if r.returncode != 0:
            cls = classify_failure(r.returncode, out)
            _note_fail(cls, out)
            # 良性回應（查無此單）代表呼叫確實通到 OKX 並拿到可理解的答覆＝算通
            if cls is None:
                _ROUND_OKS["ok"] += 1
        else:
            _ROUND_OKS["ok"] += 1        # v151：成功也要記，否則無從證明「真的通了」
        return r.returncode, out
    except subprocess.TimeoutExpired:
        _note_fail("timeout", "okx CLI timeout")
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


# ── 健康狀態／告警（v143） ────────────────────────────────────────────
# v166（r57）：健康檔「寫不進去」時的最後出口。
# 健康檔是告警層自己的記事本：寫不進去 ⇒ consecutive_fail_rounds 停在舊值、
# last_alert_ts 也落不了地 ⇒ 門檻（連 3 輪才告警）與冷卻（同類 1 小時）雙雙失效。
# 最壞情況不是吵，是**啞**：若舊值是 0，之後每輪都從 0 數到 1、永遠到不了門檻，
# 一場真實斷流可以整場沒有任何通知——與 v164 讀失敗同一物種，只是走寫入這條路。
# 節流痕跡刻意放在系統暫存目錄（跟壞掉的資料目錄不同一個地方），寫不進去／讀不到
# 一律視為「該講」——這條路徑上「沒有證據」永遠不可以推論成「不用講」。
_HEALTH_WRITE_ERR: dict[str, str] = {}
DEGRADED_MARKER = Path(tempfile.gettempdir()) / f"atk_health_write_fail_{PROFILE}.ts"


def _load_health() -> dict | None:
    """讀健康檔。回 None＝主檔與備份都讀不到／壞掉（**未知**）；⛔ 不可再回 {}。

    v164（監督員 r55）：與 v163 的本地部位帳同一物種——舊版把任何例外壓成 {}，
    等於宣告「沒有任何故障史」，於是 consecutive_fail_rounds 每輪從零重數、
    永遠到不了 FAIL_ALERT_AFTER 門檻＝**告警層自己無聲死掉**（正是 v143 要治的東西）。
    健康檔壞掉的機率不低：舊 _save_health 是「先截斷再寫」的非原子寫，行程被砍
    （排程逾時／休眠／當機）就留半截 JSON。

    「檔案不存在」＝首跑，是真的沒有故障史（合法）；壞 JSON／IO 錯／型別不對＝未知，
    此時先退回 .bak（last-known-good）續算，備份也壞才回 None。"""
    for path in (HEALTH, HEALTH.with_name(HEALTH.name + ".bak")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except Exception:  # noqa: BLE001
            continue
        if isinstance(raw, dict):
            return raw
    # 兩個都不存在＝首跑（真空帳）；存在但都壞掉＝未知
    if not HEALTH.exists() and not HEALTH.with_name(HEALTH.name + ".bak").exists():
        return {}
    return None


def _save_health(h: dict) -> bool:
    """原子寫回健康檔＋鏡射一份 .bak。回 False＝沒寫成（呼叫端要出聲）。

    v164（監督員 r55）：①暫存檔＋os.replace——非原子寫留下的半截 JSON 正是上面
    _load_health 要防的壞檔來源本身。②另存 .bak：兩次分開的原子寫，行程被砍時
    最多壞掉其中一個，另一個仍是 last-known-good。③失敗不再完全無聲（印出來）。

    v166（監督員 r57）：失敗原因存進 _HEALTH_WRITE_ERR 供 finish_round 組告警文字用
    （回傳值仍是 bool，不動既有呼叫端）。"""
    tmp = HEALTH.with_name(HEALTH.name + ".tmp")
    body = json.dumps(h, ensure_ascii=False, indent=1)
    ok = False
    _HEALTH_WRITE_ERR.pop("err", None)
    try:
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, HEALTH)
        ok = True
    except Exception as e:  # noqa: BLE001
        _HEALTH_WRITE_ERR["err"] = f"{type(e).__name__}: {str(e)[:160]}"
        print(f"⚠️ 健康檔寫入失敗（告警計數將停在舊值）：{type(e).__name__}: {e}")
    try:
        bak_tmp = HEALTH.with_name(HEALTH.name + ".bak.tmp")
        bak_tmp.write_text(body, encoding="utf-8")
        os.replace(bak_tmp, HEALTH.with_name(HEALTH.name + ".bak"))
    except Exception:  # noqa: BLE001
        pass
    return ok


def worst_class(classes) -> str | None:
    """同輪多類故障時取代表（純函式）：依 _CLASS_PRIORITY 取最嚴重者。"""
    for c in _CLASS_PRIORITY:
        if c in classes:
            return c
    return next(iter(sorted(classes)), None)


def _account_expiry(h: dict, expired, now_s: float, in_fault: bool,
                    fault_class: str | None) -> None:
    """把本輪「過期丟棄」的 intent 記進健康帳（就地改 h；呼叫端已複製過）。

    v169（監督員 r67）：過期分支原本只有一行 print，intent 就此永久消失——
    重送不會發生（過了 expires_at 就是過了）。於是一場斷流吃掉幾筆訊號，
    只能靠 grep 837KB 的 log 才問得出來；健康檔、監督帳本、Telegram 全都不知道。
    與 v164/v166/v167 同一物種：要用來下判斷的量只以 log 文字存在＝等於不存在。

    分兩本帳：total＝含正常老化（訊號本來就有時效）；during_fault＝斷流期丟的，
    才是「這場故障的代價」。混在一起會被日常噪音灌水，數字就沒有意義了。"""
    drops = list(expired or [])
    if not drops:
        return                                  # 沒事不生欄位＝帳本不長出一排 0
    h["expired_dropped_total"] = int(h.get("expired_dropped_total", 0)) + len(drops)
    if not in_fault:
        return                                  # 正常老化不算斷流代價
    h["expired_dropped_during_fault"] = \
        int(h.get("expired_dropped_during_fault", 0)) + len(drops)
    h["expired_dropped_last_ts"] = now_s
    recent = list(h.get("expired_dropped_recent") or [])
    for d in drops:
        recent.append({"intent_id": d.get("intent_id"), "symbol": d.get("symbol"),
                       "side": d.get("side"), "ts": now_s, "fault_class": fault_class})
    # 上限：健康檔每輪整份重寫，無界成長會養到寫不進去，反過來打死 v166 的告警計數
    h["expired_dropped_recent"] = recent[-EXPIRED_RECENT_MAX:]


def _account_pnl_gap(h: dict, gaps, now_s: float) -> None:
    """把本輪「已了結但損益查不到、已放棄」的部位記進健康帳（就地改 h）。

    v170（監督員 r68）：舊碼查不到損益就直接把紀錄從本地帳移出，只留一行 print。
    那筆已實現損益是 day_pnl（日 60U／週 150U 熔斷）唯一的輸入 ⇒ 熔斷從此低估已實現
    虧損，而且不可逆：紀錄一移出，就沒有任何東西知道要回去查它。與 v164/v166/v167/
    v169 同一物種（要下判斷的量只以 log 文字存在），差別是這次落在風險上限上。

    ⛔ 不分「斷流期／健康期」（與 _account_expiry 刻意不同）：過期丟棄只有在斷流時
    才是故障的代價，日常老化是正常的；但損益漏記在任何情況下都是同一個洞，
    切兩本帳只會讓人以為健康期那幾筆比較不要緊。"""
    drops = list(gaps or [])
    if not drops:
        return                                  # 沒事不生欄位＝帳本不長出一排 0
    h["pnl_unaccounted_total"] = int(h.get("pnl_unaccounted_total", 0)) + len(drops)
    h["pnl_unaccounted_last_ts"] = now_s
    recent = list(h.get("pnl_unaccounted_recent") or [])
    for d in drops:
        # 明細只留「查得回去」需要的東西：instId + 開倉時間就足以在 OKX 成交紀錄
        # 裡框出區間。⛔ 不記金額——金額正是我們查不到的那個量，寫 0 會變成謊報。
        recent.append({"inst_id": d.get("inst_id"), "pos_side": d.get("pos_side"),
                       "symbol": d.get("symbol"), "intent_id": d.get("intent_id"),
                       "placed_at": d.get("placed_at"), "retries": d.get("retries"),
                       "ts": now_s})
    # 上限：健康檔每輪整份重寫，無界成長會養到寫不進去，反過來打死 v166 的告警計數
    h["pnl_unaccounted_recent"] = recent[-PNL_GAP_RECENT_MAX:]


def _account_be_gap(h: dict, gaps, now_s: float) -> None:
    """把本輪「已吃到 TP1、但剩餘腿的止損沒搬成保本」的部位記進健康帳（就地改 h）。

    v249：保本這件事只有兩個時刻有意義——TP1 成交後、剩餘腿還在場的那段。搬不動
    （查不到掛單／amend 被拒／回應看不懂）時，倉位仍掛在**原始止損**上，也就是使用者
    以為已經保住的利潤其實還在風險裡。這個落差只以一行 log 存在就等於不存在
    （v164/v166/v167/v169/v170 同一物種）。

    ⛔ 不寫「已保本」的正向計數進這裡：這本帳只記缺口。正向證據落在部位檔的
    be.state/be.px（含實際搬到的價位），一平倉就跟著消失是刻意的——保本成功不是
    需要跨輪追蹤的風險，保本失敗才是。"""
    drops = list(gaps or [])
    if not drops:
        return                                  # 沒事不生欄位＝帳本不長出一排 0
    h["breakeven_unmoved_total"] = int(h.get("breakeven_unmoved_total", 0)) + len(drops)
    h["breakeven_unmoved_last_ts"] = now_s
    recent = list(h.get("breakeven_unmoved_recent") or [])
    for d in drops:
        recent.append({"inst_id": d.get("inst_id"), "pos_side": d.get("pos_side"),
                       "symbol": d.get("symbol"), "intent_id": d.get("intent_id"),
                       "reason": d.get("reason"), "tries": d.get("tries"),
                       "want_px": d.get("want_px"), "ts": now_s})
    h["breakeven_unmoved_recent"] = recent[-BE_GAP_RECENT_MAX:]


def update_health(h: dict, fails: dict, now_s: float, oks: int = 0,
                  expired=None, pnl_gaps=None, be_gaps=None) -> dict:
    """把本輪結果併進健康狀態（純函式，不做 I/O）。

    connsecutive_fail_rounds 只在「有故障的輪」累加；乾淨輪歸零並在曾告警過時
    留下 recovered_from＝讓恢復也有出口（無聲恢復同樣會讓人誤判）。

    v151【假痊癒治本】「沒故障」不等於「通了」——本輪一次呼叫都沒發生（intent 全
    過期／無倉可管）時 fails 也是空的。舊版把這種**空轉輪**當乾淨輪：連續故障歸零、
    送出「✅已恢復」，但故障其實一步都沒好。2026-07-31 就是這樣：401 白名單斷流
    291 輪 → 兩個 intent 剛好過期 → 空轉一輪 → 假恢復通知 → 下一輪立刻又 401。
    因此改成三態：有故障→累加；無故障但 oks==0→**空轉，維持原狀**（不歸零、不報
    恢復）；無故障且 oks>0→真的通了才算乾淨輪。oks 預設 0＝呼叫端沒證明就不算通
    （fail-closed 方向：寧可晚報恢復，不可假報恢復）。"""
    h = dict(h)
    h["rounds_seen"] = int(h.get("rounds_seen", 0)) + 1
    h["updated_at"] = now_s
    h["updated_at_local"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(now_s))
    h["profile"] = PROFILE
    h.pop("recovered_from", None)
    # v169（r67）：⛔ 過期記帳必須在下面那個「空轉輪提早 return」之前。
    #   斷流期的丟棄**恰好**發生在空轉輪：intent 全過期 ⇒ 一次呼叫都沒發出
    #   ⇒ fails 空、oks==0。記在 return 之後，唯一要記的情境就一筆都記不到。
    _account_expiry(h, expired, now_s,
                    in_fault=bool(fails) or int(h.get("consecutive_fail_rounds", 0)) > 0,
                    fault_class=(worst_class(fails.keys()) if fails
                                 else h.get("last_fail_class")))
    # v170（r68）：同理，記在空轉輪提早 return 之前。漏記不必然發生在空轉輪，但只要
    #   有一次落在那條路徑上就永久記不到，而這種東西只會在事後才被發現。
    _account_pnl_gap(h, pnl_gaps, now_s)
    # v249：同理擺在空轉輪提早 return 之前。搬不動保本最典型的成因就是查詢類故障，
    #   而那種輪很可能一次成功呼叫都沒有（oks==0）＝正好落在空轉輪分支上。
    _account_be_gap(h, be_gaps, now_s)
    if not fails and int(oks or 0) <= 0:
        # 空轉輪：沒故障也沒成功呼叫＝本輪對「是否已恢復」零資訊，維持原判
        h["idle_rounds"] = int(h.get("idle_rounds", 0)) + 1
        h["last_idle_ts"] = now_s
        return h
    if not fails:
        streak = int(h.get("consecutive_fail_rounds", 0))
        if streak >= FAIL_ALERT_AFTER and h.get("last_alert_ts"):
            h["recovered_from"] = {"class": h.get("last_fail_class"),
                                   "fail_rounds": streak}
            h.pop("last_alert_ts", None)
            h.pop("last_alert_class", None)
        h["consecutive_fail_rounds"] = 0
        h["last_ok_ts"] = now_s
        h.pop("first_fail_ts", None)
        return h
    cls = worst_class(fails.keys())
    h["consecutive_fail_rounds"] = int(h.get("consecutive_fail_rounds", 0)) + 1
    h.setdefault("first_fail_ts", now_s)
    h["last_fail_ts"] = now_s
    h["last_fail_class"] = cls
    h["last_fail_sample"] = fails.get(cls, "")
    counts = dict(h.get("class_counts") or {})
    for c in fails:
        counts[c] = int(counts.get(c, 0)) + 1
    h["class_counts"] = counts
    return h


def should_alert(h: dict, now_s: float, threshold: int = FAIL_ALERT_AFTER,
                 repeat_sec: float = FAIL_ALERT_REPEAT_SEC) -> bool:
    """要不要現在告警（純函式）：連續故障達門檻，且（未告警過／換了故障類別／
    距上次提醒超過 repeat_sec）。故障類別變了立刻再報＝新故障不被舊冷卻蓋掉。"""
    if int(h.get("consecutive_fail_rounds", 0)) < threshold:
        return False
    last_ts = h.get("last_alert_ts")
    if not last_ts:
        return True
    if h.get("last_alert_class") != h.get("last_fail_class"):
        return True
    return (now_s - float(last_ts)) >= repeat_sec


_CLASS_HINT = {
    "orphan_position": "交易所上有一個本地帳沒有的真錢部位（多半是分批進場只成交了"
                       "前面幾腿、後面查單失敗導致整筆未記帳）→ 它不在自動管理之下："
                       "不會逾時平倉、了結損益也不會進日/週熔斷口徑；但它的止損仍掛在"
                       "交易所，單筆風險仍受 SL 上限保護。請人工到 OKX 確認該倉並決定"
                       "是否平掉；在它消失前，同幣同向的新單會被自動擋下",
    "pos_state_unreadable": "本地部位帳讀不到或內容壞掉（多半是寫到一半被中斷留下半截"
                            "JSON）→ 熔斷口徑與同幣同向擋單閘在修好前都沒有資料，"
                            "執行器已自動停接新單（既有倉的止損仍掛在交易所）。"
                            "請人工檢查 atk_positions*.json；⛔ 不要直接刪掉——"
                            "刪掉等於把在場倉與近 14 天熔斷損益一起清零",
    "pos_state_write_fail": "本地部位帳寫不進去 → 剛送出的單可能沒記進帳本（下一輪會"
                            "被當孤兒部位偵測到）。請確認磁碟空間／檔案沒被鎖住",
    "done_state_unreadable": "已處理 intent 清單讀不到或內容壞掉（多半是寫到一半被中斷"
                             "留下半截 JSON）→ 分不出哪些 intent 已經下過單，執行器已"
                             "自動停接新單（既有倉照常管理、止損仍掛在交易所）。"
                             "請人工檢查 atk_consumer_*state.json；⛔ 不要直接刪掉、"
                             "也不要清成空清單——那會讓 6 小時內的舊 intent 全部重跑，"
                             "把早已平掉的倉又記成在場（假倉會擋掉之後真正該下的單）",
    "done_state_write_fail": "已處理 intent 清單寫不進去 → 本輪處理過的 intent 沒有落地，"
                             "行程重啟後會重跑（clOrdId 冪等擋重複成交，但仍應修）。"
                             "請確認磁碟空間／檔案沒被鎖住",
    "acked_state_unreadable": "「已確認手動倉」清單檔存在、但一筆也讀不出來（解不開／"
                              "頂層不是清單／鍵名不對）→ 你在聊天室確認過的那些倉,"
                              "系統這邊等於沒收到:它們仍會被當成孤兒部位每輪告警。"
                              "⚠️ 這不會讓任何風險閘失效（讀壞一律走比較嚴格的那一邊）,"
                              "管線其餘部分照常。處置：檢查 "
                              "%LOCALAPPDATA%\\TradingBot\\atk_acknowledged_positions.json,"
                              "格式是一個清單、每筆要有 inst_id 與 pos_side，例如 "
                              '[{"inst_id":"XXX-USDT-SWAP","pos_side":"long"}]',
    "exchange_rows_unreadable": "交易所查詢通了、也回了合法 JSON，但那個形狀認不得"
                                "（不是清單，也沒有 data 清單）→ 最可能是 okx CLI 換版"
                                "改了輸出格式。執行器已自動停手：本輪不對帳、不平倉、"
                                "不接新單（既有倉的止損仍掛在交易所）。⛔ 這條**不是**"
                                "「交易所上沒有倉」——真正的倉況本輪等於沒讀到。"
                                "處置：手動跑一次 `okx --profile <profile> account "
                                "positions --json` 看實際輸出長什麼樣；若 CLI 剛升級過，"
                                "回報這個新形狀以便補進解析器",
    "auth_ip_whitelist": "呼叫端 IP 不在 API key 白名單（住宅浮動 IP 換掉會復發）"
                         "→ 到 OKX 後台把下方錯誤訊息中的 IP 加進白名單，"
                         "消費器每分鐘自動重試、不需重啟",
    "auth": "認證失敗（金鑰／簽章／權限）→ 檢查 ~/.okx/config.toml 與後台權限設定",
    "health_state_unreadable": "告警層自己的健康檔（含備份）壞掉或讀不到 → 連續故障"
                               "計數失去歷史，本輪已改為「有故障就直接告警」以免無聲。"
                               "多半是寫到一半被中斷；執行器會自動重寫一份好的，"
                               "下一輪起恢復正常冷卻。連續多輪出現才需人工看磁碟／權限",
    "health_state_write_fail": "告警層自己的健康檔寫不進去 → 連續故障計數與冷卻時間都"
                               "落不了地，告警門檻可能永遠到不了（＝真的斷流也可能"
                               "不出聲）。請檢查 %LOCALAPPDATA%\\TradingBot 的磁碟空間、"
                               "資料夾權限，以及是否被防毒／同步軟體鎖住；"
                               "⛔ 不要直接刪掉 atk_consumer_*health.json——"
                               "刪掉會讓「已持續多久」從零重數，低報斷流時長",
    "intent_unreadable": "intent 檔存在但讀不出來（半截 JSON／編碼壞掉／內容不是物件）"
                         "→ 那一筆訊號每輪被跳過。⛔ 它不會自己好：訊號產生端是**依檔名"
                         "冪等**的（同一個 intent_id 的檔存在就不再寫），壞檔永遠不會被"
                         "重寫，等 expires_at 一到這筆訊號就永久消失（而且算不進「過期"
                         "丟棄」的數字——解析失敗發生在讀 expires_at 之前）。"
                         "⚠️ 管線其餘部分照常運作，只有這一筆被跳過。處置：到 "
                         "%LOCALAPPDATA%\\TradingBot\\intent_outbox 找日誌指名的那個檔"
                         "（檔名＝intent_id），確認內容確實壞掉後刪除即可"
                         "（原始訊號在 trade_journal.db 仍有紀錄）",
    "instrument_spec_unreadable": "算張數要用的合約規格（ctVal／lotSz／minSz）這一輪"
                                  "讀不出來（呼叫沒回 0，或回了 0 但輸出解不開／形狀"
                                  "認不得）→ 那一筆訊號本輪不接，**下輪自動重試**到"
                                  "expires_at 為止。⛔ 這條**不是**「這筆單不該下」——"
                                  "單子本身沒問題，是規格沒讀到。管線其餘部分照常，"
                                  "既有倉的止損仍掛在交易所。處置：若同時段有認證／"
                                  "限流／逾時類別，先修那個；只有這一類時手動跑一次 "
                                  "`okx --profile <profile> market instruments "
                                  "--instType SWAP --instId <instId> --json` 看實際"
                                  "輸出長什麼樣（CLI 剛升級過就回報這個新形狀）",
    "cli_missing": "okx CLI 不存在 → npm install -g @okx_ai/okx-trade-cli",
    "rate_limit": "被限流 → 通常自癒；持續出現才需降頻",
    "timeout": "呼叫逾時 → 檢查網路；持續出現代表對外連線有問題",
    "leverage_fail": "設槓桿失敗 → 若同時段有認證／網路類別，先修那個；若只有這一類，"
                     "多半是該 instId／posSide 已有持倉導致交易所拒絕調整槓桿——"
                     "此時算出的槓桿低於上限的單會 fail-closed 不送出（下輪重試）",
    "query_fail": "查單失敗導致 fail-closed（下游症狀，先看同時段的認證／網路類別）",
    "lev_mismatch": "在場部位的交易所側槓桿與下單時算出的值對不上 → 逐倉保證金與當初"
                    "算的不同，清算距離可能已縮到止損之內（v84 不變式：清算永不先於"
                    "止損）。⛔ 本執行器只記錄與告警，不會自動調槓桿也不會自動平倉——"
                    "請人工到 OKX 看該倉實際槓桿，決定是要調回、減倉還是平掉；"
                    "instId 與兩邊的數值在 atk_live.log 的『交易所側槓桿讀回』行"
                    "（倉平掉後部位檔那筆會消失，log 行才是留得住的證據）",
    "pnl_unaccounted": "有部位已了結、但連查幾輪都拿不到已實現損益 → 該筆損益**永遠**"
                       "不會進日/週熔斷口徑（熔斷會低估已實現虧損）。這是不可逆的，"
                       "修好連線也不會自己補回來。請到 OKX 用健康檔 "
                       "pnl_unaccounted_recent 裡的 instId 與開倉時間查回那筆成交損益，"
                       "自行把熔斷餘額打折看待；若同時段有認證／網路類別，先修那個",
    "other": "未分類錯誤 → 讀 atk_live.log 原文",
}


def alert_text(h: dict, now_s: float) -> str:
    """組告警文字（純函式，繁中可行動）。⛔只講執行器連線狀態，不含任何績效宣稱。"""
    cls = h.get("last_fail_class") or "other"
    rounds = int(h.get("consecutive_fail_rounds", 0))
    since = h.get("first_fail_ts") or now_s
    mins = max(0, int((now_s - float(since)) / 60))
    return (
        f"🚨 真錢執行器連線異常（profile={h.get('profile')}）\n"
        f"已連續 {rounds} 輪 fail-closed，持續約 {mins} 分鐘。\n"
        f"故障類別：{cls}\n"
        f"處置：{_CLASS_HINT.get(cls, _CLASS_HINT['other'])}\n"
        f"錯誤原文：{(h.get('last_fail_sample') or '')[:200]}\n"
        f"⚠️ 期間未下任何單（fail-closed 正確），也未平既有倉——"
        f"這是「沒下單」不是「虧損」。"
    )


def degraded_alert_due(now_s: float,
                       repeat_sec: float = FAIL_ALERT_REPEAT_SEC) -> bool:
    """健康檔寫不進去時，這輪要不要出聲（best-effort 節流）。

    ⛔ 節流痕跡讀不到／寫不進去一律回 True——寧可每輪吵，不可無聲：這條路徑存在的
    唯一理由就是「告警層已經記不住事情了」，此時再用『沒看到痕跡』推論『剛講過』
    就是同一個坑再踩一次。痕跡放在系統暫存目錄＝與壞掉的資料目錄分開，資料目錄被
    鎖住／權限壞掉時仍能正常節流；整台機器磁碟滿了才會退化成每輪吵（那也是對的）。"""
    due = True
    try:
        due = (now_s - float(DEGRADED_MARKER.read_text(encoding="utf-8").strip())
               ) >= repeat_sec
    except Exception:  # noqa: BLE001
        due = True
    if due:
        try:
            DEGRADED_MARKER.write_text(f"{now_s:.0f}", encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    return due


def degraded_alert_text(h: dict, now_s: float, err: str = "") -> str:
    """告警層自我降級通知（純函式）。⛔只講告警可信度，不含任何績效宣稱。"""
    streak = int(h.get("consecutive_fail_rounds", 0))
    return (
        f"🚨 告警層降級：健康檔寫不進去（profile={h.get('profile') or PROFILE}）\n"
        f"連續故障計數無法落地，會停在舊值（目前記到第 {streak} 輪）——"
        f"『連續幾輪／已持續多久』的數字在修好前都不可信。\n"
        f"影響：①門檻可能永遠到不了＝真的斷流也可能一聲不吭；"
        f"②冷卻也記不住＝同一則故障通知可能每輪重送；"
        f"③恢復通知可能不會出現。\n"
        f"⚠️ 交易路徑與風險閘不受影響（下單、熔斷、擋單各自獨立記帳）——"
        f"壞掉的是「你會不會被通知」，不是「會不會亂下單」。\n"
        f"處置：{_CLASS_HINT['health_state_write_fail']}\n"
        f"錯誤原文：{redact_secrets(err)[:200]}"
    )


def recovery_text(h: dict) -> str:
    rec = h.get("recovered_from") or {}
    return (f"✅ 真錢執行器已恢復（profile={h.get('profile')}）"
            f"——先前 {rec.get('fail_rounds')} 輪 {rec.get('class')} 故障已消失，"
            f"本輪呼叫全部成功。")


def _tg_creds() -> tuple[str | None, str | None]:
    """取 TG 憑證：先環境變數，再讀 .env（排程器環境沒有這兩個變數）。
    ⛔只回傳給送信函式使用，永不列印值。"""
    tk, cid = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tk and cid:
        return tk, cid
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k == "TELEGRAM_BOT_TOKEN" and not tk:
                tk = v
            elif k == "TELEGRAM_CHAT_ID" and not cid:
                cid = v
    except Exception:  # noqa: BLE001
        pass
    return (tk or None), (cid or None)


def send_alert(text: str, dry: bool = False) -> tuple[str, str | None]:
    """送告警到 Telegram（stdlib urllib，維持本腳本零依賴）。
    回 (channel, error)：channel ∈ {telegram, dry, none}。
    ⚠️ 告警管道自己失敗也要留痕——否則就變成「告警的無聲失敗」同一個坑。"""
    if dry:
        return "dry", None
    tk, cid = _tg_creds()
    if not tk or not cid:
        return "none", "TG 憑證缺失（環境變數與 .env 都沒有）"
    try:
        import urllib.request
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{tk}/sendMessage",
            data=json.dumps({"chat_id": cid, "text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read().decode("utf-8", "replace")
        return ("telegram", None) if '"ok":true' in body.replace(" ", "") \
            else ("none", redact_secrets(body)[:200])
    except Exception as e:  # noqa: BLE001
        return "none", f"{type(e).__name__}: {str(e)[:160]}"


def finish_round(fails: dict, now_s: float | None = None,
                 dry: bool = False, oks: int = 0, expired=None,
                 pnl_gaps=None, be_gaps=None) -> dict:
    """每輪收尾：更新健康檔、必要時告警。永不對外拋例外——
    告警層絕不可以把交易執行器弄掛（它的職責只是「讓失敗有出口」）。

    oks＝本輪成功呼叫數；沒有它就分不出「空轉輪」與「乾淨輪」（見 update_health）。"""
    now_s = now_s or time.time()
    try:
        base, fails = _load_health(), dict(fails or {})
        if base is None:
            # v164：健康檔未知。⛔ 不可當成「零連續故障」——那等於每輪把告警計數
            # 歸零。方向取 fail-closed：把基準設在門檻前一輪，這輪只要有故障就會
            # 立刻告警一次（並把健康檔重新寫成好的＝自癒，之後恢復正常冷卻）。
            fails.setdefault("health_state_unreadable",
                             f"{HEALTH.name} 與 .bak 都讀不到／內容壞掉")
            base = {"consecutive_fail_rounds": max(0, FAIL_ALERT_AFTER - 1),
                    "health_unknown_at": now_s}
            print(f"⚠️ 健康檔讀取失敗（{HEALTH.name}）——連續故障計數視為未知，"
                  f"本輪若有故障即告警，不從零重數")
        h = update_health(base, fails, now_s, oks, expired=expired,
                          pnl_gaps=pnl_gaps, be_gaps=be_gaps)
        if h.get("recovered_from"):
            ch, err = send_alert(recovery_text(h), dry=dry)
            h["last_alert_channel"], h["last_alert_error"] = ch, err
            print(f"✅ 執行器已恢復（告警管道={ch}）")
        elif should_alert(h, now_s):
            ch, err = send_alert(alert_text(h, now_s), dry=dry)
            h["last_alert_ts"] = now_s
            h["last_alert_class"] = h.get("last_fail_class")
            h["last_alert_channel"], h["last_alert_error"] = ch, err
            print(f"🚨 連續 {h['consecutive_fail_rounds']} 輪 fail-closed"
                  f"（{h.get('last_fail_class')}）——已告警，管道={ch}"
                  + (f"，管道錯誤={err}" if err else ""))
        elif not fails and int(oks or 0) <= 0 \
                and int(h.get("consecutive_fail_rounds", 0)) > 0:
            # v151：斷流中的空轉輪要留痕，否則日誌看起來像「不吵了＝好了」
            print(f"⏸ 本輪零呼叫（空轉，無 intent 可執行／無倉可管）——"
                  f"不當作恢復，維持連續故障第 "
                  f"{h.get('consecutive_fail_rounds')} 輪判定")
        elif fails:
            streak = int(h.get("consecutive_fail_rounds", 0))
            tail = (f"（已告警過，冷卻中：同類故障每 {FAIL_ALERT_REPEAT_SEC / 60:.0f} "
                    f"分鐘才再提醒一次）" if h.get("last_alert_ts")
                    else f"（達 {FAIL_ALERT_AFTER} 輪才告警）")
            print(f"⚠️ 本輪故障 {sorted(fails)}；連續第 {streak} 輪{tail}")
        if not _save_health(h):
            # v166（r57）：寫不進去＝從這輪起計數與冷卻都記不住（見 DEGRADED_MARKER
            # 上方註解）。此時**不能等門檻**——門檻本身就是靠這個檔案在數的，
            # 等下去就是等一場永遠不會來的告警。改成直接出聲（每小時最多一次）。
            h["health_write_failed"] = True
            if degraded_alert_due(now_s):
                ch, err = send_alert(
                    degraded_alert_text(h, now_s, _HEALTH_WRITE_ERR.get("err", "")),
                    dry=dry)
                h["degraded_alert_ts"] = now_s
                h["degraded_alert_channel"] = ch
                print(f"🚨 告警層降級（健康檔寫不進去）——已通知，管道={ch}"
                      + (f"，管道錯誤={err}" if err else ""))
            else:
                print("⚠️ 健康檔仍寫不進去（已在冷卻內，不重複通知）")
        return h
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ 健康狀態更新失敗（不影響交易路徑）：{type(e).__name__}: {e}")
        return {}


# v209（監督員 r104）：張數換算失敗的兩種來源必須分得開。
# 「終局」＝再讀一百次也是同一個答案，因為不成立的是**這筆單本身**。
_SIZING_TERMINAL = ("bad_distance", "below_min_size", "below_min_after_cap",
                    "spec_not_found")


def sizing_retryable(reason: str | None) -> bool:
    """張數換算失敗該不該留給下輪重試（純函式，永不 raise）。

    ⛔ 未知（含未分類的 None）一律回 True。把「這輪讀不出合約規格」折成「這筆單
    本來就不該下」＝永久丟掉一筆真錢訊號，而且連「過期丟棄」的統計都算不到它
    （那個計數只數活到 expires_at 的 intent）。日後新增失敗來源時的安全預設，
    也必須落在重試側——忘了登記只會多重試幾輪，登記錯邊會靜靜吃掉單子。"""
    return reason not in _SIZING_TERMINAL


def fetch_inst_spec(inst_id: str, *, out: dict | None = None) -> dict | None:
    """查合約規格。回 None＝這輪讀不出來／交易所沒這個 instId，原因寫進 out["reason"]。

    v249 從 contracts_for 抽出來：同一份回應裡本來就帶著 tickSz（保本改價要對齊
    最小跳動）與 lever（該合約的**最大**槓桿），舊碼只留 ctVal/lotSz/minSz 就丟掉。
    ⛔ 張數換算的數學一個字都沒動——只是多留兩個欄位；sizing 仍只讀 ctVal/lotSz/minSz。
    ⚠️ tickSz/maxLever 解不出來時留 None（**不猜**），呼叫端各自決定要不要因此止步。
    """
    def _fail(reason: str) -> None:
        if out is not None:
            out["reason"] = reason
        return None

    code, cli_out = _okx(["market", "instruments", "--instType", "SWAP",
                          "--instId", inst_id])
    if code != 0:
        return _fail("spec_cli_failed")   # _okx 那層已記了連線類故障
    try:
        # CLI --json 頂層可能是 list（實測 v1.4.2）或 {"data":[...]}，兩者都容；
        # v209：其餘形狀＝認不得＝未知（舊碼的 raw.get("data") 會回 None，
        # 接著 None[0] 拋 TypeError，與「這筆單不成立」得到同一個答案）
        items = parse_okx_rows(json.loads(cli_out))
        if items is None:
            return _fail("spec_unreadable")
        if not items:
            # {"data": []}＝交易所**確認**沒有這個 instId（v208 的邊界線）：
            # 重試一百次也不會長出來 ⇒ 終局，不可當未知每輪重試
            return _fail("spec_not_found")
        item = items[0]
        return {"ctVal": float(item["ctVal"]), "lotSz": float(item["lotSz"]),
                "minSz": float(item["minSz"]),
                "tickSz": _pos_float(item.get("tickSz")),
                "maxLever": _pos_float(item.get("lever"))}
    except Exception:  # noqa: BLE001
        return _fail("spec_unreadable")


def contracts_for(inst_id: str, entry: float, stop: float, ct_val_cache: dict,
                  *, out: dict | None = None) -> float | None:
    """風險預算→張數。sz=風險USD÷|entry−stop|÷ctVal，向下取整到 lotSz。錯→None 不下單。

    v209（r104）：回 None 時把原因寫進 out["reason"]，呼叫端據此決定「下輪重試」
    還是「永久跳過」（見 sizing_retryable）。⛔ out 沒被填到時呼叫端一律當未知。"""
    def _fail(reason: str) -> None:
        if out is not None:
            out["reason"] = reason
        return None

    spec = ct_val_cache.get(inst_id)
    if spec is None:
        why: dict = {}
        spec = fetch_inst_spec(inst_id, out=why)
        if spec is None:
            return _fail(why.get("reason") or "spec_unreadable")
        ct_val_cache[inst_id] = spec
    risk = min(RISK_USD, RISK_USD_CAP)
    dist = abs(entry - stop)
    if dist <= 0 or spec["ctVal"] <= 0:
        return _fail("bad_distance")
    units = risk / dist                      # 標的單位數
    sz = units / spec["ctVal"]               # 合約張數
    lot = spec["lotSz"]
    sz = int(sz / lot) * lot                 # 向下取整到 lotSz
    if sz < spec["minSz"]:
        return _fail("below_min_size")
    if sz * spec["ctVal"] * entry > NOTIONAL_CAP_USD:   # 名義值夾層
        sz = int(NOTIONAL_CAP_USD / (spec["ctVal"] * entry) / lot) * lot
        if sz < spec["minSz"]:
            return _fail("below_min_after_cap")
    return round(sz, 8)


_LEV_SET: set = set()


def leverage_for_trade(entry: float, stop: float, max_lev: int | None = None) -> int:
    """清算永不先於止損（v84 哲學）：止損距離 × 槓桿 ≤ 70%清算距離。
    lev = min(上限, 70/止損%)，下限 3。槓桿只影響保證金效率，風險由止損距離決定。"""
    max_lev = max_lev or LEVERAGE
    if not entry or not stop or entry <= 0:
        return min(max_lev, 5)
    stop_pct = abs(entry - stop) / entry * 100.0
    if stop_pct <= 0:
        return min(max_lev, 5)
    return max(3, min(int(max_lev), int(70.0 / stop_pct)))


LEV_TOLERANCE = 0.01        # 交易所回字串（"10"）；只要不是真的差一級就算相符


def _pos_float(v) -> float | None:
    """轉正數，不行就 None（純函式，永不 raise）。0 與負值＝無意義的槓桿＝未知。"""
    try:
        f = float(str(v).strip())
    except Exception:  # noqa: BLE001
        return None
    return f if f > 0 else None


def leverage_verdict(intended, exchange_raw) -> str:
    """交易所側槓桿讀回的三態判定：match／mismatch／unknown（純函式，永不 raise）。

    v171（監督員 r69）：ensure_leverage 只憑「設定呼叫的 exit code == 0」就回 True，
    從頭到尾沒有讀回過交易所實際的槓桿。而 v99 那個 bug 的形狀正是**設定呼叫成功
    回應、交易所卻靜默沿用預設 3x**（hedge 模式沒帶 posSide）⇒ 用來判斷「這倉的
    槓桿是對的」的量，一直只以代理值存在。同物種第七次（v164 讀失敗／v166 寫失敗／
    v167 旗標當事實／v169 過期丟棄／v170 損益查不到／r64 demo 停擺原因）。

    ⛔ 未知不可壓成 match：v171 之前開的倉沒有 lev 欄位、交易所也可能不回 lever，
    這兩種情況一律 unknown——把它們算成「相符」等於用沉默偽造證據（v162-v166 紀律）。
    """
    want, got = _pos_float(intended), _pos_float(exchange_raw)
    if want is None or got is None:
        return "unknown"
    return "match" if abs(want - got) <= LEV_TOLERANCE else "mismatch"


def _record_leverage_readback(rec: dict, exchange_raw, now_s: float) -> None:
    """把交易所側讀回的槓桿記進部位紀錄（就地改 rec；呼叫端負責存檔）。

    ⛔ 只記錄與告警：不自動調槓桿、不自動平倉、不擋新單（比照 orphan_position 的
    處置）。理由＝斷流期間已累積 13 版從未在真錢路徑跑過的程式碼（r65 量測），
    此刻再加一道沒跑過的閘，等於讓「第一筆單」同時承擔更多首航風險；而讀回本身
    是純粹加法，寫壞也不會擋掉任何一筆該送的單。

    log 行只在「第一次看到」或「判定/數值變了」時印一次——每分鐘一輪，每輪每倉都印
    會把 atk_live.log 灌成噪音（r66 才剛處理過噪音稀釋訊號的問題）。但一定要印：
    倉一平掉，部位檔那筆就消失，log 行才是留得住的證據（r65 首筆真錢驗收清單第一項
    就是「交易所側讀回的槓桿值」，在此之前那個數字任何地方都產不出來）。
    """
    verdict = leverage_verdict(rec.get("lev"), exchange_raw)
    got = _pos_float(exchange_raw)
    changed = (rec.get("lev_verdict") != verdict
               or rec.get("lev_exchange") != got)
    rec["lev_verdict"] = verdict
    rec["lev_exchange"] = got
    rec["lev_checked_ts"] = now_s
    want = _pos_float(rec.get("lev"))
    shown = f"{got:g}x" if got is not None else "讀不到"
    if verdict == "mismatch":
        msg = (f"{rec.get('inst_id')} {rec.get('pos_side')} 交易所側槓桿為 {got:g}x，"
               f"與下單時算出的 {want:g}x 對不上：逐倉保證金與當初算的不同，清算距離"
               f"可能已縮到止損之內（v84 不變式：清算永不先於止損）。⛔ 未自動調整、"
               f"未自動平倉，請人工到 OKX 確認該倉。")
        if changed:
            print(f"🚨 {msg}")
        _note_fail("lev_mismatch", msg)          # 每輪都記＝連續輪告警機制自然接手
    elif changed:
        basis = (f"下單時算出 {want:g}x" if want is not None
                 else "下單時的值未記錄＝無從比對")
        print(f"🔎 {rec.get('inst_id')} {rec.get('pos_side')} 交易所側槓桿讀回 "
              f"{shown}（{basis}，判定 {verdict}）")


def ensure_leverage(inst_id: str, pos_side: str, dry: bool,
                    lev: int | None = None) -> bool:
    """開單前設槓桿（v99 教訓：OKX 預設 3x，hedge 模式 isolated 必須帶 posSide 逐邊設，
    否則靜默沿用預設）。回 True＝交易所側確定是 lev（或 dry-run／本輪已設過）。

    v155（監督員 r45）：失敗改回 False 並記一筆 leverage_fail。
    ⚠️ 記帳這件事 _okx 本來就會做（傳輸層類別，r45 探針實證），r41 說的「完全不進
    健康帳」是錯的；真正缺的是「從 class_counts 分不出是哪一支呼叫死的」，以及
    回傳值——沒有它，呼叫端無從得知該不該擋單。擋不擋由呼叫端依風險帶決定。"""
    lev = lev or LEVERAGE
    key = (inst_id, pos_side, lev)
    if dry or key in _LEV_SET:
        return True
    code, out = _okx(["swap", "leverage", "--instId", inst_id,
                      "--lever", str(lev), "--mgnMode", "isolated",
                      "--posSide", pos_side])
    if code == 0:
        _LEV_SET.add(key)
        return True
    # v160（監督員 r48）：OKX 401 原文含 API key-id，print 這條路徑原本沒過遮蔽
    # ⇒ 斷流期每輪都把明文 key-id 寫進 atk_live.log（實測已累積 1436 行）。
    # ⚠️ 順序不可顛倒：先遮蔽再截斷。反過來會把 UUID 從中間切斷、正則失配，
    # 反而漏出半截 key-id。三處回顯 OKX 原文的輸出點同此規則（見同名回歸鎖）。
    print(f"⚠️ 設槓桿失敗 {inst_id}/{pos_side}（應設 {lev}x）："
          f"{redact_secrets(out)[:120]}")
    _note_fail("leverage_fail", f"{inst_id}/{pos_side} 應設 {lev}x 失敗：{out}")
    return False


# ── 倉位管理（v139：對帳／逾時平倉／日虧熔斷） ──────────────────────────
def _load_positions() -> dict | None:
    """讀本地部位帳。回 None＝讀不到／讀壞了（**未知**）；⛔ 不可再當成「確認空帳」。

    v163（監督員 r54）：舊版把任何例外都壓成 {"open": {}, "day_pnl": {}}，與 r53 修的
    孤兒閘同一物種（把「查詢失敗」寫成「確認沒有」），但下游更致命——同一個空值同時
    餵三個地方：①breaker_tripped 讀到空 day_pnl ⇒ 日/週熔斷整個消失，該停手的日子照
    接新單；②dup_open_same_side 讀到空 open ⇒ 同幣同向擋單閘瞎掉、曝險無聲翻倍；
    ③下單成功後 main() 會拿這本空帳寫回檔案 ⇒ 既有倉與 14 天熔斷損益被**永久抹掉**
    （倉就此脫帳成孤兒）。這三條在同一輪一起發生，而且是無聲的。

    「檔案還不存在」＝首跑，是真的空帳（合法）；JSON 壞掉／IO 錯／型別不對一律未知。
    半截 JSON 不是理論風險：舊 _save_positions 是「先截斷再寫」的非原子寫，行程在
    中途被殺（排程逾時／休眠／當機）就會留下壞檔（本機已有 utf16-corrupt 前例）。"""
    try:
        raw = json.loads(POS_STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"open": {}, "day_pnl": {}}          # 首跑：還沒有任何倉＝合法空帳
    except Exception as e:  # noqa: BLE001
        _note_fail("pos_state_unreadable",
                   f"{POS_STATE.name} 讀取失敗：{type(e).__name__}: {e}")
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("open", {}), dict) \
            or not isinstance(raw.get("day_pnl", {}), dict):
        _note_fail("pos_state_unreadable", f"{POS_STATE.name} 內容結構不對")
        return None
    raw.setdefault("open", {})
    raw.setdefault("day_pnl", {})
    return raw


def _save_positions(ps: dict) -> bool:
    """原子寫回本地部位帳。回 False＝沒寫成（呼叫端必須出聲，見 main）。

    v163（監督員 r54）：①改「暫存檔＋os.replace」——非原子寫留下的半截 JSON 會讓
    _load_positions 從此永遠讀壞（見上）。②失敗不再無聲吞掉：寫不進去＝剛送出的真錢
    單沒進帳本，那筆倉下一輪就是孤兒，這件事必須有出口。"""
    tmp = POS_STATE.with_name(POS_STATE.name + ".tmp")
    try:
        tmp.write_text(json.dumps(ps, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, POS_STATE)
        return True
    except Exception as e:  # noqa: BLE001
        _note_fail("pos_state_write_fail",
                   f"{POS_STATE.name} 寫入失敗：{type(e).__name__}: {e}")
        return False


def _day_key(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts or time.time()))


def dup_open_same_side(open_map: dict, inst_id: str, pos_side: str) -> bool:
    """同幣同向是否已在場（純函式）。open_map 以 intent_id 為鍵，
    同 (inst_id, pos_side) 本來就能並存 → 這是下單前唯一的併倉防線。"""
    return any(r.get("inst_id") == inst_id and r.get("pos_side") == pos_side
               for r in (open_map or {}).values())


def timed_out(placed_at_s: float, now_s: float,
              limit_h: float = TIMEOUT_HOURS) -> bool:
    """持倉逾時判定（純函式）。"""
    return (now_s - placed_at_s) > limit_h * 3600


ACKED_POS = Path(os.path.expandvars(
    r"%LOCALAPPDATA%\TradingBot\atk_acknowledged_positions.json"))


def parse_acked(data) -> tuple[set, str | None]:
    """把已解析的 ack 內容拆成 (可用的鍵集合, 問題描述或 None)（純函式）。

    v207（監督員 r102）：問題描述就是本次補上的東西——原本「檔在但一筆都讀不出來」
    與「檔根本不存在」回一樣的空集合,兩者的處置天差地遠。"""
    if not isinstance(data, list):
        return set(), f"頂層不是清單（是 {type(data).__name__}）"
    keys, bad = set(), 0
    for e in data:
        if isinstance(e, dict) and e.get("inst_id"):
            keys.add((e.get("inst_id"), e.get("pos_side")))
        else:
            bad += 1
    if bad:
        return keys, (f"{len(data)} 筆裡有 {bad} 筆沒有可用的 inst_id"
                      "（鍵名須為 inst_id / pos_side）")
    return keys, None


def load_acked_keys() -> set:
    """v189：使用者已確認的手動倉 {(inst_id, pos_side)}。
    用途：使用者親口確認「這筆是我自己開的」後,孤兒偵測不再對它記故障/告警,
    但**同幣同向新單照樣擋**（防自動單疊在手動倉上）、依然不自動管理它。
    檔案只在使用者於聊天室確認後由 CEO 寫入;讀壞/缺檔=空集合（安全預設:全部當孤兒）。

    v207（監督員 r102,同物種第 27 次）：安全預設不變,補的是**出聲**。舊碼把三件事
    壓成同一個空集合——①檔案不存在（正常:還沒確認過）②檔在但解不開／頂層不是清單
    ③檔在、是清單、但鍵名不對（連例外都不丟,最無聲的一種）。②③代表使用者已經在
    聊天室確認過、CEO 也寫了檔,卻被無聲丟掉:孤兒告警會永遠繼續響,而畫面上沒有任何
    線索指向那個檔。落點要緊是因為這個檔正是**目前唯一**那條真錢阻塞（WLFI 孤兒倉）
    的解除機制,靜默失敗等於使用者做了動作卻看不到任何反應。
    ⛔ 讀壞仍回空集合（全部當孤兒）＝比較嚴格的那一邊,不因為出聲就放寬。"""
    try:
        raw = ACKED_POS.read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()                      # 還沒確認過任何倉＝常態,不是故障
    except Exception as e:  # noqa: BLE001
        _note_fail("acked_state_unreadable",
                   f"{ACKED_POS.name} 讀取失敗：{type(e).__name__}: {e}")
        return set()
    try:
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        _note_fail("acked_state_unreadable",
                   f"{ACKED_POS.name} 解不開：{type(e).__name__}: {e}")
        return set()
    keys, problem = parse_acked(data)
    if problem:
        print(f"⚠️ 已確認手動倉檔 {ACKED_POS.name} {problem}"
              "——這些倉仍一律當孤兒處理（安全預設）")
        _note_fail("acked_state_unreadable", f"{ACKED_POS.name} {problem}")
    return keys


def partition_orphans(orphans: list, acked: set) -> tuple[list, list]:
    """(未確認孤兒, 已確認手動倉)（純函式）。"""
    un, ack = [], []
    for o in orphans:
        (ack if (o[0], o[1]) in acked else un).append(o)
    return un, ack


def orphan_positions(exchange_positions, open_map: dict) -> list:
    """反向對帳（純函式）：列出「交易所上真的有、但本地帳沒有」的部位。

    v159（r47）已接入 manage_positions()（規格見
    docs/2026-07-31-斷流期倉位保護-規格.md §4.2）：偵測到→記健康帳 orphan_position
    ＋擋同幣同向新單；⛔ 不自動平倉、⛔ 不自動收編進本地帳。
    接入前 manage_positions() 只從本地帳 open_map 出發逐倉去問交易所，因此「交易所有、
    本地帳沒有」的部位在結構上永遠看不見——不會逾時平倉、了結損益也永遠不會進
    day_pnl（＝日/週熔斷少算一筆真實虧損）。

    這種部位怎麼生出來的：place() 分批進場是多腿，任何一腿查單失敗就整筆回 False，
    而 main() 只在 True 時才寫本地帳；若此時前面幾腿已經成交（交易所已有倉、附掛
    SL/TP 都在），本地帳是空的。斷流（如 401 白名單）會讓後續每一輪都查單失敗，
    直到 intent 過了 expires_at 被丟棄 ⇒ 那個部位就此脫離帳本。

    回 [(inst_id, pos_side, contracts), ...]（依鍵排序）。pos=0 的不算（已平）。
    畸形資料一律略過而不丟例外——本函式將來會跑在交易路徑上。
    """
    known = {(r.get("inst_id"), r.get("pos_side"))
             for r in (open_map or {}).values()}
    out = []
    for p in (exchange_positions or []):
        if not isinstance(p, dict):
            continue
        try:
            sz = float(p.get("pos") or 0)
        except (TypeError, ValueError):
            continue
        key = (p.get("instId"), p.get("posSide"))
        if sz == 0 or not key[0] or key in known:
            continue
        out.append((key[0], key[1], sz))
    return sorted(out)


def parse_okx_rows(raw):
    """把 okx CLI 的 JSON 回應解析成「一列一筆」的清單（純函式）。三態：

        list  ＝**確認**讀到了這些筆（空清單＝確認沒有任何一筆）
        None  ＝認不得這個形狀＝**未知**（呼叫端一律走 fail-closed 分支）

    v208（監督員 r103）：舊碼兩處都寫成 `raw if isinstance(raw, list) else
    raw.get("data", [])`——`.get(..., [])` 讓「認得的空清單」與「根本不認得的
    形狀」得到同一個答案。CLI 換版、換包裝鍵，或回一個 {"code":..,"msg":..}
    的錯誤信封，就會被讀成「交易所上確認沒有」。⛔ 只認兩種形狀，其餘一律未知；
    ⛔ 也**不可**因為保險就把 {"data": []} 一起打成未知——那是確認沒有，
    打成未知會讓正常空倉輪每輪擋單（見 test_okx_rows_unreadable_v208.py）。"""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        data = raw.get("data")
        if isinstance(data, list):
            return data
    return None


WEEKLY_STOP_USD = 750.0          # 週虧熔斷（≈7.5R）：近 7 日合計虧損達此值→停接新單


def breaker_tripped(day_pnl: dict, now_s: float | None = None,
                    stop_usd: float = DAILY_STOP_USD,
                    week_stop_usd: float = WEEKLY_STOP_USD) -> bool:
    """日/週雙層熔斷（純函式）：今日(UTC)已實現虧損 ≤ −stop_usd，
    或近 7 日(UTC)合計 ≤ −week_stop_usd → True。"""
    now_s = now_s or time.time()
    if float(day_pnl.get(_day_key(now_s), 0.0)) <= -abs(stop_usd):
        return True
    week_keys = {_day_key(now_s - d * 86400) for d in range(7)}
    week_total = sum(float(v) for k, v in day_pnl.items() if k in week_keys)
    return week_total <= -abs(week_stop_usd)


def _realized_pnl_since(inst_id: str,
                        since_s: float) -> tuple[float, float | None] | None:
    """粗算該 instId 自 since 起的已實現損益（fillPnl+fee 合計）。查失敗回 None。
    v154（修B）：一併回傳最後一筆 fill 的秒級 ts（無 fill 則 None），
    給呼叫端把損益記在「成交當下」那個 UTC 日、而非「對帳當下」那個日。"""
    code, out = _okx(["swap", "fills", "--instId", inst_id])
    if code != 0 or not out.strip().startswith(("[", "{")):
        return None
    try:
        fills = parse_okx_rows(json.loads(out))
        if fills is None:
            # v208（r103）：認不得的形狀曾在這裡折成空清單 → 回 (0.0, None)＝一個
            #   **看起來成功**的答案，把那筆真錢的已實現損益以 0.00 記進 day_pnl
            #   （日/週熔斷唯一的輸入），而且繞過 v170 才補上的重試——重試只在
            #   回 None 時啟動。回 None 讓它走既有的重試／漏記記帳路徑。
            return None
        total = 0.0
        last_ts: float | None = None
        for f in fills:
            ts = float(f.get("ts") or 0) / 1000.0
            if ts >= since_s:
                total += float(f.get("fillPnl") or 0) + float(f.get("fee") or 0)
                if last_ts is None or ts > last_ts:
                    last_ts = ts
        return total, last_ts
    except Exception:  # noqa: BLE001
        return None


# ── TP1 後保本移損（v249；使用者 2026-08-03 指示） ──────────────────────
def _tick_round(px: float, tick: float | None, *, up: bool) -> float:
    """對齊最小跳動。tick 未知→原樣回（⛔ 不猜小數位數，讓 OKX 自己回 51006）。"""
    if not tick or tick <= 0:
        return px
    n = px / tick
    k = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(k * tick, 10)


def breakeven_stop_px(avg_px, orig_sl, pos_side: str, tick: float | None = None,
                      buffer_r: float = BE_BUFFER_R) -> float | None:
    """TP1 後的保本止損價（純函式）。回 None＝算不出來（⛔ 不得退回猜一個值）。

    ① 基準是**實際成交均價**，不是 intent 裡的計畫進場價。真錢上這兩個數差很多：
       2026-08-03 MU 空單計畫進場 822.88、實際成交 815.83——拿計畫價當「保本」，
       等於把止損擺在每單位虧 7.06 的位置，名字叫保本、行為是認賠出場。
    ② R 距離取「均價 ↔ 原始止損」＝這筆單**實際**承擔的風險（同理不用計畫價算）。
    ③ 緩衝往獲利側推 buffer_r 個 R：⛔ 不用 0。剛好停在均價，扣完進出雙邊手續費
       仍是淨虧，而且貼著均價＝雜訊磁鐵（自家 A/B 的 MIN_BE_BUFFER_R 同此理由）。
    ④ 取整往「離現價較遠」那側（空單進位、多單捨去）：寧可少鎖一點利，也不要因為
       取整反而更貼身被掃掉。
    ⑤ 結果必定嚴格優於原始止損；不是的話回 None（⛔ 保本永遠不該讓風險變大）。
    """
    a, s = _pos_float(avg_px), _pos_float(orig_sl)
    if a is None or s is None:
        return None
    bull = pos_side == "long"
    dist = abs(a - s)
    if dist <= 0:
        return None
    if (bull and s >= a) or (not bull and s <= a):
        return None                       # 原始止損擺在獲利側＝形狀不對，不動它
    buf = dist * max(0.0, float(buffer_r))
    raw = a + buf if bull else a - buf
    px = _tick_round(raw, tick, up=not bull)
    if bull and not (s < px < a + dist):
        return None
    if not bull and not (a - dist < px < s):
        return None
    return px


def stop_is_at_least_breakeven(cur_sl, be_px, pos_side: str) -> bool | None:
    """現有止損是否已達（或優於）保本價。回 None＝讀不出來（⛔ 不得當成「已經到了」）。"""
    c, b = _pos_float(cur_sl), _pos_float(be_px)
    if c is None or b is None:
        return None
    return c >= b if pos_side == "long" else c <= b


def pending_stop_legs(rows, pos_side: str) -> list | None:
    """從 algo orders 回應挑出「這一邊、還掛著、帶止損」的腿。

    回 None＝形狀認不得（**未知**）；回 []＝查得到且**確認**沒有掛單。
    ⛔ 這兩者不可再合用 []——「查不到掛單」和「這倉真的沒有止損」在風險上是相反的兩件事
    （後者是裸奔、要立刻喊；前者只是下輪重試）。v162/v208 同一紀律。"""
    if rows is None:
        return None
    out = []
    for r in rows:
        if not isinstance(r, dict):
            return None
        if r.get("posSide") not in (pos_side, None, ""):
            continue
        if r.get("state") not in ("live", "effective", "pause"):
            continue
        if not r.get("algoId") or not r.get("slTriggerPx"):
            continue
        out.append(r)
    return out


def move_stops_to_breakeven(rec: dict, avg_px, dry: bool) -> tuple[str, dict]:
    """把該倉**剩餘腿**的止損搬到保本價。回 (state, info)；⛔ 不就地改 rec。

    state：done＝這輪確認每一腿都在保本價（含本來就已經在）；unknown＝查不到／看不懂
    （下輪重試）；no_pending＝查得到且確認一張掛單都沒有（＝這倉此刻沒有交易所端止損，
    是比搬不動更嚴重的事，要出聲）；amend_failed＝改價被拒。

    為什麼要逐腿改而不是重下一張：三腿各自帶著自己的附掛 OCO（見 place() 的註解），
    先撤再下會出現「舊的撤了、新的還沒成」的裸奔窗，而 amend 是交易所端原子操作。
    """
    iid, side = rec.get("inst_id"), rec.get("pos_side")
    code, out = _okx(["swap", "algo", "orders", "--instId", iid,
                      "--ordType", "oco"])
    if code != 0 or not out.strip().startswith(("[", "{")):
        return "unknown", {"reason": "algo_query_failed"}
    try:
        rows = parse_okx_rows(json.loads(out))
    except Exception:  # noqa: BLE001
        return "unknown", {"reason": "algo_unreadable"}
    legs = pending_stop_legs(rows, side)
    if legs is None:
        return "unknown", {"reason": "algo_unreadable"}
    if not legs:
        return "no_pending", {"reason": "no_pending_algo"}
    # 原始止損：優先用下單時記下的（v249 起每筆都留），舊紀錄沒有時退回讀掛單上的值。
    # ⚠️ 退回路徑只在「還沒搬過」時是對的——搬過之後掛單上的值就是保本價，再拿它當
    #   基準算一次會得到更貼身的止損（棘輪）。所以 rec["be"]["state"]=="done" 一律
    #   不再進來（呼叫端負責），且下面每腿都會先檢查「是否已達保本」。
    orig_sl = rec.get("stop")
    if orig_sl is None:
        orig_sl = min((_pos_float(r.get("slTriggerPx")) or 0) for r in legs) \
            if side == "long" else \
            max((_pos_float(r.get("slTriggerPx")) or 0) for r in legs)
    be = breakeven_stop_px(avg_px, orig_sl, side, rec.get("tickSz"))
    if be is None:
        return "unknown", {"reason": "be_px_uncomputable",
                           "avg_px": _pos_float(avg_px), "orig_sl": _pos_float(orig_sl)}
    moved, already, failed = 0, 0, []
    for r in legs:
        done = stop_is_at_least_breakeven(r.get("slTriggerPx"), be, side)
        if done is True:
            already += 1
            continue
        if done is None:
            failed.append({"algoId": r.get("algoId"), "err": "sl_unreadable"})
            continue
        if dry:
            print(f"DRY-RUN: okx algo amend {iid} algoId={r.get('algoId')} "
                  f"--newSlTriggerPx {be:g}")
            moved += 1
            continue
        c2, o2 = _okx(["swap", "algo", "amend", "--instId", iid,
                       "--algoId", str(r.get("algoId")),
                       "--newSlTriggerPx", f"{be:g}"])
        if c2 == 0 and ('"sCode": "0"' in o2 or '"sCode":"0"' in o2
                        or '"algoId"' in o2):
            moved += 1
        else:
            failed.append({"algoId": r.get("algoId"),
                           "err": redact_secrets(o2)[:120]})
    info = {"px": be, "moved": moved, "already": already,
            "legs": len(legs), "failed": failed}
    if failed:
        info["reason"] = "amend_rejected"
        return "amend_failed", info
    return "done", info


def maybe_breakeven(rec: dict, live_sz: float, avg_px, dry: bool,
                    iid_key: str) -> None:
    """TP1（任一 TP 腿）成交後把剩餘腿的止損搬到保本。就地改 rec；缺口進 _ROUND_BE_GAPS。

    觸發判定＝「交易所上剩下的張數 < 下單時送出去的張數」。三腿 40/30/30 的階梯下，
    第一次成立必然是 TP1 成交那一刻（止損若先觸發，整倉會被平掉、走的是另一條分支）。
    ⛔ 不用「現價是否越過 tp1」判斷：那是拿代理值當事實，掛單成沒成交只有交易所知道。
    """
    if not BE_ENABLED:
        return
    st = dict(rec.get("be") or {})
    if st.get("state") == "done":
        return                                   # 已保本，⛔ 不再算第二次（防棘輪）
    placed = _pos_float(rec.get("contracts"))
    if placed is None:
        # 下單張數沒記錄＝連「有沒有吃到 TP1」都判不出來。⛔ 不得默認「沒吃到」——
        # 那會讓保本永遠不觸發，而且完全無聲。出聲並記缺口，由人工接手。
        st.update({"state": "blocked", "reason": "placed_size_unknown",
                   "ts": time.time()})
        rec["be"] = st
        _ROUND_BE_GAPS.append({"intent_id": iid_key, "inst_id": rec.get("inst_id"),
                               "pos_side": rec.get("pos_side"),
                               "symbol": rec.get("symbol"),
                               "reason": "placed_size_unknown", "tries": 0})
        return
    if live_sz + 1e-9 >= placed:
        return                                   # 一腿都還沒成交＝還輪不到保本
    state, info = move_stops_to_breakeven(rec, avg_px, dry)
    tries = int(st.get("tries", 0) or 0) + 1
    st.update({"state": state, "tries": tries, "ts": time.time(),
               "px": info.get("px"), "reason": info.get("reason")})
    rec["be"] = {k: v for k, v in st.items() if v is not None}
    if state == "done":
        if info.get("moved"):
            print(f"🛡 {rec.get('inst_id')} {rec.get('pos_side')} 已吃到 TP1 "
                  f"（{placed:g}→{live_sz:g} 張），剩餘 {info['moved']} 腿的止損"
                  f"已搬到保本 {info['px']:g}"
                  + (f"（另有 {info['already']} 腿本來就到位）" if info.get("already") else ""))
        return
    msg_head = {"unknown": "查不到／看不懂掛單",
                "no_pending": "⚠️ 交易所上一張掛單都沒有（此倉此刻沒有交易所端止損）",
                "amend_failed": "改價被拒"}.get(state, state)
    print(f"🚨 {rec.get('inst_id')} {rec.get('pos_side')} 已吃到 TP1，但止損沒能搬到"
          f"保本：{msg_head}（{info.get('reason')}，第 {tries} 輪）——剩餘腿仍掛在"
          f"**原始止損**上，下輪重試")
    _ROUND_BE_GAPS.append({"intent_id": iid_key, "inst_id": rec.get("inst_id"),
                           "pos_side": rec.get("pos_side"),
                           "symbol": rec.get("symbol"),
                           "reason": info.get("reason") or state, "tries": tries,
                           "want_px": info.get("px")})


def manage_positions(dry: bool) -> list | None:
    """每輪管理：①反向對帳（交易所有、本地帳沒有＝孤兒部位）②對帳（OKX 上已消失＝
    TP/SL 已了結→記日損益）③逾時強平。任何查詢失敗→本輪什麼都不做（下輪重試）。

    回 [(inst_id, pos_side), ...]＝本輪偵測到的孤兒，給主迴圈擋同幣同向新單。
    v162（監督員 r53）三態回傳：list＝掃描有跑成（空清單＝**確認**沒有孤兒）；
    None＝這輪根本查不到交易所部位（未知）。⛔ 兩者不可再合用 []——r47 只把
    「查不到 ≠ 沒有孤兒」做在函式內（不記健康帳），下游讀到 [] 仍當成確認乾淨，
    擋單閘因此在查詢失敗的輪整個消失。呼叫端必須把 None 當「不接新單」。
    """
    ps = _load_positions()
    if ps is None:
        # v163（r54）：本地帳讀不到＝連「哪些倉是我的」都不知道 ⇒ 對帳／逾時平倉一律
        #   不做（誤判會平掉不該平的、或把別人的損益記進熔斷），並回 None 讓主迴圈走
        #   既有的「未知就不接新單」分支。既有倉的交易所端止損仍在，不會裸奔。
        print("⛔ 本地部位帳讀取失敗（未知）——本輪不對帳、不平倉、不接新單（下輪重試）")
        return None
    # v159（監督員 r47）：⛔ 不可再因「本地帳空」就提早返回——「只從本地帳出發」正是
    #   孤兒部位的結構盲點本身（規格 docs/2026-07-31-斷流期倉位保護-規格.md §4.2）。
    code, out = _okx(["account", "positions"])
    if code != 0 or not out.strip().startswith(("[", "{")):
        return None                              # 查不到就不動，別誤判平倉（未知）
    try:
        plist = parse_okx_rows(json.loads(out))
    except Exception:  # noqa: BLE001
        return None                              # 解析不了＝等同查不到（fail-closed）
    if plist is None:
        # v208（監督員 r103）：解得開、但認不得形狀。舊碼在這裡折成空清單，後果兩層
        #   且都落在真錢上：①孤兒偵測回 []＝「**確認**沒有孤兒」⇒ 目前唯一那條真錢
        #   阻塞被無聲解除、擋同幣同向新單的閘一起消失（v162/r53 堵的正是這個洞，
        #   只是那次的入口是「查詢失敗」、這次是「查詢成功但看不懂」）；②本地帳上每
        #   一筆在場倉都會被判成「交易所上已消失＝已了結」⇒ 整批走結算路徑被移出部位
        #   帳。一次看不懂的回應換來一次全倉假平倉。⇒ 回 None（未知）並記帳出聲。
        msg = ("交易所部位查詢回了一個認不得的形狀（解得開、但讀不出任何一筆）——"
               "本輪不對帳、不平倉、不接新單（下輪重試）。"
               f"樣本：{redact_secrets(out.strip())[:200]}")
        print(f"⛔ {msg}")
        _note_fail("exchange_rows_unreadable", msg)
        return None
    # 反向對帳：⛔ 不自動平倉、⛔ 不自動收編進本地帳（理由見規格 §4.2）——
    # 只記健康帳（讓既有的連續輪告警機制自然接手）＋擋同幣同向新單。
    orphans = orphan_positions(plist, ps.get("open") or {})
    # v189：使用者已確認的手動倉不記故障不告警（但同幣同向照擋、依然不代管）
    _unacked, _acked = partition_orphans(orphans, load_acked_keys())
    orphan_keys = [(i, s) for i, s, _ in orphans]   # 擋單鍵含已確認者（防疊倉）
    for _iid, _side, _sz in _acked:
        print(f"ℹ️ 已確認手動倉 {_iid} {_side} {_sz:g} 張（使用者自管,擋同幣同向自動單）")
    for _iid, _side, _sz in _unacked:
        msg = (f"孤兒部位 {_iid} {_side} {_sz:g} 張：交易所上有、本地帳沒有。"
               "此倉不在自動管理之下（不會逾時平倉、了結損益不進日/週熔斷口徑），"
               "但它的止損仍掛在交易所。請人工確認後決定是否平倉；在它消失前，"
               "同幣同向的新單一律擋下。")
        print(f"🚨 {msg}")
        _note_fail("orphan_position", msg)
    if not ps.get("open"):
        return orphan_keys
    live = {(p.get("instId"), p.get("posSide")): float(p.get("pos") or 0)
            for p in plist}
    # v171（r69）：同一份回應裡本來就有交易所側的槓桿，之前整個丟掉——撿起來當讀回用，
    #   零額外呼叫、零新故障面。
    live_lever = {(p.get("instId"), p.get("posSide")): p.get("lever")
                  for p in plist}
    # v249：同一份回應也帶著**實際成交均價**——保本價唯一正確的基準（計畫進場價
    #   在真錢上會差好幾個 tick，見 breakeven_stop_px 註解①）。一樣是零額外呼叫。
    live_avg = {(p.get("instId"), p.get("posSide")): p.get("avgPx")
                for p in plist}
    now_s = time.time()
    for iid, rec in list(ps["open"].items()):
        key = (rec["inst_id"], rec["pos_side"])
        if live.get(key, 0.0) != 0.0:
            # 在場才讀回（已了結的倉交易所不會再回它的槓桿）
            _record_leverage_readback(rec, live_lever.get(key), now_s)
            # v249：TP1 成交後把剩餘腿的止損搬到保本（使用者 2026-08-03 指示）
            maybe_breakeven(rec, live.get(key, 0.0), live_avg.get(key), dry, iid)
        if live.get(key, 0.0) == 0.0:
            # 已了結（TP/SL/手動）→ 記日損益後移出
            res = _realized_pnl_since(rec["inst_id"], rec["placed_at"])
            if res is not None:
                pnl, last_ts = res
                # v154（監督員 r44・修B）：熔斷記帳吃「成交當下」的 UTC 日，不吃
                #   「對帳當下」。對帳可能晚很久才發生（本次 401 斷流 26.5h），
                #   用對帳日記帳會把前一日的損益算到今天頭上→日虧熔斷口徑失真。
                #   查不到 fill ts 就退回現行行為（寧可日期近似，也不要漏記）。
                dk = _day_key(last_ts) if last_ts else _day_key()
                # 下方只保留 14 天；回填比保留窗更舊的日期會在同一次呼叫裡被
                # 立刻剪掉、損益無聲蒸發 → 先夾到窗邊界，讓它仍計入熔斷口徑。
                dk = max(dk, _day_key(time.time() - 14 * 86400))
                ps["day_pnl"][dk] = float(ps["day_pnl"].get(dk, 0.0)) + pnl
                print(f"🏁 {rec['inst_id']} {rec['pos_side']} 已了結，"
                      f"已實現≈{pnl:+.2f} USDT"
                      f"（記入 {dk} UTC，該日累計 {ps['day_pnl'][dk]:+.2f}）")
                ps["open"].pop(iid, None)
            else:
                # v170（監督員 r68）：⛔ 這裡舊碼直接 pop——那筆已實現損益就此永遠不會
                #   進 day_pnl，而 day_pnl 是日 60U／週 150U 熔斷唯一的輸入 ⇒ 熔斷低估
                #   已實現虧損，該停手的日子可能照常接新單。且不可逆：紀錄一移出，
                #   沒有任何東西知道要回去查它。改成①有限重試（重試計數寫回檔案才活得過
                #   下一輪——每輪是獨立行程）②真的放棄時留下數字與故障類別，不只 print。
                tries = int(rec.get("pnl_retry", 0) or 0) + 1
                rec["pnl_retry"] = tries
                if tries < PNL_RETRY_MAX:
                    print(f"⏳ {rec['inst_id']} {rec['pos_side']} 已了結但損益查詢失敗"
                          f"（第 {tries}/{PNL_RETRY_MAX} 輪）——保留紀錄，下輪重查")
                    continue                      # ⛔ 不 pop：可重試的事不做成不可逆
                msg = (f"{rec['inst_id']} {rec['pos_side']} 已了結，但連 {tries} 輪都查"
                       f"不到已實現損益 ⇒ 該筆損益**永遠**不進日/週熔斷口徑（熔斷會低估"
                       f"已實現虧損）。開倉時間 "
                       f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(rec['placed_at']))}"
                       f" UTC，請人工到 OKX 查回金額。")
                print(f"🚨 {msg}")
                _ROUND_PNL_GAPS.append({"intent_id": iid,
                                        "inst_id": rec["inst_id"],
                                        "pos_side": rec["pos_side"],
                                        "symbol": rec.get("symbol"),
                                        "placed_at": rec.get("placed_at"),
                                        "retries": tries})
                _note_fail("pnl_unaccounted", msg)
                ps["open"].pop(iid, None)
        elif timed_out(rec["placed_at"], now_s):
            if dry:
                print(f"DRY-RUN: 逾時平倉 {rec['inst_id']} {rec['pos_side']}")
                continue
            code, out = _okx(["swap", "close", "--instId", rec["inst_id"],
                              "--mgnMode", "isolated",
                              "--posSide", rec["pos_side"], "--autoCxl"])
            print(("⏱ 逾時平倉已送出 " if code == 0 else "❌ 逾時平倉失敗 ")
                  + f"{rec['inst_id']}（持有 {(now_s - rec['placed_at']) / 3600:.1f}h）"
                  + ("" if code == 0 else f"：{redact_secrets(out)[:120]}"))
            # 不立即移出：下輪對帳確認消失後才記損益
    # 舊日損益只留 14 天
    ps["day_pnl"] = {k: v for k, v in ps["day_pnl"].items()
                     if k >= _day_key(time.time() - 14 * 86400)}
    _save_positions(ps)
    return orphan_keys


TP_WEIGHTS3 = (0.40, 0.30, 0.30)   # 對齊 demo 帳 TP1/2/3 分腿口徑（尾腿吃餘數）


def split_tp_levels(sz: float, lot: float, min_sz: float,
                    tps: list[float]) -> list[tuple[float, float]]:
    """分批止盈分腿（純函式）。tps=有價位的 TP 清單（1~3 個）。
    回 [(觸發價, 腿張數)...]：權重 3腿=40/30/30、2腿=50/50、1腿=100%；
    各腿 floor 到 lot、尾腿吃餘數；不足 minSz 的腿併入尾腿。"""
    tps = [p for p in tps if p]
    if not tps or sz <= 0:
        return []
    weights = {1: (1.0,), 2: (0.5, 0.5), 3: TP_WEIGHTS3}[min(len(tps), 3)]
    legs: list[tuple[float, float]] = []
    used = 0.0
    for i, (px, w) in enumerate(zip(tps, weights)):
        if i == len(weights) - 1:
            leg = round(sz - used, 8)                    # 尾腿吃餘數
        else:
            leg = int(sz * w / lot) * lot
        if leg < min_sz:
            continue                                      # 太小的腿讓尾腿吸收
        legs.append((px, round(leg, 8)))
        used = round(used + leg, 8)
    if not legs:                                          # 全部太小→單腿 100%
        return [(tps[0], sz)]
    # 若中間腿被跳過導致餘數沒分完，把差額補到最後一腿
    diff = round(sz - sum(l for _, l in legs), 8)
    if diff > 0:
        px, l = legs[-1]
        legs[-1] = (px, round(l + diff, 8))
    return legs


def _leg_args(intent: dict, leg_sz: float, tp_px: float, cl_ord_id: str) -> list[str]:
    """單腿下單參數：市價進場＋附掛該腿 TP＋整段 SL（OCO，已驗證原語）。"""
    return ["swap", "place",
            "--instId", intent["inst_id"],
            "--side", intent["side"],
            "--posSide", intent["pos_side"],
            "--ordType", "market",
            "--sz", str(leg_sz),
            "--tdMode", "isolated",
            "--clOrdId", cl_ord_id,
            "--tpTriggerPx", str(tp_px), "--tpOrdPx=-1",
            "--slTriggerPx", str(intent["stop"]), "--slOrdPx=-1"]


def _leg_ok(code: int, out: str) -> bool:
    """腿級下單成功判定：sCode=0。"""
    return code == 0 and ('"sCode": "0"' in out or '"sCode":"0"' in out)


def _order_exists(inst_id: str, cl_ord_id: str) -> bool | None:
    """該 clOrdId 的單是否已存在（含已成交）。回 True/False/None(查詢失敗)。

    ⚠️ 真冪等的關鍵（2026-07-29 demo 活測教訓）：OKX 只對「掛單中」的 clOrdId
    擋重複——市價單成交後同 clOrdId 可再下＝重試會加倍持倉。所以每腿下單前
    必須先查單，查到任何狀態（含 filled）都算已處理；查詢失敗→不下單（fail-closed）。"""
    code, out = _okx(["swap", "get", "--instId", inst_id, "--clOrdId", cl_ord_id])
    if code == 0 and '"ordId"' in out:
        return True
    if "51603" in out or "doesn't exist" in out or "does not exist" in out:
        return False
    return None


def place(intent: dict, sz: float, dry: bool,
          spec: dict | None = None) -> bool:
    """分批止盈下單（v140）：拆成多筆市價進場單，各帶「自己那腿的 TP＋同一止損價」
    的附掛 OCO——只用已驗證的單附掛原語（CLI --tpLevel 多腿會丟失 SL，activedemo
    實測 50015/無SL，不可用）。OKX 同向合併成一倉，各 OCO 管自己那段：
    TP1 觸發→只平那腿、其餘腿的 SL 續存＝與紙上 40/30/30 階梯同語義。
    每腿 clOrdId 加尾碼 a/b/c 冪等；部分失敗→回 False 由外層重試（已成腿撞
    51016 重複視為成功，不會重複開倉）。單一 TP 時維持原單筆路徑。"""
    lev = leverage_for_trade(intent.get("entry"), intent.get("stop"))
    # v248（監督員 r139）：設槓桿失敗 ⇒ **無條件**整筆不下。
    #
    # 被推翻的是 v155（r45）修C 的「風險帶」：當時只擋 lev < 上限 的情形，理由是
    # 「上限本身不可能被交易所卡在更高的舊值 ⇒ 擋單純屬白擋」。那個推理只想過
    # 「交易所側比意圖**高**」一種偏離，漏掉了對稱的另一半——**交易所側比意圖低**。
    #
    # 2026-08-03 17:12 真錢實證（不是推測）：SOXL-USDT-SWAP short，算出 lev＝上限，
    # 設槓桿被 OKX 回 59102「Leverage exceeds the maximum limit」（該合約上限低於本
    # 執行器的上限；模板第 45 行「美股代幣永續上限 25x」這個假設對 SOXL 不成立），
    # ensure_leverage 回 False、記了一筆 leverage_fail——然後因為 lev == 上限不在風險帶，
    # **三腿真錢單照樣送出去**，倉開在交易所預設的 3x 上。事後的讀回閘（v171）只能在
    # 錢已經下去之後喊 mismatch，且它按設計不擋單、不自動調整 ⇒ 那一刻沒有任何東西
    # 攔得住。這就是「fail-closed 的控制項自己 fail-open」。
    #
    # 為什麼改成無條件擋是安全方向：它只會讓單**變少**，永遠不會讓單變多。網路類的
    # 暫時失敗＝延後一輪重試（每分鐘一輪）；若是 59102 這種該合約結構性擋不住的，
    # 那就該一直擋——⛔ 開一個「槓桿設不成功」的真錢倉，本來就沒有正確版本。
    # ⚠️ 副作用要講明：上限高於該合約上限的標的，從此永遠下不了單，而不是自動降級
    # 成該合約的上限。要不要改成「查上限後夾住再算 sz」是另一個決策（會動到部位
    # 大小的數學），⛔ 不在本修範圍內；在那之前，這類標的每輪會留下一筆 leverage_fail
    # ＝擋在哪裡看得見，不是靜音。
    if not ensure_leverage(intent["inst_id"], intent["pos_side"], dry, lev=lev):
        print(f"⚠️ {intent['inst_id']}/{intent['pos_side']} 應設 {lev}x 但設槓桿失敗"
              f"——本輪整筆不下（fail-closed，下輪重試）")
        return False
    tps = [intent.get("tp1"), intent.get("tp2"), intent.get("tp3")]
    legs = (split_tp_levels(sz, spec["lotSz"], spec["minSz"], tps)
            if spec else [])
    if len(legs) < 2:
        legs = [(float(intent["tp1"]), sz)]
    all_ok = True
    for i, (tp_px, leg_sz) in enumerate(legs):
        cl = (f"{intent['cl_ord_id']}{chr(97 + i)}" if len(legs) > 1
              else intent["cl_ord_id"])[:24]
        args = _leg_args(intent, leg_sz, tp_px, cl)
        if dry:
            print("DRY-RUN:", "okx --profile demo " + " ".join(args))
            continue
        # 真冪等：下單前先查此腿 clOrdId 是否已存在（含已成交）——
        # OKX 不擋已成交市價單的 clOrdId 重用，重試盲下會加倍持倉
        exists = _order_exists(intent["inst_id"], cl)
        if exists is True:
            print(f"↩️ 腿{i + 1}/{len(legs)} clOrdId={cl} 已存在（上輪已成）——跳過")
            continue
        if exists is None:
            print(f"⚠️ 腿{i + 1}/{len(legs)} 查單失敗——本輪不下這腿（fail-closed，下輪重試）")
            # 這裡是真正「該下卻沒下」的那一刻——一定要進健康帳，否則整盤零成交無聲
            _note_fail("query_fail", f"{intent['inst_id']} clOrdId={cl} 查單失敗")
            all_ok = False
            continue
        code, out = _okx(args)
        ok = _leg_ok(code, out)
        all_ok = all_ok and ok
        print(("✅" if ok else "❌")
              + f" {intent['inst_id']} {intent['side']} 腿{i + 1}/{len(legs)} "
              f"sz={leg_sz} tp={tp_px} → {redact_secrets(out)[:160]}")
    return all_ok


def selftest_fail(rounds: int) -> int:
    """製造假故障實證告警路徑（零網路、零下單）。用臨時健康檔，絕不動真實狀態。

    重現 2026-07-30 那晚：OKX 回 401「IP 不在白名單」→ 查單失敗 → 腿全不下。
    預期：前 FAIL_ALERT_AFTER-1 輪只提示，第 FAIL_ALERT_AFTER 輪告警，
    之後冷卻不重複吵，乾淨輪送恢復通知。"""
    global HEALTH
    real, HEALTH = HEALTH, HEALTH.with_name("atk_consumer_health_selftest.json")
    try:
        HEALTH.unlink(missing_ok=True)
        fake_401 = ("Error: HTTP 401 from OKX: Your IP 203.0.113.7 is not "
                    "included in your API key's "
                    "00000000-0000-4000-8000-000000000000 whitelist")
        print(f"— 假故障實證開始（門檻={FAIL_ALERT_AFTER} 輪，告警走 dry 不真的送出）—")
        print(f"  分類結果：{classify_failure(1, fake_401)}"
              f"｜遮蔽後樣本：{redact_secrets(fake_401)[:90]}…")
        now = time.time()
        for i in range(rounds):
            _ROUND_FAILS.clear()
            _note_fail(classify_failure(1, fake_401), fake_401)
            _note_fail("query_fail", "SOXL-USDT-SWAP clOrdId=xxx 查單失敗")
            print(f"[假第 {i + 1} 輪]", end=" ")
            finish_round(dict(_ROUND_FAILS), now + i * 60, dry=True)
        _ROUND_FAILS.clear()
        # v151：先驗「空轉輪不得假痊癒」——零呼叫的一輪不可歸零、不可送恢復通知
        print("[假空轉輪]", end=" ")
        hi = finish_round({}, now + rounds * 60, dry=True, oks=0)
        print(f"— 空轉輪檢查：連續故障維持={hi.get('consecutive_fail_rounds')} 輪"
              f"（應仍為 {rounds}），未誤送恢復通知={not hi.get('recovered_from')} —")
        print("[假恢復輪]", end=" ")
        h = finish_round({}, now + (rounds + 1) * 60, dry=True, oks=1)
        print(f"— 實證結束：連續故障歸零={h.get('consecutive_fail_rounds') == 0}，"
              f"恢復通知已送={bool(h.get('recovered_from'))}，"
              f"告警冷卻已重置={not h.get('last_alert_ts')} —")
        print(f"  健康檔（臨時）：{HEALTH}")
        return 0
    finally:
        HEALTH = real


# ── 已處理清單（v165：未知 vs 確認沒處理過） ────────────────────────────
def _read_intent(path) -> dict | None:
    """讀一筆 intent。回 None＝讀不出來（已出聲、已記進本輪故障帳）。

    v192（監督員 r86）：舊版是主迴圈裡就地一句 `except Exception: continue`——同物種
    第 12 次：**讀不出來被折成沒這筆**，而且連一行 print 都沒有。

    這一處不像其他讀取點「下輪重試就會好」：訊號產生端 l4_execution/intent_outbox.py
    是依檔名冪等的（`if p.exists(): continue`）⇒ 壞檔**永遠不會被重寫**。於是那筆訊號
    每輪被靜默跳過，直到 expires_at 到期永久消失——連 v169 的「過期丟棄」數字都算不到
    它頭上（解析失敗發生在讀 expires_at 之前）。

    型別檢查同等重要：合法 JSON 但不是物件（例如 `[]`）在舊碼會逃出 try，在下一行
    `intent.get(...)` 以 AttributeError 掀掉整輪消費器。

    ⛔ 不可改回無條件 continue；⛔ 偵測端永遠不得刪改壞檔——那是唯一的鑑識證據。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        msg = (f"intent 檔讀不出來（{path.name}，{type(e).__name__}）——本輪跳過這一筆；"
               "產生端依檔名冪等，壞檔不會被自動重寫，過期後這筆訊號將永久消失")
        print(f"🚨 {msg}")
        _note_fail("intent_unreadable", msg)
        return None
    if not isinstance(raw, dict):
        msg = (f"intent 檔內容結構不對（{path.name}，頂層是 {type(raw).__name__} 不是物件）"
               "——本輪跳過這一筆；壞檔不會被自動重寫，過期後這筆訊號將永久消失")
        print(f"🚨 {msg}")
        _note_fail("intent_unreadable", msg)
        return None
    return raw


def _load_state() -> dict | None:
    """讀已處理 intent 清單。回 None＝讀不到／讀壞了（**未知**）；
    ⛔ 不可再當成「確認一筆都沒處理過」。

    v165（監督員 r56）：舊版 `except: state = {"done": []}` 是 v162/v163/v164 同一物種
    在真錢路徑上的最後一處——把「不知道」壓成「確認沒有」。這一處的下游有兩條：

    ①**永久抹掉（唯一不可逆）**：done 被清成空集合後，輪尾照樣把它寫回檔案 ⇒ 壞檔被
      一本乾淨的空清單覆蓋，最多 500 筆已處理紀錄就此消失，而且無聲。
    ②**假倉入帳**：清單歸零後，還沒過期（6h 窗）的舊 intent 會被重跑。重複成交本身有
      clOrdId 冪等擋著（_order_exists 查到已成交那腿回 True ⇒ 跳過）——但每腿都跳過會
      讓 place() 回 True，main() 便把這筆當成「本輪剛開的新倉」寫進 ps["open"]。若那筆
      倉早已平掉，本地帳就憑空多一個**交易所上不存在的部位**：同幣同向閘從此擋掉之後
      真正該下的單，對帳邏輯也會去管一個不存在的倉。冪等鎖擋得住重複成交，擋不住這個。

    「檔案還不存在」＝首跑，是真的空清單（合法）；JSON 壞掉／IO 錯／型別不對一律未知。
    半截 JSON 不是理論風險：舊寫法 write_text 是「先截斷再寫」的非原子寫，行程在中途被殺
    （排程逾時／休眠／當機）就會留下壞檔——與 v163 修的是同一個成因。"""
    try:
        raw = json.loads(STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"done": []}              # 首跑：還沒處理過任何 intent＝合法空清單
    except Exception as e:  # noqa: BLE001
        _note_fail("done_state_unreadable",
                   f"{STATE.name} 讀取失敗：{type(e).__name__}: {e}")
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("done", []), list):
        _note_fail("done_state_unreadable", f"{STATE.name} 內容結構不對")
        return None
    raw.setdefault("done", [])
    return raw


def _save_state(state: dict) -> bool:
    """原子寫回已處理清單。回 False＝沒寫成（呼叫端必須出聲）。

    v165（監督員 r56）：①改「暫存檔＋os.replace」——非原子寫留下的半截 JSON 會讓
    _load_state 從此永遠讀壞（見上，正是本輪要斷的自我製造迴圈）。②失敗不再用
    `except: pass` 無聲吞掉：寫不進去代表本輪處理過的 intent 沒落地。"""
    tmp = STATE.with_name(STATE.name + ".tmp")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, STATE)
        return True
    except Exception as e:  # noqa: BLE001
        _note_fail("done_state_write_fail",
                   f"{STATE.name} 寫入失敗：{type(e).__name__}: {e}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest-fail", type=int, metavar="N",
                    help="製造 N 輪假故障走完整告警路徑（零網路、零下單、不寫健康檔"
                         "的真實 profile 欄位以外的任何交易狀態）——驗收告警是否真的有出口")
    a = ap.parse_args()

    if a.selftest_fail:
        return selftest_fail(a.selftest_fail)
    if not a.dry_run and not verify_demo_profile():
        return 1
    # v165（r56）：三態——None＝讀壞了（未知），⛔ 勿再寫回 `or {"done": []}`
    state = _load_state()
    done = set(state.get("done", [])) if state is not None else set()
    ct_cache: dict = {}

    while True:
        now_ms = time.time() * 1000
        _ROUND_FAILS.clear()             # v143：本輪故障帳從零開始
        # v165（r56）：清單未知就每輪重讀——檔案被人修好／還原後不必重啟就自然接上。
        #   重讀也讓 done_state_unreadable 每輪重新記帳（_ROUND_FAILS 剛清空），
        #   否則只有開機那一次會出聲，之後永遠靜音。
        if state is None:
            state = _load_state()
            if state is not None:
                done = set(state.get("done", []))
        state_blind = state is None
        _ROUND_OKS["ok"] = 0             # v151：成功帳同步歸零（分辨空轉輪用）
        _ROUND_EXPIRED.clear()           # v169：本輪丟棄帳從零開始
        _ROUND_PNL_GAPS.clear()          # v170：本輪熔斷漏記帳從零開始
        _ROUND_BE_GAPS.clear()           # v249：本輪保本缺口從零開始
        # v139：先管理在場倉位（對帳/逾時平倉），再看要不要接新單
        # v159（r47）：manage_positions 同時做反向對帳，回本輪的孤兒部位鍵
        # v162（r53）：None＝本輪掃描沒跑成（查不到交易所部位）⇒ 擋單閘等於瞎掉，
        #   本輪一律不接新單（既有倉照常管理）。⛔ 勿再寫 `... or []`：那會把
        #   「未知」壓成「確認乾淨」，正是本輪要修的破口。
        orphan_scan = manage_positions(a.dry_run)
        orphan_blind = orphan_scan is None
        orphan_keys = set(orphan_scan or [])
        # v163（r54）：本地帳是熔斷口徑與同幣同向閘的唯一資料源——讀不到就兩個閘都
        #   瞎掉（day_pnl 空＝熔斷永不觸發、open 空＝併倉閘永不擋）。⛔ 勿寫成
        #   `_load_positions() or {}`：那又把「未知」壓回「確認乾淨」。
        ps_round = _load_positions()
        ledger_blind = ps_round is None
        if ledger_blind:
            print("⛔ 本地部位帳讀取失敗——熔斷口徑與同幣同向閘皆無資料，本輪不接新單"
                  "（既有倉的交易所端止損仍在）")
        halted_today = breaker_tripped((ps_round or {}).get("day_pnl", {}))
        if halted_today:
            print(f"⛔ 日虧熔斷已觸發（≤ -{DAILY_STOP_USD:.0f} USDT）——今日不接新單，"
                  "既有倉位照常管理")
        if state_blind:
            print("⛔ 已處理清單讀取失敗——分不出哪些 intent 已下過單，本輪一筆都不接"
                  "（既有倉照常管理、交易所端止損仍在；⛔ 本輪不覆寫該檔）")
        # v165（r56）：清單未知 ⇒ 冪等的第一道鎖失效，重跑舊 intent 會把早已平掉的倉
        #   憑空記回帳本（見 _load_state）⇒ 本輪整批不碰。⛔ 勿改回無條件 glob。
        for p in ([] if state_blind else sorted(OUTBOX.glob("*.json"))):
            # v192（r86）：⛔ 勿改回就地 try/except+裸 continue——讀不出來要出聲、要記帳
            intent = _read_intent(p)
            if intent is None:
                continue
            iid = intent.get("intent_id")
            if not iid or iid in done:
                continue
            if intent.get("execution_policy") != "demo_only":
                print(f"⏸ {iid} {intent.get('symbol')} human_gated——僅列印不執行")
                done.add(iid)
                continue
            if now_ms > intent.get("expires_at", 0):
                # v169（r67）：過了 expires_at 就永不重送＝這筆訊號永久消失。
                #   ⛔ 不可只 print：斷流期丟了幾筆是判斷「這場故障值不值得急著修」
                #   的唯一實質代價，只存在 log 文字裡等於問不出來（見 _account_expiry）。
                print(f"⏭ {iid} {intent.get('symbol')} 已過期——跳過")
                _ROUND_EXPIRED.append({"intent_id": iid,
                                       "symbol": intent.get("symbol"),
                                       "side": intent.get("pos_side")})
                done.add(iid)
                continue
            if halted_today:
                continue                     # 熔斷日不接新單；intent 未記 done，明日過期自清
            if ledger_blind:
                continue                     # v163（r54）：帳本未知＝風險閘全瞎，不記 done、下輪重試
            # v154（監督員 r44・修A）：同幣同向已在場 → 本輪不接。
            #   OKX hedge mode 會把同幣同向併成交易所側單一部位，一筆 realizedPnl
            #   無法歸屬兩個 intent（v130 已在模擬盤實證：同筆 pnl 雙重記帳、
            #   R 虛增 +1.30），且曝險會在逐單檢查下無聲翻倍。
            #   反向不擋（hedge 雙向合法）。不記 done：比照熔斷日，
            #   倉平掉後 intent 若還沒過期就自然接上。
            #   逐單重讀：同一輪內前一筆下單會改動帳本，後一筆必須看得到。
            #   v163（r54）：輪中途才壞掉（例如上一筆寫到一半被殺）也要擋，不是只在輪首檢查。
            ps_i = _load_positions()
            if ps_i is None:
                print(f"⏸ {iid} {intent.get('symbol')} 本地部位帳讀取失敗——本輪不接"
                      "（下輪重試）")
                continue
            if dup_open_same_side(ps_i.get("open", {}),
                                  intent["inst_id"], intent["pos_side"]):
                print(f"⏸ {iid} {intent.get('symbol')} 同幣同向已在場——本輪不接"
                      "（OKX hedge 併倉會使已實現損益無法歸屬兩單）")
                continue
            # v159（監督員 r47）：同幣同向有「孤兒部位」（交易所有、本地帳沒有）
            #   → 本輪不接。理由與修A 完全相同（併倉後已實現損益無法歸屬），
            #   而孤兒的情況更糟：本地帳根本不知道那筆倉存在，曝險會無聲翻倍。
            #   不記 done：人工處理掉那筆倉後、intent 若還沒過期就自然接上。
            if (intent["inst_id"], intent["pos_side"]) in orphan_keys:
                print(f"⏸ {iid} {intent.get('symbol')} 同幣同向有孤兒部位在交易所上"
                      "——本輪不接（先人工確認那筆脫帳的倉）")
                continue
            # v162（監督員 r53）：本輪查不到交易所部位 ⇒ 無法確認同幣同向有沒有孤兒。
            #   看不見就不開新倉（不記 done，下輪重試到 expires_at 為止）。
            #   ⛔ 別把這條移到 dup 閘之前也別移走：它是孤兒閘的「未知」分支。
            if orphan_blind:
                print(f"⏸ {iid} {intent.get('symbol')} 本輪查不到交易所部位——"
                      "無法確認有無孤兒，不接新單（下輪重試）")
                continue
            # v209（監督員 r104）：張數換算失敗分兩態。舊碼一律 done.add＝**永久**
            #   丟棄，與下面那行「只在成功時記已處理：失敗留給下輪重試」自相矛盾——
            #   規格這輪讀不出來（CLI 沒回 0／輸出解不開／形狀認不得）本是最典型的
            #   暫時性故障，卻讓一筆真錢訊號在第一次讀取失敗時就消失，連 expires_at
            #   的重試窗都用不到，也算不進「過期丟棄」的統計。
            sz_why: dict = {}
            sz = contracts_for(intent["inst_id"], intent["entry"], intent["stop"],
                               ct_cache, out=sz_why)
            if sz is None:
                why = sz_why.get("reason")
                if sizing_retryable(why):
                    print(f"⏸ {iid} 這輪讀不出合約規格（{why or '未分類'}）——"
                          "本輪不接，下輪重試（不記 done）")
                    _note_fail("instrument_spec_unreadable",
                               f"{iid} {intent['inst_id']} 合約規格讀不出來"
                               f"（{why or '未分類'}）")
                else:
                    print(f"❌ {iid} 張數換算不成立（{why}）——跳過（不猜）")
                    done.add(iid)
                continue
            # 只在成功時記已處理：失敗留給下輪重試（OKX clOrdId 冪等擋重複成交，
            # intent 過期窗兜底——永久性錯誤最多重試到 expires_at）
            if place(intent, sz, a.dry_run, spec=ct_cache.get(intent["inst_id"])):
                done.add(iid)
                if not a.dry_run:
                    ps = _load_positions()
                    if ps is None:
                        # v163（r54）：⛔ 絕不可拿空帳寫回——那會把既有倉與 14 天熔斷
                        #   損益整本抹掉。上面的閘理論上已擋住，這裡是縱深防禦：真的走
                        #   到了就出聲，那筆倉下一輪由孤兒閘接手（它的 SL 仍在交易所）。
                        print(f"🚨 {iid} 已送出但本地帳讀不到、無法記帳——"
                              "該倉下輪會被當孤兒偵測到，請人工到 OKX 確認")
                        _note_fail("pos_state_unreadable",
                                   f"{iid} 已下單但帳本讀取失敗、未記帳")
                    else:
                        ps["open"][iid] = {"inst_id": intent["inst_id"],
                                           "pos_side": intent["pos_side"],
                                           "symbol": intent.get("symbol"),
                                           "contracts": sz,
                                           # v249：保本要用「原始止損」當 R 基準。
                                           # 不記下來就只能回頭讀掛單上的值，而那個
                                           # 值在搬過一次之後就是保本價本身 ⇒ 會愈
                                           # 算愈貼身（棘輪）。tickSz 同理，改價要對齊。
                                           "stop": float(intent["stop"]),
                                           "tickSz": (ct_cache.get(
                                               intent["inst_id"]) or {}).get("tickSz"),
                                           # v171（r69）：留下「打算用的槓桿」，
                                           # 否則下輪讀回交易所的值沒有比較基準
                                           # ⇒ 只能永遠判 unknown。
                                           "lev": leverage_for_trade(
                                               intent.get("entry"),
                                               intent.get("stop")),
                                           "placed_at": time.time()}
                        if not _save_positions(ps):
                            print(f"🚨 {iid} 已送出但帳本寫入失敗——"
                                  "該倉下輪會被當孤兒偵測到，請人工到 OKX 確認")
        # v165（r56）：⛔ 未知時絕不寫回——那會把壞檔換成一本乾淨的空清單＝永久抹掉
        #   （本輪唯一不可逆的一條，比照 v163 對部位帳的處置）。
        if not state_blind:
            state["done"] = sorted(done)[-500:]
            _save_state(state)
        # v143：本輪收尾——把 fail-closed 記帳並在連續失敗時告警（dry-run 不真送）
        finish_round(dict(_ROUND_FAILS), dry=a.dry_run, oks=_ROUND_OKS["ok"],
                     expired=list(_ROUND_EXPIRED),
                     pnl_gaps=list(_ROUND_PNL_GAPS),
                     be_gaps=list(_ROUND_BE_GAPS))
        if a.once or a.dry_run:
            return 0
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
