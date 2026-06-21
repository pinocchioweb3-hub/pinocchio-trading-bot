"""把 TriggerDecision dict 渲染成 Telegram HTML 中文訊息。

技術術語保留英文以求精確（CVD、OI、TP1/2/3、R 等），描述全中文。
HTML 比 MarkdownV2 易處理，只需 escape <, >, &。
"""
from __future__ import annotations

import datetime as dt
from typing import Any


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# 訊號名稱中文化
_SIGNAL_LABEL = {
    "cvd_divergence": "CVD 背離",
    "funding": "資金費率",
    "large_holder": "大戶 vs 散戶",
    "oi_trajectory": "OI 軌跡",
    "btc_gate": "BTC 閘",
    "in_hot": "強勢清單",
    "trend_4h": "4h 趨勢",
    "cvd_silent_accumulation": "CVD 靜默吸籌",
    "large_holder_creeping": "大戶緩進",
    "atr_coiling": "ATR 收斂",
    "volume_drying": "量能枯竭",
    "oi_steady": "OI 穩定",
    "higher_lows": "高低點抬升",
}


def _fmt_signal_evidence(sig: dict) -> str:
    name = sig["name"]
    ev = sig.get("evidence", {})

    def g(k, default="—"):
        v = ev.get(k)
        return f"{v}" if v is not None else default

    if name == "cvd_divergence":
        slope = ev.get("cvd_slope", 0) or 0
        div = ev.get("divergence", "—")
        div_zh = {"bull": "看漲", "bear": "看跌", "none": "無"}.get(div, div)
        return f"{div_zh}背離，斜率 {slope:+.3f}"

    if name == "funding":
        fund = (ev.get("funding") or 0) * 100
        regime = ev.get("regime", "—")
        regime_zh = {"shorts_pay": "空方付錢", "overheated": "多頭過熱",
                     "neutral": "中性"}.get(regime, regime)
        return f"{fund:+.4f}% / 8h（{regime_zh}）"

    if name == "large_holder":
        return f"大戶={g('top_trader')} vs 散戶={g('retail')}"

    if name == "cvd_silent_accumulation":
        s = ev.get("cvd_slope_7d", 0) or 0
        return f"7d CVD 斜率 {s:+.3f}"

    if name == "large_holder_creeping":
        s = ev.get("top_trader_slope_7d", 0) or 0
        return f"7d 大戶持倉斜率 {s:+.4f}"

    return _esc(str(ev)[:80])


def render_fire_message(decision_dict: dict[str, Any]) -> tuple[str, list[list[dict]]]:
    """渲染 FIRE 訊息 → (HTML text, inline buttons)。中文版。"""
    snap = decision_dict["snapshot"]
    sym = snap["symbol"]
    direction = decision_dict["direction"]
    setup = decision_dict["setup_name"]
    score = decision_dict["composite_score"]

    emoji = "🟢" if setup == "intraday" else "🟡"
    setup_zh = "日內爆發" if setup == "intraday" else "左側埋伏"
    dir_emoji = "📈" if direction == "bull" else "📉"
    dir_zh = "做多" if direction == "bull" else "做空"

    # === 風控數字（單一權威：telegram_bot/plan.build_canonical_plan）===
    # v76 task#10：進場區/止損/槓桿/倉位/止盈不再在此重算 —— 與機器 intent、帳本同源於
    # plan.py，終結三處 ± 常數拷貝的漂移隱患。卡片口徑用預設 risk_usd（CONFIG.risk_per_trade_usd）；
    # sl_distance_pct 與金額無關，故與機器 intent 的 1.0 口徑顯示完全一致。
    from botconfig import CONFIG
    from telegram_bot.plan import build_canonical_plan
    plan = build_canonical_plan(decision_dict)
    entry = plan["entry"]
    stop = plan["stop"]
    pos = plan["pos"]
    tp_r = plan["tp_r"]
    tps = plan["tps"]
    entry_low = plan["entry_low"]
    entry_high = plan["entry_high"]

    # === 訊號描述 ===
    sig_lines = []
    for sig in decision_dict.get("confirmed", []):
        state = sig["state"]
        if state in ("bull", "bear"):
            sig_emoji = "📈" if state == "bull" else "📉"
            label = _SIGNAL_LABEL.get(sig["name"], sig["name"])
            sig_lines.append(f"   {sig_emoji} <b>{_esc(label)}</b>:{_fmt_signal_evidence(sig)}")
    sig_block = "\n".join(sig_lines) if sig_lines else "   （無方向確認）"

    # === 時間戳 ===
    ts = snap.get("ts", 0)
    if ts:
        timestamp = dt.datetime.fromtimestamp(ts / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    else:
        timestamp = "現在"

    strength = snap.get("strength_score")
    strength_str = f"{strength}" if strength is not None else "—"

    text = (
        f"{emoji} <b>{_esc(sym)}/USDT</b>  [{setup_zh}]\n"
        f"{dir_emoji} <b>{dir_zh}</b>  綜合分=<code>{score:+.2f}</code>  "
        f"強勢分=<code>{strength_str}</code>\n"
        f"⚡ <b>可立即執行</b> — 現價在進場區內，"
        f"{'掛限價或市價' + dir_zh if setup == 'intraday' else '分批限價埋伏'}皆可\n"
        f"🕐 {timestamp}   主時框 4h／進場 1h\n"
        "\n"
        "📊 <b>觸發原因</b>\n"
        f"{sig_block}\n"
        "\n"
        "🎯 <b>倉位配置（R 倍數制 — 本金、倉位、槓桿由你自己定）</b>\n"
        f"   進場區間：<code>${entry_low}</code> – <code>${entry_high}</code>"
        f"  <i>(分 2 段掛限價)</i>\n"
        f"   訊號參考價：<code>${entry}</code>\n"
        f"   止損：<code>${stop}</code>　進場→止損 = <b>1R</b>（價差 {pos['sl_distance_pct']}%）\n"
        f"   <i>R 是「一個風險單位」，不是固定金額。你願意為這筆冒多少、倉位開多大、"
        f"開幾倍槓桿，全由你自己決定。</i>\n"
        f"   <i>常見做法：把單筆風險固定成總倉的 2% / 2.5% / 3% / 5%，自己挑一個並長期守住。</i>\n"
        "\n"
        "🎯 <b>止盈（三段分批）</b>\n"
        f"   TP1（{tp_r[0]}R）：<code>${tps['tp1']}</code> → "
        f"平 {CONFIG.tp_size_split[0]*100:.0f}%、止損移到開倉價\n"
        f"   TP2（{tp_r[1]}R）：<code>${tps['tp2']}</code> → "
        f"平 {CONFIG.tp_size_split[1]*100:.0f}%\n"
        f"   TP3（{tp_r[2]}R）：<code>${tps['tp3']}</code> → "
        f"平剩餘 {CONFIG.tp_size_split[2]*100:.0f}% 或移動止損\n"
    )

    if setup == "ambush":
        text += "\n💡 <i>左側埋伏注意：前 36h 沒爆量是正常震盪，不要被嚇出。爆量+OI 拉升才是真啟動。</i>\n"

    text += f"\n❌ <b>失效條件</b>：4h 收盤跌破 <code>${stop}</code>"

    # v29 第5層：訊號 vs 主導敘事一致性（世界因果背景餵回交易）
    try:
        from news_feed.narrative_engine import narrative_alignment
        text += narrative_alignment(sym, direction)
    except Exception:
        pass

    buttons = [
        [
            {"text": "✅ 已下單", "callback_data": f"filled:{sym}:{setup}"},
            {"text": "⏭ 略過",   "callback_data": f"skip:{sym}:{setup}"},
        ],
        [{"text": "📊 看詳細數據", "callback_data": f"details:{sym}:{setup}"}],
    ]
    return text, buttons


def render_heartbeat(snapshots: list[dict], fires_this_cycle: int) -> str:
    """掃描週期心跳：即使沒 FIRE 你也知道機器人活著。"""
    now = dt.datetime.utcnow().strftime("%H:%M UTC")
    lines = [f"📡 <b>掃描 {now}</b>  觸發數=<code>{fires_this_cycle}</code>"]
    for s in snapshots:
        sym = s["symbol"]
        price = s.get("price")
        gate = s.get("btc_gate_open")
        gate_icon = "🟢" if gate else ("🔴" if gate is False else "⚪️")
        funding = s.get("funding")
        funding_str = f"{funding * 100:+.3f}%" if funding is not None else "—"
        price_str = f"${price}" if price is not None else "—"
        # 過熱費率打警示
        if funding is not None and abs(funding) >= 0.0008:
            funding_str += " ⚠️"
        lines.append(f"   {gate_icon} <b>{_esc(sym)}</b>  價={price_str}  費率={funding_str}")

    # 加上 BTC 閘狀態說明
    if snapshots:
        btc_open = next((s.get("btc_gate_open") for s in snapshots if s["symbol"] == "BTC"), None)
        if btc_open is False:
            lines.append("\n🔴 <i>BTC 閘關閉中（4h 跌破 200MA）→ 所有 setup 暫停做多</i>")
        elif btc_open is True:
            lines.append("\n🟢 <i>BTC 閘開啟（4h 站上 200MA）→ 系統可正常運作</i>")

    return "\n".join(lines)


def render_startup(backend: str, watchlist: list[str], interval_s: int) -> str:
    backend_zh = {"mock": "模擬數據", "coinglass": "CoinGlass 真實",
                  "local": "本地 TimescaleDB"}.get(backend, backend)
    # v23-6: 啟用策略與風控參數改讀真實來源（registry + botconfig）
    try:
        from l2_trigger.registry import enabled_strategies
        # v55-2: 橫幅誠實化 — 帶上成熟度標記，避免把實驗性策略（如美股代幣突破）
        #         與已驗證策略（日內爆發）並列得像同級。其他面（/strategies、/settings）本就有標。
        _mi = {"live": "🟢", "paper": "🧪", "experimental": "🔬"}
        _ml = {"live": "🟢 已驗證", "paper": "🧪 紙上實驗", "experimental": "🔬 實驗中·純紙上"}
        _ens = enabled_strategies()
        strat = "、".join(f"{m.display_name_zh}{_mi.get(m.maturity, '')}"
                          for m in _ens) or "（無）"
        _mats: list[str] = []
        for m in _ens:
            if m.maturity not in _mats:
                _mats.append(m.maturity)
        strat_legend = "　".join(_ml[x] for x in _mats if x in _ml)
    except Exception:
        strat = "日內爆發🟢"
        strat_legend = ""
    try:
        from botconfig import CONFIG
        if CONFIG.risk_per_trade_pct > 0:
            rtxt = f"帳戶 {CONFIG.risk_per_trade_pct:g}%（≈${CONFIG.risk_per_trade_usd:.0f}）"
        else:
            rtxt = f"${CONFIG.risk_per_trade_usd:.0f}"
        risk_line = (f"   單筆風險：<code>{rtxt}</code>（1R），"
                     f"最多 <code>{CONFIG.max_concurrent_trades}</code> 倉位\n")
    except Exception:
        risk_line = ""
    # v27: 全市場框架 — 不再列固定種子幣，掃描器涵蓋全 OKX 永續，訊號層動態 Top N
    try:
        from l3_dispatcher.market_scanner import get_latest_breadth
        b = get_latest_breadth()
        market_n = b["n_total"] if b else "—"
        scanned_n = b.get("n_scanned") if b else None
    except Exception:
        market_n = "—"
        scanned_n = None
    n = len(watchlist)
    # 全市場掃描＝原始掃描檔數（scanned_n，~372）；其中 market_n 檔達 $10M 流動性。
    # 舊版只印 market_n 卻標「全市場」→ 看起來像縮水，這裡誠實分開顯示。
    if scanned_n:
        scan_line = (f"   全市場掃描：<code>OKX 永續 {scanned_n} 檔</code>"
                     f"（{market_n} 檔達 $10M 流動性，異常即時偵測）\n")
    else:
        scan_line = f"   全市場掃描：<code>OKX 永續 {market_n} 檔達流動性</code>（異常即時偵測）\n"
    return (
        "🤖 <b>交易機器人上線</b>\n"
        f"   數據後端：<code>{_esc(backend_zh)}</code>\n"
        + scan_line +
        f"   訊號層：<code>動態 Top {n}</code>（依強勢排名，非固定幣種）\n"
        f"   掃描間隔：<code>{interval_s} 秒</code>\n"
        f"   啟用策略：<code>{_esc(strat)}</code>\n"
        + (f"     <i>{_esc(strat_legend)}</i>\n" if strat_legend else "")
        + risk_line +
        "\n🛰 開始監看市場...（/settings 自訂風險與策略 ｜ /strategies 看策略清單）"
    )


def render_health_alert(check) -> str:
    """Supervisor 健康異常警報。"""
    icon = "🚨" if check.severity == "alert" else "⚠️"
    sev_zh = {"alert": "嚴重", "warn": "警告", "info": "提示"}.get(check.severity, check.severity)
    kind_zh = {
        "source_down": "資料來源失聯",
        "source_exception": "資料來源異常",
        "queue_jammed": "訊號隊伍塞車",
        "dispatch_failures": "訊號送達失敗",
        "scheduler_stalled": "排程卡住",
        "data_quality_low": "資料品質下降",
    }.get(check.kind, check.kind)
    return (
        f"{icon} <b>系統監督警報 [{sev_zh}]</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"類型：<code>{_esc(kind_zh)}</code>\n"
        f"訊息：{_esc(check.message)}\n"
        f"細節：<code>{_esc(str(check.detail)[:200])}</code>\n"
        f"（30 分鐘內同類型警報只推一次）"
    )


def render_confidence_badge(confidence: int) -> str:
    if confidence >= 80: return "🟢 高信心"
    if confidence >= 60: return "🟡 中等信心"
    if confidence >= 40: return "🟠 低信心"
    return "🔴 極低信心"


def render_fire_with_checks(decision_dict: dict, check_result) -> tuple[str, list[list[dict]]]:
    """渲染 FIRE 訊息 + cross-check 結果。"""
    base_text, buttons = render_fire_message(decision_dict)
    # 加 cross-check 區塊
    badge = render_confidence_badge(check_result.confidence)
    addon = [
        "",
        f"🔍 <b>Cross-Check：{badge}（{check_result.confidence}/100）</b>",
    ]
    for c in check_result.checks:
        passed = c.get("pass", True)
        delta = c.get("delta", 0)
        icon = "✅" if passed and delta >= 0 else ("⚠️" if not passed else "ℹ️")
        addon.append(f"   {icon} {_esc(c.get('note', ''))}")
    return base_text + "\n".join(addon), buttons


def render_macro_report(state: dict, watchlist) -> str:
    """每小時宏觀分析報告 → Telegram HTML 中文。"""
    metrics = state["metrics"]
    extras = state["extras"]
    regime = state["regime"]
    advice = state["regime_advice"]
    eth_btc = state["eth_btc_ratio"]
    now = state["ts"].strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"📊 <b>宏觀分析  {now}</b>",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"🌐 <b>市場 Regime</b>：{advice['color']} {_esc(advice['label'])}",
        "",
        "<b>📍 指標層（規範市場走勢）</b>",
    ]

    for sym in watchlist.indicator:
        m = metrics.get(sym, {})
        e = extras.get(sym, {})
        if m.get("error"):
            lines.append(f"   ⚠️ {sym}: {_esc(m['error'])[:60]}")
            continue
        cur = m.get("current_price", 0)
        r7 = m.get("return_7d_pct", "—")
        r30 = m.get("return_30d_pct", "—")
        r90 = m.get("return_90d_pct", "—")
        dd = m.get("drawdown_from_high_pct", "—")
        ma50 = m.get("ma50")
        above_50 = m.get("above_ma50")
        above_90 = m.get("above_ma90")

        ma_emoji = "🟢" if (above_50 and above_90) else ("🟡" if above_90 or above_50 else "🔴")

        lines.append(f"   {ma_emoji} <b>{sym}</b>  <code>${cur}</code>")
        if isinstance(r7, (int, float)):
            lines.append(f"      近7d <code>{r7:+.1f}%</code>  近30d <code>{r30 if isinstance(r30, str) else f'{r30:+.1f}%'}</code>  近90d <code>{r90 if isinstance(r90, str) else f'{r90:+.1f}%'}</code>")
        if isinstance(dd, (int, float)):
            high = m.get("window_high", 0)
            lines.append(f"      距期內高 <code>${high}</code>：<code>{dd:+.1f}%</code>")
        if ma50 is not None:
            lines.append(f"      50d MA <code>${ma50}</code>　90d MA <code>${m.get('ma90', '—')}</code>")
        if e.get("funding") is not None:
            fund_pct = e["funding"] * 100
            warn = " ⚠️" if abs(fund_pct) >= 0.08 else ""
            lines.append(f"      資金費率 <code>{fund_pct:+.4f}%</code>/8h{warn}")

    # ETH/BTC ratio
    if eth_btc is not None:
        lines.append("")
        lines.append(f"📐 ETH/BTC = <code>{eth_btc}</code>")

    # 現貨層
    if watchlist.spot:
        lines.append("")
        lines.append("<b>💰 現貨層（你的倉位）</b>")
        for sym in watchlist.spot:
            m = metrics.get(sym, {})
            e = extras.get(sym, {})
            if m.get("error"):
                lines.append(f"   ⚠️ {sym}: 拉取失敗")
                continue
            cur = m.get("current_price", 0)
            r7 = m.get("return_7d_pct", "—")
            r30 = m.get("return_30d_pct", "—")
            dd = m.get("drawdown_from_high_pct", "—")
            fund = e.get("funding")
            fund_str = f"{fund * 100:+.4f}%" if fund is not None else "—"
            fund_warn = " ⚠️過熱" if fund is not None and abs(fund) >= 0.0008 else ""
            lines.append(f"   <b>{sym}</b>  <code>${cur}</code>  資金費率 <code>{fund_str}</code>/8h{fund_warn}")
            if isinstance(r7, (int, float)):
                lines.append(f"      近7d <code>{r7:+.1f}%</code>  近30d <code>{r30 if isinstance(r30, str) else f'{r30:+.1f}%'}</code>")
            if isinstance(dd, (int, float)):
                lines.append(f"      距期內高：<code>{dd:+.1f}%</code>")

    # 交易層
    if watchlist.trading:
        lines.append("")
        lines.append(f"<b>🎯 動態交易層（Top {len(watchlist.trading)}）</b>")
        for i, sym in enumerate(watchlist.trading, 1):
            if sym in watchlist.spot:
                continue   # 現貨已單列
            lines.append(f"   {i}. <code>{sym}</code>")

    # ETF 機構流向
    etf_btc = state.get("etf_btc", {})
    etf_eth = state.get("etf_eth", {})
    if not etf_btc.get("error") or not etf_eth.get("error"):
        lines.append("")
        lines.append("<b>🏛 ETF 機構流向（7日累計）</b>")
        if not etf_btc.get("error"):
            cum = etf_btc.get("cumulative_7d_flow_usd", 0)
            d24 = etf_btc.get("latest_24h_flow_usd", 0)
            icon = "🟢" if cum > 0 else "🔴"
            lines.append(f"   {icon} BTC ETF: 7d <code>${cum/1e6:+,.0f}M</code>  24h <code>${d24/1e6:+,.0f}M</code>")
        if not etf_eth.get("error"):
            cum = etf_eth.get("cumulative_7d_flow_usd", 0)
            d24 = etf_eth.get("latest_24h_flow_usd", 0)
            icon = "🟢" if cum > 0 else "🔴"
            lines.append(f"   {icon} ETH ETF: 7d <code>${cum/1e6:+,.0f}M</code>  24h <code>${d24/1e6:+,.0f}M</code>")

    # 情緒指標
    sent = state.get("sentiment", {})
    if not sent.get("error"):
        lines.append("")
        lines.append("<b>📈 市場情緒/估值</b>")
        fg = sent.get("fear_greed_now")
        fg_label = sent.get("fear_greed_label", "—")
        ahr = sent.get("ahr999_now")
        ahr_label = sent.get("ahr999_label", "—")
        if fg is not None:
            lines.append(f"   Fear & Greed: <code>{fg}</code> ({_esc(fg_label)})")
        if ahr is not None:
            lines.append(f"   AHR999 (BTC 估值): <code>{ahr}</code> ({_esc(ahr_label)})")

    # 清算掃描（前 5 大）
    liq = state.get("liq_scan", {})
    if not liq.get("error"):
        items = liq.get("items", [])[:5]
        if items:
            lines.append("")
            lines.append("<b>💥 全市場清算前 5（24h）</b>")
            for it in items:
                sym = it.get("symbol")
                total = it.get("total_24h", 0)
                imb = it.get("imbalance", 0)
                tag = " 軋空燃料" if imb > 0.3 else (" 多殺多" if imb < -0.3 else "")
                lines.append(f"   <code>{sym:6}</code> 清算 <code>${total/1e6:.1f}M</code>  imb=<code>{imb:+.2f}</code>{tag}")

    # Hyperliquid 鯨魚淨倉位（每幣 long/short 失衡）
    whales = state.get("whales", {})
    if not whales.get("error"):
        per_sym = whales.get("per_symbol_aggregate", [])[:5]
        if per_sym:
            lines.append("")
            lines.append("<b>🐋 Hyperliquid 鯨魚淨倉（前 5）</b>")
            for w in per_sym:
                sym = w.get("symbol")
                net = w.get("net_long_pct", 0)
                total = w.get("total_usd", 0)
                icon = "🟢" if net > 30 else ("🔴" if net < -30 else "⚪️")
                tag = " 壓倒做多" if net > 50 else (" 壓倒做空" if net < -50 else "")
                lines.append(f"   {icon} <code>{sym:6}</code> 淨多 <code>{net:+.0f}%</code>  總倉 <code>${total/1e6:.1f}M</code>{tag}")

    # 期現基差（spot vs futures）
    basis_btc = state.get("basis_btc", {})
    basis_eth = state.get("basis_eth", {})
    if not basis_btc.get("error") or not basis_eth.get("error"):
        lines.append("")
        lines.append("<b>⚖️ 期現基差（期貨 vs 現貨）</b>")
        for sym, b in [("BTC", basis_btc), ("ETH", basis_eth)]:
            if b.get("error"): continue
            bp = b.get("basis_pct", 0)
            interp = b.get("interpretation", "")
            interp_zh = {"expensive_futures": "期貨溢價（多頭情緒）",
                         "discount_futures": "期貨折價（看空情緒）",
                         "near_par": "持平"}.get(interp, interp)
            icon = "🟢" if bp > 0.1 else ("🔴" if bp < -0.1 else "⚪️")
            lines.append(f"   {icon} {sym}: 基差 <code>{bp:+.3f}%</code> ({_esc(interp_zh)})")

    # Funding 極端值（過熱+過冷）
    fo = state.get("funding_outliers", {})
    if not fo.get("error"):
        hottest = fo.get("hottest", [])[:5]
        coldest = fo.get("coldest", [])[:5]
        if hottest or coldest:
            lines.append("")
            lines.append("<b>🌡 Funding 極端值（過熱+過冷 Top 5）</b>")
            for h in hottest:
                if h["funding"] > 0.0005:  # > 0.05%/8h 才算過熱
                    lines.append(f"   🔥 <code>{h['symbol']:6}</code> <code>{h['funding_pct_8h']:+.4f}%</code> @ {_esc(h.get('exchange',''))[:10]}")
            for c in coldest:
                if c["funding"] < -0.0001:  # < -0.01%/8h 算過冷
                    lines.append(f"   🧊 <code>{c['symbol']:6}</code> <code>{c['funding_pct_8h']:+.4f}%</code> @ {_esc(c.get('exchange',''))[:10]}")

    # 跨所 funding 套利機會
    fa = state.get("funding_arb", {})
    if not fa.get("error"):
        arb_items = fa.get("items", [])[:3]
        if arb_items:
            lines.append("")
            lines.append("<b>💱 跨所 Funding 套利（Top 3）</b>")
            for a in arb_items:
                buy_at = str(a.get('buy_at') or '')[:12]
                sell_at = str(a.get('sell_at') or '')[:12]
                lines.append(f"   <code>{a['symbol']:6}</code> 買 {_esc(buy_at)} 賣 {_esc(sell_at)}  APR <code>{a.get('apr')}%</code>")

    # 期權市場 OI 變化（機構建倉/出清）
    obtc = state.get("options_btc", {})
    oeth = state.get("options_eth", {})
    if not obtc.get("error") or not oeth.get("error"):
        lines.append("")
        lines.append("<b>📈 期權市場 OI（機構動向）</b>")
        for sym, o in [("BTC", obtc), ("ETH", oeth)]:
            if o.get("error"): continue
            total = o.get("total_oi_usd", 0)
            change_24h = o.get("weighted_24h_change_pct", 0)
            icon = "🟢" if change_24h > 1 else ("🔴" if change_24h < -1 else "⚪️")
            lines.append(f"   {icon} {sym}: OI 總額 <code>${total/1e9:.2f}B</code>  24h <code>{change_24h:+.2f}%</code>")

    # BTC 週期指標（Pi Cycle/Puell/S2F + Golden Ratio + 2yr MA 完整版）
    cycle = state.get("cycle", {})
    if not cycle.get("error"):
        pi = cycle.get("pi_cycle", {})
        pu = cycle.get("puell", {})
        gr = cycle.get("golden_ratio", {})
        two = cycle.get("two_year_ma", {})
        if pi or pu or gr or two:
            lines.append("")
            lines.append("<b>🔮 BTC 週期指標</b>")
            if pi:
                dist = pi.get("distance_pct", 0)
                icon = "🔴" if pi.get("signal") == "top_warning" else "🟢"
                lines.append(f"   {icon} Pi Cycle: 110d MA <code>${pi.get('ma_110')}</code>  距 350d×2 <code>{dist:+.1f}%</code>")
            if pu:
                lines.append(f"   Puell Multiple: <code>{pu.get('value')}</code> ({_esc(pu.get('label', '—'))})")
            if gr:
                lines.append(f"   Golden Ratio: <code>{gr.get('multiplier')}x</code> 350d MA ({_esc(gr.get('label', '—'))})")
            if two:
                lines.append(f"   2-year MA Multiplier: <code>{two.get('multiplier')}x</code> ({_esc(two.get('label', '—'))})")

    # 即時新聞（CryptoPanic 重要新聞前 3）
    news = state.get("news", {})
    if not news.get("error"):
        posts = news.get("posts", [])[:3]
        if posts:
            lines.append("")
            lines.append("<b>📰 重要新聞（CryptoPanic）</b>")
            for p in posts:
                title = (p.get("title") or "")[:80]
                source = p.get("source") or ""
                age = p.get("age_minutes")
                age_str = f"{age}m前" if age is not None and age < 60 else (f"{age//60}h前" if age else "")
                imp = p.get("important_votes", 0)
                imp_str = f" 🔥{imp}" if imp >= 5 else ""
                lines.append(f"   • {_esc(title)}")
                lines.append(f"     <i>{_esc(source)}  {age_str}{imp_str}</i>")

    # OKX 官方公告（watchlist 相關 + 全部最新）
    okx_news = state.get("okx_news", {})
    if not okx_news.get("error"):
        relevant = okx_news.get("watchlist_relevant", [])[:5]
        all_recent = okx_news.get("all_recent", [])[:5]
        import time as _t
        now_ms = int(_t.time() * 1000)

        def _age(it):
            age_h = (now_ms - it.get("pTime", 0)) / 1000 / 3600
            if age_h < 1: return f"{int(age_h*60)}m前"
            if age_h < 48: return f"{int(age_h)}h前"
            return f"{int(age_h/24)}d前"

        def _type_zh(t):
            return {
                "announcements-new-listings": "🆕 新上幣",
                "announcements-delistings": "❌ 下架",
                "announcements-trading-updates": "📝 規則變動",
                "announcements-deposit-withdrawal-suspension-resumption": "🚫 入出金中斷",
                "latest-events": "📌 重大事件",
                "announcements-others": "📎 其他",
            }.get(t, t)

        if relevant or all_recent:
            lines.append("")
            lines.append("<b>📢 OKX 官方公告（72h 內）</b>")
            if relevant:
                lines.append("   <b>⚠️ 涉及你 watchlist：</b>")
                for it in relevant:
                    matched = it.get("matched_symbol", "")
                    t = _type_zh(it.get("annType", ""))
                    title = (it.get("title") or "")[:80]
                    lines.append(f"   • [{matched}] {t} <i>{_age(it)}</i>")
                    lines.append(f"     {_esc(title)}")
            if all_recent and not relevant:
                lines.append(f"   <i>近 72h 共 {okx_news.get('total_recent', 0)} 則，最近 3 則：</i>")
                for it in all_recent[:3]:
                    t = _type_zh(it.get("annType", ""))
                    title = (it.get("title") or "")[:80]
                    lines.append(f"   • {t} <i>{_age(it)}</i>")
                    lines.append(f"     {_esc(title)}")

    # 操作建議
    lines.append("")
    lines.append("<b>📋 操作建議</b>")
    lines.append(f"   日內爆發：{_esc(advice.get('long_setups', '—'))}")
    lines.append(f"   左側埋伏：{_esc(advice.get('ambush', '—'))}")
    lines.append(f"   等待條件：{_esc(advice.get('wait_for', '—'))}")

    return "\n".join(lines)


def render_refresh_summary(refresh_result: dict, watchlist) -> str:
    """交易層 refresh 結果通知。"""
    added = refresh_result.get("added", [])
    dropped = refresh_result.get("dropped", [])
    chosen = refresh_result.get("chosen", [])
    elapsed = refresh_result.get("elapsed_sec", 0)
    top5 = refresh_result.get("scored", [])[:5]

    lines = [
        f"🔁 <b>交易層 Refresh</b>  耗時 <code>{elapsed}s</code>",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"<b>本期 Top {len(chosen)}</b>: <code>{_esc(', '.join(chosen))}</code>",
    ]
    if added:
        lines.append(f"➕ 新進：<code>{_esc(', '.join(added))}</code>")
    if dropped:
        lines.append(f"➖ 退出：<code>{_esc(', '.join(dropped))}</code>")
    if top5:
        lines.append("")
        lines.append("<b>強勢分數 Top 5</b>")
        for it in top5:
            score = it.get("strength_score", 0)
            sym = it.get("symbol", "?")
            ret = it.get("return_7d_pct", 0)
            lines.append(f"   {sym:6}  score <code>{score}</code>  近期變化 <code>{ret:+.1f}%</code>")

    return "\n".join(lines)


def render_shutdown(stats: dict) -> str:
    sent = stats.get("sent", 0)
    queued = stats.get("queued", 0)
    failed = stats.get("failed", 0)
    return (
        "🔴 <b>交易機器人離線</b>\n"
        f"   已送：<code>{sent}</code>  排隊中：<code>{queued}</code>  失敗：<code>{failed}</code>"
    )
