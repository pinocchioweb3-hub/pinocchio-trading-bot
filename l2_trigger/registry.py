"""策略註冊表（v23-5）— 開源用戶可選策略選單的地基。

設計（UltraCode 分析 wf_b03830ec + Hummingbot controller × Jesse params schema）：
    每個策略 = 一筆 StrategyMeta（id / 顯示名 / 時框 / config 工廠 /
    可調參數 schema / 成熟度 / 預設啟用）。
    scheduler 從 enabled_strategies() 取啟用清單跑掃描，
    取代原本寫死的 `(get_intraday_config,)`。

啟用控制（env，用戶自訂）：
    STRATEGIES_ENABLED="intraday,alt_momentum"   # 白名單；空=用各自 enabled_default

成熟度（maturity）：
    live         已驗證，進實倉訊號流（trade + paper）
    paper        實驗中，只進 paper_journal（不推 FIRE 按鈕）
    experimental 開發中，預設關閉

新策略落地三件套：l2_trigger/signals_<id>.py（eval 純函式）+
    configs/<id>.py（config 工廠）+ 在此註冊一筆 StrategyMeta。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Literal


@dataclass(frozen=True)
class ParamSpec:
    """可調參數自描述（仿 Jesse hyperparameters）— 未來生成 Telegram 選單與驗證用。"""
    name: str
    type: Literal["int", "float", "bool", "enum"]
    default: object
    min: float | None = None
    max: float | None = None
    choices: tuple = ()
    label_zh: str = ""
    unit: str = ""


@dataclass(frozen=True)
class StrategyMeta:
    id: str                          # 唯一鍵（進 cooldown / journal tag）
    setup_name: str                  # 寫進 FIRE / trade_journal 的 setup 欄
    display_name_zh: str
    description_zh: str
    timeframes: tuple[str, ...]
    config_factory: Callable | None  # symbol -> TriggerConfig（None = 自有 worker）
    maturity: Literal["live", "paper", "experimental"]
    enabled_default: bool
    universe: Literal["watchlist", "btc_only", "us_whitelist", "all_market"] = "watchlist"
    params: tuple[ParamSpec, ...] = ()
    own_worker: bool = False         # True = 不走 scheduler（如美股自有 loop）


def _lazy(modpath: str, fn: str) -> Callable:
    """延遲載入 config 工廠（避免 import 環）。"""
    def _factory(symbol: str, *a, **k):
        import importlib
        return getattr(importlib.import_module(modpath), fn)(symbol, *a, **k)
    return _factory


REGISTRY: dict[str, StrategyMeta] = {
    "intraday": StrategyMeta(
        id="intraday", setup_name="intraday",
        display_name_zh="日內爆發",
        description_zh="ATR 收斂 + 量縮後爆量突破，5 過濾器投票制（≥2 同向）",
        timeframes=("4h", "1h"),
        config_factory=_lazy("l2_trigger.configs.intraday", "get_intraday_config"),
        maturity="live", enabled_default=True,
        params=(
            ParamSpec("min_votes", "int", 2, 1, 5, label_zh="最少同向票數"),
        ),
    ),
    "ambush": StrategyMeta(
        id="ambush", setup_name="ambush",
        display_name_zh="左側埋伏",
        description_zh="支撐區下方分批限價埋伏，等回踩（待 SMC+Wyckoff 重構驗證）",
        timeframes=("4h", "1h"),
        config_factory=_lazy("l2_trigger.configs.ambush", "get_ambush_config"),
        maturity="paper", enabled_default=False,
    ),
    "alt_momentum": StrategyMeta(
        id="alt_momentum", setup_name="intraday",
        display_name_zh="山寨強勢幣",
        description_zh="只在強勢分數排行前段的山寨幣觸發（intraday + 強勢門檻）",
        timeframes=("4h", "1h"),
        config_factory=_lazy("l2_trigger.configs.intraday", "get_intraday_config"),
        maturity="paper", enabled_default=False,
        universe="watchlist",
    ),
    "us_breakout": StrategyMeta(
        id="us_breakout", setup_name="us_breakout",
        display_name_zh="美股代幣突破",
        description_zh="美股永續代幣盤中突破（QQQ 閘門 + 開盤時段），紙上實驗",
        timeframes=("1h",),
        config_factory=None, own_worker=True,
        maturity="experimental", enabled_default=True,
        universe="us_whitelist",
    ),
    # === 規劃中（待實作 signals_<id>.py，先登錄讓選單可見、roadmap 透明）===
    "smc_mtf": StrategyMeta(
        id="smc_mtf", setup_name="smc_mtf",
        display_name_zh="SMC 多時框結構",
        description_zh="BOS/CHoCH/OB/FVG 多時框（4h/1h/15m/5m）— 開發中",
        timeframes=("4h", "1h", "15m", "5m"),
        config_factory=None, maturity="experimental", enabled_default=False,
    ),
    "snr": StrategyMeta(
        id="snr", setup_name="snr",
        display_name_zh="支撐阻力 S&R",
        description_zh="高低密集區的支撐阻力反應 — 開發中",
        timeframes=("4h", "1h"),
        config_factory=None, maturity="experimental", enabled_default=False,
    ),
    "wyckoff": StrategyMeta(
        id="wyckoff", setup_name="wyckoff",
        display_name_zh="威克夫長波段",
        description_zh="吸籌/派發階段判定（atr 收斂+量縮+OI 穩定的擴充）— 開發中",
        timeframes=("1d", "4h"),
        config_factory=None, maturity="experimental", enabled_default=False,
    ),
    "fib": StrategyMeta(
        id="fib", setup_name="fib",
        display_name_zh="斐波那契回撤/推展",
        description_zh="swing 高低 + 0.382/0.5/0.618 回撤位反應 — 開發中",
        timeframes=("4h", "1h"),
        config_factory=None, maturity="experimental", enabled_default=False,
        params=(
            ParamSpec("retrace", "enum", "0.618", choices=("0.382", "0.5", "0.618"),
                      label_zh="回撤位"),
        ),
    ),
}


def enabled_strategies() -> list[StrategyMeta]:
    """目前啟用的策略（會被 scheduler 跑）。
    STRATEGIES_ENABLED 白名單優先；否則用各策略 enabled_default。"""
    wl = (os.getenv("STRATEGIES_ENABLED") or "").strip()
    if wl:
        ids = {s.strip() for s in wl.split(",") if s.strip()}
        return [m for m in REGISTRY.values() if m.id in ids]
    return [m for m in REGISTRY.values() if m.enabled_default]


def scheduler_strategies() -> list[StrategyMeta]:
    """scheduler 實際要跑的（啟用 + 有 config_factory + 非自有 worker）。"""
    return [m for m in enabled_strategies()
            if m.config_factory is not None and not m.own_worker]


def render_strategy_menu() -> str:
    """策略選單（給 /strategies 指令與系統狀態顯示）。"""
    enabled_ids = {m.id for m in enabled_strategies()}
    mat_icon = {"live": "🟢", "paper": "🧪", "experimental": "🔬"}
    lines = ["📋 <b>策略清單</b>（開源可選）", "━━━━━━━━━━━━━━━━"]
    for m in REGISTRY.values():
        on = "✅" if m.id in enabled_ids else "⬜"
        tf = "/".join(m.timeframes)
        lines.append(f"{on} {mat_icon.get(m.maturity, '')} <b>{m.display_name_zh}</b>"
                     f"（<code>{m.id}</code>，{tf}）")
        lines.append(f"     <i>{m.description_zh}</i>")
    lines.append("\n🟢 已驗證｜🧪 紙上實驗｜🔬 開發中")
    lines.append("<i>啟用設定：.env 的 STRATEGIES_ENABLED（逗號分隔 id）；"
                 "時框/觀察清單/單筆風險/最多倉位皆可在 .env 自訂。</i>")
    return "\n".join(lines)


if __name__ == "__main__":
    print(f"註冊策略 {len(REGISTRY)} 個")
    print(f"啟用中：{[m.id for m in enabled_strategies()]}")
    print(f"scheduler 跑：{[m.id for m in scheduler_strategies()]}")
    print()
    print(render_strategy_menu().replace("<b>", "").replace("</b>", "")
          .replace("<i>", "").replace("</i>", "").replace("<code>", "")
          .replace("</code>", ""))
