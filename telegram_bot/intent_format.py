"""通用「交易意圖（trade-intent）」輸出層 — 機器版，與 message_format（人看版）並存同源。

設計依據：docs/research/v44-通用下單指令schema研究.md（多 agent 對抗式查證，40 條發現）。

核心觀念（為什麼是「意圖」不是「訂單」）：
    六家交易所（OKX/Binance/Gate/BingX/Bitget/Bybit）下單核心概念相同，但拼法全不同，
    且最基礎的「數量單位」就分裂成 張數 / 幣本位 / 反向張 三種互不相容世界。
    在「下單參數層」做通用 schema = 重寫一個 CCXT（leaky abstraction 重災區）。
    正確切法：輸出「意圖」——進場區、失效價、風險%、R 倍數目標——把張數/tick 進位/
    單向雙向/保證金模式全部留給交易所 adapter 在執行邊界解（借鏡 DeFi intent：宣告式而非命令式）。

    本模組只造「意圖描述 + 給人看的決策理由」（CCXT 不做的部分）；
    若日後要真的 resolve 成訂單參數，交給 CCXT 吃掉六套 REST 差異，本模組不碰。

⛔ 紅線（程式層硬性保證，不可違反）：
    本系統永不自動下實盤。execution_policy.mode 只允許 human_gated / demo_only，
    傳入 auto_live 直接 raise。intent 只是讓訊號「可被執行」，不是「自動執行」。

單位鐵則（呼應 v44 R 去美元化）：
    整份 intent 不出現「張數/contracts」，也不綁死固定 U 金額。
    曝險只用 risk.pct_of_account（帳戶%）+ entry_zone/invalidation（定義 1R）表達；
    張數/金額由交易員或 adapter 自己決定。

來源對照（同一個 decision_dict，render_fire_message 也吃它）：
    symbol/side ← snapshot.symbol / decision_dict.direction
    entry_zone  ← 進場區間計算（與 message_format 同邏輯）
    invalidation.price ← stop（CONFIG.sl_pct）
    risk.suggested_leverage ← choose_leverage()
    take_profits ← compute_tp_prices() + CONFIG.tp_size_split
    rationale ← composite_score / strength_score / confirmed[]
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any, Optional

from l2_trigger.leverage import choose_leverage, compute_position, compute_tp_prices

SCHEMA_NAME = "trade-intent"
SCHEMA_VERSION = "1.0"

# 紅線：永不自動下實盤。只允許這兩種執行政策。
ALLOWED_EXECUTION_MODES = ("human_gated", "demo_only")

# 各資產類 / setup 的執行參數預設（max_slippage、意圖有效時長）
_POLICY = {
    ("crypto_perp", "intraday"): {"slippage": 0.15, "ttl_h": 4},
    ("crypto_perp", "ambush"): {"slippage": 0.30, "ttl_h": 48},
    ("crypto_spot", "intraday"): {"slippage": 0.15, "ttl_h": 4},
    ("crypto_spot", "ambush"): {"slippage": 0.30, "ttl_h": 48},
    ("equity_signal", "intraday"): {"slippage": 0.30, "ttl_h": 96},
    ("equity_signal", "ambush"): {"slippage": 0.30, "ttl_h": 96},
}
_POLICY_DEFAULT = {"slippage": 0.30, "ttl_h": 24}


def _iso(ts_ms: int | None) -> str:
    """ms epoch → ISO8601 UTC（Z 結尾）。None → 現在。"""
    if ts_ms:
        t = dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc)
    else:
        t = dt.datetime.now(tz=dt.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _signal_note(sig: dict) -> str:
    """把一個 confirmed 訊號濃縮成一句機器/人都讀得懂的 note（不含 HTML）。"""
    name = sig.get("name", "")
    ev = sig.get("evidence", {}) or {}
    if name == "cvd_divergence":
        slope = ev.get("cvd_slope", 0) or 0
        div = {"bull": "看漲", "bear": "看跌", "none": "無"}.get(ev.get("divergence", ""), "")
        return f"{div}背離，斜率 {slope:+.3f}"
    if name == "funding":
        fund = (ev.get("funding") or 0) * 100
        regime = {"shorts_pay": "空方付錢", "overheated": "多頭過熱",
                  "neutral": "中性"}.get(ev.get("regime", ""), ev.get("regime", ""))
        return f"{fund:+.4f}%/8h（{regime}）"
    if name == "oi_trajectory":
        return "OI 軌跡確認"
    if name == "large_holder":
        return f"大戶={ev.get('top_trader', '—')} vs 散戶={ev.get('retail', '—')}"
    if name == "cvd_silent_accumulation":
        return f"7d CVD 斜率 {(ev.get('cvd_slope_7d', 0) or 0):+.3f}"
    if name == "large_holder_creeping":
        return f"7d 大戶持倉斜率 {(ev.get('top_trader_slope_7d', 0) or 0):+.4f}"
    # 退化：給出原始證據的短摘要
    return str(ev)[:80] if ev else ""


def _compute_plan(decision_dict: dict[str, Any]) -> dict[str, Any]:
    """重算進場區/止損/槓桿/倉位/止盈 — 與 telegram_bot/message_format.render_fire_message
    同邏輯、同來源（botconfig.CONFIG），確保「人看的卡片」與「機器 intent」永不打架。

    ⚠️ 若 message_format 的進場區/止損公式變動，這裡必須同步（selftest 會抓內部一致性，
    但跨檔同步靠人工：兩者都從 CONFIG 取數，差異只在進場區的 ± 百分比常數）。
    """
    from botconfig import CONFIG

    snap = decision_dict["snapshot"]
    direction = decision_dict["direction"]          # "bull"/"bear"
    setup = decision_dict["setup_name"]             # "intraday"/"ambush"
    sym = snap["symbol"]
    entry = snap["price"]

    sl_pct = CONFIG.sl_pct(setup)
    if direction == "bull":
        stop = round(entry * (1 - sl_pct / 100), 6)
    else:
        stop = round(entry * (1 + sl_pct / 100), 6)

    lev = choose_leverage(sym, snap.get("atr_pct_7d"))
    # 倉位用 1U 風險做「形狀」計算（只為拿 sl_distance_pct，金額不外洩 — R 去美元化）
    pos = compute_position(entry, stop, 1.0, lev)
    tp_r = CONFIG.tp_r(setup)
    tps = compute_tp_prices(entry, stop, direction, tp_r)

    # 進場區間（與 message_format 完全一致）
    if setup == "intraday":
        if direction == "bull":
            entry_low, entry_high = round(entry * 0.997, 6), round(entry * 1.002, 6)
        else:
            entry_low, entry_high = round(entry * 0.998, 6), round(entry * 1.003, 6)
    else:  # ambush
        if direction == "bull":
            entry_low, entry_high = round(entry * 0.985, 6), round(entry, 6)
        else:
            entry_low, entry_high = round(entry, 6), round(entry * 1.015, 6)

    return {
        "sym": sym, "direction": direction, "setup": setup, "entry": entry,
        "stop": stop, "lev": lev, "pos": pos, "tp_r": tp_r, "tps": tps,
        "entry_low": entry_low, "entry_high": entry_high,
        "sl_distance_pct": pos["sl_distance_pct"],
    }


def to_trade_intent(decision_dict: dict[str, Any], *,
                    asset_class: Optional[str] = None,
                    execution_policy: str = "human_gated") -> dict[str, Any]:
    """把 render_fire_message 吃的同一個 decision_dict 編譯成通用 trade-intent dict。

    asset_class: None → 自動判定（snapshot/decision 帶 asset_class 則用之，否則 crypto_perp）。
    execution_policy: 只允許 human_gated / demo_only（紅線）。
    """
    if execution_policy not in ALLOWED_EXECUTION_MODES:
        raise ValueError(
            f"execution_policy 只允許 {ALLOWED_EXECUTION_MODES}，"
            f"收到 {execution_policy!r}（本系統永不自動下實盤）")

    from botconfig import CONFIG

    snap = decision_dict["snapshot"]
    plan = _compute_plan(decision_dict)
    sym = plan["sym"]
    direction = plan["direction"]
    setup = plan["setup"]
    side = "long" if direction == "bull" else "short"

    # 資產類別判定
    if asset_class is None:
        asset_class = (decision_dict.get("asset_class")
                       or snap.get("asset_class") or "crypto_perp")

    # symbol 規範形：加密 BASE-USDT；美股維持代碼
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
    # intent_id：FIX ClOrdID 角色（去重/對帳）。同一訊號→同一 id（deterministic，便於 idempotent）。
    seed = f"{symbol_canonical}|{setup}|{ts_ms}|{plan['entry']}|{plan['stop']}"
    short = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]
    intent_id = f"ti_{created_at}_{sym}_{setup}_{short}"

    pol = _POLICY.get((asset_class, setup), _POLICY_DEFAULT)
    ref = plan["entry"]
    price_band_pct = round(abs(plan["entry_high"] - plan["entry_low"]) / ref * 100, 3) if ref else 0.0
    deadline = _iso((ts_ms or int(dt.datetime.now(tz=dt.timezone.utc).timestamp() * 1000))
                    + pol["ttl_h"] * 3600 * 1000)

    # 失效規則（多/空方向正確；不像舊卡片一律寫「跌破」）
    if direction == "bull":
        inval_rule = f"4h 收盤跌破 {plan['stop']}（1R 止損）"
    else:
        inval_rule = f"4h 收盤站上 {plan['stop']}（1R 止損）"

    # 風險：R 去美元化 — 用帳戶% 表達。% 模式給設定值；固定 $ 模式留 null（不外洩金額，交易員自設）。
    pct_of_account = CONFIG.risk_per_trade_pct if CONFIG.risk_per_trade_pct > 0 else None

    # 止盈（多段分批）
    tp_r = plan["tp_r"]
    tps = plan["tps"]
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

    # 決策理由（給人/LLM 上下文，不影響執行）
    signals = []
    for sig in decision_dict.get("confirmed", []):
        st = sig.get("state")
        if st in ("bull", "bear"):
            signals.append({
                "name": sig.get("name"),
                "state": st,
                "note": _signal_note(sig),
            })
    setup_zh = "日內爆發" if setup == "intraday" else "左側埋伏"
    dir_zh = "做多" if direction == "bull" else "做空"
    narrative = f"{sym} {setup_zh}；{dir_zh} setup（綜合分 {decision_dict.get('composite_score')}）"

    intent = {
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
            "low": plan["entry_low"],
            "high": plan["entry_high"],
            "reference": ref,
        },
        "invalidation": {"price": plan["stop"], "rule": inval_rule},
        "risk": {
            "pct_of_account": pct_of_account,
            "suggested_leverage": plan["lev"],
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
    return intent


# ── 輕量驗證器（零外部相依；不依賴 jsonschema 套件）────────────────────────────
_REQUIRED_TOP = (
    "schema", "version", "intent_id", "created_at", "asset_class",
    "symbol_canonical", "side", "order_type", "entry_zone", "invalidation",
    "risk", "acceptance", "execution_policy",
)
_ENUM = {
    "asset_class": ("crypto_perp", "crypto_spot", "equity_signal"),
    "side": ("long", "short"),
    "order_type": ("limit", "market"),
    "time_in_force": ("gtc", "ioc", "fok"),
}


def validate_intent(intent: dict[str, Any]) -> list[str]:
    """回傳問題清單；空 list = 通過。純結構/型別/enum/紅線檢查。"""
    problems: list[str] = []
    if not isinstance(intent, dict):
        return ["intent 必須是 dict"]
    for k in _REQUIRED_TOP:
        if k not in intent:
            problems.append(f"缺必填欄位：{k}")
    if intent.get("schema") != SCHEMA_NAME:
        problems.append(f"schema 必須是 {SCHEMA_NAME!r}")
    for k, allowed in _ENUM.items():
        if k in intent and intent[k] not in allowed:
            problems.append(f"{k}={intent[k]!r} 不在允許值 {allowed}")
    # entry_zone 結構與數值
    ez = intent.get("entry_zone", {})
    if isinstance(ez, dict):
        for f in ("low", "high", "reference"):
            if not isinstance(ez.get(f), (int, float)):
                problems.append(f"entry_zone.{f} 必須是數字")
        if isinstance(ez.get("low"), (int, float)) and isinstance(ez.get("high"), (int, float)):
            if ez["low"] > ez["high"]:
                problems.append("entry_zone.low 不可大於 high")
    else:
        problems.append("entry_zone 必須是物件")
    # invalidation
    inv = intent.get("invalidation", {})
    if not (isinstance(inv, dict) and isinstance(inv.get("price"), (int, float))):
        problems.append("invalidation.price 必須是數字")
    # risk
    risk = intent.get("risk", {})
    if isinstance(risk, dict):
        if "pct_of_account" not in risk:
            problems.append("risk.pct_of_account 必須存在（可為 null＝交易員自設）")
        lev = risk.get("suggested_leverage")
        if not (isinstance(lev, int) and 1 <= lev <= 50):
            problems.append("risk.suggested_leverage 必須是 1–50 的整數")
    else:
        problems.append("risk 必須是物件")
    # 紅線：execution_policy
    ep = intent.get("execution_policy", {})
    mode = ep.get("mode") if isinstance(ep, dict) else None
    if mode not in ALLOWED_EXECUTION_MODES:
        problems.append(f"⛔ execution_policy.mode={mode!r} 違反紅線（只允許 {ALLOWED_EXECUTION_MODES}）")
    # take_profits（選填，但有的話要結構正確）
    for i, tp in enumerate(intent.get("take_profits", []) or []):
        if not isinstance(tp.get("price"), (int, float)):
            problems.append(f"take_profits[{i}].price 必須是數字")
    return problems


def to_intent_json(decision_dict: dict[str, Any], **kw) -> str:
    """便捷：decision_dict → 美化 JSON 字串（ensure_ascii=False 保留中文）。"""
    return json.dumps(to_trade_intent(decision_dict, **kw),
                      ensure_ascii=False, indent=2)


# ── selftest ──────────────────────────────────────────────────────────────────
def _synthetic_decision(direction="bull", setup="intraday") -> dict:
    return {
        "direction": direction,
        "setup_name": setup,
        "composite_score": 2.31,
        "confirmed": [
            {"name": "cvd_divergence", "state": "bull",
             "evidence": {"divergence": "bull", "cvd_slope": 0.142}},
            {"name": "oi_trajectory", "state": "bull", "evidence": {}},
            {"name": "funding", "state": "neutral",
             "evidence": {"funding": 0.000042, "regime": "neutral"}},
        ],
        "snapshot": {
            "symbol": "BTC", "price": 64700.0, "atr_pct_7d": 4.2,
            "strength_score": 78, "ts": 1_750_000_000_000,
        },
    }


def _selftest() -> int:
    failures = 0

    def check(cond, msg):
        nonlocal failures
        if not cond:
            failures += 1
            print(f"  ✗ {msg}")
        else:
            print(f"  ✓ {msg}")

    print("[intent_format] selftest")
    # 1) 多單 intraday
    d = _synthetic_decision("bull", "intraday")
    it = to_trade_intent(d)
    probs = validate_intent(it)
    check(not probs, f"多單 intraday 通過驗證（problems={probs}）")
    check(it["side"] == "long", "direction=bull → side=long")
    check(it["symbol_canonical"] == "BTC-USDT", "symbol 規範形 BTC-USDT")
    check(it["asset_class"] == "crypto_perp", "預設 asset_class=crypto_perp")
    # R 結構內部一致：invalidation = stop；tp1 距離 = 1R = |entry-stop|
    entry = it["entry_zone"]["reference"]
    stop = it["invalidation"]["price"]
    r = abs(entry - stop)
    tp1 = it["take_profits"][0]
    check(abs((tp1["price"] - entry) - tp1["r_multiple"] * r) < 1e-6,
          "tp1 價位 = entry + r_multiple×1R（多單）")
    check(it["risk"]["pct_of_account"] is None or it["risk"]["pct_of_account"] > 0,
          "risk.pct_of_account 為 null 或正數（R 去美元化，無金額外洩）")
    check(1 <= it["risk"]["suggested_leverage"] <= 50, "建議槓桿落在 1–50x")
    # 2) 空單 ambush：失效規則應為「站上」
    d2 = _synthetic_decision("bear", "ambush")
    it2 = to_trade_intent(d2)
    check(not validate_intent(it2), "空單 ambush 通過驗證")
    check(it2["side"] == "short", "direction=bear → side=short")
    check("站上" in it2["invalidation"]["rule"], "空單失效規則用「站上」非「跌破」")
    tp1b = it2["take_profits"][0]
    e2, s2 = it2["entry_zone"]["reference"], it2["invalidation"]["price"]
    check(tp1b["price"] < e2, "空單 tp1 價位低於進場（方向正確）")
    # 3) 美股訊號：margin_mode=null、symbol 不加 -USDT
    d3 = _synthetic_decision("bull", "intraday")
    d3["snapshot"]["symbol"] = "NVDA"
    it3 = to_trade_intent(d3, asset_class="equity_signal")
    check(it3["margin_mode"] is None, "美股 margin_mode=null")
    check(it3["symbol_canonical"] == "NVDA", "美股 symbol 不加 -USDT")
    # 4) 紅線：auto_live 必須被擋
    try:
        to_trade_intent(d, execution_policy="auto_live")
        check(False, "auto_live 應該 raise（紅線）")
    except ValueError:
        check(True, "⛔ auto_live 被程式層硬擋（紅線通過）")
    # 5) 驗證器能抓壞 intent
    bad = dict(it)
    bad = json.loads(json.dumps(bad))
    bad["execution_policy"] = {"mode": "auto_live"}
    check(any("紅線" in p for p in validate_intent(bad)), "驗證器能抓出違反紅線的 intent")
    # 6) intent_id 對同一訊號穩定（idempotent）
    check(to_trade_intent(d)["intent_id"] == it["intent_id"],
          "同一訊號 → 同一 intent_id（去重/idempotent）")

    print(f"[intent_format] {'PASS' if not failures else f'FAIL ({failures})'}")
    return 1 if failures else 0


if __name__ == "__main__":
    import sys
    from pathlib import Path

    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    rc = _selftest()
    print("\n--- 範例 intent JSON（多單 intraday）---")
    print(to_intent_json(_synthetic_decision("bull", "intraday")))
    sys.exit(rc)
