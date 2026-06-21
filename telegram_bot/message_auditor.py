"""訊息稽核 Session（v24）— 每則對外訊息發送前即時自我檢測。

三道檢查（規則式，發送前，<5ms，不碰網路）：
    (1) 路由稽核   訊息內容推斷的類型 vs 實際送往的主題是否相符
    (2) 重複稽核   近期是否已發過逐字相同（content_hash）或模板相同（struct_hash）
    (3) 明確性稽核 數字缺單位、缺方向、過短無資訊、欄位矛盾 — 人/AI 都難解析

設計（UltraCode wf_59d55445 分析）：
    - 在 TopicRouter.client() 出廠時包一層 AuditedClient，單一攔截點，
      不必改動 30+ 個 send_message 呼叫點。
    - 逐字重複 → 直接擋下不發（block）；路由錯置/模板重複/不明確 → 放行但記錄（warn）。
    - 全部寫 message_audit.db 的 send_log；run_audit_loop 週期把警示彙整推給管理員。
    - 動機：這些訊息未來要餵 AI 下單 agent，「機器可確定性解析」是最高標準。
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from dataclasses import dataclass, field

from botpaths import db_path as _db_path

DB_PATH = _db_path("message_audit.db")

DUP_EXACT_WINDOW_S = 3600     # 逐字重複偵測窗口
DUP_TMPL_WINDOW_S = 300       # 模板重複（骨架相同）短窗
MIN_CONTENT_LEN = 20         # 去格式後低於此 = 過短


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    conn = _conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS send_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sent_at INTEGER NOT NULL,
                topic_key TEXT,
                msg_kind TEXT,
                content_hash TEXT NOT NULL,
                struct_hash TEXT,
                text_preview TEXT,
                full_len INTEGER,
                audit_route TEXT,
                audit_dup TEXT,
                audit_clarity TEXT,
                severity TEXT,
                blocked INTEGER NOT NULL DEFAULT 0,
                reported INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_slog_sent ON send_log(sent_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_slog_chash ON send_log(content_hash)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_slog_shash ON send_log(struct_hash)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_slog_report ON send_log(reported, severity)")
    finally:
        conn.close()


# ── 規範化 + 雜湊 ─────────────────────────────────────────────────────────
_TAG_RE = re.compile(r"<[^>]+>")
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF←-⇿⬀-⯿]")
_NUM_RE = re.compile(r"[-+]?\$?\d[\d,]*\.?\d*%?")
_WS_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    t = _TAG_RE.sub("", text or "")
    t = _EMOJI_RE.sub("", t)
    t = _WS_RE.sub(" ", t).strip().lower()
    return t


def _skeletonize(text: str) -> str:
    return _NUM_RE.sub("<N>", _normalize(text))


def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


# ── 類型推斷 ──────────────────────────────────────────────────────────────
# v27: 每類給一個「唯一強特徵」（命中即定案，不靠計分避免誤判）
# v82: 治本 msg_kind 分類漏洞（呈現評估 wf 發現）——CEO 簡報原撞 econc 關鍵詞被誤標
#   econ、優化器報告原撞弱特徵「已平倉」被誤標 tp_sl→再 cascade 出 trade_msg_no_direction
#   偽陽性；OTS 錨定/美股永續快照/掛單逾時 無規則→灌 unknown(占 ~498)。以下五條強特徵
#   置於最前（命中即定案，先於 econ 與弱 tp_sl）治本之。純標籤層，零渲染/訊號數學變更。
STRONG_SIGNATURES = [
    ("ceo_brief",   ("每日簡報", "監督人角色")),          # CEO 簡報，須先於 econ
    ("tuner_report", ("自動優化器",)),                    # 復盤優化器報告，須先於弱 tp_sl
    ("anchor",      ("帳本防竄改錨定", "opentimestamps")),  # OTS 比特幣錨定
    ("usquote",     ("美股永續行情",)),                   # 美股永續即時快照（≠美股快訊 usnews）
    ("expiry",      ("掛單逾時作廢", "逾時作廢")),          # 限價掛單逾時取消
    ("system",   ("稽核 session 報告", "worker 崩潰", "機器人上線", "🧵 threads", "token 續期")),
    ("pulse",    ("每小時即時動態",)),
    ("macro",    ("daily macro", "每日宏觀分析")),
    ("perf",     ("每日績效", "每日總帳", "成績卡")),
    ("order_card", ("訂單卡 #",)),
    ("fire",     ("可立即執行", "倉位配置（r")),
    ("waiting",  ("等待觸發",)),
    ("paper",    ("紙上驗證事件", "紙上對照", "美股紙上事件", "分批進場觸發")),
    ("narrative", ("市場敘事脈絡", "因果鏈")),
    ("usnews",   ("美股快訊",)),
    ("econ",     ("經濟數據", "解鎖")),
    ("alert",    ("異常警報", "資金流向圖")),
    ("invite",   ("歡迎加入", "邀請碼")),
    ("suggestion", ("建議 #", "貢獻排行", "ai 初評")),
]
# 弱特徵（計分用，前面強特徵都沒命中才用）
KIND_SIGNATURES = [
    ("tp_sl",    ("命中止盈", "觸發停損", "持倉超時", "已平倉")),
    ("position", ("持倉追蹤", "距 tp1", "距 sl")),
    ("news",     ("truth social", "realdonaldtrump")),
]

# v36: 群組精簡 12→6（保留 trade/positions/intel/news/system/ideas；
#      美股訊號併 trade、美股持倉併 positions、pulse/econ/alerts 併 intel、美股新聞/行情併 news）
KIND_TO_TOPICS = {
    "fire":      {"trade"},
    "waiting":   {"trade"},
    "order_card": {"positions"},
    "tp_sl":     {"positions"},
    "position":  {"positions"},
    "paper":     {"positions"},
    "perf":      {"positions"},
    "macro":     {"intel"},
    "narrative": {"intel"},
    "pulse":     {"intel"},
    "usnews":    {"news"},
    "news":      {"news"},
    "econ":      {"intel"},
    "alert":     {"intel", "trade"},   # 掃描警報→intel；熔斷可進 trade
    "system":    {"system", None},
    "invite":    {None},                # 私訊
    "suggestion": {"ideas", None},
}


def infer_msg_kind(text: str) -> str:
    low = _normalize(text)
    # 先比強特徵（命中即定案）
    for kind, sigs in STRONG_SIGNATURES:
        if any(s in low for s in sigs):
            return kind
    # 再用弱特徵計分
    best, best_score = "unknown", 0
    for kind, sigs in KIND_SIGNATURES:
        score = sum(1 for s in sigs if s in low)
        if score > best_score:
            best, best_score = kind, score
    return best


# ── 三道檢查 ──────────────────────────────────────────────────────────────
def check_route(topic_key: str | None, msg_kind: str) -> str:
    if msg_kind == "unknown":
        return "ok"   # 推不出類型就不報路由（避免雜訊）
    allowed = KIND_TO_TOPICS.get(msg_kind)
    if allowed is None:
        return "ok"
    if topic_key in allowed:
        return "ok"
    return f"MISROUTE:kind={msg_kind},topic={topic_key},expected={'/'.join(str(a) for a in allowed)}"


def check_duplicate(content_hash: str, struct_hash: str) -> tuple[str, bool]:
    """回 (verdict, should_block)。逐字重複 → block。"""
    now = int(time.time())
    conn = _conn()
    try:
        ex = conn.execute(
            "SELECT id, sent_at FROM send_log WHERE content_hash=? AND sent_at > ? "
            "ORDER BY id DESC LIMIT 1", (content_hash, now - DUP_EXACT_WINDOW_S)).fetchone()
        if ex:
            return f"DUP:exact,id={ex[0]},age={now-ex[1]}s", True
        tmpl = conn.execute(
            "SELECT COUNT(*) FROM send_log WHERE struct_hash=? AND sent_at > ?",
            (struct_hash, now - DUP_TMPL_WINDOW_S)).fetchone()[0]
        if tmpl >= 2:
            return f"DUP:template,recent={tmpl}", False
        return "ok", False
    finally:
        conn.close()


def check_clarity(text: str, msg_kind: str) -> list[str]:
    problems = []
    norm = _normalize(text)
    if len(norm) < MIN_CONTENT_LEN and msg_kind in ("news", "usnews", "unknown"):
        problems.append("too_short")
    # 佔位/抓取失敗殘跡
    if any(p in norm for p in ("[no title]", "rt @", "net.", "read the full release below")):
        problems.append("placeholder_or_fragment")
    # 交易類訊息必須有方向
    if msg_kind in ("fire", "waiting", "tp_sl", "order_card"):
        if not any(d in text for d in ("做多", "做空", "多單", "空單", "long", "short", "bull", "bear")):
            problems.append("trade_msg_no_direction")
    return problems


@dataclass
class AuditVerdict:
    route: str = "ok"
    dup: str = "ok"
    clarity: list[str] = field(default_factory=list)
    severity: str | None = None   # None / 'info' / 'warn' / 'block'
    msg_kind: str = "unknown"
    content_hash: str = ""
    struct_hash: str = ""

    @property
    def should_block(self) -> bool:
        return self.severity == "block"


def audit_message(text: str, *, topic_key: str | None) -> AuditVerdict:
    """規則式三檢合一。不碰網路。"""
    init_db()
    kind = infer_msg_kind(text)
    ch, sh = _h(_normalize(text)), _h(_skeletonize(text))
    v = AuditVerdict(msg_kind=kind, content_hash=ch, struct_hash=sh)
    v.route = check_route(topic_key, kind)
    v.dup, block = check_duplicate(ch, sh)
    v.clarity = check_clarity(text, kind)

    if block:
        v.severity = "block"
    elif v.route.startswith("MISROUTE") or v.dup.startswith("DUP") or v.clarity:
        v.severity = "warn"
    else:
        v.severity = "info"
    return v


def log_send(text: str, v: AuditVerdict, *, topic_key: str | None,
             blocked: bool) -> None:
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO send_log (sent_at, topic_key, msg_kind, content_hash, "
            "struct_hash, text_preview, full_len, audit_route, audit_dup, "
            "audit_clarity, severity, blocked) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), topic_key, v.msg_kind, v.content_hash, v.struct_hash,
             _normalize(text)[:300], len(text), v.route, v.dup,
             ",".join(v.clarity) or "ok", v.severity, 1 if blocked else 0))
    finally:
        conn.close()


# ── 週期稽核報告 worker ────────────────────────────────────────────────────
async def run_audit_loop(tg_admin, interval_seconds: int = 3600):
    """每小時把稽核警示彙整推給管理員（私訊）。無警示則靜默。"""
    import asyncio
    init_db()
    await asyncio.sleep(min(interval_seconds, 120))
    while True:
        try:
            conn = _conn()
            try:
                rows = conn.execute(
                    "SELECT id, msg_kind, topic_key, audit_route, audit_dup, "
                    "audit_clarity, severity, blocked FROM send_log "
                    "WHERE reported=0 AND severity IN ('warn','block') "
                    "ORDER BY id DESC LIMIT 30").fetchall()
                ids = [r[0] for r in rows]
                if ids:
                    conn.execute(
                        f"UPDATE send_log SET reported=1 WHERE id IN "
                        f"({','.join('?'*len(ids))})", ids)
                total = conn.execute(
                    "SELECT COUNT(*) FROM send_log WHERE sent_at > ?",
                    (int(time.time()) - interval_seconds,)).fetchone()[0]
            finally:
                conn.close()
            if rows and tg_admin is not None:
                blocked = [r for r in rows if r[7]]
                misroute = [r for r in rows if (r[3] or "").startswith("MISROUTE")]
                dups = [r for r in rows if (r[4] or "").startswith("DUP")]
                unclear = [r for r in rows if r[5] and r[5] != "ok"]
                lines = [f"🔍 <b>稽核 Session 報告</b>（過去 {interval_seconds//3600}h 共 {total} 則）",
                         "━━━━━━━━━━━━━━━━"]
                lines.append(f"🚫 已擋逐字重複：{len(blocked)}")
                lines.append(f"📍 路由疑慮：{len(misroute)}")
                lines.append(f"♻️ 重複疑慮：{len(dups)}")
                lines.append(f"❓ 不明確：{len(unclear)}")
                for r in (misroute[:3] + unclear[:3]):
                    detail = r[3] if (r[3] or "").startswith("MISROUTE") else r[5]
                    lines.append(f"  • [{r[1]}→{r[2]}] {detail}")
                lines.append("\n<i>稽核器自動運行中，逐字重複已即時攔截，"
                             "其餘為改進線索。</i>")
                try:
                    await tg_admin.send_message("\n".join(lines), parse_mode="HTML")
                except Exception:
                    pass
                print(f"[auditor] reported {len(rows)} warnings")
            # 清理 7 天前舊紀錄
            conn = _conn()
            try:
                conn.execute("DELETE FROM send_log WHERE sent_at < ?",
                             (int(time.time()) - 7 * 86400,))
            finally:
                conn.close()
        except Exception as e:
            print(f"[auditor] loop error: {type(e).__name__}: {e}")
        await asyncio.sleep(interval_seconds)


def audit_stats(hours: int = 24) -> dict:
    init_db()
    conn = _conn()
    try:
        since = int(time.time()) - hours * 3600
        total = conn.execute("SELECT COUNT(*) FROM send_log WHERE sent_at>?", (since,)).fetchone()[0]
        blocked = conn.execute("SELECT COUNT(*) FROM send_log WHERE sent_at>? AND blocked=1", (since,)).fetchone()[0]
        warn = conn.execute("SELECT COUNT(*) FROM send_log WHERE sent_at>? AND severity='warn'", (since,)).fetchone()[0]
        return {"total": total, "blocked": blocked, "warn": warn}
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    # 煙霧測試
    # v82: 修正過期煙霧樣本——fire 強特徵是「可立即執行/倉位配置（R」，原樣本缺之故誤判 unknown
    v1 = audit_message("🔥 <b>BTC/USDT</b> 做多 可立即執行 倉位配置（R 計）進場區間 $65000", topic_key="trade")
    assert v1.msg_kind == "fire" and v1.route == "ok", v1
    v2 = audit_message("📊 每日宏觀分析 regime=bull", topic_key="trade")  # 錯主題
    assert v2.route.startswith("MISROUTE"), v2
    log_send("🔥 dup test 做多", audit_message("🔥 dup test 做多", topic_key="trade"),
             topic_key="trade", blocked=False)
    v3 = audit_message("🔥 dup test 做多", topic_key="trade")
    assert v3.should_block, v3   # 逐字重複應 block
    v4 = audit_message("True", topic_key="news")
    assert "too_short" in v4.clarity, v4
    conn = _conn(); conn.execute("DELETE FROM send_log"); conn.close()
    print("message_auditor 煙霧測試 ALL PASS")
    print("stats:", audit_stats())
