"""task #39: OKX 模擬盤自動操盤手 (demo_operator) — daemon worker。

定位：把 paper_journal 的新訊號鏡像成 OKX 模擬盤上的真實限價單，並以 OKX 真相監控
（成交偵測 / 平倉 realizedPnl / 逾時平倉 / 對帳漂移），結果寫進 demo_journal.demo_trades。
它驅動「真實交易所機制下的驗證樣本」時鐘——realized_r 全取自 OKX positions-history
（紅線③：絕不本地捏造）。

⚠️ demo_trades 是**模擬盤**、不是真錢，也不是 Phase 0「真實小額 ≥30 筆」門檻（那一格只認
   trades 表的真錢交易、按 ✅ 才寫）。本帳呈現時一律標「模擬盤」，永不當真錢績效宣稱。

兩道鑰匙（two-key arming）——把 worker 安全接進 daemon、待使用者拍板再啟動：
  鑰匙①  OKX_DEMO_TRADING_ENABLED（demo_guard 設定層）：模擬盤金鑰/連線總開關。
  鑰匙②  DEMO_OPERATOR_ACTIVE（本層，預設 0）：操盤手「真的下單」總開關。
  兩把都開、且模擬盤金鑰齊備、kill switch 未按，才會真的在模擬盤下單。
  任一沒開 → 本 worker **完全空轉、零 OKX 互動**（連線都不建）。已開倉者仍受 OKX 原生附帶
  止損/止盈 (attachAlgoOrds) 保護，故即使中途關掉鑰匙②也不裸倉。

安全鐵律（承 demo_trader / demo_guard，對抗式審查 2/3 UNSAFE 的 fail-closed 修正）：
  - 永不呼叫 okx-trade-mcp（那是真錢）。只走 demo_guard.make_demo_exchange 模擬盤客戶端。
  - 每輪建「全新 ex」（避免共享 headers 的 TOCTOU），下單前 place_demo_plan 會再正向證明。
  - 每輪先檢 kill switch；對帳漂移 → 設 halt 旗標停所有新單、但監控續跑收斂。
  - realized_r 只來自 OKX 真相；positions-history 尚未回填就保守等待，不推估、不平本地帳。

純決策函式（離線可測、零網路/零下單）：is_active / is_crypto_signal / select_new_signals /
  classify_untracked_halt / infer_exit_reason / pending_expired / needs_timeout_close。
連線層（需 .env + 網路 + 兩把鑰匙）：run_demo_operator_cycle / run_demo_operator_loop。

CLI：
    python -m l3_dispatcher.demo_operator --selftest     # 離線測純決策（零下單）
    python -m l3_dispatcher.demo_operator --status       # 列帳本/高水位/halt（零下單）
    python -m l3_dispatcher.demo_operator --cycle-once   # 連 OKX 跑一輪（受兩把鑰匙與 kill switch 約束）
"""
from __future__ import annotations

import os
import time

# ---------------------------------------------------------------------------
# 設定（明確 env 優先；純函式以參數注入故可離線覆寫）
# ---------------------------------------------------------------------------
ACTIVE_FLAG = "DEMO_OPERATOR_ACTIVE"        # 鑰匙②（預設關）
_TRUTHY = ("1", "true", "yes", "on")

# 只鏡像加密訊號（排除美股 us_breakout：走另一條，且非 OKX 永續宇宙）
EXCLUDED_SETUPS = ("us_breakout",)
# 只鏡像「近 N 分鐘內」的新訊號——daemon 曾離線時不回補陳舊訊號（價位早已失效）
INTAKE_MAX_AGE_MIN = int(os.getenv("DEMO_INTAKE_MAX_AGE_MIN", "90"))
INTAKE_BATCH_LIMIT = int(os.getenv("DEMO_INTAKE_BATCH_LIMIT", "3"))   # 每輪最多開幾筆新倉（保守）
SCAN_LIMIT = int(os.getenv("DEMO_INTAKE_SCAN_LIMIT", "200"))         # 每輪從 paper 讀新訊號上限
DEMO_MIN_RR = float(os.getenv("DEMO_MIN_RR", "1.5"))                 # 模擬盤交易訊號品質閘：只開 R:R≥此值（研究 w7r04t691）
ENTRY_EXPIRY_HOURS = float(os.getenv("DEMO_ENTRY_EXPIRY_HOURS", "8"))  # 限價掛單逾時作廢
TIME_LIMIT_HOURS = float(os.getenv("DEMO_TIME_LIMIT_HOURS", "24"))     # 持倉逾時平倉（鏡 demo_trader.TIME_LIMIT_HOURS=24）


# ---------------------------------------------------------------------------
# 純決策函式（離線可測）
# ---------------------------------------------------------------------------
def is_active(env=None) -> bool:
    """鑰匙②：DEMO_OPERATOR_ACTIVE 是否開（預設關）。"""
    env = os.environ if env is None else env
    return (env.get(ACTIVE_FLAG) or "").strip().lower() in _TRUTHY


def is_crypto_signal(setup, excluded=EXCLUDED_SETUPS) -> bool:
    """是否為應鏡像到 OKX 模擬盤的加密訊號（排除美股等非永續宇宙）。"""
    return (setup or "") not in tuple(excluded)


def signal_rr(entry, stop, tp1):
    """tp1 報酬:風險比 = |tp1-entry| / |entry-stop|；無法算回 None。"""
    try:
        risk = abs(float(entry) - float(stop))
        reward = abs(float(tp1) - float(entry))
        return (reward / risk) if risk > 0 else None
    except (TypeError, ValueError):
        return None


def is_quality_signal(row, min_rr=None) -> bool:
    """【模擬盤交易 Session】訊號品質閘——只開高 R:R(高期望值)單，避免過度交易耗盡保證金
    （研究 w7r04t691：最低 R:R 篩選；解 51008 的篩選半，與槓桿效率半互補）。

    ⚠️ 兩 Session 獨立：這只 gate【模擬盤交易】；【紙上驗證 Session】(paper_journal) 仍記錄
       全部訊號做回測迭代，不受此限。無 tp1/無法算 R:R → 不擋（交由其他閘），只擋明確 R:R 過低。"""
    if min_rr is None:
        min_rr = DEMO_MIN_RR
    rr = signal_rr(row.get("entry_price"), row.get("stop_price"), row.get("tp1"))
    return True if rr is None else rr >= min_rr


def select_new_signals(rows, hwm, now_ms, *, max_age_min=INTAKE_MAX_AGE_MIN,
                       limit=INTAKE_BATCH_LIMIT, excluded=EXCLUDED_SETUPS,
                       tradable_symbols=None):
    """從 paper 新列挑出該鏡像的訊號，回 (selected, new_hwm)。

    合格條件（全 AND）：id>hwm、加密 setup、status=='open'、方向合法、有 entry/stop、
    且 entry_at 在近 max_age_min 分內。

    高水位推進原則（修去重正確性）：升冪掃描，對「已決定」的列（選中或確定不合格）推進
    new_hwm；遇到「合格但本輪額度已滿」的列就**停在那筆之前**（break），確保合格訊號不會
    被高水位跳過而漏鏡像。"""
    new_hwm = hwm
    picked = []
    age_ms = max_age_min * 60 * 1000
    for r in sorted(rows, key=lambda x: x.get("id", 0)):
        rid = r.get("id", 0)
        if rid <= hwm:
            continue
        ea = r.get("entry_at") or 0
        eligible = (
            is_crypto_signal(r.get("setup"), excluded)
            and r.get("status") == "open"
            and r.get("direction") in ("bull", "bear")
            and bool(r.get("entry_price")) and bool(r.get("stop_price"))
            and (now_ms - ea) <= age_ms
            and is_quality_signal(r)        # v84 task#5/#6：模擬盤只開高 R:R 訊號（紙上驗證 Session 不受限）
            and (tradable_symbols is None   # v84 task#8：只挑 OKX 模擬盤可交易幣（治本 not_on_okx 主因）
                 or (r.get("symbol") or "").upper() in tradable_symbols)
        )
        if eligible and len(picked) >= limit:
            break                       # 合格但額度滿 → 留到下輪，高水位不跨過它
        new_hwm = rid                   # 已決定（選中或不合格）→ 推進
        if eligible:
            picked.append(r)
    return picked, new_hwm


def classify_untracked_halt(okx_positions, tracked_keys):
    """OKX 上有、本地帳本未追蹤的持倉 → 致命漂移（人工開倉/崩潰殘留/金鑰污染）→ 應 halt。
    回 (should_halt, reason, untracked_list)。"""
    untracked = [p for p in okx_positions
                 if (p.get("symbol"), p.get("pos_side")) not in tracked_keys]
    if untracked:
        desc = ", ".join(
            f"{p.get('symbol')}/{p.get('pos_side')}×{p.get('contracts')}" for p in untracked)
        return True, f"OKX 出現未追蹤持倉：{desc}", untracked
    return False, "", []


def infer_exit_reason(marker, pnl_usd) -> str:
    """平倉原因推斷：有 timeout marker → 'timeout'；否則依 realizedPnl 正負粗分 'tp'/'stop'。
    （模擬盤驗證層的粗標——出場類型；精確分腿命中由 OKX 原生算法單真相決定，此處只記類別。）"""
    if marker == "timeout":
        return "timeout"
    return "tp" if (pnl_usd or 0) > 0 else "stop"


def pending_expired(entry_at, now_ms, expiry_hours=ENTRY_EXPIRY_HOURS) -> bool:
    """限價進場單從掛出起逾時未成交 → 作廢（entry_expired）。"""
    return (now_ms - (entry_at or 0)) > expiry_hours * 3600 * 1000


def needs_timeout_close(filled_at, entry_at, now_ms, time_limit_hours=TIME_LIMIT_HOURS) -> bool:
    """持倉自成交（無成交時刻則自下單）起逾時 → 主動市價平倉（stale signal 出場）。"""
    ref = filled_at or entry_at or now_ms
    return (now_ms - ref) > time_limit_hours * 3600 * 1000


# ---------------------------------------------------------------------------
# 連線層輔助（需 .env + 網路）
# ---------------------------------------------------------------------------
def _notify(tg, msg: str) -> None:
    print(f"[demo_op] {msg}")
    if tg:
        try:
            tg(msg)
        except Exception:  # noqa: BLE001 — 通知失敗不可影響操盤
            pass


def _free_usdt(bal) -> float:
    try:
        v = (bal.get("free") or {}).get("USDT")
        if v is None:
            v = (bal.get("USDT") or {}).get("free")
        return float(v or 0)
    except Exception:  # noqa: BLE001
        return 0.0


async def _make_ex():
    """每輪全新 ex（避免共享 headers 的 TOCTOU），先正向證明模擬盤。"""
    from l4_execution import demo_guard
    ex = demo_guard.make_demo_exchange()
    await demo_guard.confirm_okx_demo(ex)
    return ex


_DEMO_UNIVERSE = {"set": None, "ts": 0.0}
_DEMO_UNIVERSE_TTL = 3600.0  # 1h 快取


async def _okx_demo_universe(ex):
    """OKX 模擬盤可交易的 USDT 永續『基礎幣』集合（快取 1h）。
    用於 intake 預過濾：把有限額度(3/輪)用在『真能在 OKX 模擬盤成交』的訊號上，治本
    not_on_okx 拒單主因(驗活 17/35)＝避免額度被不可交易幣浪費、提升 demo 樣本 throughput。
    任何失敗 → None（呼叫端不過濾、退回原行為，安全降級、零回歸）。"""
    import time as _t
    now = _t.time()
    cached = _DEMO_UNIVERSE["set"]
    if cached is not None and (now - _DEMO_UNIVERSE["ts"]) < _DEMO_UNIVERSE_TTL:
        return cached
    try:
        markets = await ex.load_markets()
        uni = set()
        for m in (markets or {}).values():
            try:
                if (m.get("swap") and m.get("active", True)
                        and m.get("quote") == "USDT" and m.get("base")):
                    uni.add(str(m["base"]).upper())
            except Exception:
                continue
        if not uni:
            return None
        _DEMO_UNIVERSE["set"] = uni
        _DEMO_UNIVERSE["ts"] = now
        return uni
    except Exception as e:  # noqa: BLE001
        print(f"[demo_op] OKX demo 宇宙抓取失敗（本輪不過濾）：{type(e).__name__}: {e}")
        return None


async def _place_one(ex, signal, *, avail_usd, tg=None) -> dict:
    """鏡像一筆 paper 訊號到模擬盤。回 {placed, reason, margin_est?}。
    任何「不下」情況（拒絕/失敗）皆寫一筆 status='rejected' 審計痕跡，使高水位能安全前進、
    同一訊號不每輪重試。以 intent_id 冪等（已處理過 → 直接略過、不重複下單）。"""
    from l3_dispatcher import demo_journal as dj
    from l3_dispatcher.risk_manager import CORRELATED_FAMILIES
    from l4_execution import demo_trader as dt

    symbol = signal["symbol"]
    direction = signal["direction"]
    entry = float(signal["entry_price"])
    stop = float(signal["stop_price"])
    seq = signal.get("fire_id") or f"p{signal['id']}"     # 持久 seq（intent_id 用，跨時去重）

    # v56 #54 治本：標的不在 OKX 永續宇宙（ex.market 丟 BadSymbol）→ 寫一筆 rejected
    # 審計痕跡，而非讓例外逸出被 _intake 當無名 error 靜默漏記。這讓高水位能安全前進、
    # 同一訊號不每輪重試，且「為何沒鏡像到模擬盤」在 demo_trades 帳本可見可歸因（解決
    # 「資料沉沒看不到」）。只攔『確定不在 OKX』的 BadSymbol/BadRequest；網路/超時等
    # 暫時性錯誤照舊往上拋，不誤把有效訊號永久標 rejected。
    try:
        spec = await dt.fetch_okx_contract_spec(ex, symbol)   # 真實 ctVal/lotSz/minSz
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ not in ("BadSymbol", "BadRequest"):
            raise                                            # 暫時性 → 交由上層計 error、下輪重試
        rej_intent = f"noinstr:{seq}"
        if not dj.intent_exists(rej_intent):
            dj.record_demo_entry(
                intent_id=rej_intent, cl_ord_id=f"x{seq}",
                paper_id=signal["id"], fire_id=signal.get("fire_id"),
                symbol=symbol, setup=signal.get("setup"), direction=direction,
                entry_price=entry, stop_price=stop,
                tp1=signal.get("tp1"), tp2=signal.get("tp2"), tp3=signal.get("tp3"),
                leverage=0, notional_usd=0.0, margin_usd=0.0, contracts=0.0,
                ct_val=0.0, risk_usd=0.0, entry_order_id=None,
                regime=signal.get("regime"), status="rejected",
                exit_reason="reject:not_on_okx",
                # task#73(A) 誠實措辭：被擋的是 OKX「模擬盤(demo/testnet)」較精簡的永續
                #   宇宙——該標的在 OKX 正式盤(live)很可能存在，只是 demo 未上架。措辭講清楚
                #   「demo 沒有」而非「OKX 沒有」，避免日後誤判訊號標的本身無效（紅線③不誤導）。
                note=(f"標的不在 OKX 模擬盤(demo/testnet)永續宇宙"
                      f"（live 可能有、demo 較精簡）：{type(e).__name__}"))
        return {"placed": False, "reason": "not_on_okx"}
    plan = dt.build_order_plan(
        symbol, direction, entry, stop,
        atr_pct_7d=None, ct_val=spec["ct_val"], lot_sz=spec["lot_sz"],
        min_sz=spec["min_sz"], seq=seq,
    )

    if dj.intent_exists(plan.intent_id):
        return {"placed": False, "reason": "already_handled"}

    def _record(status, exit_reason, entry_order_id, note):
        dj.record_demo_entry(
            intent_id=plan.intent_id, cl_ord_id=plan.cl_ord_id,
            paper_id=signal["id"], fire_id=signal.get("fire_id"),
            symbol=symbol, setup=signal.get("setup"), direction=direction,
            entry_price=entry, stop_price=stop,
            tp1=signal.get("tp1"), tp2=signal.get("tp2"), tp3=signal.get("tp3"),
            leverage=plan.leverage, notional_usd=plan.notional_usd,
            margin_usd=plan.margin_usd, contracts=plan.contracts,
            ct_val=plan.ct_val, risk_usd=(plan.realized_risk_usd or plan.risk_usd or 0.0),
            entry_order_id=entry_order_id, regime=signal.get("regime"),
            status=status, exit_reason=exit_reason, note=note)

    if not plan.ok:
        _record("rejected", f"reject:{plan.reject_reason}", None, "build_order_plan 拒絕")
        return {"placed": False, "reason": plan.reject_reason}

    open_trades = dj.get_live_demo_trades()               # 桶風險：當下在倉清單
    res = await dt.place_demo_plan(ex, plan, avail_usd=avail_usd,
                                   open_trades=open_trades,
                                   families=CORRELATED_FAMILIES)
    if not res.get("ok"):
        _record("rejected", f"reject:{res.get('error', 'place_failed')}", None,
                "place_demo_plan 未成功")
        return {"placed": False, "reason": res.get("error")}

    _record("pending", None, res.get("entry_order_id"), None)
    _notify(tg, f"🧪 模擬盤開倉(pending)：{symbol} {direction} 進{entry} 損{stop} "
                f"×{plan.contracts}張 lev{plan.leverage} 風險${plan.realized_risk_usd:.0f}"
                f"　paper#{signal['id']}")
    return {"placed": True, "margin_est": plan.margin_usd}


async def _monitor(ex, *, now_ms, tg=None) -> dict:
    """以 OKX 真相監控所有未平倉模擬盤單：成交偵測 / 平倉 realizedPnl / 逾時平倉 / 對帳。"""
    from l3_dispatcher import demo_journal as dj
    from l4_execution import demo_trader as dt

    okx_list = await dt.fetch_okx_positions(ex)
    okx_map = {(p["symbol"], p["pos_side"]): p for p in okx_list}
    live = dj.get_live_demo_trades()
    summary = {"checked": len(live), "filled": 0, "closed": 0, "expired": 0,
               "timeout_initiated": 0, "await_pnl": 0, "errors": 0}
    tracked_keys = set()
    for t in live:
        key = (t["symbol"], t["pos_side"])
        tracked_keys.add(key)
        iid = t["intent_id"]
        try:
            if t["status"] == "pending":
                if key in okx_map:
                    dj.mark_filled(iid)
                    summary["filled"] += 1
                elif pending_expired(t["entry_at"], now_ms):
                    await dt.cancel_demo_entry(ex, t["symbol"], t.get("entry_order_id"),
                                               t.get("cl_ord_id"))
                    dj.apply_demo_close(iid, pnl_usd=0.0, exit_reason="entry_expired",
                                        note="限價逾時未成交")
                    summary["expired"] += 1
                else:
                    dj.touch_synced(iid)
            elif t["status"] == "open":
                if key not in okx_map:
                    # OKX 上已無此倉 → 已平倉，取真相 realizedPnl。
                    # since_ms 作為本地 uTime scope 的「下界」：必須用 entry_at(下單時刻)，
                    # **不可**用 filled_at——filled_at 是本機輪詢偵測到成交的『記錄時刻』，會落後
                    # 真實成交達一個輪詢週期；對快速進出的單，平倉 uTime 可能早於 filled_at 數百 ms
                    # → 用 filled_at 當下界會把本倉平倉誤排除、永卡 await_pnl（LAB 即此例，差 488ms）。
                    # entry_at 必早於任何平倉 uTime，是安全且正確的下界。
                    res = await dt.fetch_okx_closed_pnl(
                        ex, t["symbol"], t["pos_side"],
                        since_ms=t.get("entry_at") or t.get("filled_at"))
                    if res.get("found"):
                        marker = dj.get_state(f"closing:{iid}", "")
                        reason = infer_exit_reason(marker, res["pnl_usd"])
                        dj.apply_demo_close(iid, pnl_usd=res["pnl_usd"], exit_reason=reason)
                        if marker:
                            dj.set_state(f"closing:{iid}", "")
                        summary["closed"] += 1
                    else:
                        dj.touch_synced(iid)   # history 未回填 → 保守等待，下輪重試
                        summary["await_pnl"] += 1
                else:
                    # 仍在倉：檢逾時平倉
                    if (needs_timeout_close(t.get("filled_at"), t["entry_at"], now_ms)
                            and not dj.get_state(f"closing:{iid}", "")):
                        remaining = float(okx_map[key].get("contracts") or 0)
                        close = await dt.market_close_demo(
                            ex, t["symbol"], t["pos_side"], remaining)
                        if close.get("ok"):
                            dj.set_state(f"closing:{iid}", "timeout")
                            summary["timeout_initiated"] += 1
                        else:
                            print(f"[demo_op] 逾時平倉失敗（下輪重試）：{iid} {close.get('error')}")
                    dj.touch_synced(iid)
        except Exception as e:  # noqa: BLE001 — 單筆失敗不拖垮整輪
            summary["errors"] += 1
            print(f"[demo_op] 監控 {iid} 失敗：{type(e).__name__}: {e}")

    should_halt, reason, _ = classify_untracked_halt(okx_list, tracked_keys)
    if should_halt:
        dj.set_halt(reason)
        summary["halt"] = reason
        _notify(tg, f"⛔ 模擬盤操盤手對帳漂移 → 已停新單：{reason}")
    return summary


async def _intake(ex, *, now_ms, tg=None) -> dict:
    """鏡像新 paper 訊號到模擬盤。首次啟動只設高水位（不回補歷史）。"""
    from l3_dispatcher import demo_journal as dj
    from l3_dispatcher import paper_journal as pj

    hwm = dj.get_high_water_mark()
    summary = {"hwm_before": hwm, "scanned": 0, "selected": 0,
               "placed": 0, "rejected": 0, "errors": 0}

    if hwm == 0:
        max_id = pj.max_paper_id()
        dj.set_high_water_mark(max_id)
        summary["first_run_hwm"] = max_id
        return summary                       # 本輪不開倉，下輪起只接未來新訊號

    rows = pj.get_signals_after(hwm, limit=SCAN_LIMIT)
    summary["scanned"] = len(rows)
    uni = await _okx_demo_universe(ex)        # v84 task#8：預過濾不可交易幣，不浪費額度
    if uni is not None:
        summary["universe_size"] = len(uni)
    selected, new_hwm = select_new_signals(rows, hwm, now_ms, tradable_symbols=uni)
    summary["selected"] = len(selected)

    if selected:
        bal = await ex.fetch_balance({"type": "swap"})
        avail = _free_usdt(bal)
        summary["avail_usd"] = round(avail, 2)
        for r in selected:
            try:
                placed = await _place_one(ex, r, avail_usd=avail, tg=tg)
                if placed.get("placed"):
                    summary["placed"] += 1
                    avail -= placed.get("margin_est", 0)   # 同輪粗扣，下輪 fetch_balance 校正
                else:
                    summary["rejected"] += 1
            except Exception as e:  # noqa: BLE001
                summary["errors"] += 1
                print(f"[demo_op] intake paper#{r.get('id')} 失敗：{type(e).__name__}: {e}")

    dj.set_high_water_mark(new_hwm)
    summary["hwm_after"] = dj.get_high_water_mark()
    return summary


async def run_demo_operator_cycle(*, now_ms=None, tg=None) -> dict:
    """一輪：鑰匙/kill-switch 閘 → 建全新 ex → 監控（先收斂既有）→ 進場（鏡像新訊號）。
    任一鑰匙沒開 / kill switch / demo_guard 設定不過 → 完全空轉、零 OKX 互動。"""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    from l3_dispatcher import demo_journal as dj
    from l4_execution import demo_guard, demo_trader as dt
    dj.init_db()
    out = {"ts": now_ms}

    if not is_active():
        out["skipped"] = f"inactive({ACTIVE_FLAG}!=1)"
        return out
    if dt.kill_switch_active():
        out["skipped"] = "kill_switch"
        return out
    try:
        demo_guard.ensure_demo_env()          # 鑰匙① + 實盤金鑰全空 + 模擬金鑰齊備
    except demo_guard.DemoGuardError as e:
        out["skipped"] = f"demo_guard:{e}"
        return out

    ex = None
    try:
        ex = await _make_ex()
        out["monitor"] = await _monitor(ex, now_ms=now_ms, tg=tg)
        halted, reason = dj.is_halted()
        if halted:
            out["intake_skipped"] = f"halted:{reason}"
        else:
            out["intake"] = await _intake(ex, now_ms=now_ms, tg=tg)
    except demo_guard.DemoGuardError as e:
        out["error"] = f"demo_guard:{e}"
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        if ex is not None:
            try:
                await ex.close()
            except Exception:  # noqa: BLE001
                pass
    return out


async def run_demo_operator_loop(interval_s: int = 180, tg=None):
    """daemon worker 主迴圈。startup sleep → while True: cycle / sleep。
    空轉（兩把鑰匙未開）時安靜不洗版；有錯誤才印。"""
    import asyncio
    await asyncio.sleep(20)
    while True:
        try:
            res = await run_demo_operator_cycle(tg=tg)
            if res.get("error"):
                print(f"[demo_op] cycle error: {res['error']}")
            elif not res.get("skipped"):
                mon = res.get("monitor", {})
                ink = res.get("intake", {})
                if any(mon.get(k) for k in ("filled", "closed", "expired",
                                            "timeout_initiated")) or ink.get("placed"):
                    print(f"[demo_op] cycle: monitor={mon} intake={ink}")
        except Exception as e:  # noqa: BLE001
            print(f"[demo_op] loop error: {type(e).__name__}: {e}")
        await asyncio.sleep(interval_s)


# ---------------------------------------------------------------------------
# 自測 / CLI
# ---------------------------------------------------------------------------
def _selftest() -> bool:  # noqa: C901
    cases: list[tuple[bool, str]] = []

    def check(cond, label):
        cases.append((bool(cond), label))

    HOUR = 3600 * 1000
    now = 1_000_000_000_000

    # is_crypto_signal
    check(is_crypto_signal("deepdive"), "deepdive 為加密訊號")
    check(not is_crypto_signal("us_breakout"), "us_breakout 排除")
    check(is_crypto_signal(None), "None setup 視為加密（保守收）")

    # infer_exit_reason
    check(infer_exit_reason("timeout", 5.0) == "timeout", "timeout marker → timeout")
    check(infer_exit_reason("", 5.0) == "tp", "正 PnL → tp")
    check(infer_exit_reason("", -3.0) == "stop", "負 PnL → stop")
    check(infer_exit_reason("", 0) == "stop", "0 PnL → stop（保守）")

    # pending_expired / needs_timeout_close
    check(pending_expired(now - 9 * HOUR, now, expiry_hours=8), "9h 掛單逾時")
    check(not pending_expired(now - 2 * HOUR, now, expiry_hours=8), "2h 掛單未逾時")
    check(needs_timeout_close(now - 25 * HOUR, None, now, time_limit_hours=24), "25h 持倉逾時(用 filled_at)")
    check(not needs_timeout_close(now - 1 * HOUR, None, now, time_limit_hours=24), "1h 持倉未逾時")
    check(needs_timeout_close(None, now - 30 * HOUR, now, time_limit_hours=24),
          "無 filled_at 退用 entry_at 計逾時")

    # classify_untracked_halt
    okx = [{"symbol": "BTC", "pos_side": "long", "contracts": 1},
           {"symbol": "ETH", "pos_side": "short", "contracts": 2}]
    tracked = {("BTC", "long")}
    halt, reason, untr = classify_untracked_halt(okx, tracked)
    check(halt and len(untr) == 1 and untr[0]["symbol"] == "ETH", "未追蹤 ETH 倉 → halt")
    halt2, _, _ = classify_untracked_halt(okx, {("BTC", "long"), ("ETH", "short")})
    check(not halt2, "全部追蹤 → 不 halt")

    # select_new_signals：基本選取 + 排除 + 高水位推進
    rows = [
        {"id": 10, "setup": "deepdive", "status": "open", "direction": "bull",
         "entry_price": 100, "stop_price": 95, "entry_at": now - 5 * 60 * 1000},   # 合格
        {"id": 11, "setup": "us_breakout", "status": "open", "direction": "bull",
         "entry_price": 50, "stop_price": 48, "entry_at": now - 5 * 60 * 1000},    # 美股排除
        {"id": 12, "setup": "deepdive", "status": "closed", "direction": "bear",
         "entry_price": 100, "stop_price": 105, "entry_at": now - 5 * 60 * 1000},  # 已平倉排除
        {"id": 13, "setup": "deepdive", "status": "open", "direction": "bull",
         "entry_price": 100, "stop_price": 95, "entry_at": now - 999 * 60 * 1000}, # 太舊排除
        {"id": 14, "setup": "deepdive", "status": "open", "direction": "bear",
         "entry_price": 200, "stop_price": 210, "entry_at": now - 1 * 60 * 1000},  # 合格
    ]
    sel, nhwm = select_new_signals(rows, hwm=5, now_ms=now, max_age_min=90, limit=5)
    sel_ids = [s["id"] for s in sel]
    check(sel_ids == [10, 14], f"選中合格 [10,14]（得 {sel_ids}）")
    check(nhwm == 14, f"全掃完高水位=14（得 {nhwm}）")

    # 額度滿 → 合格但延後者不被高水位跳過
    sel2, nhwm2 = select_new_signals(rows, hwm=5, now_ms=now, max_age_min=90, limit=1)
    check([s["id"] for s in sel2] == [10], "limit=1 只選 10")
    check(nhwm2 == 13, f"高水位停在 14 之前(=13，11/12/13 已決定不合格)（得 {nhwm2}）")

    # 已處理過的 id 不重選
    sel3, nhwm3 = select_new_signals(rows, hwm=13, now_ms=now, max_age_min=90, limit=5)
    check([s["id"] for s in sel3] == [14], "hwm=13 後只剩 14")
    check(nhwm3 == 14, "高水位推進到 14")

    # is_active（env 注入）
    check(is_active({"DEMO_OPERATOR_ACTIVE": "1"}), "ACTIVE=1 → on")
    check(not is_active({"DEMO_OPERATOR_ACTIVE": "0"}), "ACTIVE=0 → off")
    check(not is_active({}), "未設 → off（預設關）")

    passed = sum(1 for ok, _ in cases if ok)
    for ok, label in cases:
        print(f"  {'✅' if ok else '❌'} {label}")
    print(f"\ndemo_operator 自測：{passed}/{len(cases)} 通過")
    return passed == len(cases)


def _print_status() -> None:
    from l3_dispatcher import demo_journal as dj
    dj.init_db()
    print(f"{ACTIVE_FLAG} = {is_active()}  (env={os.getenv(ACTIVE_FLAG)!r})")
    print(f"high_water_mark = {dj.get_high_water_mark()}")
    halted, reason = dj.is_halted()
    print(f"halted = {halted}  {reason!r}")
    live = dj.get_live_demo_trades()
    print(f"live demo trades = {len(live)}")
    for t in live:
        print(f"  [{t['status']}] {t['symbol']} {t['direction']} "
              f"intent={t['intent_id']} entry_order={t['entry_order_id']}")
    print("stats(30d):", dj.get_demo_stats(30))
    print("phase0 closed(透明用，不改 ready):", dj.count_closed_for_phase0())


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="OKX 模擬盤自動操盤手")
    ap.add_argument("--selftest", action="store_true", help="離線測純決策（零下單）")
    ap.add_argument("--status", action="store_true", help="列帳本/高水位/halt（零下單）")
    ap.add_argument("--cycle-once", action="store_true",
                    help="連 OKX 跑一輪（受兩把鑰匙與 kill switch 約束）")
    args = ap.parse_args()

    # --status / --cycle-once 要連線/讀帳本 → 需 .env（金鑰、旗標）；--selftest 保持離線無 env。
    if args.status or getattr(args, "cycle_once", False):
        from dotenv import load_dotenv
        from botpaths import PROJECT_ROOT
        load_dotenv(PROJECT_ROOT / ".env")

    if args.selftest:
        raise SystemExit(0 if _selftest() else 1)
    elif args.status:
        _print_status()
    elif getattr(args, "cycle_once", False):
        import asyncio
        import json as _json
        result = asyncio.run(run_demo_operator_cycle())
        print(_json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        ap.print_help()
