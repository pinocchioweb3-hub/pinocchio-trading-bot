"""FIRE 前一致性檢查（cross-check synthesizer）。

每個 FIRE 進 queue 前跑這些檢查 → 算出 confidence 分數 (0-100)。
不通過直接丟棄 + log；分數低於閾值降級為 WARN 等級訊息。

檢查項目：
    1. 數據合理性（price > 0、ts 在合理時間內）
    2. BTC 閘狀態一致性（FIRE BULL 不該配 BTC 閘關）
    3. Funding 極端值警告（FIRE BULL + funding hot = 高風險）
    4. 清算失衡確認（FIRE BULL 時應該有空清算累積）
    5. ETF 流向背離（BTC FIRE 時 ETF 流出 = 跟機構反向）
    6. 情緒極端值（極度貪婪時 FIRE BULL 風險升高）
    7. 新聞敘事偏向（task#66 Q2 Phase 1；**delta 恆 0 純觀測**，不參與計分）

v238：外部資料讀不到時，該項不再從卡上無聲消失。舊行為是整段 `if` 跳過 ⇒
七項只印三項、結論仍印 `all checks passed`、confidence 100，讀卡的人分不出
「這項通過」與「這項沒跑過」。現在一律留一列 `unknown=True`（delta 恆 0）。
⛔ 未知**不扣分**：讓未知保守降分是進場濾網數學的改動，要走回測閘（PSR/DSR）。
⛔ 「不適用」不是「量不到」：山寨幣沒有現貨 ETF，不得標未知（見 Check 5）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from l2_trigger.types import TriggerDecision, TriggerAction, SignalState


def _unknown_row(name: str, note: str) -> dict:
    """「這項量不到」的觀測列（v238）。

    舊行為是資料缺失就整段 `if` 跳過 —— 卡上連一列都不會出現，於是七項檢查
    只印三項、結論還是 `all checks passed`／confidence 100。讀卡的人（和 CEO
    報告）無從分辨「這項通過」與「這項沒跑過」。

    ⛔ delta 恆 0：本層只治可見性。「未知是否該像 Check 2 的 BTC 閘那樣保守
       扣分」屬於進場濾網數學的改動，要走回測閘（PSR/DSR），不得在此順手做。
    ⛔ pass 維持 True：這一列不是「檢查失敗」，是「沒有結論」。標成 False 會讓
       下游把它算成扣分項，等於偷偷改了計分。可見性靠 `unknown` 這個獨立旗標。
    """
    # note 本身不帶符號：渲染端會依 `unknown` 旗標補 ❓，重複前綴會變成「❓ ❓ …」。
    return {"name": name, "pass": True, "delta": 0, "unknown": True,
            "note": f"{note}（未計分）"}


@dataclass
class ConsistencyResult:
    confidence: int  # 0-100
    pass_: bool
    checks: list[dict]  # 每項檢查結果
    reason: str

    def downgraded(self) -> bool:
        return self.pass_ and self.confidence < 60

    def unknown_names(self) -> list[str]:
        """量不到的檢查名（由 checks 列即時算出，不另存欄位以免兩處漂移）。"""
        return [c["name"] for c in self.checks if c.get("unknown")]


async def cross_check_fire(
    decision: TriggerDecision,
    *,
    etf_flows: dict | None = None,
    sentiment: dict | None = None,
    liq_scan: dict | None = None,
) -> ConsistencyResult:
    """跑 cross-check，回 (confidence, pass, checks)。
    外部資料（ETF/sentiment/liq）為 None → 該檢查跳過不算分。
    """
    if decision.action != TriggerAction.FIRE:
        return ConsistencyResult(confidence=0, pass_=False, checks=[],
                                 reason="not a fire decision")

    checks: list[dict] = []
    score = 100  # 從滿分扣

    snap = decision.snapshot
    direction = decision.direction
    sym = snap.symbol

    # === Check 1: 數據合理性 ===
    if snap.price <= 0:
        checks.append({"name": "price_sanity", "pass": False,
                       "note": f"price={snap.price} invalid"})
        return ConsistencyResult(0, False, checks, "invalid_price")
    checks.append({"name": "price_sanity", "pass": True, "note": f"${snap.price}"})

    # === Check 2: BTC 閘一致性（FIRE BULL on alts 與 BTC trend 衝突 = 扣分）===
    if direction == SignalState.BULL and snap.btc_gate_open is False:
        checks.append({"name": "btc_gate_alignment", "pass": False, "delta": -25,
                       "note": "BTC 趨勢向下但發出 BULL FIRE"})
        score -= 25
    elif direction == SignalState.BULL and snap.btc_gate_open is None:
        # BTC 閘資料缺失/stale → 無法確認 BULL 與大盤對齊 → 保守降分（fail-closed 精神）
        checks.append({"name": "btc_gate_alignment", "pass": False, "delta": -10,
                       "note": "BTC 閘資料缺失/stale，BULL 對齊無法確認 → 保守降分"})
        score -= 10
    else:
        checks.append({"name": "btc_gate_alignment", "pass": True, "delta": 0,
                       "note": f"BTC gate {snap.btc_gate_open}"})

    # === Check 3: Funding 極端值 ===
    if snap.funding is None:
        checks.append(_unknown_row(
            "funding_check", "funding 讀不到（stale/缺欄）→ 過熱與否無法確認"))
    else:
        f_pct = snap.funding * 100
        if direction == SignalState.BULL and snap.funding >= 0.0008:
            checks.append({"name": "funding_check", "pass": False, "delta": -15,
                           "note": f"BULL 但 funding {f_pct:+.3f}%/8h 過熱"})
            score -= 15
        elif direction == SignalState.BEAR and snap.funding <= -0.0001:
            checks.append({"name": "funding_check", "pass": False, "delta": -10,
                           "note": f"BEAR 但 funding {f_pct:+.4f}% 偏負"})
            score -= 10
        else:
            checks.append({"name": "funding_check", "pass": True, "delta": 0,
                           "note": f"funding {f_pct:+.4f}%"})

    # === Check 4: 清算失衡（如果 liq_scan 有提供）===
    if liq_scan and not liq_scan.get("error"):
        items = liq_scan.get("items", [])
        sym_liq = next((it for it in items if it.get("symbol") == sym), None)
        if sym_liq:
            imb = sym_liq.get("imbalance", 0)
            # bull squeeze 燃料：短清算 > 多清算（imb > 0）
            if direction == SignalState.BULL and imb < -0.2:
                checks.append({"name": "liquidation_alignment", "pass": False, "delta": -10,
                               "note": f"BULL 但近期多殺多（imb={imb}）"})
                score -= 10
            elif direction == SignalState.BULL and imb > 0.2:
                checks.append({"name": "liquidation_alignment", "pass": True, "delta": +5,
                               "note": f"短清算累積 imb={imb}（軋空燃料）"})
                score = min(100, score + 5)
            else:
                checks.append({"name": "liquidation_alignment", "pass": True, "delta": 0,
                               "note": f"imb={imb}"})
        else:
            checks.append({"name": "liquidation_alignment", "pass": True, "delta": 0,
                           "note": "標的不在前 20 大清算榜（量小）"})
    else:
        # ⛔「榜上沒有它」（上面那列，量到了、答案是量小）與「整份榜單讀不到」
        #    是兩件事，不可共用同一列措辭。
        checks.append(_unknown_row(
            "liquidation_alignment", "清算榜讀不到 → 多空清算失衡無法確認"))

    # === Check 5: ETF 流向（僅 BTC/ETH）===
    # ⛔ 山寨幣沒有現貨 ETF 這回事 ⇒ 那是「不適用」，不是「量不到」，不得標未知；
    #    否則每張山寨幣卡都會多一列假警訊，❓ 這個符號很快就沒人看了。
    if sym not in ("BTC", "ETH"):
        pass
    elif not etf_flows or etf_flows.get("error"):
        checks.append(_unknown_row(
            "etf_alignment", f"{sym} ETF 流向讀不到 → 機構同向與否無法確認"))
    else:
        cum_7d = etf_flows.get("cumulative_7d_flow_usd", 0)
        # 7d 累計流出 > $500M = 機構在減倉
        if direction == SignalState.BULL and cum_7d < -500_000_000:
            checks.append({"name": "etf_alignment", "pass": False, "delta": -20,
                           "note": f"BULL 但 ETF 7d 流出 ${cum_7d/1e6:.0f}M"})
            score -= 20
        elif direction == SignalState.BULL and cum_7d > 200_000_000:
            checks.append({"name": "etf_alignment", "pass": True, "delta": +10,
                           "note": f"ETF 7d 流入 ${cum_7d/1e6:.0f}M（機構同向）"})
            score = min(100, score + 10)
        else:
            checks.append({"name": "etf_alignment", "pass": True, "delta": 0,
                           "note": f"ETF 7d ${cum_7d/1e6:.0f}M"})

    # === Check 6: 情緒極端值 ===
    # F&G 與 AHR999 是全系統唯二會量化影響開單的情緒/估值接點；讀不到時無聲跳過，
    # 等於「極度貪婪該扣的 15 分」永遠不會扣，而且卡上看不出來少扣了。
    _sent_ok = bool(sentiment) and not sentiment.get("error")
    fg = sentiment.get("fear_greed_now") if _sent_ok else None
    ahr = sentiment.get("ahr999_now") if _sent_ok else None

    # v242：源那層（coinglass.get_sentiment）現在會說出「哪一個子請求死了、
    # 為什麼」。⛔ 這句話要一路帶到卡上——只寫「讀不到」的話，讀卡的人分不出
    # 金鑰到期（要續訂／換源）和端點回空清單（要查資料契約）。
    # 舊形狀（沒有這個鍵）→ _why 回空字串，note 維持原樣，不得憑空生成成因。
    _unavail = (sentiment or {}).get("unavailable") or {}

    def _why(*keys: str) -> str:
        seen: list[str] = []
        for k in keys:
            r = str(_unavail.get(k) or "").strip()
            if r and r not in seen:
                seen.append(r)
        return "：" + "；".join(seen) if seen else ""

    if fg is None and ahr is None:
        # ⛔ 包含「連上了、dict 回來了、但兩個欄位都是 None」——有回應不等於有讀數。
        checks.append(_unknown_row(
            "sentiment_check",
            "F&G／AHR999 皆讀不到 → 情緒與估值極端值無法確認"
            + _why("fear_greed", "ahr999")))
    else:
        if fg is None:
            checks.append(_unknown_row(
                "sentiment_check",
                "F&G 讀不到 → 情緒極端值無法確認" + _why("fear_greed")))
        elif direction == SignalState.BULL and fg >= 85:
            checks.append({"name": "sentiment_check", "pass": False, "delta": -15,
                           "note": f"BULL 但 F&G {fg}（極度貪婪）"})
            score -= 15
        elif direction == SignalState.BULL and fg <= 20:
            checks.append({"name": "sentiment_check", "pass": True, "delta": +10,
                           "note": f"BULL + F&G {fg}（極度恐懼，反向有利）"})
            score = min(100, score + 10)
        else:
            checks.append({"name": "sentiment_check", "pass": True, "delta": 0,
                           "note": f"F&G {fg}"})

        if ahr is None:
            checks.append(_unknown_row(
                "valuation_check",
                "AHR999 讀不到 → 估值高低無法確認" + _why("ahr999")))
        elif direction == SignalState.BULL and ahr > 1.2:
            checks.append({"name": "valuation_check", "pass": False, "delta": -10,
                           "note": f"AHR999 {ahr}（高估區）"})
            score -= 10

    # === Check 7: 新聞敘事偏向（task#66 Q2 Phase 1）===
    # 影子鐵則：**delta 恆 0** — 結構上不可能改 score / pass_ / 方向 / downgraded。
    # 只把「進場那刻消息面敘事偏哪、與本單方向是否同向」當成『觀測列』釘進 checks 卡，
    # 供人/CEO 報告看見「系統有在看新聞」。閘②（離線回測 PSR/DSR/CAAR 顯著）通過前，
    # 新聞對真實/模擬/demo 任何下單數學影響嚴格為零。任何讀取失敗 → 中性觀測列，零影響。
    try:
        from news_feed.news_score import narrative_lean_for, _active_narratives_safe
        _lean = narrative_lean_for(sym, _active_narratives_safe())
        _ld = _lean.get("lean", "neutral")
        if _ld == "neutral":
            _rel = "無命中"
        elif ((_ld == "bull" and direction == SignalState.BULL)
              or (_ld == "bear" and direction == SignalState.BEAR)):
            _rel = "與本單同向"
        else:
            _rel = "與本單反向"
        checks.append({"name": "news_sentiment", "pass": True, "delta": 0,
                       "note": f"📰 敘事 {_ld}（{_rel}，n_hits={_lean.get('n_hits', 0)}）"
                               f"｜觀測中・閘②前 delta=0 不進開單"})
    except Exception:
        checks.append({"name": "news_sentiment", "pass": True, "delta": 0,
                       "note": "📰 新聞觀測暫不可用（影子降級，零影響）"})

    # === 結論 ===
    score = max(0, min(100, score))
    pass_ = score >= 30  # 低於 30 直接擋
    reason_parts = []
    for c in checks:
        # ⛔ 未知列 pass=True/delta=0，兩個既有條件都抓不到它 —— 必須顯式納入，
        #    否則「量不到」照樣被結論那行吞掉（那正是本次要治的東西）。
        if c.get("unknown"):
            # reason 是純文字串（會單獨進 log／payload／CEO 報告，看不到 ❓ 圖示），
            # 所以這裡自己帶上「未能量測」四個字，不依賴渲染端。
            reason_parts.append(f"未能量測：{c['note']}")
        elif not c.get("pass") or (c.get("delta") and c["delta"] != 0):
            reason_parts.append(c["note"])

    n_unknown = sum(1 for c in checks if c.get("unknown"))
    if reason_parts:
        reason = " | ".join(reason_parts)
    elif n_unknown:                      # 理論上不會走到（未知列已進 reason_parts）
        reason = f"{n_unknown} 項未能量測，其餘通過"
    else:
        reason = "all checks passed"     # ⛔ 只有真的每項都量到了才准這樣講

    return ConsistencyResult(
        confidence=score,
        pass_=pass_,
        checks=checks,
        reason=reason,
    )
