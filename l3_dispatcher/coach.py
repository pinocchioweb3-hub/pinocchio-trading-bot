"""🧑‍🏫 教練層（task #8 ⑤）— 不只報事件，還會「踩煞車」。

皮諾丘的初衷是替「小資本／自訂預算」的使用者陪跑（3000U 只是例子，本金與風控
依各自設定分級，見 botconfig.budget_tier）。小資爆倉幾乎不是因為看錯方向，
而是因為紀律破口：凹單（該止損不止）、追高（在計畫進場區外追入）、情緒性連續開倉、
重壓（總曝險失控）。這層在既有持倉監控迴圈裡，於這些破口出現的當下，
推一句「溫柔但堅定」的提醒到「持倉與績效」主題。

設計：
- 純函式產生候選提醒（可離線測試），每則帶去重 ``key``。
- 只針對**使用者真正下單的實倉**（trade_journal.get_open_trades()，即按過「已下單」的單）；
  紙上自動倉是引擎自我驗證、不關使用者紀律，一律不教練（避免洗版）。
- 不下任何單、不發任何對外內容；推送對象＝使用者自己的持倉主題（與既有事件推播同級的內部通知）。
- 由呼叫端維護 in-memory ``seen`` 集合做節流（每筆單每型提醒一生一次；帳戶級每日一次）。
"""
from __future__ import annotations

from dataclasses import dataclass

# 接近止損門檻：價格已走完 entry→stop 距離的 75%（仍在水下、尚未觸停）
STOP_PROXIMITY_FRAC = 0.75
# 曝險接近上限門檻：已用總曝險 ≥ 上限的 80%
EXPOSURE_NEAR_FRAC = 0.80
# v42 手續費侵蝕：來回 taker 手續費 ≥ 1R 的此比例 → 提醒（止損過窄被手續費磨）
TAKER_FEE_RATE = 0.0005     # OKX 永續 taker ~0.05%（與 l4_execution.demo_trader 一致）
FEE_EROSION_FRAC = 0.10
# v42 回撤降檔：今日虧損達日熔斷線的此比例（尚未觸線）→ 建議減半風險、放慢
DD_DOWNSHIFT_FRAC = 0.6

_NOTE = "<i>（教練提醒，非操作指令）</i>"


@dataclass
class CoachMsg:
    key: str       # 去重鍵（呼叫端用 in-memory set 節流）
    text: str      # 繁中 HTML 提醒
    kind: str      # 'chase' | 'hold_stop' | 'daily_stop' | 'exposure'


def _progress_to_stop(direction: str, entry: float, stop: float, price: float) -> float:
    """價格朝止損方向走了多少（0=在進場價, 1=觸及止損, 負=往獲利方向跑）。"""
    dist = abs(entry - stop)
    if dist <= 0:
        return 0.0
    adverse = (entry - price) if direction == "bull" else (price - entry)
    return adverse / dist


def _fee_frac_of_1r(entry: float, stop: float) -> float:
    """來回 taker 手續費佔 1R 的比例（與倉位大小無關，只取決於止損距離）。

    notional = 1R / stop_frac；來回費 = 2×taker×notional
        → 來回費 / 1R = 2×taker / stop_frac
    止損越窄 → 同樣 1R 需越大倉位 → 手續費佔 1R 越重。"""
    if entry <= 0:
        return 0.0
    stop_frac = abs(entry - stop) / entry
    if stop_frac <= 0:
        return 0.0
    return 2 * TAKER_FEE_RATE / stop_frac


def coach_position(trade: dict, price: float) -> list[CoachMsg]:
    """單筆實倉的教練提醒（可能多則；空 list = 此刻無話要說）。"""
    out: list[CoachMsg] = []
    legs = set(trade.get("legs_hit") or [])
    sym = trade["symbol"]
    tid = trade["id"]
    dir_zh = "做多" if trade["direction"] == "bull" else "做空"

    # 1) 追高回顧（市價追單）— 一進場就值得提醒，下次用等待觸發
    if trade.get("entry_kind") == "market_chase" and "tp1" not in legs:
        out.append(CoachMsg(
            key=f"chase:{tid}",
            kind="chase",
            text=(f"🏃 <b>{sym} {dir_zh}｜這筆是追高進場</b>\n"
                  f"成交價落在計畫進場區之外。追高是小資最貴的習慣 —— "
                  f"下次讓「等待觸發」幫你等價格回踩再進，風報比會更好。\n"
                  f"{_NOTE}"),
        ))

    # 2) 接近止損（仍水下、尚未止盈過）— 最危險的凹單時刻
    if not legs:
        prog = _progress_to_stop(trade["direction"], trade["entry_price"],
                                 trade["stop_price"], price)
        if STOP_PROXIMITY_FRAC <= prog < 1.0:
            out.append(CoachMsg(
                key=f"hold_stop:{tid}",
                kind="hold_stop",
                text=(f"🧯 <b>{sym} {dir_zh}｜接近止損</b>\n"
                      f"現價 <code>${price:,.6g}</code> 已走完約 {prog*100:.0f}% 到止損 "
                      f"<code>${trade['stop_price']:,.6g}</code>。\n"
                      f"到價就照計畫走，<b>別把止損往後挪</b>。一筆 1R 的虧損是計畫的一部分；"
                      f"凹單把 1R 凹成 3R 才是真正的風險。\n"
                      f"{_NOTE}"),
            ))

    # 3) v42 手續費侵蝕（止損過窄）— 進場後一次性提醒，與倉位大小無關
    if "tp1" not in legs:
        fee_frac = _fee_frac_of_1r(trade["entry_price"], trade["stop_price"])
        if fee_frac >= FEE_EROSION_FRAC:
            stop_frac = abs(trade["entry_price"] - trade["stop_price"]) / trade["entry_price"]
            out.append(CoachMsg(
                key=f"fee:{tid}",
                kind="fee_erosion",
                text=(f"🪙 <b>{sym} {dir_zh}｜止損偏窄，手續費侵蝕重</b>\n"
                      f"止損距進場僅 <code>{stop_frac*100:.2f}%</code>，"
                      f"來回 taker 手續費約吃掉這筆 <b>{fee_frac*100:.0f}% 的 1R</b>。\n"
                      f"小資最容易被手續費磨死 —— 下次優先用較寬的<b>結構止損</b>"
                      f"（OB／Swing 之外）或較高時框，別讓每筆的邊際先輸在起跑線。\n"
                      f"{_NOTE}"),
            ))
    return out


def coach_account(risk_status: dict, utc_date: str) -> list[CoachMsg]:
    """帳戶級教練（每日節流，key 帶日期）。"""
    out: list[CoachMsg] = []

    opened = risk_status.get("opened_today", 0)
    cap = risk_status.get("daily_max_opens", 0)
    at_open_cap = bool(cap and opened >= cap)

    # 3) 今天別再交易了 — 開倉次數達上限 或 已觸日線熔斷
    if at_open_cap or risk_status.get("daily_breached"):
        if risk_status.get("daily_breached"):
            reason = f"今日已觸日線熔斷（{risk_status.get('today_pnl_pct', 0):+.1f}%）"
        else:
            reason = f"今日已開 {opened} 倉，達上限 {cap}"
        out.append(CoachMsg(
            key=f"daily_stop:{utc_date}",
            kind="daily_stop",
            text=(f"🛑 <b>今天到此為止</b>\n{reason}。\n"
                  f"情緒性連續交易是小資爆倉的頭號原因。<b>關掉盤面，去做別的事</b>，"
                  f"明天 UTC 00:00 重置後再來。錯過的機會明天還有，本金沒了就沒了。\n"
                  f"{_NOTE}"),
        ))

    # 4) 曝險接近上限 — 寧可錯過不要重壓（已達日開倉上限時不重複嘮叨）
    open_risk = risk_status.get("total_risk_open_usd", 0)
    cap_usd = risk_status.get("risk_cap_usd", 0)
    if cap_usd and open_risk >= cap_usd * EXPOSURE_NEAR_FRAC and not at_open_cap:
        out.append(CoachMsg(
            key=f"exposure:{utc_date}",
            kind="exposure",
            text=(f"⚖️ <b>曝險接近上限</b>\n"
                  f"目前總曝險 <code>${open_risk:.0f} / ${cap_usd:.0f}</code>"
                  f"（{open_risk / cap_usd * 100:.0f}%）。\n"
                  f"接下來的訊號可能被風控擋下 —— 那是保護，不是 bug。寧可錯過，不要重壓。\n"
                  f"{_NOTE}"),
        ))

    # 5) v42 回撤降檔 — 今日虧損逼近日熔斷線（尚未觸線、未達開倉上限）→ 砍半放慢
    today_pct = risk_status.get("today_pnl_pct", 0.0)
    dd_limit = risk_status.get("daily_dd_limit_pct", 0.0)   # 負值，如 -3.0
    if (dd_limit < 0 and not risk_status.get("daily_breached") and not at_open_cap
            and today_pct <= dd_limit * DD_DOWNSHIFT_FRAC):
        out.append(CoachMsg(
            key=f"downshift:{utc_date}",
            kind="downshift",
            text=(f"📉 <b>今天逆風，先降檔</b>\n"
                  f"今日已 <code>{today_pct:+.1f}%</code>，逼近日熔斷線 "
                  f"<code>{dd_limit:.0f}%</code>。\n"
                  f"連虧時最該做的不是加碼凹回來，而是<b>把後續每筆風險砍一半</b>、放慢節奏。"
                  f"守住本金，明天才有得打。\n"
                  f"{_NOTE}"),
        ))
    return out


def build_coaching(opens: list[dict], prices: dict, risk_status: dict,
                   utc_date: str) -> list[CoachMsg]:
    """彙整本輪所有候選教練提醒（呼叫端再以 seen set 節流後推送）。"""
    msgs: list[CoachMsg] = []
    for t in opens:
        price = prices.get(t["symbol"])
        if price is None or price <= 0:
            continue
        msgs.extend(coach_position(t, price))
    msgs.extend(coach_account(risk_status, utc_date))
    return msgs


# ===========================================================================
# 自測（離線，合成資料）
# ===========================================================================
if __name__ == "__main__":
    import re

    def _plain(s):
        return re.sub(r"<[^>]+>", "", s)

    def _dump(label, msgs):
        print(f"\n--- {label} ---")
        if not msgs:
            print("(無)")
        for m in msgs:
            print(f"[{m.kind}] {_plain(m.text)}")

    # 接近止損（做多，現價走到 80% 到止損；止損 3.3% 寬 → 不觸發手續費侵蝕）
    t_stop = {"id": 1, "symbol": "ETH", "direction": "bull",
              "entry_price": 3000.0, "stop_price": 2900.0, "legs_hit": [],
              "entry_kind": "direct_fire"}
    _dump("接近止損", coach_position(t_stop, 2920.0))   # 80% → 應觸發 hold_stop

    # 已止盈過 → 不該再喊接近止損
    t_safe = dict(t_stop, legs_hit=["tp1"])
    _dump("已 TP1（應為空）", coach_position(t_safe, 2920.0))

    # 追高
    t_chase = {"id": 2, "symbol": "SOL", "direction": "bull",
               "entry_price": 150.0, "stop_price": 144.0, "legs_hit": [],
               "entry_kind": "market_chase"}
    _dump("追高", coach_position(t_chase, 151.0))

    # v42 手續費侵蝕（止損僅 0.5% → 來回費約 20% 的 1R）
    t_tight = {"id": 3, "symbol": "BTC", "direction": "bull",
               "entry_price": 60000.0, "stop_price": 59700.0, "legs_hit": [],
               "entry_kind": "direct_fire"}
    _dump("手續費侵蝕（窄止損 0.5%）", coach_position(t_tight, 60000.0))

    # 帳戶級：達日開倉上限 + 曝險滿
    rs = {"opened_today": 3, "daily_max_opens": 3, "daily_breached": False,
          "total_risk_open_usd": 300.0, "risk_cap_usd": 300.0,
          "today_pnl_pct": -1.2}
    _dump("帳戶級（達開倉上限）", coach_account(rs, "2026-06-15"))

    # v42 回撤降檔（今日 -2.0%、日線 -3%、未觸線、未達開倉上限）
    rs2 = {"opened_today": 1, "daily_max_opens": 3, "daily_breached": False,
           "total_risk_open_usd": 50.0, "risk_cap_usd": 300.0,
           "today_pnl_pct": -2.0, "daily_dd_limit_pct": -3.0}
    _dump("回撤降檔", coach_account(rs2, "2026-06-15"))
