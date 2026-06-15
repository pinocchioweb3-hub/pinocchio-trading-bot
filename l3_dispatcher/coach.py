"""🧑‍🏫 教練層（task #8 ⑤）— 不只報事件，還會「踩煞車」。

皮諾丘的初衷是替「只有 3000U 的小資本人」陪跑。小資爆倉幾乎不是因為看錯方向，
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


def coach_position(trade: dict, price: float) -> CoachMsg | None:
    """單筆實倉的教練提醒。回 None = 此刻無話要說。"""
    legs = set(trade.get("legs_hit") or [])
    sym = trade["symbol"]
    tid = trade["id"]
    dir_zh = "做多" if trade["direction"] == "bull" else "做空"

    # 1) 追高回顧（市價追單）— 一進場就值得提醒，下次用等待觸發
    if trade.get("entry_kind") == "market_chase" and "tp1" not in legs:
        return CoachMsg(
            key=f"chase:{tid}",
            kind="chase",
            text=(f"🏃 <b>{sym} {dir_zh}｜這筆是追高進場</b>\n"
                  f"成交價落在計畫進場區之外。追高是小資最貴的習慣 —— "
                  f"下次讓「等待觸發」幫你等價格回踩再進，風報比會更好。\n"
                  f"{_NOTE}"),
        )

    # 2) 接近止損（仍水下、尚未止盈過）— 最危險的凹單時刻
    if not legs:
        prog = _progress_to_stop(trade["direction"], trade["entry_price"],
                                 trade["stop_price"], price)
        if STOP_PROXIMITY_FRAC <= prog < 1.0:
            return CoachMsg(
                key=f"hold_stop:{tid}",
                kind="hold_stop",
                text=(f"🧯 <b>{sym} {dir_zh}｜接近止損</b>\n"
                      f"現價 <code>${price:,.6g}</code> 已走完約 {prog*100:.0f}% 到止損 "
                      f"<code>${trade['stop_price']:,.6g}</code>。\n"
                      f"到價就照計畫走，<b>別把止損往後挪</b>。一筆 1R 的虧損是計畫的一部分；"
                      f"凹單把 1R 凹成 3R 才是真正的風險。\n"
                      f"{_NOTE}"),
            )
    return None


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
    return out


def build_coaching(opens: list[dict], prices: dict, risk_status: dict,
                   utc_date: str) -> list[CoachMsg]:
    """彙整本輪所有候選教練提醒（呼叫端再以 seen set 節流後推送）。"""
    msgs: list[CoachMsg] = []
    for t in opens:
        price = prices.get(t["symbol"])
        if price is None or price <= 0:
            continue
        m = coach_position(t, price)
        if m:
            msgs.append(m)
    msgs.extend(coach_account(risk_status, utc_date))
    return msgs


# ===========================================================================
# 自測（離線，合成資料）
# ===========================================================================
if __name__ == "__main__":
    import re

    def _plain(s):
        return re.sub(r"<[^>]+>", "", s)

    # 接近止損（做多，現價走到 80% 到止損）
    t_stop = {"id": 1, "symbol": "ETH", "direction": "bull",
              "entry_price": 3000.0, "stop_price": 2900.0, "legs_hit": [],
              "entry_kind": "direct_fire"}
    print("--- 接近止損 ---")
    print(_plain(coach_position(t_stop, 2920.0).text))   # 80% → 應觸發

    # 已止盈過 → 不該再喊接近止損
    t_safe = dict(t_stop, legs_hit=["tp1"])
    print("\n--- 已 TP1（不喊）---", coach_position(t_safe, 2920.0))

    # 追高
    t_chase = {"id": 2, "symbol": "SOL", "direction": "bull",
               "entry_price": 150.0, "stop_price": 144.0, "legs_hit": [],
               "entry_kind": "market_chase"}
    print("\n--- 追高 ---")
    print(_plain(coach_position(t_chase, 151.0).text))

    # 帳戶級：達日開倉上限 + 曝險滿
    rs = {"opened_today": 3, "daily_max_opens": 3, "daily_breached": False,
          "total_risk_open_usd": 300.0, "risk_cap_usd": 300.0,
          "today_pnl_pct": -1.2}
    print("\n--- 帳戶級 ---")
    for m in coach_account(rs, "2026-06-15"):
        print(f"[{m.kind}] {_plain(m.text)}")
