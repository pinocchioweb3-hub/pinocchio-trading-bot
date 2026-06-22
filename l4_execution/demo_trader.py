"""OKX 模擬盤自動交易層 (demo_trader) — Task #4 的「操盤手大腦」。

定位：把一筆通過 L2 觸發 + L3 風控的 FIRE 訊號，轉成一份完整、可審計、
冪等的下單計畫，並（在能正向證明模擬盤時）下到 OKX 模擬盤。

設計鐵律（承 research workflow `oss-trading-bot-risk-design` 的規格定案）：
  1. 風險驅動倉位，不是保證金驅動。名目由 risk_usd / |entry-stop| 決定
     （reuse l2_trigger.leverage.compute_position，**不重算**）。槓桿是「衍生值」，
     只決定保證金、永遠不決定風險：取能讓 OKX 清算價落在止損「之外」的最高合理槓桿。
     —— 這直接修掉舊 okx_executor.py:183 的 margin*leverage 定倉 bug。
  2. 進場與防線「原子綁定」：進場 LIMIT 單，止損＋分批止盈以 OKX 原生附帶算法單
     (attachAlgoOrds) 依附其上，**成交時才一起生效**——不會有「倉位還沒成交就先掛
     reduce-only TP」的裸單（修掉舊版 place_demo_plan 的 TP-before-fill bug）。止損市價
     (slOrdPx='-1') 可 survive 斷線；止盈分批 40/30/30、最後一腿吃餘數
     （修舊 okx_executor.py:230 平均分配會掉一張的 bug）。
     ⚠️ 逾時平倉(TIME_LIMIT_HOURS) **目前尚未實作**：它需要一個常駐監控層輪詢倉位年齡
     並主動市價平倉，本下單層不負責；計畫的 time_limit_hours 僅記錄「意圖」，真正執行
     待監控層接上（見 fetch_okx_positions / reconcile_positions 之後的 monitor TODO）。
  3. 相關性用「桶風險上限」取代舊的「每族最多 2 筆」：BTC/ETH/SOL 同屬 crypto-beta 桶，
     桶內風險預算加總上限 = 2R（200U），第 3 筆相關單一律拒。
  4. 每筆下單前先過預算檢查（required_margin = notional/leverage + 手續費 + buffer）；
     付不起整筆 → 拒絕並告警，絕不開半套破倉位。
  5. 冪等：每個「意圖」帶持久 intent_id + 決定性 clOrdId，逾時重送會撞回原單、不會開雙倉。
  6. 對帳：拉 OKX 真實持倉/成交 DIFF 本地帳本，任何漂移 → 停新單 + 告警。
  7. **預設拒絕的模擬盤閘**：所有真正下單路徑都先過 demo_guard 正向證明，
     證不出在模擬盤就不下。實盤永遠 OFF，需使用者另行明確拍板（本模組不主動）。

本模組刻意**不走** l4_execution.okx_executor（它有上述 bug 且帶唯讀金鑰退路）。

純函式層（無網路、可離線自測）：sizing / 槓桿上限 / TP 分腿 / 桶風險 / 預算 /
                                 clOrdId / 對帳 DIFF / kill switch。
連線層（需 OKX_DEMO_* + 網路，過 demo_guard 才動）：實際下單 / 拉持倉對帳。

CLI：
    python -m l4_execution.demo_trader --selftest    # 離線自測純邏輯
    python -m l4_execution.demo_trader --plan BTC bull 65000 63700 --atr 4.2
                                                     # 印出一筆下單計畫（不下單）
    python -m l4_execution.demo_trader --check       # 帶 .env 連 OKX 驗證模擬盤可下單前置
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Optional

from botconfig import CONFIG as _BC
from l2_trigger.leverage import (
    choose_leverage, compute_position, compute_tp_prices,
    leverage_for_stop, LEVERAGE_OVERRIDES,
)

# ---------------------------------------------------------------------------
# 規格常數（research spec 定案；可由 .env / RiskConfig 覆寫處標註）
# ---------------------------------------------------------------------------
# v42: 1R 走 botconfig 單一來源（依預算分級；明確 env 優先）。與 risk_manager 一致，
#      不再各自硬編碼 100.0。（本層尚未接 daemon，零在倉影響，但先正源以防未來接線。）
RISK_PER_TRADE_USD = _BC.risk_per_trade_usd          # 1R
BUCKET_RISK_CAP_USD = round(_BC.risk_per_trade_usd * 2, 2)   # 相關桶上限 = 2R（第 3 筆相關單拒）
TP_WEIGHTS = (0.40, 0.30, 0.30)     # TP1/TP2/TP3 分腿比例（最後一腿吃餘數）
TP_R_MULTIPLES = (1.0, 1.5, 2.0)    # 與 types.TriggerConfig 一致
SL_PAD_PCT = 0.5                    # 止損觸發價緩衝（快市才填得到）
TAKER_FEE_RATE = 0.0005             # OKX 永續 taker ~0.05%
BUDGET_BUFFER_PCT = 5.0             # 預算安全 buffer
TIME_LIMIT_HOURS = 24              # 逾時平倉（stale signal 自動出場）
MIN_REALIZED_RISK_RATIO = 0.5      # 防禦性下界：取整後實際風險 < 0.5R → 拒絕（floor-only 設計下幾乎不觸發，留作未來若改採 min-bump 的回歸防線）
LIQ_BUFFER = 0.25                  # 要求清算距離 ≥ 止損距離 ×(1+此值)
MAINT_MARGIN_RATE = 0.0065         # 保守維持保證金率（mmr，含手續費緩衝）。逐倉真實清算距離
                                   # ≈ entry×(1/lev − mmr)，故清算封頂必須納入 mmr，否則窄止損會
                                   # 讓清算先於止損。實際各標的/各檔位 mmr 由連線層 instruments tier
                                   # 取真值；純函式層以此偏保守值注入（對抗式審查 finding #3）。
QTY_DRIFT_TOL = 1e-6               # 對帳數量容差（張）

# 上線階梯門檻（僅文件用；實盤需使用者明確拍板，本模組不會自動切）
LIVE_GATE_MIN_DEMO_TRADES = 30
LIVE_GATE_MIN_DEMO_DAYS = 7

# OKX 永續慣例：1 張 BTC-USDT-SWAP = 0.01 BTC（ctVal）。各標的不同，連線時由
# market_get_instruments / ccxt market['contractSize'] 取真值；純函式以參數注入。
DEFAULT_CT_VAL = 0.01
DEFAULT_LOT_SZ = 1.0               # 多數 USDT 永續最小變動 1 張
DEFAULT_MIN_SZ = 1.0


# ---------------------------------------------------------------------------
# 資料型別
# ---------------------------------------------------------------------------
@dataclass
class TpLeg:
    label: str          # 'tp1' / 'tp2' / 'tp3'
    contracts: float    # OKX 張數（reduce-only）
    price: float
    r: float
    size_pct: float     # 占總倉位比例（記帳用）


@dataclass
class OrderPlan:
    """一筆完整、可審計的下單計畫。reject_reason 非 None ⇒ 不該下單。"""
    symbol: str
    direction: str                 # 'bull' / 'bear'
    entry: float
    stop: float
    leverage: int
    notional_usd: float
    margin_usd: float
    qty_base: float                # 名目對應的基礎幣數量（如 BTC 顆數）
    contracts: float               # OKX 張數（已向下取整到 lot_sz、≥ min_sz）
    ct_val: float
    lot_sz: float
    min_sz: float
    risk_usd: float
    realized_risk_usd: float       # 取整後實際 1R 風險（USD）
    sl_trigger: float              # 止損觸發價
    tp_legs: list[TpLeg] = field(default_factory=list)
    time_limit_hours: int = TIME_LIMIT_HOURS
    intent_id: str = ""
    cl_ord_id: str = ""
    reject_reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.reject_reason is None

    @property
    def side(self) -> str:
        return "buy" if self.direction == "bull" else "sell"

    @property
    def pos_side(self) -> str:
        return "long" if self.direction == "bull" else "short"

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["tp_legs"] = [leg.__dict__ for leg in self.tp_legs]
        d["ok"] = self.ok
        return d


# ---------------------------------------------------------------------------
# 純函式：槓桿（衍生值，封頂在「清算落在止損之外」）
# ---------------------------------------------------------------------------
def max_safe_leverage(entry: float, stop: float, buffer: float = LIQ_BUFFER,
                      mmr: float = MAINT_MARGIN_RATE) -> int:
    """能讓 OKX 逐倉清算價落在止損『之外』(含 buffer) 的最高整數槓桿。

    逐倉真實清算距離 ≈ entry ×(1/leverage − mmr)（mmr＝維持保證金率，使清算「早於」
    entry/leverage 觸發）。**必須納入 mmr**，否則窄止損會算出過高槓桿、讓清算先於止損
    （對抗式審查 finding #3：舊版略去 mmr，窄止損＋高槓桿會違反『清算永不先於止損』）。
    需求：entry ×(1/leverage − mmr) ≥ |entry-stop| ×(1+buffer)
        → 1/leverage ≥ mmr + |entry-stop| ×(1+buffer) / entry
        → leverage ≤ 1 / ( mmr + |entry-stop| ×(1+buffer) / entry )
    """
    sl_distance = abs(entry - stop)
    if sl_distance <= 0 or entry <= 0:
        return 1
    denom = mmr + sl_distance * (1.0 + buffer) / entry
    if denom <= 0:
        return 1
    cap = 1.0 / denom
    return max(1, int(math.floor(cap)))


def choose_safe_leverage(symbol: str, entry: float, stop: float,
                         atr_pct_7d: Optional[float],
                         tier_leverage: Optional[int] = None) -> int:
    """槓桿 tier，再以 max_safe_leverage（含 mmr）封頂（清算永不先於止損）。

    v83 task#5（研究 w7r04t691 落地）：ATR 未知時（demo 路徑 atr=None）原本 choose_leverage
    硬退守 5x → 資金效率極差、每單鎖過多保證金 → 5000U 很快耗盡 → OKX 51008 餘額不足。
    改為：ATR 未知時用「止損距離推導效率 tier」(leverage_for_stop)，再交由 max_safe_leverage
    封頂保安全。symbol 硬 override（如 WLFI=5）與顯式 tier_leverage 仍優先。風險不變（由止損定
    倉位），只是少鎖保證金 → 同樣 5000U 能持更多單。"""
    if tier_leverage is not None:
        tier = tier_leverage
    elif symbol in LEVERAGE_OVERRIDES:
        tier = LEVERAGE_OVERRIDES[symbol]
    elif atr_pct_7d is not None:
        tier = choose_leverage(symbol, atr_pct_7d, default=15)
    else:
        sl_pct = abs(entry - stop) / entry * 100 if entry else 0.0
        tier = leverage_for_stop(sl_pct)
    return max(1, min(tier, max_safe_leverage(entry, stop)))


# ---------------------------------------------------------------------------
# 純函式：張數取整 / 實際風險 / TP 分腿
# ---------------------------------------------------------------------------
def _to_float(v) -> float:
    """安全把 OKX 原始字串規格（'1'、'0.1'、''、None）轉 float；無法解析回 0.0。"""
    try:
        if v is None or v == "":
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _dec(x) -> Decimal:
    """float→Decimal 走 str()（取該 float 的最短忠實表示），避免 Decimal(float) 把
    0.1 之類灌成 0.1000000000000000055…。"""
    return Decimal(str(x))


def _lot_floor(value: float, lot_sz: float) -> float:
    """把 value 向下取整到 lot_sz 的整數倍，全程 Decimal 精確網格，回傳乾淨 float。
    取代 `math.floor(value/lot)*lot` 的兩個 float 病灶（OKX 51121 根因）：
      ① value/lot 的浮點誤差讓 floor 偶爾少掉一格（少報倉位）；
      ② n*lot 的浮點殘渣（7*0.1=0.7000000000000001）被原樣送進 OKX → 51121。
    float(Decimal('0.7')) 回的是「乾淨」的雙精度（repr '0.7'），而 7*0.1 不是。"""
    lot = _dec(lot_sz)
    n = (_dec(value) / lot).to_integral_value(rounding=ROUND_FLOOR)
    return float(n * lot)


def round_contracts_down(qty_base: float, ct_val: float, lot_sz: float) -> float:
    """基礎幣數量 → OKX 張數，向下取整到 lot_sz（絕不無中生有放大風險）。
    全程 Decimal：杜絕 float 乘法殘渣讓 OKX 51121，亦修 float 除法誤差導致的偶發少報。"""
    if ct_val <= 0 or lot_sz <= 0:
        raise ValueError("ct_val / lot_sz must be > 0")
    raw_contracts = _dec(qty_base) / _dec(ct_val)
    lot = _dec(lot_sz)
    n_lots = (raw_contracts / lot).to_integral_value(rounding=ROUND_FLOOR)
    return float(n_lots * lot)


def realized_risk_usd(contracts: float, ct_val: float, sl_distance: float) -> float:
    """取整後實際 1R 風險（USD）= 張數 × ctVal × 止損距離。"""
    return contracts * ct_val * sl_distance


def split_tp_contracts(total_contracts: float, lot_sz: float,
                       weights: tuple[float, ...] = TP_WEIGHTS) -> list[float]:
    """把總張數分成 len(weights) 腿，每腿向下取整到 lot_sz，**最後一腿吃餘數**，
    確保各腿加總 == total（不掉張、不超發）。張數不足以分腿時自動退化（前腿可能 0）。"""
    if total_contracts <= 0:
        return [0.0 for _ in weights]
    # 全程 Decimal 網格：前腿向下取整到 lot、最後一腿吃精確餘數，杜絕 n*lot 浮點殘渣
    # （0.7000000000000001）被原樣寫進 attachAlgoOrds.sz 觸發 OKX 51121。
    lot = _dec(lot_sz)
    total = _dec(total_contracts)
    legs: list[float] = []
    allocated = Decimal(0)
    for w in weights[:-1]:
        n_lots = ((total * _dec(w)) / lot).to_integral_value(rounding=ROUND_FLOOR)
        leg = n_lots * lot
        # 不能讓前腿吃掉超過剩餘
        if allocated + leg > total:
            leg = Decimal(0)
        legs.append(float(leg))
        allocated += leg
    legs.append(float(total - allocated))   # 最後一腿 = 精確餘數（守恆，仍是 lot 整數倍）
    return legs


# ---------------------------------------------------------------------------
# 純函式：冪等 id
# ---------------------------------------------------------------------------
def make_intent_id(symbol: str, direction: str, entry: float, stop: float,
                   seq: int | str) -> str:
    """持久意圖 id：同一訊號（含 fire_id/seq）恆等。用於跨時對帳與去重。"""
    raw = f"{symbol}|{direction}|{entry:.10g}|{stop:.10g}|{seq}"
    h = hashlib.sha1(raw.encode()).hexdigest()[:16]
    return f"pino-{symbol}-{direction}-{h}"


def make_cl_ord_id(intent_id: str) -> str:
    """OKX clOrdId：字母開頭、僅英數、≤32 字元（OKX 限制）。
    決定性 ⇒ 逾時重送會撞回 in-flight 原單（冪等，避免雙倉）。
    注意：OKX clOrdId 終態後可重用，故它只是「飛行中」防撞；跨時去重靠 intent_id。"""
    h = hashlib.sha1(intent_id.encode()).hexdigest()
    cl = "p" + h                      # 字母開頭
    return cl[:32]


# ---------------------------------------------------------------------------
# 純函式：組裝下單計畫（核心）
# ---------------------------------------------------------------------------
def build_order_plan(
    symbol: str,
    direction: str,
    entry: float,
    stop: float,
    *,
    atr_pct_7d: Optional[float] = None,
    risk_usd: float = RISK_PER_TRADE_USD,
    ct_val: float = DEFAULT_CT_VAL,
    lot_sz: float = DEFAULT_LOT_SZ,
    min_sz: float = DEFAULT_MIN_SZ,
    seq: int | str,
    tier_leverage: Optional[int] = None,
) -> OrderPlan:
    """從訊號參數組一份完整下單計畫。任何不該下單的情況回傳帶 reject_reason 的計畫。

    seq 為**必填**（對抗式審查 finding：舊版預設 0）：須帶上游 L2 訊號的持久 fire_id，
    intent_id/clOrdId 由它決定。漏帶會讓「相同價位、不同時間」的訊號共用 id、跨時去重
    失效而開重複倉，故設為必填、強制呼叫端綁定持久識別。"""
    intent_id = make_intent_id(symbol, direction, entry, stop, seq)
    cl_ord_id = make_cl_ord_id(intent_id)

    def _reject(reason: str, **kw) -> OrderPlan:
        return OrderPlan(
            symbol=symbol, direction=direction, entry=entry, stop=stop,
            leverage=kw.get("leverage", 0),
            notional_usd=kw.get("notional_usd", 0.0),
            margin_usd=kw.get("margin_usd", 0.0),
            qty_base=kw.get("qty_base", 0.0),
            contracts=kw.get("contracts", 0.0),
            ct_val=ct_val, lot_sz=lot_sz, min_sz=min_sz,
            risk_usd=risk_usd, realized_risk_usd=kw.get("realized_risk_usd", 0.0),
            sl_trigger=kw.get("sl_trigger", stop),
            intent_id=intent_id, cl_ord_id=cl_ord_id, reject_reason=reason,
        )

    if direction not in ("bull", "bear"):
        return _reject(f"bad_direction:{direction}")
    sl_distance = abs(entry - stop)
    if sl_distance <= 0:
        return _reject("entry_equals_stop")
    if entry <= 0 or stop <= 0:
        return _reject("nonpositive_price")
    # 方向自洽：多單止損須在進場之下，空單在進場之上
    if direction == "bull" and stop >= entry:
        return _reject("bull_stop_above_entry")
    if direction == "bear" and stop <= entry:
        return _reject("bear_stop_below_entry")

    leverage = choose_safe_leverage(symbol, entry, stop, atr_pct_7d, tier_leverage)
    pos = compute_position(entry, stop, risk_usd, leverage)   # reuse，不重算
    notional_usd = pos["notional_usd"]
    margin_usd = pos["margin_usd"]
    qty_base = notional_usd / entry

    contracts = round_contracts_down(qty_base, ct_val, lot_sz)
    if contracts < min_sz:
        return _reject(
            f"below_min_size(contracts={contracts} < min_sz={min_sz})",
            leverage=leverage, notional_usd=notional_usd, margin_usd=margin_usd,
            qty_base=qty_base, contracts=contracts)

    realized = realized_risk_usd(contracts, ct_val, sl_distance)
    if realized < risk_usd * MIN_REALIZED_RISK_RATIO:
        return _reject(
            f"dust_after_rounding(realized={realized:.2f} < {risk_usd*MIN_REALIZED_RISK_RATIO:.2f})",
            leverage=leverage, notional_usd=notional_usd, margin_usd=margin_usd,
            qty_base=qty_base, contracts=contracts, realized_risk_usd=realized)

    # 三道防線
    sl_trigger = round(stop, 10)
    tp_prices = compute_tp_prices(entry, stop, direction, TP_R_MULTIPLES)
    leg_contracts = split_tp_contracts(contracts, lot_sz, TP_WEIGHTS)
    tp_legs: list[TpLeg] = []
    for i, (w, n) in enumerate(zip(TP_WEIGHTS, leg_contracts), start=1):
        if n <= 0:
            continue
        tp_legs.append(TpLeg(
            label=f"tp{i}", contracts=n, price=tp_prices[f"tp{i}"],
            r=TP_R_MULTIPLES[i - 1],
            size_pct=round(n / contracts, 6) if contracts else 0.0,
        ))

    return OrderPlan(
        symbol=symbol, direction=direction, entry=entry, stop=stop,
        leverage=leverage, notional_usd=notional_usd, margin_usd=margin_usd,
        qty_base=round(qty_base, 8), contracts=contracts, ct_val=ct_val,
        lot_sz=lot_sz, min_sz=min_sz, risk_usd=risk_usd,
        realized_risk_usd=round(realized, 2), sl_trigger=sl_trigger,
        tp_legs=tp_legs, intent_id=intent_id, cl_ord_id=cl_ord_id,
        reject_reason=None,
    )


# ---------------------------------------------------------------------------
# 純函式：相關桶風險上限（取代舊的「每族最多 2 筆」）
# ---------------------------------------------------------------------------
def _bucket_of(symbol: str, families: dict[str, tuple[str, ...]]) -> Optional[str]:
    for fam, members in families.items():
        if symbol in members:
            return fam
    return None


def bucket_risk_check(symbol: str, risk_usd: float, open_trades: list[dict],
                      families: dict[str, tuple[str, ...]],
                      cap_usd: float = BUCKET_RISK_CAP_USD) -> tuple[bool, str, dict]:
    """同相關桶的「風險預算加總」是否超上限。回 (ok, reason, detail)。
    ok=False ⇒ 拒絕本筆（例：BTC、ETH 已各 100U，第 3 筆 SOL 會讓桶風險破 200U）。"""
    bucket = _bucket_of(symbol, families)
    detail = {"symbol": symbol, "bucket": bucket, "cap_usd": cap_usd}
    if bucket is None:
        detail["msg"] = "symbol 不屬任何相關桶，僅受全域併發上限約束"
        return True, "no_bucket", detail
    members = set(families[bucket])
    existing = [o for o in open_trades if o.get("symbol") in members]
    existing_risk = sum((o.get("risk_usd") or RISK_PER_TRADE_USD) for o in existing)
    detail.update({
        "existing_symbols": [o.get("symbol") for o in existing],
        "existing_risk_usd": round(existing_risk, 2),
        "incoming_risk_usd": risk_usd,
        "projected_risk_usd": round(existing_risk + risk_usd, 2),
    })
    if existing_risk + risk_usd > cap_usd + 1e-9:
        detail["msg"] = (f"{bucket} 桶風險 {existing_risk:.0f}+{risk_usd:.0f}="
                         f"{existing_risk+risk_usd:.0f}U > 上限 {cap_usd:.0f}U → 拒絕")
        return False, "bucket_risk_exceeded", detail
    detail["msg"] = f"{bucket} 桶風險 {existing_risk+risk_usd:.0f}U ≤ {cap_usd:.0f}U → 通過"
    return True, "ok", detail


# ---------------------------------------------------------------------------
# 純函式：開倉前預算檢查（all-or-none）
# ---------------------------------------------------------------------------
def preflight_budget(plan: OrderPlan, avail_usd: float,
                     taker_fee_rate: float = TAKER_FEE_RATE,
                     buffer_pct: float = BUDGET_BUFFER_PCT) -> tuple[bool, dict]:
    """required = 保證金 + taker 手續費(進+出) + buffer，對比可用餘額。
    付不起整筆 → False（呼叫端應拒絕並告警，不開半套）。"""
    fee = plan.notional_usd * taker_fee_rate * 2     # 進場 + 預期出場
    buffer = plan.margin_usd * (buffer_pct / 100.0)
    required = plan.margin_usd + fee + buffer
    ok = required <= avail_usd + 1e-9
    return ok, {
        "required_usd": round(required, 2),
        "margin_usd": round(plan.margin_usd, 2),
        "fee_usd": round(fee, 2),
        "buffer_usd": round(buffer, 2),
        "avail_usd": round(avail_usd, 2),
        "affordable": ok,
    }


# ---------------------------------------------------------------------------
# 純函式：對帳 DIFF（本地帳本 vs OKX 真實持倉）
# ---------------------------------------------------------------------------
def reconcile_positions(local_positions: list[dict], okx_positions: list[dict],
                        qty_tol: float = QTY_DRIFT_TOL) -> list[dict]:
    """DIFF 本地『該有的持倉』與 OKX『真實持倉』。回傳漂移清單（空 = 一致）。

    輸入正規化（皆 list[dict]）：
        local: {symbol, pos_side, contracts}
        okx:   {symbol, pos_side, contracts}
    任一漂移（缺倉/多倉/數量不符）→ 呼叫端應停新單 + 告警。"""
    def key(p):
        return (p.get("symbol"), p.get("pos_side"))

    loc = {key(p): float(p.get("contracts") or 0) for p in local_positions}
    okx = {key(p): float(p.get("contracts") or 0) for p in okx_positions}
    drifts: list[dict] = []
    for k in set(loc) | set(okx):
        lc, oc = loc.get(k, 0.0), okx.get(k, 0.0)
        if abs(lc - oc) > qty_tol:
            kind = ("missing_on_okx" if oc == 0 else
                    "unexpected_on_okx" if lc == 0 else "qty_mismatch")
            drifts.append({
                "symbol": k[0], "pos_side": k[1],
                "local_contracts": lc, "okx_contracts": oc,
                "drift": round(oc - lc, 10), "kind": kind,
            })
    return drifts


# ---------------------------------------------------------------------------
# Kill switch（檔案旗標 + 可擴充 signal）
# ---------------------------------------------------------------------------
def kill_switch_path():
    from botpaths import data_dir
    return data_dir() / "KILL_SWITCH"


def kill_switch_active() -> bool:
    """data/KILL_SWITCH 檔存在 → 全面停新單。

    fail-safe（對抗式審查 finding）：路徑解析失敗時回 True（當作停機）。停機開關寧可
    誤停也不可盲下——出錯時 fail-open 會讓使用者按下的緊急停機被靜默忽略。"""
    try:
        return kill_switch_path().exists()
    except Exception:
        return True


# ---------------------------------------------------------------------------
# 純函式：組裝 OKX 進場單 params（止損/分批止盈用原生附帶算法單，成交時才生效）
# ---------------------------------------------------------------------------
def _fmt_px(px: float) -> str:
    """OKX 價格字串：十進位、**絕不科學記號**、去除多餘尾零。
    用 Decimal(str(px)) 取最短忠實表示，normalize 去尾零，format(_,'f') 強制定點
    （修 finding：舊版 f'{x:.10g}' 對 |x|<1e-4 的小幣價會輸出 '1.2e-05'，OKX 拒收）。"""
    d = Decimal(str(px)).normalize()
    return format(d, "f")


def _fmt_qty(q: float, lot_sz: Optional[float] = None) -> str:
    """OKX 數量(張)字串：整數不帶小數點、**絕不科學記號**。

    帶 lot_sz 時先把數量 snap（denoise）回 lot 網格：第二道防線，杜絕任何 float 乘法殘渣
    （n*0.1=0.7000000000000001）被原樣寫進 attachAlgoOrds.sz 觸發 OKX 51121。數量在 source
    （round_contracts_down/split_tp_contracts）已 Decimal 對齊，此處 snap 僅去浮點雜訊、
    不改實際倉位（HALF_UP 把離格 ε 拉回最近的格點，兩個方向皆然）。"""
    d = _dec(q)
    if lot_sz and lot_sz > 0:
        lot = _dec(lot_sz)
        d = (d / lot).to_integral_value(rounding=ROUND_HALF_UP) * lot
    if d == d.to_integral_value():
        return str(int(d))
    return format(d.normalize(), "f")


def _attach_algo_cl_ord_id(cl_ord_id: str, label: str) -> str:
    """附帶算法單的決定性 clOrdId：字母開頭、僅英數、≤32（OKX 限制）。"""
    h = hashlib.sha1(f"{cl_ord_id}|{label}".encode()).hexdigest()
    return ("a" + h)[:32]


def build_okx_entry_params(plan: OrderPlan) -> dict:
    """組裝進場 LIMIT 單的 OKX params：把止損與分批止盈用「原生附帶算法單」
    (attachAlgoOrds) 依附在進場單上，**成交時才一起生效**——不會有任何 reduce-only
    掛單在倉位還沒成交前就先打出去（修掉舊版的 TP-before-fill bug）。

    **OKX split-TP 合規結構（對抗式審查 finding #1/#2 修正，error 51076）**：
      OKX 規定「split TPs 模式下每個 attachAlgoOrds 元素只能單向（純 TP 或純 SL），不可
      在同一元素同時帶 tp* 與 sl*」。且 split-TP 的止損是**單一、不帶 sz、套用整倉**的
      SL 元素，不是每腿各帶一份。故正確組法為：
        - 每個 TP 腿 → 一個「純 TP」元素：sz(該腿張數) + tpTriggerPx + tpOrdPx='-1'
          + tpTriggerPxType（**不放任何 sl* 欄位**）。
        - 全倉止損 → **一個獨立「純 SL」元素**：slTriggerPx + slOrdPx='-1'
          + slTriggerPxType（**不帶 sz**，OKX 自動套用整倉）。
      語意：某腿 TP 觸發→只平該腿 sz，剩餘部位仍由那一張整倉 SL 保護；SL 觸發→整倉市價
      出場。（舊版每腿各塞一份 slTriggerPx＝同價，會踩 51076 整單被退、且語意是超額平倉。）

    無 TP 分腿（極小倉位/人工構造）時退化為「整單一個原生止損」（頂層 slTriggerPx），
    不進 split-TP 模式、至少不裸倉。純函式、回傳純 dict，可離線自測。"""
    attach: list[dict] = []
    # 分批止盈：每腿一個「純 TP」元素（帶自己的 sz，不放任何 sl* 欄位）
    for leg in plan.tp_legs:
        attach.append({
            "attachAlgoClOrdId": _attach_algo_cl_ord_id(plan.cl_ord_id, leg.label),
            "sz": _fmt_qty(leg.contracts, plan.lot_sz),
            "tpTriggerPx": _fmt_px(leg.price),
            "tpOrdPx": "-1",            # 市價止盈
            "tpTriggerPxType": "last",
        })
    # 全倉止損：單一獨立「純 SL」元素，不帶 sz（OKX 自動套整倉）
    if attach:
        attach.append({
            "attachAlgoClOrdId": _attach_algo_cl_ord_id(plan.cl_ord_id, "sl"),
            "slTriggerPx": _fmt_px(plan.sl_trigger),
            "slOrdPx": "-1",            # 市價止損
            "slTriggerPxType": "last",
        })
    params: dict = {
        "tdMode": "isolated",
        "posSide": plan.pos_side,
        "clOrdId": plan.cl_ord_id,
    }
    if attach:
        params["attachAlgoOrds"] = attach
    else:
        # 無分腿 → 退化為整單頂層原生止損（非 split-TP 模式），不裸倉
        params["slTriggerPx"] = _fmt_px(plan.sl_trigger)
        params["slOrdPx"] = "-1"
    return params


# ---------------------------------------------------------------------------
# 連線層（需 OKX_DEMO_* + 網路；每一步都先過 demo_guard 正向證明模擬盤）
# ---------------------------------------------------------------------------
async def place_demo_plan(ex, plan: OrderPlan, *,
                          avail_usd: float,
                          open_trades: list[dict],
                          families: dict[str, tuple[str, ...]]) -> dict:
    """在『已正向證明的模擬盤』上落實一份 OrderPlan：**單一進場 LIMIT 單**，止損與分批
    止盈以 OKX 原生附帶算法單 (attachAlgoOrds) 依附其上，成交時原子地一起生效。

    呼叫前提：ex 由 demo_guard.make_demo_exchange 產生、且已過 confirm_okx_demo。
    本函式仍再次斷言 header，雙重保險。

    **硬性前置風險閘（fail-safe，對抗式審查 finding：舊版護欄純函式卻沒接線）**：
    下單前依序強制過 (1) kill_switch_active (2) preflight_budget(avail_usd) all-or-none
    (3) bucket_risk_check(open_trades, families) 桶風險上限；任一不過即拒、不開半套。
    avail_usd/open_trades/families 為**必填**，由呼叫端（daemon）提供當下餘額與在倉清單，
    強制 fail-safe（不寄望呼叫端記得做檢查）。

    與舊版差異（finding #2）：不再於進場成交前就掛 reduce-only TP（那會打在尚未存在的倉位
    上）；所有防線改由進場單的 attachAlgoOrds 攜帶，成交才生效。因此沒有「TP 全失敗卻仍回
    ok:True」的問題——只有一個進場單，它失敗即 ok:False。

    逾時平倉(time_limit_hours)、stale 掛單清理、attach 腿成交後回讀驗證、本地帳本對帳，
    均屬監控層職責，本函式不處理（見模組 docstring；接 daemon 前須先補上監控層）。
    任何步驟失敗回 {ok:False, error}。"""
    from l4_execution.demo_guard import DemoGuardError, confirm_okx_demo

    if not plan.ok:
        return {"ok": False, "error": f"plan rejected: {plan.reject_reason}"}
    # 硬性前置風險閘（fail-safe；任一不過即拒，不開半套）
    if kill_switch_active():
        return {"ok": False, "error": "kill_switch_active"}
    afford_ok, budget = preflight_budget(plan, avail_usd)
    if not afford_ok:
        return {"ok": False, "error": "insufficient_budget", "budget": budget}
    bucket_ok, bucket_reason, bucket_detail = bucket_risk_check(
        plan.symbol, plan.risk_usd, open_trades, families)
    if not bucket_ok:
        return {"ok": False, "error": f"bucket_risk:{bucket_reason}", "bucket": bucket_detail}
    # 雙重保險：下單前再正向證明一次模擬盤（實盤金鑰/真錢一律到不了這裡）
    await confirm_okx_demo(ex)

    inst_id = f"{plan.symbol}/USDT:USDT"
    results: dict = {
        "intent_id": plan.intent_id, "cl_ord_id": plan.cl_ord_id,
        "attached_tp_legs": [{"label": leg.label, "contracts": leg.contracts,
                              "price": leg.price} for leg in plan.tp_legs],
    }
    try:
        await ex.set_leverage(plan.leverage, inst_id, params={"mgnMode": "isolated"})
    except Exception as e:  # noqa: BLE001 — 已是目標槓桿等情形不致命，記錄續行
        results["leverage_note"] = str(e)

    # 進場 LIMIT + 原生附帶止損/分批止盈（attachAlgoOrds：成交時才一起生效，不裸倉、不 TP-before-fill）
    params = build_okx_entry_params(plan)
    try:
        entry_order = await ex.create_order(
            symbol=inst_id, type="limit", side=plan.side, amount=plan.contracts,
            price=plan.entry, params=params,
        )
        results["entry_order_id"] = entry_order.get("id")
    except DemoGuardError:
        raise
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"entry failed: {e}", **results}

    results["ok"] = True
    return results


async def fetch_okx_positions(ex) -> list[dict]:
    """拉 OKX 模擬盤真實持倉（正規化給 reconcile_positions）。先過 demo_guard。"""
    from l4_execution.demo_guard import confirm_okx_demo
    await confirm_okx_demo(ex)
    raw = await ex.fetch_positions()
    out: list[dict] = []
    for p in raw:
        contracts = float(p.get("contracts") or 0)
        if contracts <= 0:
            continue
        sym = (p.get("symbol") or "").split("/")[0]
        out.append({"symbol": sym, "pos_side": p.get("side"),
                    "contracts": contracts})
    return out


async def fetch_okx_contract_spec(ex, symbol: str) -> dict:
    """拉某標的的 OKX 永續合約規格（ctVal / lotSz / minSz），供 build_order_plan 正確取整。

    這修掉「純函式層用 DEFAULT_CT_VAL=0.01 假設」對非 BTC 標的的錯倉位：各標的 ctVal 不同
    （SOL/DOGE… 與 BTC 差很多），不取真值會把張數/實際風險算錯。讀取性質（不下單），故
    不另呼 confirm_okx_demo——真正下單前的 place_demo_plan 仍會再正向證明模擬盤。"""
    inst_id = f"{symbol}/USDT:USDT"
    await ex.load_markets()
    m = ex.market(inst_id)
    prec = m.get("precision") or {}
    amt_limit = (m.get("limits") or {}).get("amount") or {}
    info = m.get("info") or {}  # OKX 原始合約規格（lotSz/minSz/ctVal）
    ct_val = float(m.get("contractSize") or DEFAULT_CT_VAL)

    # lotSz：優先讀 OKX 原始 info.lotSz（張數最小變動單位＝張數須為其整數倍，errCode 51121）。
    # ccxt 的 precision.amount 對 OKX 永續不可靠（常給小數位數而非「張數步進」，曾把 ARB 算成
    # 4629.6 張被 51121 退單）。原始值缺漏才退回 precision.amount，再退 DEFAULT。
    lot_sz = _to_float(info.get("lotSz")) or _to_float(prec.get("amount")) or DEFAULT_LOT_SZ
    if lot_sz <= 0:
        lot_sz = DEFAULT_LOT_SZ
    min_sz = _to_float(info.get("minSz")) or _to_float(amt_limit.get("min")) or lot_sz
    if min_sz <= 0:
        min_sz = lot_sz
    return {"ct_val": ct_val, "lot_sz": lot_sz, "min_sz": min_sz, "inst_id": inst_id}


async def cancel_demo_entry(ex, symbol: str, order_id: str | None,
                            cl_ord_id: str | None = None) -> dict:
    """取消一張未成交的模擬盤進場限價單（entry_expired 路徑）。
    下單面動作 → 先再正向證明模擬盤（雙重保險），證不出在模擬盤就不動。"""
    from l4_execution.demo_guard import confirm_okx_demo
    await confirm_okx_demo(ex)
    inst_id = f"{symbol}/USDT:USDT"
    params: dict = {}
    if not order_id and cl_ord_id:
        params["clOrdId"] = cl_ord_id
    try:
        res = await ex.cancel_order(order_id, inst_id, params=params)
        return {"ok": True, "result_id": (res or {}).get("id") if isinstance(res, dict) else None}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def market_close_demo(ex, symbol: str, pos_side: str, contracts: float) -> dict:
    """市價平掉模擬盤某倉的剩餘張數（逾時平倉路徑）。reduceOnly。
    下單面動作 → 先再正向證明模擬盤。contracts ≤ 0 視為無倉、不動。"""
    from l4_execution.demo_guard import confirm_okx_demo
    await confirm_okx_demo(ex)
    if contracts <= 0:
        return {"ok": False, "error": "no_contracts"}
    inst_id = f"{symbol}/USDT:USDT"
    close_side = "sell" if pos_side == "long" else "buy"
    params = {"tdMode": "isolated", "posSide": pos_side, "reduceOnly": True}
    try:
        res = await ex.create_order(symbol=inst_id, type="market", side=close_side,
                                    amount=contracts, params=params)
        return {"ok": True, "order_id": (res or {}).get("id")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def fetch_okx_closed_pnl(ex, symbol: str, pos_side: str,
                               *, since_ms: int | None = None) -> dict:
    """從 OKX positions-history 取某標的最近一筆已平倉的 realizedPnl（**真相，非捏造**）。

    realizedPnl 為 OKX 計入手續費/資金費後的淨已實現損益，正是 realized_r 的分子來源；
    部分止盈(TP1/TP2 分腿)的已實現損益會由 OKX 累計進整倉平倉後的 realizedPnl，故整倉
    平倉後取此值即含全部分腿。回 {ok, found, pnl_usd, u_time}。找不到（history 尚未回填）→
    found=False，呼叫端應保守處理（不平本地帳、下輪重試），**絕不本地推估 PnL**。

    ⚠️ 治本(2026-06-23)：**絕不可**把 since_ms 傳進 ex.fetch_positions_history 的 since 參數。
    OKX positions-history 的 since/分頁語意會把「晚於該 ts 才平倉」的本倉整個濾掉——實測
    since=filled_at → 回 0 列（整批已平倉 demo 倉因此永卡 await_pnl、零筆 tp 回填）；since=None
    → 正確回該倉 realizedPnl（OP +99 / RESOLV +410 等真實止盈）。故改為 since=None 取近 100 筆，
    再於本地以 uTime 做 scope（只認 uTime≥since_ms 的本倉平倉，避免誤配同標的更早的舊平倉）。
    ⚠️ since_ms 應傳「下單/進場時刻 entry_at」當下界，**勿傳 filled_at**（本機偵測成交的記錄
    時刻會落後真實成交，快進快出的單其平倉 uTime 可能早於 filled_at→本倉被誤排除、永卡）。"""
    inst_id = f"{symbol}/USDT:USDT"
    try:
        # since 一律 None（傳 since_ms 會讓 OKX 回 0 列，見上方治本說明）；scope 改在本地做。
        hist = await ex.fetch_positions_history([inst_id], since=None, limit=100)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "found": False, "error": f"{type(e).__name__}: {e}"}
    cands: list[tuple[int, float]] = []
    for h in hist:
        info = h.get("info") or {}
        ps = info.get("posSide") or h.get("side")
        if pos_side and ps and ps != pos_side:
            continue
        raw_u = info.get("uTime") or info.get("cTime") or h.get("timestamp")
        try:
            u = int(raw_u) if raw_u else 0
        except (TypeError, ValueError):
            u = 0
        # 本地 scope：早於本倉成交時刻的平倉屬同標的的舊倉，非本倉 → 跳過（取代壞掉的 API since）。
        if since_ms and u and u < int(since_ms):
            continue
        pnl_raw = info.get("realizedPnl")
        if pnl_raw is None:
            pnl_raw = info.get("pnl")
        try:
            pnl = float(pnl_raw) if pnl_raw is not None and pnl_raw != "" else None
        except (TypeError, ValueError):
            pnl = None
        if pnl is None:
            continue
        cands.append((u, pnl))
    if not cands:
        return {"ok": True, "found": False}
    cands.sort(key=lambda x: x[0], reverse=True)
    u, pnl = cands[0]
    return {"ok": True, "found": True, "pnl_usd": pnl, "u_time": u}


# ---------------------------------------------------------------------------
# 自測 / CLI
# ---------------------------------------------------------------------------
def _selftest() -> bool:  # noqa: C901 — 自測集中於此
    cases: list[tuple[bool, str]] = []

    def check(cond: bool, label: str):
        cases.append((bool(cond), label))

    FAMILIES = {"btc_family": ("BTC", "ETH", "SOL")}

    # --- 倉位：風險驅動，槓桿不改風險 ---
    # risk_usd 顯式釘 100，使本測不受 botconfig 分級預設（現為 Standard $50）漂移影響：
    # 這裡驗的是「向下取整 + 實際風險」邏輯，需固定風險預算才有確定的期望張數（floor(7.69)=7）。
    p = build_order_plan("BTC", "bull", 65000.0, 63700.0, atr_pct_7d=4.2,
                         risk_usd=100.0,
                         ct_val=0.01, lot_sz=1.0, min_sz=1.0, seq=1)
    check(p.ok, f"BTC 多單計畫成立（reject={p.reject_reason}）")
    # notional = 100/1300*65000 = 5000；qty_base=5000/65000≈0.0769 BTC；張=0.0769/0.01=7.69→7 張
    check(p.contracts == 7.0, f"張數向下取整正確（contracts={p.contracts}，預期 7）")
    # 實際風險 = 7*0.01*1300 = 91U（在 0.5R..1R 間，合理）
    check(89 <= p.realized_risk_usd <= 92, f"取整後實際風險≈91U（得 {p.realized_risk_usd}）")
    # 槓桿封頂：1/(mmr0.0065 + 1300*1.25/65000=0.025)=1/0.0315≈31 → ATR 4.2%→tier 15，min(15,31)=15
    check(p.leverage == 15, f"槓桿=15（tier 15 未被清算封頂壓低，得 {p.leverage}）")

    # 槓桿封頂生效：極寬止損（含 mmr）。dist=10, 1/(0.0065+10*1.25/100=0.125)=1/0.1315≈7.6→7
    lev = max_safe_leverage(100.0, 90.0)
    check(lev == 7, f"寬止損槓桿封頂=7（含 mmr，得 {lev}）")
    # mmr 不變式回歸：窄止損+高 tier 不得讓清算先於止損（finding #3）
    narrow = max_safe_leverage(3000.0, 2970.0)   # ETH 1% 止損；舊版會回 80，含 mmr 後應更保守
    real_liq_dist = 3000.0 * (1.0 / narrow - MAINT_MARGIN_RATE)
    check(real_liq_dist >= 30.0, f"窄止損清算距離({real_liq_dist:.1f})≥止損距離(30)（清算不先於止損，lev={narrow}）")

    # --- TP 分腿：加總守恆、最後一腿吃餘數 ---
    legs = split_tp_contracts(7.0, 1.0)    # 0.4*7=2.8→2, 0.3*7=2.1→2, 餘 3
    check(abs(sum(legs) - 7.0) < 1e-9, f"TP 分腿加總=7（得 {legs}）")
    check(legs == [2.0, 2.0, 3.0], f"分腿向下取整+餘數正確（得 {legs}）")
    legs2 = split_tp_contracts(2.0, 1.0)   # 不足以三分：0,0,2
    check(abs(sum(legs2) - 2.0) < 1e-9, f"小倉位分腿仍守恆（得 {legs2}）")

    # --- 分數 lotSz 浮點對齊（OKX 51121 根因回歸）---
    # 舊碼 n_lots*lot_sz 對 lot=0.1 會出 0.7000000000000001，原樣送進 attachAlgoOrds.sz → 51121。
    def _is_grid(x, lot):           # 模擬 OKX 視角：_fmt_qty 出的字串須為 lot 整數倍
        return (Decimal(_fmt_qty(x, lot)) % Decimal(str(lot))) == 0
    rc = round_contracts_down(20.83, 0.1, 0.1)        # 208.3 張：舊碼此類值會離格
    check(_is_grid(rc, 0.1), f"round_contracts_down 對齊 lot=0.1（得 {rc!r}）")
    legs01 = split_tp_contracts(rc, 0.1)
    check(all(_is_grid(x, 0.1) for x in legs01) and abs(sum(legs01) - rc) < 1e-9,
          f"split_tp 各腿對齊 lot=0.1 且守恆（得 {legs01}）")
    # AVAX 型（lot=0.1）重現：build_order_plan→每個 attach sz 必為 lot 整數倍（否則 51121）
    pav = build_order_plan("AVAX", "bull", 22.0, 21.4, risk_usd=125.0,
                           ct_val=1.0, lot_sz=0.1, min_sz=0.1, seq=11)
    av_szs = [a["sz"] for a in build_okx_entry_params(pav).get("attachAlgoOrds", []) if "sz" in a]
    check(bool(av_szs) and all((Decimal(s) % Decimal("0.1")) == 0 for s in av_szs),
          f"AVAX attach sz 全為 lot=0.1 整數倍（得 {av_szs}）")
    # _fmt_qty denoise（第二道防線）：餵髒值也能 snap 回乾淨格
    check(_fmt_qty(0.7000000000000001, 0.1) == "0.7",
          f"_fmt_qty denoise 小值（得 {_fmt_qty(0.7000000000000001, 0.1)}）")
    check(_fmt_qty(62.400000000000006, 0.1) == "62.4",
          f"_fmt_qty denoise 大值（得 {_fmt_qty(62.400000000000006, 0.1)}）")

    # --- 數字格式：絕不科學記號（finding：小幣價/小量會被 OKX 拒）---
    check("e" not in _fmt_px(0.000012345).lower(),
          f"小價格不出科學記號（得 {_fmt_px(0.000012345)}）")
    check("e" not in _fmt_qty(0.0000001).lower(),
          f"小數量不出科學記號（得 {_fmt_qty(0.0000001)}）")
    check(_fmt_px(65000.0) == "65000", f"整數價去尾零（得 {_fmt_px(65000.0)}）")

    # --- OKX 進場單 params：split-TP 合規結構（finding #1/#2，error 51076）---
    ep = build_okx_entry_params(p)   # p 有 3 條 TP 腿（2/2/3 張）
    attach = ep.get("attachAlgoOrds", [])
    tp_elems = [a for a in attach if "tpTriggerPx" in a]
    sl_elems = [a for a in attach if "slTriggerPx" in a]
    # 結構：N 條純 TP 腿 + 1 條純 SL 元素
    check(len(attach) == len(p.tp_legs) + 1,
          f"attach = TP腿數+1個整倉SL（得 {len(attach)}，預期 {len(p.tp_legs)+1}）")
    check(len(tp_elems) == len(p.tp_legs) and len(sl_elems) == 1,
          f"純TP元素 {len(tp_elems)} 條 + 純SL元素 {len(sl_elems)} 條")
    # 關鍵：one-way 不變式——沒有任何元素同時帶 tp* 與 sl*（否則踩 51076）
    check(all(not ("tpTriggerPx" in a and "slTriggerPx" in a) for a in attach),
          "無元素同時帶 TP+SL（守住 OKX one-way 規則，不踩 51076）")
    # TP 腿：帶 sz、市價止盈、且不夾帶任何 sl 欄位
    check(all("sz" in a and a["tpOrdPx"] == "-1" and "slTriggerPx" not in a and "slOrdPx" not in a
              for a in tp_elems), "TP 腿純止盈（帶 sz、tpOrdPx=-1、無 sl*）")
    check(sum(int(a["sz"]) for a in tp_elems) == int(p.contracts),
          f"TP 各腿 sz 加總 == 全倉張數（得 {sum(int(a['sz']) for a in tp_elems)}，預期 {int(p.contracts)}）")
    # SL 元素：單一、整倉、不帶 sz、市價
    sl = sl_elems[0]
    check("sz" not in sl and sl["slOrdPx"] == "-1" and "tpTriggerPx" not in sl,
          "SL 元素純止損（不帶 sz=整倉、slOrdPx=-1、無 tp*）")
    check(sl["slTriggerPx"] == _fmt_px(p.sl_trigger),
          f"SL 觸發價 == 全倉止損價（得 {sl['slTriggerPx']}，預期 {_fmt_px(p.sl_trigger)}）")
    # clOrdId 合規 + 互異
    check(all(a["attachAlgoClOrdId"][0].isalpha() and a["attachAlgoClOrdId"].isalnum()
              and len(a["attachAlgoClOrdId"]) <= 32 for a in attach),
          "attachAlgoClOrdId 合規（字母開頭/純英數/≤32）")
    check(len({a["attachAlgoClOrdId"] for a in attach}) == len(attach),
          "各元素 attachAlgoClOrdId 互異（不撞單）")
    check(ep["tdMode"] == "isolated" and ep["posSide"] == p.pos_side
          and ep["clOrdId"] == p.cl_ord_id,
          "進場單 tdMode/posSide/clOrdId 正確")
    # 退化路徑：手工構造一個無 TP 腿的 OrderPlan → 整單頂層原生止損、不裸倉
    bare = OrderPlan(
        symbol="BTC", direction="bull", entry=65000.0, stop=63700.0, leverage=10,
        notional_usd=0.0, margin_usd=0.0, qty_base=0.0, contracts=1.0,
        ct_val=0.01, lot_sz=1.0, min_sz=1.0, risk_usd=100.0, realized_risk_usd=0.0,
        sl_trigger=63700.0, tp_legs=[], intent_id="x", cl_ord_id="ptest")
    bp = build_okx_entry_params(bare)
    check("attachAlgoOrds" not in bp and bp["slOrdPx"] == "-1"
          and bp["slTriggerPx"] == _fmt_px(63700.0),
          "無 TP 腿→退化為整單頂層原生市價止損（不裸倉）")

    # --- 方向自洽 / 拒絕路徑 ---
    check(build_order_plan("BTC", "bull", 100.0, 110.0, seq=2).reject_reason == "bull_stop_above_entry",
          "多單止損在進場之上→拒絕")
    check(build_order_plan("BTC", "bear", 100.0, 90.0, seq=3).reject_reason == "bear_stop_below_entry",
          "空單止損在進場之下→拒絕")
    check(build_order_plan("BTC", "bull", 100.0, 100.0, seq=4).reject_reason == "entry_equals_stop",
          "entry==stop→拒絕")
    # 微風險：倉位小到取整後 < 1 張（低於最小單位）→ 拒絕（不開無法成交/低於最小單位的倉）
    # risk=0.005、止損距離=1 → qty_base=0.005 → raw=0.5 張 → floor 0 → below_min_size
    dust = build_order_plan("BTC", "bull", 65000.0, 64999.0, risk_usd=0.005,
                            ct_val=0.01, lot_sz=1.0, min_sz=1.0, seq=5)
    check(dust.reject_reason is not None and "below_min_size" in dust.reject_reason,
          f"微風險（取整後不足 1 張）→拒絕（reject={dust.reject_reason}）")

    # --- 桶風險上限：第 3 筆相關單拒 ---
    opens = [{"symbol": "BTC", "risk_usd": 100}, {"symbol": "ETH", "risk_usd": 100}]
    ok3, reason3, _ = bucket_risk_check("SOL", 100, opens, FAMILIES, cap_usd=200)
    check(not ok3 and reason3 == "bucket_risk_exceeded", "BTC+ETH 後第 3 筆 SOL→拒絕")
    ok2, _, _ = bucket_risk_check("ETH", 100, [{"symbol": "BTC", "risk_usd": 100}], FAMILIES, 200)
    check(ok2, "BTC 後第 2 筆 ETH→通過")
    okn, rn, _ = bucket_risk_check("DOGE", 100, opens, FAMILIES, 200)
    check(okn and rn == "no_bucket", "非相關桶標的不受桶上限約束")

    # --- 預算 all-or-none ---
    okb, db = preflight_budget(p, avail_usd=10000.0)
    check(okb, f"餘額充足→可下（required={db['required_usd']}）")
    okb2, _ = preflight_budget(p, avail_usd=1.0)
    check(not okb2, "餘額不足→拒絕（不開半套）")

    # --- clOrdId 格式 / 決定性 / 長度 ---
    iid = make_intent_id("BTC", "bull", 65000.0, 63700.0, 42)
    cl = make_cl_ord_id(iid)
    check(cl[0].isalpha() and cl.isalnum() and len(cl) <= 32, f"clOrdId 合規（{cl}）")
    check(make_cl_ord_id(iid) == cl, "clOrdId 決定性（同 intent 恆等）")
    check(make_intent_id("BTC", "bull", 65000.0, 63700.0, 42) == iid, "intent_id 決定性")
    check(make_intent_id("BTC", "bull", 65000.0, 63700.0, 43) != iid, "不同 seq→不同 intent")

    # --- 對帳 DIFF ---
    drift = reconcile_positions(
        [{"symbol": "BTC", "pos_side": "long", "contracts": 7}],
        [{"symbol": "BTC", "pos_side": "long", "contracts": 7}])
    check(drift == [], "本地=OKX→無漂移")
    d2 = reconcile_positions(
        [{"symbol": "BTC", "pos_side": "long", "contracts": 7}], [])
    check(len(d2) == 1 and d2[0]["kind"] == "missing_on_okx", "本地有 OKX 無→missing_on_okx")
    d3 = reconcile_positions(
        [], [{"symbol": "ETH", "pos_side": "short", "contracts": 3}])
    check(len(d3) == 1 and d3[0]["kind"] == "unexpected_on_okx", "OKX 有本地無→unexpected_on_okx")
    d4 = reconcile_positions(
        [{"symbol": "BTC", "pos_side": "long", "contracts": 7}],
        [{"symbol": "BTC", "pos_side": "long", "contracts": 5}])
    check(len(d4) == 1 and d4[0]["kind"] == "qty_mismatch", "張數不符→qty_mismatch")

    # --- place_demo_plan 硬性前置風險閘（fail-safe 接線回歸；ex=None：三閘都在碰 ex 前就拒）---
    import asyncio
    # 前提：自測機未按下緊急停機（否則下面兩個閘測會被 kill_switch 短路，故先驗它沒誤觸）
    check(not kill_switch_active(),
          "自測環境無 KILL_SWITCH（否則閘測會被停機開關短路）")
    # (1) 預算閘：計畫成立但餘額僅 1U → insufficient_budget（不開半套）
    r_budget = asyncio.run(place_demo_plan(
        None, p, avail_usd=1.0, open_trades=[], families=FAMILIES))
    check(r_budget["ok"] is False and r_budget["error"] == "insufficient_budget",
          f"預算不足→place_demo_plan 拒（得 {r_budget.get('error')}）")
    # (2) 桶風險閘：餘額充足但同桶已 ETH+SOL 各 100U → 第 3 筆 BTC 超 200U 上限
    r_bucket = asyncio.run(place_demo_plan(
        None, p, avail_usd=100000.0,
        open_trades=[{"symbol": "ETH", "risk_usd": 100}, {"symbol": "SOL", "risk_usd": 100}],
        families=FAMILIES))
    check(r_bucket["ok"] is False and r_bucket["error"].startswith("bucket_risk"),
          f"桶風險超限→place_demo_plan 拒（得 {r_bucket.get('error')}）")
    # (3) 計畫本身被拒（方向不自洽）→ 連閘都不進、直接回 plan rejected
    bad_plan = build_order_plan("BTC", "bull", 100.0, 110.0, seq=9)
    r_bad = asyncio.run(place_demo_plan(
        None, bad_plan, avail_usd=100000.0, open_trades=[], families=FAMILIES))
    check(r_bad["ok"] is False and "plan rejected" in r_bad["error"],
          f"計畫已拒→place_demo_plan 不下單（得 {r_bad.get('error')}）")

    ok = all(passed for passed, _ in cases)
    for passed, label in cases:
        print(f"  [{'ok ' if passed else 'FAIL'}] {label}")
    print(f"\n自測完成：{'全部通過 ✅' if ok else '有失敗 ❌'}（{sum(p for p,_ in cases)}/{len(cases)}）")
    return ok


def _print_plan(symbol: str, direction: str, entry: float, stop: float,
                atr: Optional[float]) -> None:
    p = build_order_plan(symbol, direction, entry, stop, atr_pct_7d=atr, seq=0)
    print(f"\n=== 下單計畫 {symbol} {direction} ===")
    if not p.ok:
        print(f"  ⛔ 拒絕：{p.reject_reason}")
        return
    print(f"  進場 LIMIT {p.entry}　止損(原生市價) {p.sl_trigger}　槓桿 {p.leverage}x")
    print(f"  名目 ${p.notional_usd}　保證金 ${p.margin_usd}　張數 {p.contracts}"
          f"（ctVal {p.ct_val}）　實際風險 ${p.realized_risk_usd}")
    for leg in p.tp_legs:
        print(f"  {leg.label.upper()}  {leg.contracts} 張 @ {leg.price}"
              f"（{leg.r}R, {leg.size_pct*100:.0f}%）原生附帶止盈（成交時才生效）")
    print(f"  逾時平倉 {p.time_limit_hours}h（意圖；監控層尚未實作）"
          f"　intent={p.intent_id}　clOrdId={p.cl_ord_id}")


def _check() -> None:
    """帶 .env 連 OKX：先過 demo_guard，再示範拉持倉（不下單）。"""
    import asyncio
    from pathlib import Path

    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    from l4_execution.demo_guard import DemoGuardError, make_demo_exchange

    async def run():
        try:
            ex = make_demo_exchange()
        except DemoGuardError as e:
            print(f"❌ demo_guard 設定層未過（預期，未填 OKX_DEMO_*）：{e}")
            return
        try:
            pos = await fetch_okx_positions(ex)
            print(f"✅ 模擬盤連線正常，當前持倉 {len(pos)} 筆：{pos}")
        except DemoGuardError as e:
            print(f"❌ 模擬盤正向證明失敗：{e}")
        finally:
            await ex.close()

    asyncio.run(run())


if __name__ == "__main__":
    import sys

    argv = sys.argv[1:]
    if "--check" in argv:
        _check()
    elif "--plan" in argv:
        i = argv.index("--plan")
        sym, dir_, entry, stop = argv[i + 1], argv[i + 2], float(argv[i + 3]), float(argv[i + 4])
        atr = float(argv[argv.index("--atr") + 1]) if "--atr" in argv else None
        _print_plan(sym, dir_, entry, stop, atr)
    else:
        sys.exit(0 if _selftest() else 1)
