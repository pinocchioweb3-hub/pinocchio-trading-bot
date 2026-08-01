"""⚙️ 調參 Session（v31 task#27 → v56 task#52 收斂）— 自動分析紙上帳，產**純描述**報告。

v56 收斂（task#52，防兩套並存發自相矛盾訊號）：
    本檔過去吐「祈使建議」（建議縮短/收緊/下調…）。但動參數的唯一合法路徑已收斂為
    **champion/challenger 離線回放 → L2 四關閘**（l3_dispatcher.champion_challenger +
    backtest.l2_stat_gates）；把關靠統計嚴謹度而非人工逐次點頭，也不靠這裡的口語建議。
    因此本檔**移除所有祈使建議，改為純描述**（出場劇本分布、期望值、勝率），只負責：
      (1) 每日把已平倉樣本蒸餾進教訓庫 lessons.jsonl（rebuild，derived view）；
      (2) 推一份『純描述』系統主題報告（含 quadrant 彙總 + 誠實樣本不足橫幅）；
      (3) 明確指向：要真的改參數，走 champion/challenger + L2，不在這裡。
    → 杜絕「自動優化器要調 A、調參報告卻喊調 B」的自相矛盾。

分析維度（每個 setup 各算，純描述、不下指令）：
    期望值（R/筆）、勝率、樣本數、出場劇本分布（TP全收/部分止盈後出場/止損/逾時）
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sqlite3
import tempfile
import time

from botpaths import data_dir, db_path as _db_path

DB_PATH = _db_path("trade_journal.db")
MIN_SAMPLE = 20   # 低於此只報「樣本不足」

# ── task#78：每日復盤啟動補跑（解 daemon 頻繁重啟跨不過每日 02:00 UTC 觸發點）────────
# 根因：舊 loop 只在每日固定 02:00 UTC 觸發、無補跑機制；daemon 因開發迭代＋watchdog
# 頻繁重啟，幾乎從不存活滿 24h 剛好跨過該秒，故 lessons rebuild／auto_optimizer／
# entry_policy_optimizer／調參報告「全自動復盤引擎」實際上幾乎從不自動執行
# （entry_policy_audit.jsonl 從不存在為證）。治本＝以 UTC 日期戳記持久化「今日是否已跑」，
# 啟動後若今日尚未跑且已過觸發點則『立即補跑一次』，之後回正常每日節奏；至多每 UTC 日一次。
# 純模擬盤：補跑只驅動 paper／demo 復盤，過 L2 統計閘才寫覆寫表（樣本<30→0 晉升→零行為變更），
# 真錢執行層永不讀（紅線①）。
_REVIEW_STATE_NAME = "auto_tuner_state.json"
_WARMUP_S_DEFAULT = 240


def _now_utc() -> dt.datetime:
    """UTC 現在（抽成函式供測試注入；勿用本地時間）。"""
    return dt.datetime.now(tz=dt.timezone.utc)


def _review_state_path():
    return data_dir() / _REVIEW_STATE_NAME


# v200（監督員 r94）：讀取三態。⛔ 勿再折回單一 None。
LOAD_OK = "ok"
LOAD_MISSING = "missing"          # 真的沒有檔＝真·從未復盤過，唯一可當「沒有歷史」用的一態
LOAD_UNREADABLE = "unreadable"    # 檔在但讀不出／壞檔／形狀不對＝上次跑的日期**未知**


def _atomic_write(p, text: str) -> None:
    """temp + flush + fsync + os.replace。失敗向上拋，由呼叫端決定怎麼收斂。

    fsync 不可省：少了它，內容可能還在作業系統快取裡就換了名，斷電後目的地留下零長度／
    半截檔——那正是「壞掉的戳記檔」的自產來源（本機有斷電事件史，v177 才補電力哨兵）。
    """
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".auto_tuner_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _preserve_bad(p, err: Exception) -> None:
    """壞檔留一份鑑識副本（原檔下一輪就會被蓋掉）並出聲。⛔ 不刪不改原檔。"""
    kept = ""
    try:
        bad = p.with_suffix(".bad")
        if not bad.exists():           # 只留最早那一份（後續覆蓋會沖掉第一現場）
            bad.write_bytes(p.read_bytes())
        kept = f"；壞檔已留證於 {bad.name}"
    except Exception:  # noqa: BLE001 留證是 best-effort，不可反過來壓掉主訊息
        kept = "；（留證失敗）"
    print(f"🚨 [auto_tuner] 每日復盤戳記檔存在但讀不出來（{type(err).__name__}: {err}）{kept}"
          "——⛔ 不當成『從未復盤過』")


def _load_last_review_status() -> tuple[str | None, str]:
    """讀上次復盤日期，回 (date, status)，status ∈ {ok, missing, unreadable}。

    為何要三態：舊版把「沒有檔（真·從未復盤）」與「檔在但壞掉」折成同一個 None，迴圈把
    None 一律讀成「今日尚未跑」。單看一輪這是保守的（多跑一次冪等安全），但它與下方
    `continue` 分支相乘之後就不是了——見 run_auto_tuner_loop 的熱迴圈說明。
    """
    p = _review_state_path()
    try:
        if not p.exists():
            return None, LOAD_MISSING
    except OSError:
        return None, LOAD_UNREADABLE      # 連「在不在」都問不出來 → 未知，不可當沒有
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 壞檔／權限／編碼一律歸「未知」
        _preserve_bad(p, e)
        return None, LOAD_UNREADABLE
    if not isinstance(d, dict) or not isinstance(d.get("last_review_date"), str):
        _preserve_bad(p, ValueError("形狀不對：缺 last_review_date 字串"))
        return None, LOAD_UNREADABLE
    return d["last_review_date"], LOAD_OK


def _stamp_review_date(date_str: str) -> bool:
    """戳記今日已跑（UTC 日期），原子寫。回傳是否寫成功——⛔ 呼叫端不可假設一定成功。"""
    try:
        _atomic_write(_review_state_path(),
                      json.dumps({"last_review_date": date_str}, ensure_ascii=False))
        return True
    except Exception as e:  # noqa: BLE001 寫不進去不可拖垮迴圈，但必須回報失敗
        print(f"[auto_tuner] 警告：寫入 review 狀態失敗（改用行程內記憶去重）："
              f"{type(e).__name__}: {e}")
        return False


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
            "WHERE setup=? AND status='closed' AND IFNULL(exit_reason,'')!='entry_expired' "
            "AND entry_at>=?", (setup, cutoff)).fetchall()  # v33: 掛單逾時作廢非真實交易，排除於調參樣本
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


def describe(a: dict) -> list[str]:
    """**純描述**現象（出場劇本分布／期望值偏態），不下任何祈使參數指令。

    v56（task#52）：刻意不再回「建議縮短/收緊/下調…」。動參數的唯一合法路徑＝
    champion/challenger 離線回放過 L2 四關（見模組 docstring）。這裡只把「看到什麼」
    講清楚，讓那條 L2 路徑去決定「該不該改、改了統計上有沒有顯著更好」。
    """
    if a["n"] < MIN_SAMPLE:
        notes = [f"樣本僅 {a['n']}/{MIN_SAMPLE} 筆 — 樣本不足，僅供觀察、未達統計顯著"]
        anchor = _backtest_anchor(a["setup"])
        if anchor:
            notes.append(anchor)
        return notes
    s, notes = a, []
    to_pct = s["timeouts"] / s["n"]
    stop_pct = s["stops"] / s["n"]
    if to_pct >= 0.3:
        notes.append(f"逾時出場佔 {to_pct*100:.0f}%（出場劇本分布；現象描述）")
    if stop_pct >= 0.5 and s["expectancy_r"] < 0:
        notes.append(f"止損佔 {stop_pct*100:.0f}% 且期望值為負（{s['expectancy_r']:+.2f}R）")
    if s["tp1_only"] / s["n"] >= 0.4 and s["tp_full"] / s["n"] <= 0.15:
        notes.append(f"多數只到 TP1（{s['tp1_only']}/{s['n']}）、少到 TP3（分批吃利分布）")
    if s["expectancy_r"] >= 0.2 and s["win_rate"] >= 45:
        notes.append(f"期望值正（{s['expectancy_r']:+.2f}R）、勝率 {s['win_rate']}%（描述，"
                     f"是否顯著請以 L2 閘判定）")
    if not notes:
        notes.append(f"期望值 {s['expectancy_r']:+.2f}R、勝率 {s['win_rate']}% — 無明顯偏態（純描述）")
    return notes


def _lessons_block() -> str | None:
    """教訓庫 quadrant 彙總（純描述，含誠實樣本不足橫幅）。失敗回 None（不擋報告）。"""
    try:
        from l3_dispatcher.lessons_store import summarize_by_quadrant
        s = summarize_by_quadrant()
    except Exception:
        return None
    if not s or not s.get("by_quadrant"):
        return None
    lines = [f"📚 <b>教訓庫</b>（lessons.jsonl｜共 {s['total']} 筆，僥倖單已排除於正向集）"]
    for q, b in sorted(s["by_quadrant"].items(), key=lambda kv: -kv[1]["n"]):
        suff = "" if b["sample_sufficient"] else " ⚠️樣本不足"
        lines.append(f"  [{q}] n={b['n']}｜正向 {b['n_positive']}｜僥倖 {b['n_lucky']}"
                     f"｜賠 {b['n_loss']}｜avg_r {b['avg_r']}{suff}")
    return "\n".join(lines)


def build_report(days: int = 60) -> str | None:
    """掃所有 setup 產**純描述**報告 + 教訓庫彙總。無資料回 None。"""
    setups = ["intraday", "ambush", "us_breakout", "deepdive"]
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
        notes = "\n".join(f"  • {t}" for t in describe(a))
        blocks.append(f"{head}\n{dist}\n{notes}")
    lessons = _lessons_block()
    if not any_data and not lessons:
        return None
    body_parts = []
    if any_data:
        body_parts.append("\n\n".join(blocks))
    if lessons:
        body_parts.append(lessons)
    return ("⚙️ <b>調參 Session 報告</b>（紙上帳『純描述』，不下參數指令）\n"
            "━━━━━━━━━━━━━━━━\n" + "\n\n".join(body_parts) +
            "\n\n<i>⚠️ 本報告只描述現象。動參數的唯一合法路徑＝champion/challenger "
            "離線回放過 L2 四關（統計嚴謹度把關，非人工逐次點頭）；不在此報告、也不靠口語建議。</i>")


async def _run_daily_review(tg) -> None:
    """執行一次每日復盤全程（教訓蒸餾 → TP 優化器 → 入場積極度優化器 → 純描述調參報告）。

    每一段獨立 try/except：任一段失敗不影響其餘（韌性），全程只驅動模擬盤復盤、過 L2 統計閘
    才寫覆寫表（樣本<30→0 晉升→零行為變更），真錢執行層永不讀（紅線①）。
    """
    # v56 task#52：每日先把已平倉樣本蒸餾進教訓庫（derived view，DB 為真相源）
    # v129（監督員發現+RB-1）：本函式的同步重活（lessons 蒸餾/優化器數百桶重放/報告）
    # 過去直接跑在事件迴圈上→整個 daemon 心跳停 ~24 分→watchdog 誤判死機、近 8 天 6 天
    # 把 daemon 砍在復盤半路。修法＝同步段全部 to_thread（鏡像 v111 cycle 前例），
    # 事件迴圈保持轉動、scheduler 心跳照常戳、watchdog 不再誤殺。
    try:
        from l3_dispatcher.lessons_store import rebuild_lessons_file
        res = await asyncio.to_thread(rebuild_lessons_file)
        print(f"[auto_tuner] lessons rebuilt: {res['n']} 筆"
              f"（正向 {res['n_positive']}／僥倖 {res['n_lucky']}／賠 {res['n_loss']}）")
    except Exception as e:
        print(f"[auto_tuner] lessons rebuild error: {type(e).__name__}: {e}")
    # task#53(step8)：教訓蒸餾後跑自動優化器——把已平倉 paper 按 (symbol×象限) 分桶，
    # champion/challenger 離線回放過 L2 四關；verdict.promote=True 才由 auto_param_store
    # 直接寫「活躍 TP 分配覆寫表」，模擬盤下一筆同桶進場即生效。把關靠統計嚴謹度
    # （minTRL≥30 fail-closed、DSR/PBO/FDR），非人工逐次點頭。今日樣本<30→0 晉升→零行為變更。
    try:
        from l3_dispatcher import auto_optimizer
        opt = await asyncio.to_thread(auto_optimizer.run_optimization)
        print(f"[auto_tuner] auto_optimizer: {opt['n_buckets']} 桶、"
              f"{opt['n_promoted']} 晉升")
        opt_rep = auto_optimizer.render_report(opt)
        if opt_rep and tg is not None:
            await tg.send_message(opt_rep, parse_mode="HTML")
            print("[auto_tuner] auto_optimizer report sent")
    except Exception as e:
        print(f"[auto_tuner] auto_optimizer error: {type(e).__name__}: {e}")
    # task#61(step9)：入場積極度自動優化器——把已平倉/逾時(含 entry_expired) paper 按
    # (symbol×象限) 分桶，逐根 K 線重放 champion(現行深限價可到期) vs challenger
    # (D 深限價到期轉市價／市價即進)，過 L2 四關才寫「模擬盤入場政策覆寫表」。
    # 與 TP 優化器互補：那個調出場分批，這個調進場積極度。納入 entry_expired（缺料樣本）。
    # 學習＋揭示半：每日累積 L2 證據；覆寫表的進場執行層消費為下一步接線（現為觀測/待接）。
    # async：需取快取 OHLC 做逐根重放。今日對齊樣本<30→minTRL fail-closed→0 晉升→零行為變更。
    try:
        from l3_dispatcher import entry_policy_optimizer as epo
        eopt = await epo.run_entry_optimization()
        print(f"[auto_tuner] entry_policy_optimizer: {eopt['n_buckets']} 桶、"
              f"{eopt['n_promoted']} 晉升（符合完整重放窗 {eopt.get('n_eligible', '?')} 筆）")
        eopt_rep = epo.render_report(eopt)
        if eopt_rep and tg is not None:
            await tg.send_message(eopt_rep, parse_mode="HTML")
            print("[auto_tuner] entry_policy_optimizer report sent")
    except Exception as e:
        print(f"[auto_tuner] entry_policy_optimizer error: {type(e).__name__}: {e}")
    try:
        rep = await asyncio.to_thread(build_report)
        if rep and tg is not None:
            await tg.send_message(rep, parse_mode="HTML")
            print("[auto_tuner] report sent")
        else:
            print("[auto_tuner] 無足夠紙上資料，略過")
    except Exception as e:
        print(f"[auto_tuner] error: {type(e).__name__}: {e}")


async def run_auto_tuner_loop(tg, interval_seconds: int = 86400,
                              target_hour_utc: int = 2,
                              warmup_seconds: float = _WARMUP_S_DEFAULT):
    """每日調參分析 session（預設 10:00 台北 = 02:00 UTC），含啟動補跑（task#78）。

    控制流（至多每 UTC 日執行一次每日復盤）：
      1. 暖機 warmup_seconds（避開開機尖峰，與 macro_confluence 同精神）。
      2. **啟動補跑**：若「今日(UTC)尚未跑」且「已過今日觸發點 target_hour_utc」→ 立即補跑一次
         並戳記今日；解 daemon 在 02:00 UTC 之後才啟動／頻繁重啟而永遠跨不過觸發點的結構性失能。
      3. 否則睡到下一個 target_hour_utc，醒來執行、戳記當日，無限循環。
    冪等：以 data_dir/auto_tuner_state.json 的 last_review_date(UTC) 去重；即使狀態檔遺失而多補跑
    一次，優化器用固定 trial epoch（不灌水 n_trials）＋樣本<30→0 晉升→零行為變更，安全。

    v200（監督員 r94）——去重不可只靠持久化：
      補跑分支結尾是 `continue`，中間**沒有任何 sleep**；唯一擋住熱迴圈的是戳記有寫成功。
      但寫入失敗（唯讀檔／ACL／磁碟滿）是被吞掉的，而讀不出來（壞檔）同樣回「今天沒跑過」。
      兩者任一成立，迴圈就會以最快速度無限重跑整份每日復盤（DB 全掃 + lessons rebuild +
      優化器 + 每輪一則 Telegram）。已在改動前的碼上實證：50 次復盤、0 次睡眠。
      治法＝**行程內記憶** ran_in_process：本行程今天確實跑過就算跑過，與持久化成敗脫鉤。
      ⛔ 不可改用「讀不出來就跳過復盤」來擋——復盤是模型變強的唯一回路，跳掉比多跑一次糟得多。
    """
    print("[auto_tuner] loop online（每日調參分析；含啟動補跑 task#78）")
    if warmup_seconds:
        await asyncio.sleep(warmup_seconds)
    ran_in_process: str | None = None   # 本行程內確定已完成復盤的 UTC 日期（熱迴圈的真正煞車）
    warned_unreadable: str | None = None
    while True:
        now = _now_utc()
        today = now.date().isoformat()
        last_date, status = _load_last_review_status()
        if status == LOAD_UNREADABLE and warned_unreadable != today:
            warned_unreadable = today
            print(f"🚨 [auto_tuner] 復盤戳記讀不出來 → 今天({today})跑過沒有＝**未知**"
                  "（⛔ 不等於『沒跑過』）；本輪照跑一次，並改用行程內記憶去重")
        ran_today = (last_date == today) or (ran_in_process == today)
        past_fire = now.hour >= target_hour_utc
        if past_fire and not ran_today:
            # 啟動補跑：今日已過觸發點但尚未執行（多因 daemon 在 02:00 UTC 後才起／剛重啟）
            print(f"[auto_tuner] 啟動補跑：{today} 今日尚未執行每日復盤（已過 {target_hour_utc:02d}:00 UTC）")
            await _run_daily_review(tg)
            ran_in_process = today      # ⛔ 必須先於戳記且與其成敗脫鉤，否則寫失敗＝熱迴圈
            _stamp_review_date(today)
            continue  # 重算下一觸發點（此時 ran_today 已 True → 走排程睡眠分支）
        # 排程睡眠到下一個 target_hour_utc
        nxt = now.replace(hour=target_hour_utc, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += dt.timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        await _run_daily_review(tg)
        ran_in_process = _now_utc().date().isoformat()   # 同上：先記，再嘗試持久化
        _stamp_review_date(ran_in_process)


if __name__ == "__main__":
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    import re
    for st in ("intraday", "ambush", "us_breakout"):
        print(st, analyze_setup(st))
    r = build_report()
    print(re.sub(r"<[^>]+>", "", r) if r else "（無資料）")
