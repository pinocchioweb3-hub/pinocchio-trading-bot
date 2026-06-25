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
import time
from dataclasses import dataclass

_WARNINGS: list[str] = []

# v27: 執行期覆寫層（Telegram /settings 選單寫入 bot_settings.json）
#      優先序：runtime override > env > 預設
try:
    from botpaths import data_dir as _data_dir
    _SETTINGS_FILE = _data_dir() / "bot_settings.json"
    # v56: 設定變更稽核軌跡（復盤引擎全自動化前置；每次寫入留 before/after/source/git_sha）
    _AUDIT_FILE = _data_dir() / "config_audit.jsonl"
except Exception:
    _SETTINGS_FILE = None
    _AUDIT_FILE = None

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
    """取原始字串值：override 優先，再 env。

    v56：``SHADOW_`` 前綴鍵仍隔離於熱路徑（一律回 None，只能用 get_shadow 讀）。這原是
    step0 寫入鎖的一環；使用者 2026-06-20 移除寫入鎖後，SHADOW_ 不再是「自動端唯一能寫」
    的安全邊界（自動優化器現可直接寫活鍵讓優化即時生效），而降為**選用的暫存區**——供
    champion/challenger 把『提議但尚未晉升』的參數先擱在 SHADOW_*、過統計閘後再寫成活鍵。
    保留此隔離無害且對分階段晉升有用。（紅線①的真正邊界在執行層而非此處——見 set_override。）"""
    if key.startswith("SHADOW_"):
        return None
    if key in _OVERRIDES and _OVERRIDES[key] not in (None, ""):
        return str(_OVERRIDES[key])
    return os.getenv(key)


def _git_sha() -> str:
    """目前 repo 短 commit（稽核軌跡用）。取不到一律回 'unknown'，絕不拋例外。"""
    try:
        import subprocess
        from pathlib import Path
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True, text=True, timeout=3)
        sha = (out.stdout or "").strip()
        return sha if sha else "unknown"
    except Exception:
        return "unknown"


def _audit_write(key: str, before, after, source: str) -> None:
    """附加一行 JSONL 設定變更稽核軌跡。永不因稽核失敗影響主流程（fail-safe）。"""
    if not _AUDIT_FILE:
        return
    try:
        rec = {
            "ts_ms": int(time.time() * 1000),
            "key": key,
            "before": before,
            "after": after,
            "source": source,
            "git_sha": _git_sha(),
        }
        with open(_AUDIT_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def set_override(key: str, value, *, source: str = "auto") -> None:
    """寫入執行期覆寫並持久化。

    v56 政策（使用者 2026-06-20 拍板**移除** step0 寫入鎖）：
      自動優化器（復盤／回測／分析綜合數據評估後）可**直接寫任何鍵**——含影響模擬盤
      行為的「活鍵」（槓桿／風險／策略開關）——讓優化結果即時生效，而非只寫永不生效的
      影子鍵。理由：現在跑的是模擬盤、不是真金白銀；只寫影子＝改得好看卻永不生效，錯誤
      參數會一直錯下去。把關靠**統計嚴謹度**（L2 stats／回測顯著性）而非人工逐次點頭。

    透明（紅線③）：每次寫入都留稽核軌跡（before/after/source/git_sha），供每日 CEO
      報告浮現「改了什麼／為何／依據哪些回測」，並可事後 revert_key 回滾——解決「變更被
      埋著、忙時看不到也改不到」的痛點。source 僅作稽核標註（human=/settings 親手按；
      auto=程式／優化器）。

    紅線①（真錢下單／轉帳 AI 永不自動執行）**不受影響**——它在『執行層』把關：真錢只能
      人工手動執行（okx-trade-mcp 全庫零呼叫＋黑名單），自動端 config 只驅動訊號／paper／
      OKX-demo 模擬盤，活鍵物理上到不了真錢執行層（三票對抗驗證 refuted=0、confidence
      high）。活鍵仍受 BotConfig.from_env 的範圍夾擠（如 leverage∈[1,50]、risk_pct≤20）
      保護——那是『範圍安全』非『寫入鎖』，保留。"""
    _load_overrides()
    before = _OVERRIDES.get(key)
    _OVERRIDES[key] = value
    if _SETTINGS_FILE:
        try:
            _SETTINGS_FILE.write_text(json.dumps(_OVERRIDES, ensure_ascii=False,
                                                 indent=2), encoding="utf-8")
        except Exception:
            pass
    _audit_write(key, before, value, source)
    reload()


def get_shadow(key: str, default=None):
    """讀取影子鍵（``SHADOW_*``）。僅供復盤/優化引擎讀回自己寫的建議參數；這些鍵被
    _raw 物理隔離，永不進入實盤熱路徑。傳入非 SHADOW_ 鍵一律拒讀（防誤把影子當活鍵用）。"""
    if not key.startswith("SHADOW_"):
        raise ValueError(f"get_shadow 只接受 SHADOW_* 鍵，收到 {key!r}")
    _load_overrides()
    v = _OVERRIDES.get(key)
    return v if v not in (None, "") else default


def revert_key(key: str, *, source: str = "human") -> bool:
    """人工回滾：把某鍵還原成稽核軌跡中「最後一次非回滾寫入之前」的值。
    回 True＝有還原；找不到歷史回 False。僅限人工（與 set_override 同鎖）。"""
    if source != "human":
        raise PermissionError("revert_key 僅限人工（source='human'）")
    if not _AUDIT_FILE or not _AUDIT_FILE.exists():
        return False
    last_before = None
    found = False
    try:
        for line in _AUDIT_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("key") == key and rec.get("source") != "revert":
                last_before = rec.get("before")
                found = True
    except Exception:
        return False
    if not found:
        return False
    _load_overrides()
    if last_before is None:
        _OVERRIDES.pop(key, None)
    else:
        _OVERRIDES[key] = last_before
    if _SETTINGS_FILE:
        try:
            _SETTINGS_FILE.write_text(json.dumps(_OVERRIDES, ensure_ascii=False,
                                                 indent=2), encoding="utf-8")
        except Exception:
            pass
    _audit_write(key, "<revert>", last_before, "revert")
    reload()
    return True


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


def _is_set(key: str) -> bool:
    """該鍵是否被使用者「明確」設定（override 或 env 且非空）。

    用來區分「使用者沒設 → 套用預算分級的保守預設」與「使用者明確設值 →
    一律以使用者為準（紅線②可覆寫）」。``_f``/``_i`` 的預設值替換看不出這個差別，
    故需要本函式。"""
    raw = _raw(key)
    return raw is not None and raw.strip() != ""


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


# ===========================================================================
# v42: 依預算自適應的風控分級（budget-adaptive tiering）
# ---------------------------------------------------------------------------
# 開源後每個自架者本金不同，原本「3000U 陪跑」太死。改成：本金是可設定參數，
# 風控護欄依本金分級。分級只提供「使用者沒設定時的保守預設」；任何明確設定的
# env/override 一律優先（紅線②可覆寫）。設計原則：本金越小、保護越嚴，且小本金
# 永遠不會比大本金更激進（單調保守）—— 這不是投資建議，是工具的安全預設。
# ===========================================================================
@dataclass(frozen=True)
class TierBand:
    name: str               # 分級代號（micro/small/standard/large）
    label: str              # 繁中標籤
    min_usd: float          # 本金下界（含）
    leverage_cap: int       # 未設 DEFAULT_LEVERAGE 時的預設槓桿（保守；可被明確 env 覆寫）
    risk_pct_default: float # 未明確設定風險時，1R = 帳戶 × 此 %
    total_risk_cap_pct: float  # 總曝險上限（帳戶 %）
    daily_max_opens: int    # 每日最多開倉次數
    spot_unlocked: bool     # 是否解鎖獨立現貨策略（小本金期貨手續費佔比過重 → 先不開）


# 由大到小排列；budget_tier 由上而下找第一個 balance >= min_usd
_TIERS: tuple[TierBand, ...] = (
    #          name        label    min_usd  lev  risk%  cap%  opens  spot
    TierBand("large",    "大資本",  10_000.0,   5,   1.0,  6.0,    3,  True),
    TierBand("standard", "標準",     5_000.0,   5,   1.0,  6.0,    3,  True),
    TierBand("small",    "小資本",   1_000.0,   5,   1.0,  6.0,    3,  False),
    TierBand("micro",    "微型",         0.0,   3,   1.0,  5.0,    2,  False),
)


def budget_tier(balance_usd: float) -> TierBand:
    """依帳戶本金回傳風控分級（純函式、無副作用、可離線測試）。

    分級只決定「未設定鍵」的保守預設值；明確設定的 env/override 一律優先。"""
    for t in _TIERS:
        if balance_usd >= t.min_usd:
            return t
    return _TIERS[-1]   # micro（min_usd=0），理論上不會落到這


@dataclass(frozen=True)
class BotConfig:
    # === 帳戶與風險（用戶最常自訂的三個）===
    account_balance_usd: float
    risk_per_trade_usd: float        # 單筆風險 = 1R 的美元值（最終生效值）
    risk_per_trade_pct: float        # v27: >0 時改用「帳戶 %」計算 1R（覆蓋固定 USD）
    max_concurrent_trades: int       # 最多同時持倉數
    default_leverage: int
    total_risk_cap_pct: float        # v42: 總曝險上限（帳戶 %）— 單一來源（原在 risk_manager）
    daily_max_opens: int             # v42: 每日最多開倉 — 單一來源（原在 risk_manager）
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

        # v42: 依預算分級。tier 只填「使用者沒設定」的鍵；明確 env/override 永遠優先。
        bal = _f("ACCOUNT_BALANCE_USD", 5000, 100, 10_000_000)
        tier = budget_tier(bal)

        # 風險（1R）優先序：明確 RISK_PER_TRADE_PCT>0 ＞ 明確 RISK_PER_TRADE_USD
        #                  ＞ 兩者皆未設 → 落 tier 保守 %（小本金永不更激進）
        # 安全夾擠上限 20%→5%：20%/單筆＝約 5 連敗近歸零（無法存活），屬誤設量級；
        # 5% 對齊使用者自定的最大風險偏好上限。注意：以現有紙上 maxDD≈-10.7R，
        # 即使 5% 也代表帳戶約 -53%，故 5% 為「硬上限」非建議值（建議 1R ≤2.5%）。
        pct = _f("RISK_PER_TRADE_PCT", 0.0, 0.0, 5.0)
        if pct > 0:
            risk_usd = round(bal * pct / 100, 2)
        elif _is_set("RISK_PER_TRADE_USD"):
            risk_usd = _f("RISK_PER_TRADE_USD", 100, 1, 10_000)
        else:
            pct = tier.risk_pct_default
            risk_usd = round(bal * pct / 100, 2)

        return cls(
            account_balance_usd=bal,
            risk_per_trade_usd=risk_usd,
            risk_per_trade_pct=pct,
            max_concurrent_trades=_i("MAX_CONCURRENT_TRADES", 3, 1, 20),
            # 未設 DEFAULT_LEVERAGE → tier 保守槓桿；明確設值一律優先（紅線②）
            default_leverage=_i("DEFAULT_LEVERAGE", tier.leverage_cap, 1, 50),
            # 未設則落 tier 預設（_f 的 default 即 tier 值 → 明確設值優先）
            total_risk_cap_pct=_f("TOTAL_RISK_CAP_PCT", tier.total_risk_cap_pct, 1.0, 50.0),
            daily_max_opens=_i("DAILY_MAX_OPENS", tier.daily_max_opens, 1, 50),
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

    @property
    def tier(self) -> TierBand:
        """目前本金對應的風控分級（含 spot_unlocked 等旗標）。"""
        return budget_tier(self.account_balance_usd)


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
    print(f"account_balance = ${c.account_balance_usd:,.0f}  → tier={c.tier.name}（{c.tier.label}）")
    print(f"risk_per_trade  = ${c.risk_per_trade_usd}  (pct={c.risk_per_trade_pct}%)")
    print(f"max_trades      = {c.max_concurrent_trades}")
    print(f"leverage        = {c.default_leverage}x")
    print(f"total_risk_cap  = {c.total_risk_cap_pct}%   daily_max_opens = {c.daily_max_opens}")
    print(f"spot_unlocked   = {c.tier.spot_unlocked}")
    print(f"SL intraday/ambush = {c.sl_pct_intraday}% / {c.sl_pct_ambush}%")
    print(f"TP intraday     = {c.tp_r_intraday}  split={c.tp_size_split}")

    # ===================================================================
    # 安全不變量自測（不依賴實際 .env / bot_settings.json）
    # 證明：現行 $5000 明確設定 → 零行為改變；清掉明確值 → 落 tier 保守預設。
    # ===================================================================
    print("\n--- v42 不變量自測 ---")
    _SETTINGS_FILE = None          # 停用 override 檔，讓自測純由 env 決定

    def _set(k, v):
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    # 不變量①：現行部署（明確 USD/槓桿、未設 PCT/CAP/OPENS）→ 與升級前完全一致
    for k in ("RISK_PER_TRADE_PCT", "TOTAL_RISK_CAP_PCT", "DAILY_MAX_OPENS"):
        _set(k, None)
    _set("ACCOUNT_BALANCE_USD", "5000")
    _set("RISK_PER_TRADE_USD", "100")
    _set("DEFAULT_LEVERAGE", "15")
    c = reload()
    assert c.risk_per_trade_usd == 100, c.risk_per_trade_usd
    assert c.default_leverage == 15, c.default_leverage
    assert c.total_risk_cap_pct == 6.0, c.total_risk_cap_pct
    assert c.daily_max_opens == 3, c.daily_max_opens
    assert c.tier.name == "standard", c.tier.name
    assert c.risk_per_trade_pct == 0.0, c.risk_per_trade_pct
    print("✓ ①現行 $5000 明確設定 → 1R=$100 / 15x / 6% / 3 opens（零行為改變）")

    # 不變量②：清掉明確 USD 與槓桿 → 落 Standard tier 預設（1.0%＝$50、5x）
    _set("RISK_PER_TRADE_USD", None)
    _set("DEFAULT_LEVERAGE", None)
    c = reload()
    assert c.risk_per_trade_usd == 50.0, c.risk_per_trade_usd   # 5000 × 1.0%
    assert c.default_leverage == 5, c.default_leverage
    assert c.risk_per_trade_pct == 1.0, c.risk_per_trade_pct
    print("✓ ②清掉明確值 → Standard 1.0%＝$50 / 5x")

    # 不變量③：micro 帳戶（$800）→ 最嚴護欄、現貨未解鎖
    _set("ACCOUNT_BALANCE_USD", "800")
    c = reload()
    assert c.tier.name == "micro", c.tier.name
    assert c.default_leverage == 3, c.default_leverage
    assert c.daily_max_opens == 2, c.daily_max_opens
    assert c.total_risk_cap_pct == 5.0, c.total_risk_cap_pct
    assert c.tier.spot_unlocked is False
    print("✓ ③$800 → micro 3x / 2 opens / 5% cap / 現貨未解鎖")

    # 不變量④：明確設定一律優先 — micro 帳戶仍可被使用者明確覆寫成 10x
    _set("DEFAULT_LEVERAGE", "10")
    c = reload()
    assert c.default_leverage == 10, c.default_leverage
    print("✓ ④明確 env 覆寫優先 — micro 帳戶仍可手動設 10x（紅線②可覆寫）")

    # 不變量⑤（v100 安全夾擠）：RISK_PER_TRADE_PCT 上限＝5% — 誤設 10%/20% 一律夾回 5
    _set("RISK_PER_TRADE_PCT", "10")
    c = reload()
    assert c.risk_per_trade_pct == 5.0, c.risk_per_trade_pct
    _set("RISK_PER_TRADE_PCT", None)
    print("✓ ⑤RISK_PER_TRADE_PCT 安全上限＝5%（誤設 10% 夾回 5，杜絕災難倉位）")

    print("--- 全部不變量通過 ---")
