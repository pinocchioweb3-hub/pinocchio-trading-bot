"""⚙️ 調參 Session（v31，task #27）— 自動分析紙上帳，產參數調整建議。

界線（使用者決定）：**只建議、不自動套用**。報告推系統主題，套用需人工
（未來可接 /settings）。每日一次。這是「AI 自我評估、提優化」的一環，
但保留人類對參數變更的最終決定權。

分析維度（每個 setup 各算）：
    期望值（R/筆）、勝率、樣本數、出場劇本分布（TP全收/部分止盈後出場/止損/逾時）
建議邏輯（規則式，保守）：
    - 樣本 <20：只報「樣本不足，繼續累積」
    - 逾時比例高 → 建議縮短持倉時限或放寬 TP
    - 止損比例高且期望值負 → 建議收緊進場條件（提高 min_votes）或放寬 SL
    - TP1 命中高但 TP3 少 → 建議下調 TP3 R 或加大 TP1 平倉比例
    - 期望值正且穩 → 維持，可考慮加碼
"""
from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3
import time

from botpaths import db_path as _db_path

DB_PATH = _db_path("trade_journal.db")
MIN_SAMPLE = 20   # 低於此只報「樣本不足」


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def analyze_setup(setup: str, days: int = 60) -> dict:
    """單一 setup 的紙上表現 + 出場劇本分布。"""
    cutoff = int(time.time() * 1000) - days * 86400 * 1000
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT realized_r, exit_reason, legs_hit, pnl_usd FROM paper_trades "
            "WHERE setup=? AND status='closed' AND entry_at>=?", (setup, cutoff)).fetchall()
    finally:
        conn.close()
    n = len(rows)
    if n == 0:
        return {"setup": setup, "n": 0}
    rs = [r[0] or 0 for r in rows]
    wins = sum(1 for r in rs if r > 0)
    exp = sum(rs) / n
    tp_full = sum(1 for r in rows if (r[2] or "").count("tp") >= 3)
    stops = sum(1 for r in rows if "stop" in (r[1] or "") and (r[0] or 0) <= 0)
    timeouts = sum(1 for r in rows if "timeout" in (r[1] or ""))
    tp1_only = sum(1 for r in rows if "tp1" in (r[2] or "") and (r[2] or "").count("tp") < 3)
    return {"setup": setup, "n": n, "win_rate": round(wins / n * 100, 1),
            "expectancy_r": round(exp, 3), "tp_full": tp_full, "stops": stops,
            "timeouts": timeouts, "tp1_only": tp1_only}


def _backtest_anchor(setup: str) -> str | None:
    """紙上樣本不足時，引用回測 Session 的歷史回放期望值當錨點。"""
    try:
        from backtest.backtest_session import latest_backtest
        bt = latest_backtest(setup)
    except Exception:
        bt = None
    if not bt or bt.get("n_trades", 0) < MIN_SAMPLE:
        return None
    exp = bt["expectancy_r"]
    tone = "✅正期望" if exp >= 0.2 else ("⚠️負期望" if exp < 0 else "持平")
    return (f"回測錨點（近 {bt['days']}天 {bt['n_symbols']}檔回放，{bt['n_trades']}筆）："
            f"期望值 {exp:+.2f}R、勝率 {bt['win_rate']*100:.0f}%、PF {bt['profit_factor']:.2f} {tone}")


def suggest(a: dict) -> list[str]:
    """規則式參數建議（保守）。回建議清單。"""
    if a["n"] < MIN_SAMPLE:
        tips = [f"樣本僅 {a['n']}/{MIN_SAMPLE} 筆 — 繼續累積，暫不調參"]
        anchor = _backtest_anchor(a["setup"])
        if anchor:
            tips.append(anchor)
        return tips
    s, tips = a, []
    to_pct = s["timeouts"] / s["n"]
    stop_pct = s["stops"] / s["n"]
    if to_pct >= 0.3:
        tips.append(f"逾時出場佔 {to_pct*100:.0f}% 偏高 → 建議縮短 HOLD_MAX_HOURS 或下調 TP 目標（價格走不到）")
    if stop_pct >= 0.5 and s["expectancy_r"] < 0:
        tips.append(f"止損佔 {stop_pct*100:.0f}% 且期望值負（{s['expectancy_r']:+.2f}R）→ "
                    f"建議收緊進場（提高 min_votes/cross-check 門檻）或重檢 SL%")
    if s["tp1_only"] / s["n"] >= 0.4 and s["tp_full"] / s["n"] <= 0.15:
        tips.append(f"多數只到 TP1（{s['tp1_only']}/{s['n']}）少到 TP3 → "
                    f"建議下調 TP3 R 倍數，或加大 TP1 平倉比例鎖更多利潤")
    if s["expectancy_r"] >= 0.2 and s["win_rate"] >= 45:
        tips.append(f"期望值正（{s['expectancy_r']:+.2f}R）勝率 {s['win_rate']}% — 表現穩健，維持參數")
    if not tips:
        tips.append(f"期望值 {s['expectancy_r']:+.2f}R、勝率 {s['win_rate']}% — 無明顯偏態，維持觀察")
    return tips


def build_report(days: int = 60) -> str | None:
    """掃所有 setup 產調參建議報告。無資料回 None。"""
    setups = ["intraday", "ambush", "us_breakout"]
    blocks = []
    any_data = False
    for st in setups:
        a = analyze_setup(st, days)
        if a["n"] == 0:
            continue
        any_data = True
        head = (f"<b>{st}</b>：{a['n']} 筆｜勝率 {a['win_rate']}%｜"
                f"期望值 <code>{a['expectancy_r']:+.2f}R</code>")
        dist = (f"  出場：TP全收 {a['tp_full']}／止損 {a['stops']}／"
                f"逾時 {a['timeouts']}／僅TP1 {a['tp1_only']}")
        tips = "\n".join(f"  💡 {t}" for t in suggest(a))
        blocks.append(f"{head}\n{dist}\n{tips}")
    if not any_data:
        return None
    return ("⚙️ <b>調參 Session 報告</b>（紙上帳分析，僅建議不自動套用）\n"
            "━━━━━━━━━━━━━━━━\n" + "\n\n".join(blocks) +
            "\n\n<i>採納方式：到 /settings 或 .env 調整對應參數。"
            "AI 持續評估，參數變更權保留給你。</i>")


async def run_auto_tuner_loop(tg, interval_seconds: int = 86400,
                              target_hour_utc: int = 2):
    """每日調參分析 session（預設 10:00 台北 = 02:00 UTC）。"""
    print("[auto_tuner] loop online（每日調參分析）")
    await asyncio.sleep(240)
    while True:
        now = dt.datetime.now(tz=dt.timezone.utc)
        nxt = now.replace(hour=target_hour_utc, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += dt.timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            rep = build_report()
            if rep and tg is not None:
                await tg.send_message(rep, parse_mode="HTML")
                print("[auto_tuner] report sent")
            else:
                print("[auto_tuner] 無足夠紙上資料，略過")
        except Exception as e:
            print(f"[auto_tuner] error: {type(e).__name__}: {e}")


if __name__ == "__main__":
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    import re
    for st in ("intraday", "ambush", "us_breakout"):
        print(st, analyze_setup(st))
    r = build_report()
    print(re.sub(r"<[^>]+>", "", r) if r else "（無資料）")
