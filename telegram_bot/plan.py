"""交易計畫「單一權威」層（canonical plan authority）。

task#10 雙受眾呈現層的地基。在此之前，「進場區 ± 帶 / 止損 / 槓桿 / 倉位 / 止盈」
這套計算散落三份各自重算的拷貝：
    • telegram_bot/message_format.render_fire_message（人看的卡片）
    • telegram_bot/intent_format._compute_plan（機器 intent）
    • l3_dispatcher/dispatcher.compute_entry_zone（帳本/監控用，且帶潛伏 bug：忽略 setup
      永遠用 intraday 帶、無 round、無 ambush 分支）
三份「靠人工同步」，任何一處改了 ± 常數另兩處不會跟著動 = 漂移隱患。

本模組把這套計算收斂成「一個權威」：
    build_canonical_plan(decision_dict)          → 規則型（intraday/ambush）
    canonical_from_deepdive(sym, llm_plan, ...)  → deepdive（用 LLM 計畫的絕對價，R 反推）
    canonical_to_intent(canonical, decision_dict) → 把 canonical 編成通用 trade-intent JSON

兩個 builder 回「同一種 canonical 形狀」，所以人看的卡片與機器 JSON 永遠同源、永不打架；
deepdive（目前唯一活躍的使用者卡）也終於能產出可餵交易所 AI agent 的機器 JSON。

⛔ 紅線守則：
    • 純呈現層（display-only）。零下單/訊號數學變更，碰不到執行層（紅線①）。
    • canonical_to_intent 沿用 intent_format 的硬性保證：execution_policy 只允許
      human_gated / demo_only，傳入 auto_live 直接 raise（本系統永不自動下實盤）。
    • deepdive→JSON 的資料只來自 paper_trades / LLM plan dict，絕不寫/讀真錢帳本 `trades`。

進場區 ± 帶的「唯一真值」就在下面這張 _ENTRY_BAND 表 —— 要改帶寬只改這裡一處。
"""
from __future__ import annotations

import datetime as dt
import hashlib
from typing import Any, Optional

from l2_trigger.leverage import choose_leverage, compute_position, compute_tp_prices

# 進場區間 ± 帶（單一真值）。key=(setup_class, direction) → (low_mult, high_mult)。
# setup_class = "intraday" if setup=="intraday" else "ambush"。
# 數值與舊三處拷貝逐位元相同（golden parity test 把關）：
#   intraday bull -0.3%~+0.2% / bear -0.2%~+0.3%
#   ambush   bull 支撐下方 1.5%~現價 / bear 現價~上方 1.5%
_ENTRY_BAND: dict[tuple[str, str], tuple[float, float]] = {
    ("intraday", "bull"): (0.997, 1.002),
    ("intraday", "bear"): (0.998, 1.003),
    ("ambush", "bull"): (0.985, 1.000),
    ("ambush", "bear"): (1.000, 1.015),
}

# setup → 中文短標（canonical_to_intent 的 narrative 用；deepdive 為新增分支）
_SETUP_ZH = {"intraday": "日內爆發", "ambush": "左側埋伏", "deepdive": "深度分析"}

# 各資產類 / setup 的執行參數預設（與 intent_format._POLICY 同源；缺項退 _POLICY_DEFAULT）
_POLICY = {
    ("crypto_perp", "intraday"): {"slippage": 0.15, "ttl_h": 4},
    ("crypto_perp", "ambush"): {"slippage": 0.30, "ttl_h": 48},
    ("crypto_perp", "deepdive"): {"slippage": 0.30, "ttl_h": 48},
    ("crypto_spot", "intraday"): {"slippage": 0.15, "ttl_h": 4},
    ("crypto_spot", "ambush"): {"slippage": 0.30, "ttl_h": 48},
    ("equity_signal", "intraday"): {"slippage": 0.30, "ttl_h": 96},
    ("equity_signal", "ambush"): {"slippage": 0.30, "ttl_h": 96},
}
_POLICY_DEFAULT = {"slippage": 0.30, "ttl_h": 24}

# 紅線：永不自動下實盤。只允許這兩種執行政策。
ALLOWED_EXECUTION_MODES = ("human_gated", "demo_only")

SCHEMA_NAME = "trade-intent"
SCHEMA_VERSION = "1.0"


def _entry_band(setup: str, direction: str, entry: float) -> tuple[float, float]:
    """回 (entry_low, entry_high)，已 round(.,6)。setup 非 intraday 一律歸 ambush 帶。"""
    setup_class = "intraday" if setup == "intraday" else "ambush"
    lo_mult, hi_mult = _ENTRY_BAND[(setup_class, direction)]
    return round(entry * lo_mult, 6), round(entry * hi_mult, 6)


def build_canonical_plan(decision_dict: dict[str, Any], *,
                         risk_usd: Optional[float] = None) -> dict[str, Any]:
    """規則型計畫（intraday/ambush）的單一權威計算。

    與舊 message_format.render_fire_message / intent_format._compute_plan **逐位元相同**：
        止損 = entry × (1 ∓ sl_pct/100)，round(.,6)
        槓桿 = choose_leverage(sym, atr_pct_7d)
        倉位 = compute_position(entry, stop, risk_usd, lev)
        止盈 = compute_tp_prices(entry, stop, dir, CONFIG.tp_r(setup))
        進場區 = _ENTRY_BAND 表

    risk_usd: None → CONFIG.risk_per_trade_usd（＝舊 message_format 口徑）；
              intent 層傳 1.0（R 去美元化「形狀」計算）。sl_distance_pct 與 risk_usd 無關，
              故卡片/JSON 統一不影響任何「可執行欄位」（零回歸的數學保證）。
    """
    from botconfig import CONFIG

    snap = decision_dict["snapshot"]
    direction = decision_dict["direction"]          # "bull"/"bear"
    setup = decision_dict["setup_name"]             # "intraday"/"ambush"
    sym = snap["symbol"]
    entry = snap["price"]

    if risk_usd is None:
        risk_usd = CONFIG.risk_per_trade_usd

    sl_pct = CONFIG.sl_pct(setup)
    if direction == "bull":
        stop = round(entry * (1 - sl_pct / 100), 6)
    else:
        stop = round(entry * (1 + sl_pct / 100), 6)

    lev = choose_leverage(sym, snap.get("atr_pct_7d"))
    pos = compute_position(entry, stop, risk_usd, lev)
    tp_r = CONFIG.tp_r(setup)
    tps = compute_tp_prices(entry, stop, direction, tp_r)
    entry_low, entry_high = _entry_band(setup, direction, entry)

    return {
        "sym": sym, "direction": direction, "setup": setup, "entry": entry,
        "stop": stop, "lev": lev, "pos": pos, "tp_r": tp_r, "tps": tps,
        "entry_low": entry_low, "entry_high": entry_high,
        "sl_distance_pct": pos["sl_distance_pct"],
    }


def canonical_from_deepdive(sym: str, llm_plan: dict[str, Any], *,
                            atr_pct_7d: Optional[float] = None,
                            risk_usd: Optional[float] = None) -> dict[str, Any]:
    """把 deepdive LLM 計畫編成同一種 canonical 形狀（用 LLM 的絕對價，不重算 CONFIG）。

    llm_plan: synthesizer._extract_plan_block 的輸出
        {actionable, direction(bull/bear), entry_type(market/limit),
         entry, entry_lo, entry_hi, stop, tp1, tp2, tp3}
    進場價解析與 macro._record_deepdive_plan 一致：limit 缺 entry → 用 (lo+hi)/2 中點。

    R 倍數由絕對價反推：r_i = |tp_i − entry| / |entry − stop|，round(.,2)。
    缺漏的 TP 依存在者順序壓縮重編號（tp1→tp2→tp3），避免 take_profits 與 tp_r 長度不一致。
    缺 atr → choose_leverage 退保守 5x（誠實降級；paper 帳沒存 atr）。

    任何結構性缺料（方向非 bull/bear、缺 stop、entry==stop）→ raise ValueError，
    由呼叫端 try/except 安全降級（與 _record_deepdive_plan 的「回 None 不阻塞」同精神）。
    """
    from botconfig import CONFIG

    direction = llm_plan.get("direction")
    if direction not in ("bull", "bear"):
        raise ValueError(f"deepdive plan direction 必須 bull|bear，收到 {direction!r}")

    entry = llm_plan.get("entry")
    lo, hi = llm_plan.get("entry_lo"), llm_plan.get("entry_hi")
    is_limit = (llm_plan.get("entry_type") == "limit") and lo is not None and hi is not None
    if entry is None and is_limit:
        entry = (lo + hi) / 2
    stop = llm_plan.get("stop")
    if entry is None or stop is None:
        raise ValueError("deepdive plan 缺 entry/stop，無法建 canonical")

    risk_dist = abs(entry - stop)
    if risk_dist == 0:
        raise ValueError("deepdive plan entry == stop，1R 為 0，無法建 canonical")

    if risk_usd is None:
        risk_usd = CONFIG.risk_per_trade_usd
    lev = choose_leverage(sym, atr_pct_7d)
    pos = compute_position(entry, stop, risk_usd, lev)

    # TP：用 LLM 的絕對價，R 反推；缺漏壓縮重編號（永遠連續 tp1..tpN）
    tps: dict[str, float] = {}
    tp_r_list: list[float] = []
    idx = 0
    for raw in (llm_plan.get("tp1"), llm_plan.get("tp2"), llm_plan.get("tp3")):
        if raw is None:
            continue
        idx += 1
        r = round(abs(raw - entry) / risk_dist, 2)
        tps[f"tp{idx}"] = round(raw, 6)
        tps[f"tp{idx}_r"] = r
        tp_r_list.append(r)

    # 進場區：limit → LLM 的 lo/hi；market → 退化成單點（entry, entry）
    if is_limit:
        entry_low, entry_high = round(lo, 6), round(hi, 6)
    else:
        entry_low = entry_high = round(entry, 6)

    return {
        "sym": sym, "direction": direction, "setup": "deepdive",
        "entry": round(entry, 6), "stop": round(stop, 6), "lev": lev, "pos": pos,
        "tp_r": tuple(tp_r_list), "tps": tps,
        "entry_low": entry_low, "entry_high": entry_high,
        "sl_distance_pct": pos["sl_distance_pct"],
    }


def _iso(ts_ms: int | None) -> str:
    """ms epoch → ISO8601 UTC（Z 結尾）。None → 現在。"""
    if ts_ms:
        t = dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc)
    else:
        t = dt.datetime.now(tz=dt.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_to_intent(canonical: dict[str, Any],
                        decision_dict: dict[str, Any], *,
                        asset_class: Optional[str] = None,
                        execution_policy: str = "human_gated") -> dict[str, Any]:
    """把一個 canonical 計畫 + 其 decision 上下文編成通用 trade-intent dict。

    這是 intent 組裝的「單一權威」：intent_format.to_trade_intent 將委派到此
    （golden parity test 證明逐位元等價），deepdive→JSON 也走同一條組裝路徑。

    ⛔ 紅線：execution_policy 只允許 human_gated / demo_only，傳入其他（如 auto_live）直接 raise。
    """
    if execution_policy not in ALLOWED_EXECUTION_MODES:
        raise ValueError(
            f"execution_policy 只允許 {ALLOWED_EXECUTION_MODES}，"
            f"收到 {execution_policy!r}（本系統永不自動下實盤）")

    from botconfig import CONFIG
    from telegram_bot.intent_format import _signal_note

    snap = decision_dict["snapshot"]
    sym = canonical["sym"]
    direction = canonical["direction"]
    setup = canonical["setup"]
    side = "long" if direction == "bull" else "short"

    if asset_class is None:
        asset_class = (decision_dict.get("asset_class")
                       or snap.get("asset_class") or "crypto_perp")

    if asset_class == "equity_signal":
        symbol_canonical = sym
        margin_mode = None
    elif asset_class == "crypto_spot":
        symbol_canonical = f"{sym}-USDT"
        margin_mode = None
    else:  # crypto_perp
        symbol_canonical = f"{sym}-USDT"
        margin_mode = "isolated"

    ts_ms = snap.get("ts", 0)
    created_at = _iso(ts_ms)
    seed = f"{symbol_canonical}|{setup}|{ts_ms}|{canonical['entry']}|{canonical['stop']}"
    short = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]
    intent_id = f"ti_{created_at}_{sym}_{setup}_{short}"

    pol = _POLICY.get((asset_class, setup), _POLICY_DEFAULT)
    ref = canonical["entry"]
    price_band_pct = round(abs(canonical["entry_high"] - canonical["entry_low"]) / ref * 100, 3) if ref else 0.0
    deadline = _iso((ts_ms or int(dt.datetime.now(tz=dt.timezone.utc).timestamp() * 1000))
                    + pol["ttl_h"] * 3600 * 1000)

    if direction == "bull":
        inval_rule = f"4h 收盤跌破 {canonical['stop']}（1R 止損）"
    else:
        inval_rule = f"4h 收盤站上 {canonical['stop']}（1R 止損）"

    pct_of_account = CONFIG.risk_per_trade_pct if CONFIG.risk_per_trade_pct > 0 else None

    tp_r = canonical["tp_r"]
    tps = canonical["tps"]
    split = CONFIG.tp_size_split
    actions = [
        f"平 {split[0] * 100:.0f}% + 止損移到開倉價",
        f"平 {split[1] * 100:.0f}%",
        f"平剩餘 {split[2] * 100:.0f}% 或移動止損",
    ]
    take_profits = []
    for i in range(len(tp_r)):
        take_profits.append({
            "r_multiple": tp_r[i],
            "price": tps[f"tp{i + 1}"],
            "size_pct": round(split[i] * 100) if i < len(split) else None,
            "action": actions[i] if i < len(actions) else "平剩餘",
        })

    signals = []
    for sig in decision_dict.get("confirmed", []):
        st = sig.get("state")
        if st in ("bull", "bear"):
            signals.append({
                "name": sig.get("name"),
                "state": st,
                "note": _signal_note(sig),
            })
    setup_zh = _SETUP_ZH.get(setup, setup)
    dir_zh = "做多" if direction == "bull" else "做空"
    narrative = f"{sym} {setup_zh}；{dir_zh} setup（綜合分 {decision_dict.get('composite_score')}）"

    return {
        "schema": SCHEMA_NAME,
        "version": SCHEMA_VERSION,
        "intent_id": intent_id,
        "created_at": created_at,
        "asset_class": asset_class,
        "symbol_canonical": symbol_canonical,
        "venue_hint": None,
        "side": side,
        "order_type": "limit",
        "time_in_force": "gtc",
        "post_only": False,
        "reduce_only": False,
        "margin_mode": margin_mode,
        "entry_zone": {
            "low": canonical["entry_low"],
            "high": canonical["entry_high"],
            "reference": ref,
        },
        "invalidation": {"price": canonical["stop"], "rule": inval_rule},
        "risk": {
            "pct_of_account": pct_of_account,
            "suggested_leverage": canonical["lev"],
            "max_slippage_pct": pol["slippage"],
        },
        "take_profits": take_profits,
        "acceptance": {
            "price_band_pct": price_band_pct,
            "deadline": deadline,
            "max_slippage_pct": pol["slippage"],
        },
        "rationale": {
            "composite_score": decision_dict.get("composite_score"),
            "strength_score": snap.get("strength_score"),
            "signals": signals,
            "narrative": narrative,
        },
        "execution_policy": {"mode": execution_policy},
    }


def intent_from_deepdive_paper(paper: dict[str, Any], *,
                               asset_class: Optional[str] = None,
                               execution_policy: str = "human_gated") -> dict[str, Any]:
    """把一筆 deepdive 紙上計畫（paper_journal.get_latest_deepdive_plan 的輸出）編成
    通用 trade-intent dict。deepdive 卡「📋 複製 JSON」按鈕 / /intent 後備走此路。

    deepdive 唯一活躍卡的機器 JSON 在此之前是「死的」：get_signal_for_intent 只讀真錢帳本
    trades（恆空，deepdive 只寫 paper_trades）。本函式從 paper 重建的計畫直接組裝 intent，
    與 build_canonical_plan→canonical_to_intent 同一條組裝權威，人看卡與機器 JSON 仍同源。

    ⛔ 紅線①：輸入只來自 paper_trades 重建的 plan dict（呼叫端保證），永不碰真錢帳本 trades。
    """
    # v205：進場階梯讀不出來的計畫，永遠不得編成可執行 intent。呼叫端各自都有攔截，
    #   這裡是第二道（防未來新增的呼叫端漏掉）——canonical_from_deepdive 會把
    #   entry_type 非 limit 的計畫退化成單點市價，那正是要避免的假確定。
    if paper.get("entry_zone_status") == "unreadable":
        raise ValueError(
            "deepdive plan 的進場階梯讀不出來（entry_splits 壞檔）——這不等於它是市價單，"
            f"拒絕產生可執行 JSON：{paper.get('entry_zone_error')}")
    sym = paper.get("symbol")
    canonical = canonical_from_deepdive(sym, paper)
    snap = {
        "symbol": sym,
        "price": canonical["entry"],
        "ts": paper.get("entry_at") or 0,
        "strength_score": None,
    }
    decision = {
        "direction": canonical["direction"],
        "setup_name": "deepdive",
        "composite_score": None,
        "confirmed": [],
        "snapshot": snap,
    }
    return canonical_to_intent(canonical, decision,
                               asset_class=asset_class,
                               execution_policy=execution_policy)
