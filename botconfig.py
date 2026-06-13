"""botconfig.py — 全域唯一配置來源（v23-2）。

規則：env 讀取 → 型別轉換 → 範圍夾擠（clamp）→ frozen dataclass 單例。
worker / 渲染 / 帳本一律 `from botconfig import CONFIG`，禁止再寫字面值。

修正的歷史債（UltraCode 稽核 2026-06-13）：
    - RISK_PER_TRADE_USD 原本只進風控統計，不影響實際倉位計算（三處硬編碼 100.0）
    - MAX_CONCURRENT_POSITIONS（.env.example 原鍵名）從未被讀取 — 程式讀的是
      MAX_CONCURRENT_TRADES → 這裡雙鍵 fallback 相容
    - DEFAULT_LEVERAGE 從未被讀取
    - SL%/TP R 倍數在 dispatcher 與 message_format 各複製一份（v15 曾因此出過
      訊息 3.5% / 帳本 4.0% 的不同步事故）→ 單一來源化
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

_WARNINGS: list[str] = []

# v27: 執行期覆寫層（Telegram /settings 選單寫入 bot_settings.json）
#      優先序：runtime override > env > 預設
try:
    from botpaths import data_dir as _data_dir
    _SETTINGS_FILE = _data_dir() / "bot_settings.json"
except Exception:
    _SETTINGS_FILE = None

_OVERRIDES: dict = {}


def _load_overrides() -> None:
    global _OVERRIDES
    _OVERRIDES = {}
    try:
        if _SETTINGS_FILE and _SETTINGS_FILE.exists():
            _OVERRIDES = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        _OVERRIDES = {}


def _raw(key: str):
    """取原始字串值：override 優先，再 env。"""
    if key in _OVERRIDES and _OVERRIDES[key] not in (None, ""):
        return str(_OVERRIDES[key])
    return os.getenv(key)


def set_override(key: str, value) -> None:
    """寫入執行期覆寫並持久化（選單用）。"""
    _load_overrides()
    _OVERRIDES[key] = value
    if _SETTINGS_FILE:
        try:
            _SETTINGS_FILE.write_text(json.dumps(_OVERRIDES, ensure_ascii=False,
                                                 indent=2), encoding="utf-8")
        except Exception:
            pass
    reload()


_load_overrides()


def _f(key: str, default: float, lo: float, hi: float) -> float:
    raw = _raw(key)
    if raw is None or not raw.strip():
        return default
    try:
        v = float(raw)
    except ValueError:
        _WARNINGS.append(f"{key}={raw!r} 不是數字，回退預設 {default}")
        return default
    if not (lo <= v <= hi):
        _WARNINGS.append(f"{key}={v} 超出範圍 [{lo}, {hi}]，已夾擠")
    return max(lo, min(hi, v))


def _i(key: str, default: int, lo: int, hi: int) -> int:
    return int(_f(key, float(default), float(lo), float(hi)))


def _tf(key: str, default: tuple[float, ...], lo: float = 0.1,
        hi: float = 20.0) -> tuple[float, ...]:
    """逗號分隔浮點數列（如 TP_R_INTRADAY=1.0,1.5,2.0）。強制遞增、長度=3。"""
    raw = _raw(key)
    if raw is None or not raw.strip():
        return default
    try:
        vals = tuple(float(x) for x in raw.split(","))
        if len(vals) != len(default) or any(not (lo <= v <= hi) for v in vals) \
           or list(vals) != sorted(vals):
            raise ValueError
        return vals
    except ValueError:
        _WARNINGS.append(f"{key}={raw!r} 格式錯誤（需 {len(default)} 個遞增數字），回退預設")
        return default


@dataclass(frozen=True)
class BotConfig:
    # === 帳戶與風險（用戶最常自訂的三個）===
    account_balance_usd: float
    risk_per_trade_usd: float        # 單筆風險 = 1R 的美元值（最終生效值）
    risk_per_trade_pct: float        # v27: >0 時改用「帳戶 %」計算 1R（覆蓋固定 USD）
    max_concurrent_trades: int       # 最多同時持倉數
    default_leverage: int
    # === 交易計畫 ===
    sl_pct_intraday: float
    sl_pct_ambush: float
    tp_r_intraday: tuple[float, ...]
    tp_r_ambush: tuple[float, ...]
    tp_size_split: tuple[float, ...]   # 分批比例，總和必須 = 1.0
    trading_size: int                  # v27: 訊號層動態 Top N（全市場挑強勢）

    @classmethod
    def from_env(cls) -> "BotConfig":
        # MAX_CONCURRENT_TRADES 優先；舊鍵 MAX_CONCURRENT_POSITIONS 相容
        max_trades_raw = os.getenv("MAX_CONCURRENT_TRADES") or \
            os.getenv("MAX_CONCURRENT_POSITIONS") or "3"
        os.environ.setdefault("MAX_CONCURRENT_TRADES", max_trades_raw)

        split = _tf("TP_SIZE_SPLIT", (0.5, 0.3, 0.2), lo=0.05, hi=0.9)
        if abs(sum(split) - 1.0) > 0.01:
            _WARNINGS.append(f"TP_SIZE_SPLIT 總和 {sum(split)} ≠ 1.0，回退預設")
            split = (0.5, 0.3, 0.2)

        # v27: 風險百分比模式 — RISK_PER_TRADE_PCT>0 時，1R = 帳戶 × %
        bal = _f("ACCOUNT_BALANCE_USD", 5000, 100, 10_000_000)
        pct = _f("RISK_PER_TRADE_PCT", 0.0, 0.0, 20.0)
        risk_usd = round(bal * pct / 100, 2) if pct > 0 else _f("RISK_PER_TRADE_USD", 100, 1, 10_000)

        return cls(
            account_balance_usd=bal,
            risk_per_trade_usd=risk_usd,
            risk_per_trade_pct=pct,
            max_concurrent_trades=_i("MAX_CONCURRENT_TRADES", 3, 1, 20),
            default_leverage=_i("DEFAULT_LEVERAGE", 15, 1, 50),
            sl_pct_intraday=_f("SL_PCT_INTRADAY", 4.0, 0.5, 15.0),
            sl_pct_ambush=_f("SL_PCT_AMBUSH", 5.0, 0.5, 20.0),
            tp_r_intraday=_tf("TP_R_INTRADAY", (1.0, 1.5, 2.0)),
            tp_r_ambush=_tf("TP_R_AMBUSH", (1.0, 1.5, 2.5)),
            tp_size_split=split,
            trading_size=_i("TRADING_SIZE", 15, 3, 40),   # v29: 12→15（掃描穩定後再擴）
        )

    def sl_pct(self, setup: str) -> float:
        return self.sl_pct_intraday if setup == "intraday" else self.sl_pct_ambush

    def tp_r(self, setup: str) -> tuple[float, ...]:
        return self.tp_r_intraday if setup == "intraday" else self.tp_r_ambush


CONFIG = BotConfig.from_env()

if _WARNINGS:
    for w in _WARNINGS:
        print(f"[botconfig] ⚠️ {w}")


def get_str(key: str, default: str = "") -> str:
    """字串設定（override > env > default）— 給策略白名單等非數值設定用。"""
    v = _raw(key)
    return v if v not in (None, "") else default


def reload() -> BotConfig:
    """設定熱更新（/settings 選單寫入後呼叫）"""
    global CONFIG
    _WARNINGS.clear()
    _load_overrides()
    CONFIG = BotConfig.from_env()
    return CONFIG


if __name__ == "__main__":
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(Path(__file__).resolve().parent / ".env")
    c = reload()
    print(f"risk_per_trade  = ${c.risk_per_trade_usd}")
    print(f"max_trades      = {c.max_concurrent_trades}")
    print(f"leverage        = {c.default_leverage}x")
    print(f"SL intraday/ambush = {c.sl_pct_intraday}% / {c.sl_pct_ambush}%")
    print(f"TP intraday     = {c.tp_r_intraday}  split={c.tp_size_split}")
