"""Risk Manager：FIRE 前風控檢查 + 熔斷機制。

規則：
- 單筆風險上限 1R（金額由 botconfig 單一來源；支援帳戶 % 制，3000U 小資友善）
- 同時最多 N 筆持倉（預設 3）
- 總曝險上限：所有未平倉風險加總 + 本筆 不得超過 帳戶 × cap%（預設 6%）  ← P0-A 新增
- 每日最多開倉 N 次（預設 3，防情緒連續開倉）                          ← P0-A 新增
- BTC family (BTC/ETH/SOL) 同方向最多 2 筆（高相關）
- 同 symbol 同方向只允許 1 筆
- 每日 PnL -3% → 暫停新 FIRE 至隔日 UTC 00:00
- 每週 PnL -7% → 完全暫停 + 強制人工 review

API:
    should_block(decision_dict) -> (blocked: bool, reason: str, details: dict)
    get_risk_status() -> dict  # 即時風險狀態，給 supervisor 用
"""
from __future__ import annotations

import datetime as dt
import os
import time
from dataclasses import dataclass, field
from typing import Any

from .trade_journal import (
    count_opens_today, get_open_trades, get_today_pnl, get_week_pnl,
)

# P0-A: 風控金額/帳戶/筆數一律走 botconfig 單一來源（已含「帳戶 %」換算與 clamp），
# 不再各自 os.getenv 重複讀 → 與實際倉位計算一致，且 3000U 小資的 % 制自動生效。
from botconfig import CONFIG as _BC


# === 風控參數（帳戶/金額/併發走 botconfig；風控專屬閘門可由 env 覆寫）===
@dataclass(frozen=True)
class RiskConfig:
    # 帳戶層（單一來源：botconfig，已含 RISK_PER_TRADE_PCT 帳戶 % 制換算）
    account_balance_usd: float = _BC.account_balance_usd
    max_risk_per_trade_usd: float = _BC.risk_per_trade_usd

    # 部位層
    max_concurrent_trades: int = _BC.max_concurrent_trades
    max_per_family: int = int(os.getenv("MAX_PER_CORRELATED_FAMILY", "2"))

    # P0-A 新增：總曝險上限（% of 帳戶）+ 每日最多開倉次數
    # v42: 改走 botconfig 單一來源（依預算分級；明確 env 仍優先，tier 只填未設值）
    total_risk_cap_pct: float = _BC.total_risk_cap_pct
    daily_max_opens: int = _BC.daily_max_opens

    # 熔斷層
    daily_dd_limit_pct: float = float(os.getenv("DAILY_DD_LIMIT_PCT", "-3.0"))
    weekly_dd_limit_pct: float = float(os.getenv("WEEKLY_DD_LIMIT_PCT", "-7.0"))


# 高相關 family 對照表
CORRELATED_FAMILIES = {
    "btc_family":      ("BTC", "ETH", "SOL"),       # 三大主流，相關性 0.8+
    "alt_l1":          ("AVAX", "NEAR", "ATOM", "FTM"),
    "alt_perp":        ("SUI", "APT", "INJ", "SEI", "TIA"),
    "ai_token":        ("FET", "RNDR", "AGIX", "OCEAN"),
    "meme":            ("DOGE", "SHIB", "PEPE", "WIF"),
    "exchange_token":  ("BNB", "CRO", "OKB"),
}


def get_family(symbol: str) -> str | None:
    """查 symbol 屬於哪個相關 family"""
    for family, members in CORRELATED_FAMILIES.items():
        if symbol in members:
            return family
    return None


# ===========================================================================
# 主檢查函式
# ===========================================================================
def should_block(decision: dict | Any,
                 config: RiskConfig | None = None) -> tuple[bool, str, dict]:
    """檢查是否要 block 這筆 FIRE。

    Args:
        decision: dict（fire_queue serialize 後的）或 TriggerDecision 物件
        config: RiskConfig，預設讀環境變數

    Returns:
        (blocked, reason, details)
        - blocked: True = 拒絕、False = 放行
        - reason: 短碼（如 "daily_dd_breach"、"family_max_exceeded"）
        - details: 含 PnL/曝險/原因說明
    """
    cfg = config or RiskConfig()

    # 取 symbol + direction（支援 dict 或物件）
    if isinstance(decision, dict):
        snap = decision.get("snapshot", {})
        symbol = snap.get("symbol", "")
        direction = decision.get("direction", "")
    else:
        symbol = decision.snapshot.symbol
        direction = decision.direction.value

    details: dict[str, Any] = {
        "symbol": symbol, "direction": direction,
        "config": dataclass_to_dict(cfg),
    }

    # === Check 1: 每週熔斷（最高優先；先檢查避免日內反覆）===
    week = get_week_pnl(cfg.account_balance_usd)
    details["week_pnl"] = week
    if week["pnl_pct_of_account"] <= cfg.weekly_dd_limit_pct:
        return True, "weekly_dd_breach", {
            **details,
            "msg": f"週累計 PnL {week['pnl_pct_of_account']}% ≤ {cfg.weekly_dd_limit_pct}% → 強制暫停",
        }

    # === Check 2: 每日熔斷 ===
    today = get_today_pnl(cfg.account_balance_usd)
    details["today_pnl"] = today
    if today["pnl_pct_of_account"] <= cfg.daily_dd_limit_pct:
        return True, "daily_dd_breach", {
            **details,
            "msg": f"今日 PnL {today['pnl_pct_of_account']}% ≤ {cfg.daily_dd_limit_pct}% → 暫停至明日 UTC 00:00",
        }

    # === Check 2.5: 經濟數據靜默期（v16：高影響數據前30/後15分鐘暫停新訊號）===
    try:
        from news_feed.econ_calendar import in_blackout
        bo, ev_name = in_blackout()
        if bo:
            return True, "econ_blackout", {
                **details,
                "msg": f"高影響經濟數據「{ev_name}」發布窗口（前30/後15分），"
                       f"技術面在消息行情中失靈，暫停新訊號",
            }
    except Exception:
        pass  # 經濟日曆故障絕不能擋住交易管線

    # === Check 3: 同時最多持倉數 ===
    opens = get_open_trades()
    details["open_count"] = len(opens)
    details["open_symbols"] = [o["symbol"] for o in opens]
    if len(opens) >= cfg.max_concurrent_trades:
        return True, "max_concurrent_exceeded", {
            **details,
            "msg": f"已 {len(opens)} 筆持倉，上限 {cfg.max_concurrent_trades}",
        }

    # === Check 3.5: 總曝險上限（P0-A，3000U 小資保護）===
    # 所有未平倉風險加總 + 本筆 1R，不得超過 帳戶 × cap%。
    # 這是「$ 金額」層的安全網：即使單筆/併發都合規，總風險也不會失控。
    open_risk = sum((o.get("risk_usd") or cfg.max_risk_per_trade_usd) for o in opens)
    prospective_risk = open_risk + cfg.max_risk_per_trade_usd
    risk_cap_usd = cfg.account_balance_usd * cfg.total_risk_cap_pct / 100
    details["open_risk_usd"] = round(open_risk, 2)
    details["prospective_risk_usd"] = round(prospective_risk, 2)
    details["risk_cap_usd"] = round(risk_cap_usd, 2)
    if prospective_risk > risk_cap_usd:
        return True, "total_risk_cap_exceeded", {
            **details,
            "msg": f"總曝險 ${open_risk:.0f} + 本筆 ${cfg.max_risk_per_trade_usd:.0f} "
                   f"= ${prospective_risk:.0f} > 上限 ${risk_cap_usd:.0f} "
                   f"({cfg.total_risk_cap_pct}% × ${cfg.account_balance_usd:.0f})",
        }

    # === Check 3.6: 每日最多開倉次數（P0-A，防情緒連續開倉）===
    opened_today = count_opens_today()
    details["opened_today"] = opened_today
    if opened_today >= cfg.daily_max_opens:
        return True, "daily_max_opens_reached", {
            **details,
            "msg": f"今日已開倉 {opened_today} 次，達上限 {cfg.daily_max_opens}（防情緒連續交易）→ 明日 UTC 00:00 重置",
        }

    # === Check 4: 同 symbol 同方向重複 ===
    for o in opens:
        if o["symbol"] == symbol and (o.get("direction") or "") == direction:
            return True, "duplicate_symbol_direction", {
                **details,
                "msg": f"已有 {symbol}/{direction} 持倉中（trade_id={o['id']}）",
                "existing_trade_id": o["id"],
            }

    # === Check 5: 相關 family 上限 ===
    family = get_family(symbol)
    details["family"] = family
    if family:
        family_members = CORRELATED_FAMILIES[family]
        in_family_open = [o for o in opens if o["symbol"] in family_members]
        details["family_open_count"] = len(in_family_open)
        details["family_open_symbols"] = [o["symbol"] for o in in_family_open]
        if len(in_family_open) >= cfg.max_per_family:
            return True, "family_max_exceeded", {
                **details,
                "msg": f"{family} family 已 {len(in_family_open)} 筆持倉 "
                       f"({', '.join(o['symbol'] for o in in_family_open)})，上限 {cfg.max_per_family}",
            }

    # === 全部通過 ===
    return False, "ok", {
        **details,
        "msg": f"風控通過 ({len(opens)}/{cfg.max_concurrent_trades} 持倉, "
               f"曝險 ${prospective_risk:.0f}/${risk_cap_usd:.0f}, "
               f"今日開倉 {opened_today}/{cfg.daily_max_opens}, "
               f"今日 PnL {today['pnl_pct_of_account']}%, 週 {week['pnl_pct_of_account']}%)",
    }


def dataclass_to_dict(obj) -> dict:
    """簡易 dataclass → dict（避免循環 import）"""
    return {k: getattr(obj, k) for k in obj.__dataclass_fields__}


# ===========================================================================
# 即時風險狀態（給 supervisor / dashboard）
# ===========================================================================
def get_risk_status(config: RiskConfig | None = None) -> dict:
    """回傳當前風險快照"""
    cfg = config or RiskConfig()
    opens = get_open_trades()
    today = get_today_pnl(cfg.account_balance_usd)
    week = get_week_pnl(cfg.account_balance_usd)

    # 統計每 family 持倉
    family_breakdown: dict[str, list[str]] = {}
    for o in opens:
        family = get_family(o["symbol"])
        if family:
            family_breakdown.setdefault(family, []).append(o["symbol"])

    # 算總曝險（每筆 risk_usd 累加）
    total_risk_open = sum(o.get("risk_usd") or cfg.max_risk_per_trade_usd for o in opens)
    risk_cap_usd = cfg.account_balance_usd * cfg.total_risk_cap_pct / 100
    opened_today = count_opens_today()

    # 算當前狀態旗標
    daily_breached = today["pnl_pct_of_account"] <= cfg.daily_dd_limit_pct
    weekly_breached = week["pnl_pct_of_account"] <= cfg.weekly_dd_limit_pct

    status = "halted_weekly" if weekly_breached else (
             "paused_daily" if daily_breached else "active")

    return {
        "status": status,
        "open_trades": len(opens),
        "open_symbols": [o["symbol"] for o in opens],
        "max_concurrent": cfg.max_concurrent_trades,
        "total_risk_open_usd": round(total_risk_open, 2),
        "risk_cap_usd": round(risk_cap_usd, 2),
        "total_risk_cap_pct": cfg.total_risk_cap_pct,
        "opened_today": opened_today,
        "daily_max_opens": cfg.daily_max_opens,
        "max_risk_per_trade": cfg.max_risk_per_trade_usd,
        "family_breakdown": family_breakdown,
        "today_pnl_usd": today["total_pnl_usd"],
        "today_pnl_pct": today["pnl_pct_of_account"],
        "today_n_trades": today["n_trades_closed"],
        "today_wins": today["n_wins"],
        "today_losses": today["n_losses"],
        "week_pnl_usd": week["total_pnl_usd"],
        "week_pnl_pct": week["pnl_pct_of_account"],
        "daily_dd_limit_pct": cfg.daily_dd_limit_pct,
        "weekly_dd_limit_pct": cfg.weekly_dd_limit_pct,
        "daily_breached": daily_breached,
        "weekly_breached": weekly_breached,
        "account_balance_usd": cfg.account_balance_usd,
    }


def render_risk_status(status: dict) -> str:
    """文字化風險狀態（給 Telegram 推送）"""
    icon = {"active": "🟢", "paused_daily": "🟡", "halted_weekly": "🔴"}.get(status["status"], "⚪")
    lines = [
        f"🛡 <b>風控狀態 {icon} {status['status'].upper()}</b>",
        f"━━━━━━━━━━━━━━━━",
        f"持倉：<code>{status['open_trades']}/{status['max_concurrent']}</code>",
    ]
    if status["open_symbols"]:
        lines.append(f"標的：<code>{', '.join(status['open_symbols'])}</code>")
    # P0-A: 總曝險 / 上限 + 今日開倉 / 上限（小資保護兩道閘門，永遠顯示）
    _cap = status.get("risk_cap_usd")
    if _cap is not None:
        lines.append(f"總曝險：<code>${status['total_risk_open_usd']:.0f} / "
                     f"${_cap:.0f}</code>（上限 {status.get('total_risk_cap_pct')}%）")
    if status.get("daily_max_opens") is not None:
        lines.append(f"今日開倉：<code>{status.get('opened_today', 0)} / "
                     f"{status['daily_max_opens']}</code>")
    if status["family_breakdown"]:
        for fam, syms in status["family_breakdown"].items():
            lines.append(f"  {fam}: {', '.join(syms)} ({len(syms)} 筆)")
    lines.append(f"\n今日 PnL：<code>${status['today_pnl_usd']:+.2f}</code> "
                 f"(<code>{status['today_pnl_pct']:+.2f}%</code>)  "
                 f"{status['today_wins']} 勝 / {status['today_losses']} 負")
    lines.append(f"本週 PnL：<code>${status['week_pnl_usd']:+.2f}</code> "
                 f"(<code>{status['week_pnl_pct']:+.2f}%</code>)")
    lines.append(f"\n熔斷閾值：日 <code>{status['daily_dd_limit_pct']}%</code> / "
                 f"週 <code>{status['weekly_dd_limit_pct']}%</code>")
    if status["daily_breached"]:
        lines.append("⚠️ <b>日線熔斷觸發！</b>暫停新 FIRE 至明日 UTC 00:00")
    if status["weekly_breached"]:
        lines.append("🚨 <b>週線熔斷觸發！</b>完全暫停，需人工 review")
    return "\n".join(lines)


# ===========================================================================
# 自測
# ===========================================================================
if __name__ == "__main__":
    # 模擬一個 FIRE decision
    decision = {
        "snapshot": {"symbol": "ETH"},
        "direction": "bull",
        "setup_name": "intraday",
    }
    blocked, reason, details = should_block(decision)
    print(f"\nFIRE decision: ETH bull intraday")
    print(f"Blocked: {blocked}  Reason: {reason}")
    print(f"Details: {details.get('msg')}")

    status = get_risk_status()
    print(f"\n當前風險狀態:")
    print(render_risk_status(status))
