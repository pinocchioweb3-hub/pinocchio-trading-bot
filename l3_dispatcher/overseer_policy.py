"""🛂 監督員治理策略層（task #51，復盤引擎 step6）——純策略、零 IO、零網路、零下單。

Layer 2「監督員 Claude」是唯一一個被設計成「會自己規劃下一步、必要時開新 Session」
的環節。它有自由度，所以它周圍必須有一圈**確定性的框架護欄**，讓它的自由度永遠
落在安全集合裡。本模組就是那圈護欄，四道彼此獨立、可離線單測：

  1) PDP（Policy Decision Point）新鮮度前置門
        監督員只能根據「新鮮的」確定性帳本行動。本門用本機 UTC 時鐘（time.time()）
        校驗 oversight_ledger 的 generated_at_ms：過期、缺時戳、或時戳在未來（時鐘
        異常/竄改）一律 DENY。這是「不要拿舊快照下結論」紀律的程式化版本。

  2) 工具暴露 allowlist（預設 deny）
        監督員只准碰「純讀」工具：Read/Grep/Glob（本機檔）、市場行情讀取
        （market_get* / get_ticker / get_candle…）、檔案讀取（read_file_content…）。
        其餘一律 deny。**okx-trade-mcp 整個 server 預設 deny**——它是接真錢的下單
        伺服器（demo:false），即便其中有 market_* 讀取工具，整 server 仍 blacklist；
        監督員的行情讀取走獨立的純行情 server。另對「任何 server」的下單/改單/撤單/
        轉帳/槓桿/寫檔類動詞做縱深防禦式 deny（紅線①在框架層的具體化）。

  3) 確定性分數鎖死（FrozenScores，反 reward-hacking）
        餵給監督員的確定性分數一旦產生即不可變（immutable Mapping + sha256 digest）。
        監督員只能「引用」分數、不能改；它的派工若引用了不存在的分數鍵＝拒絕。
        這擋掉「LLM 為了讓自己的行動看起來合理而竄改/捏造輸入分數」。

  4) 派工死 schema（Self-Reflection 驗證）
        監督員的每一筆派工必須符合一個剛性 schema（固定動作集合、必填欄位、未知欄位
        即拒）。reflect_and_validate 把前三道門 AND 起來：PDP 通過 + schema 合法 +
        引用分數皆存在 + 請求的每個工具皆過 allowlist，才 accepted。

紅線對位：
  • 紅線① 真錢 AI 永不自動執行 → 第 2 道門在框架層讓任何下單類工具呼叫必被拒。
  • 紅線③ 不捏造          → 第 3 道門讓監督員無法竄改/虛構確定性分數。
  • 「不拿舊快照下結論」  → 第 1 道門把新鮮度變成硬性前置條件。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field


# ===========================================================================
# 例外
# ===========================================================================
class ToolDenied(PermissionError):
    """工具暴露 allowlist 拒絕一個工具呼叫時拋出（框架層硬擋）。"""


# ===========================================================================
# 1) 工具暴露 allowlist（預設 deny）
# ===========================================================================
# 接真錢的整個 server 一律 deny（即便其中有 market_* 讀取工具也一併擋）。
DENY_SERVERS: frozenset[str] = frozenset({"okx-trade-mcp"})

# 縱深防禦：任何 server 的工具名只要含這些動詞片段＝有副作用（下單/改單/撤單/轉帳/
# 槓桿/理財申贖/寫檔），一律 deny。allowlist 的純讀工具皆不含這些片段。
_EXEC_VERBS: tuple[str, ...] = (
    "place_order", "place_algo", "place_move_stop", "move_stop",
    "batch_orders", "batch_amend", "batch_cancel",
    "amend_order", "amend_algo", "cancel_order", "cancel_algo",
    "close_position", "set_leverage", "set_position_mode",
    "transfer", "create_order", "stop_order",
    "grid_create", "grid_stop", "dca_create", "dca_stop",
    "savings_purchase", "savings_redeem", "fixed_purchase", "fixed_redeem",
    "earn_", "lending", "auto_set", "_purchase", "_redeem", "_subscribe",
    # 寫檔 / 對外發布類
    "create_file", "copy_file", "upload", "create_new_file",
    "delete", "write", "edit_file", "send_message", "post_",
)

# 本機內建純讀工具（無 mcp__ 前綴）。
_BUILTIN_ALLOW: frozenset[str] = frozenset({"Read", "Grep", "Glob", "LS"})

# 純讀 MCP 工具的「工具部分」前綴白名單（不分 server，但 DENY_SERVERS 仍優先）。
_ALLOW_TOOL_PREFIXES: tuple[str, ...] = (
    "market_get", "market_list",          # 行情讀取（走非 okx 的純行情 server）
    "get_ticker", "get_tickers", "get_candle", "get_index", "get_mark",
    "get_book", "get_trades", "get_instrument", "get_funding", "get_open_interest",
    "get_orderbook", "get_price_limit",
    "read_file_content", "search_files", "list_recent_files",   # filesystem 讀
    "get_file_metadata", "get_file_permissions", "download_file_content",
)


@dataclass(frozen=True)
class ToolDecision:
    allow: bool
    reason: str
    server: str | None = None


def _parse_tool(name: str) -> tuple[str | None, str]:
    """拆 mcp__<server>__<tool>；非 mcp 工具回 (None, name)。"""
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return parts[1], parts[2]
        # 形狀異常的 mcp__ 名稱：保守當作未知工具部分
        return (parts[1] if len(parts) > 1 else None), name
    return None, name


class ToolPolicy:
    """確定性工具決策。decide() 回判定；check() 不通過即拋 ToolDenied。"""

    def __init__(self, deny_servers: frozenset[str] = DENY_SERVERS):
        self.deny_servers = deny_servers

    def decide(self, tool_name: str) -> ToolDecision:
        if not isinstance(tool_name, str) or not tool_name.strip():
            return ToolDecision(False, "工具名為空或非字串", None)
        name = tool_name.strip()
        server, tool = _parse_tool(name)

        # (1) 整 server 黑名單（最高優先；okx-trade-mcp 接真錢，整個 deny）
        if server is not None and server in self.deny_servers:
            return ToolDecision(
                False, f"server『{server}』整體預設 deny（接真錢下單伺服器，紅線①）", server)

        # (2) 縱深防禦：任何含副作用動詞的工具一律 deny
        low = tool.lower()
        for verb in _EXEC_VERBS:
            if verb in low:
                return ToolDecision(
                    False, f"工具含副作用動詞『{verb}』（下單/改單/轉帳/寫檔類，框架層拒絕）", server)

        # (3) allowlist：內建純讀工具
        if server is None and name in _BUILTIN_ALLOW:
            return ToolDecision(True, "內建純讀工具", None)

        # (3) allowlist：純讀工具前綴（行情 / 檔案讀取）
        for pref in _ALLOW_TOOL_PREFIXES:
            if tool.startswith(pref):
                return ToolDecision(True, f"純讀工具（前綴 {pref}）", server)

        # (4) 預設 deny
        return ToolDecision(False, "不在純讀 allowlist（預設 deny）", server)

    def check(self, tool_name: str) -> ToolDecision:
        d = self.decide(tool_name)
        if not d.allow:
            raise ToolDenied(f"{tool_name}: {d.reason}")
        return d


DEFAULT_POLICY = ToolPolicy()


# ===========================================================================
# 2) PDP 新鮮度前置門
# ===========================================================================
# 帳本最大可接受年齡（秒）。預設 90 分鐘＝3 個 Layer-1 盤點週期；可由 env 覆蓋。
LEDGER_MAX_AGE_SEC: int = int(
    os.getenv("OVERSEER_LEDGER_MAX_AGE_SEC", str(90 * 60)) or 90 * 60)
# 容忍的未來時鐘偏移（秒）；超過代表時鐘異常或時戳被竄改。
_FUTURE_SKEW_SEC = 60


@dataclass(frozen=True)
class PDPDecision:
    allow: bool
    reasons: list[str] = field(default_factory=list)
    checks: dict = field(default_factory=dict)


def evaluate_freshness(generated_at_ms, *, max_age_sec: int = LEDGER_MAX_AGE_SEC,
                       now_epoch_sec: float | None = None) -> PDPDecision:
    """校驗一個 UTC epoch（毫秒）時戳是否「夠新鮮」。

    確定性前置門：監督員只能根據新鮮帳本行動。
      • now 一律取本機即時時鐘（time.time()，UTC epoch）——**不信任外傳的 now**
        （環境陷阱：判斷新鮮度勿用被判讀資料自帶的 epoch 當作現在）。
        now_epoch_sec 參數僅供離線測試注入確定性時間。
      • 缺時戳 / 非數字          → DENY
      • 時戳在未來（>skew）       → DENY（時鐘異常或竄改）
      • age > max_age_sec        → DENY（過期）
    """
    now = float(now_epoch_sec) if now_epoch_sec is not None else time.time()
    reasons: list[str] = []
    checks: dict = {"max_age_sec": int(max_age_sec), "now_epoch_sec": now}

    if generated_at_ms is None or not isinstance(generated_at_ms, (int, float)):
        return PDPDecision(False, ["帳本缺 generated_at_ms 或非數字時戳"], checks)

    gen_sec = float(generated_at_ms) / 1000.0
    age = now - gen_sec
    checks["generated_at_sec"] = gen_sec
    checks["age_sec"] = age

    if age < -_FUTURE_SKEW_SEC:
        reasons.append(f"時戳在未來 {-age:.0f}s（>容忍 {_FUTURE_SKEW_SEC}s）：時鐘異常或竄改")
    if age > max_age_sec:
        reasons.append(f"帳本過期 age={age:.0f}s > 上限 {max_age_sec}s")

    return PDPDecision(len(reasons) == 0, reasons, checks)


def ledger_freshness(ledger: dict, *, max_age_sec: int = LEDGER_MAX_AGE_SEC,
                     now_epoch_sec: float | None = None) -> PDPDecision:
    """從已讀入的帳本 dict 取 generated_at_ms（或 generated_at 秒）做新鮮度校驗。純函式。"""
    gen_ms = None
    if isinstance(ledger, dict):
        if isinstance(ledger.get("generated_at_ms"), (int, float)):
            gen_ms = ledger["generated_at_ms"]
        elif isinstance(ledger.get("generated_at"), (int, float)):
            gen_ms = ledger["generated_at"] * 1000.0
    return evaluate_freshness(gen_ms, max_age_sec=max_age_sec, now_epoch_sec=now_epoch_sec)


def build_pdp_block(generated_at_ms: int, *, max_age_sec: int = LEDGER_MAX_AGE_SEC) -> dict:
    """供 ceo_oversight 在寫帳本時嵌入的 PDP 契約區塊（讓 Layer 2 知道新鮮度合約）。"""
    return {
        "policy_max_age_sec": int(max_age_sec),
        "generated_at_ms": int(generated_at_ms),
        "epoch_source": "time.time()/UTC",
        # 寫入當下必然新鮮；Layer 2 讀取時須以 ledger_freshness 重新校驗（時間已流逝）。
        "fresh_at_write": True,
    }


# ===========================================================================
# 3) 確定性分數鎖死（反 reward-hacking）
# ===========================================================================
class FrozenScores(Mapping):
    """不可變的確定性分數視圖。無 __setitem__＝監督員只能引用不能改。

    .digest  = 規範化 JSON 的 sha256（內容指紋）
    .verify(expected) = 指紋比對（偵測被換掉）
    """

    __slots__ = ("_data", "_digest")

    def __init__(self, data: dict):
        # 深拷貝成基本型別快照，切斷與外部可變物件的連結。
        self._data = json.loads(json.dumps(dict(data), ensure_ascii=False, sort_keys=True))
        self._digest = hashlib.sha256(
            json.dumps(self._data, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8")).hexdigest()

    def __getitem__(self, k):
        return self._data[k]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    @property
    def digest(self) -> str:
        return self._digest

    def verify(self, expected_digest: str) -> bool:
        return self._digest == expected_digest

    def __repr__(self):
        return f"FrozenScores(keys={list(self._data)}, digest={self._digest[:12]}…)"


# ===========================================================================
# 4) 派工死 schema + Self-Reflection 驗證
# ===========================================================================
DISPATCH_ACTIONS: frozenset[str] = frozenset(
    {"continue_task", "open_session", "nudge_user", "no_op"})
_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {"action", "rationale", "cited_scores", "tools_requested"})
_OPTIONAL_FIELDS: frozenset[str] = frozenset({"task_id", "session_kind"})
_ALLOWED_FIELDS: frozenset[str] = _REQUIRED_FIELDS | _OPTIONAL_FIELDS


def validate_dispatch(obj) -> tuple[bool, list[str]]:
    """剛性 schema 校驗（死 schema）。回 (ok, errors)。未知欄位即拒。"""
    errs: list[str] = []
    if not isinstance(obj, dict):
        return False, ["派工非 dict"]

    keys = set(obj)
    unknown = keys - _ALLOWED_FIELDS
    if unknown:
        errs.append(f"未知欄位（死 schema 拒絕）：{sorted(unknown)}")
    missing = _REQUIRED_FIELDS - keys
    if missing:
        errs.append(f"缺必填欄位：{sorted(missing)}")

    action = obj.get("action")
    if action not in DISPATCH_ACTIONS:
        errs.append(f"action『{action}』不在允許集合 {sorted(DISPATCH_ACTIONS)}")

    if not isinstance(obj.get("rationale"), str) or not obj.get("rationale", "").strip():
        errs.append("rationale 須為非空字串")

    cs = obj.get("cited_scores")
    if not isinstance(cs, list) or not all(isinstance(x, str) for x in cs):
        errs.append("cited_scores 須為字串陣列")

    tr = obj.get("tools_requested")
    if not isinstance(tr, list) or not all(isinstance(x, str) for x in tr):
        errs.append("tools_requested 須為字串陣列")

    # 條件式必填
    if action == "open_session" and not isinstance(obj.get("session_kind"), str):
        errs.append("action=open_session 須附 session_kind（字串）")
    if action == "continue_task" and not isinstance(obj.get("task_id"), str):
        errs.append("action=continue_task 須附 task_id（字串）")

    return len(errs) == 0, errs


@dataclass(frozen=True)
class DispatchVerdict:
    accepted: bool
    reasons: list[str] = field(default_factory=list)


def reflect_and_validate(dispatch: dict, *, frozen_scores: FrozenScores,
                         pdp: PDPDecision, policy: ToolPolicy = DEFAULT_POLICY) -> DispatchVerdict:
    """把四道門 AND 起來：新鮮度 + schema + 引用分數皆存在 + 請求工具皆過 allowlist。"""
    reasons: list[str] = []

    # 門 1：PDP 新鮮度（拿舊快照行動＝拒）
    if not pdp.allow:
        reasons.append("PDP 新鮮度未通過：" + "；".join(pdp.reasons or ["帳本不新鮮"]))

    # 門 4：死 schema
    ok, errs = validate_dispatch(dispatch)
    if not ok:
        reasons.extend(errs)

    # 門 3：引用分數必須真實存在（反捏造/反 reward-hacking）
    if isinstance(dispatch, dict):
        for key in dispatch.get("cited_scores", []) or []:
            if key not in frozen_scores:
                reasons.append(f"引用了不存在的確定性分數鍵『{key}』（反捏造）")

        # 門 2：請求的每個工具皆須過 allowlist
        for tool in dispatch.get("tools_requested", []) or []:
            d = policy.decide(tool)
            if not d.allow:
                reasons.append(f"請求工具被拒：{tool}（{d.reason}）")

    return DispatchVerdict(len(reasons) == 0, reasons)


# ===========================================================================
# 自測 / CLI
# ===========================================================================
def _selftest() -> bool:
    ok = 0
    fail = 0

    def check(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  ✅ {name}")
        else:
            fail += 1
            print(f"  ❌ {name}")

    p = DEFAULT_POLICY

    # --- 工具 allowlist：okx-trade-mcp 整 server deny（含其 market_* 讀取工具）---
    check("okx 下單 futures_place_order → deny",
          not p.decide("mcp__okx-trade-mcp__futures_place_order").allow)
    check("okx swap 平倉 → deny",
          not p.decide("mcp__okx-trade-mcp__swap_close_position").allow)
    check("okx 轉帳 → deny", not p.decide("mcp__okx-trade-mcp__account_transfer").allow)
    check("okx set_leverage → deny",
          not p.decide("mcp__okx-trade-mcp__swap_set_leverage").allow)
    check("okx 連 market_* 讀取也 deny（整 server）",
          not p.decide("mcp__okx-trade-mcp__market_get_candles").allow)
    check("okx 帳戶讀取(get_balance)也 deny（整 server）",
          not p.decide("mcp__okx-trade-mcp__account_get_balance").allow)

    # --- 縱深防禦：別的 server 帶下單/寫檔動詞也 deny ---
    check("任意 server place_order → deny",
          not p.decide("mcp__some-other__spot_place_order").allow)
    check("任意 server create_file → deny",
          not p.decide("mcp__265305a9__create_file").allow)
    check("任意 server delete → deny",
          not p.decide("mcp__265305a9__delete_thing").allow)

    # --- allowlist 放行純讀 ---
    check("內建 Read → allow", p.decide("Read").allow)
    check("內建 Grep → allow", p.decide("Grep").allow)
    check("純行情 get_candlestick → allow",
          p.decide("mcp__8762a8e2__get_candlestick").allow)
    check("純行情 get_ticker → allow", p.decide("mcp__8762a8e2__get_ticker").allow)
    check("filesystem read_file_content → allow",
          p.decide("mcp__265305a9__read_file_content").allow)
    check("filesystem search_files → allow",
          p.decide("mcp__265305a9__search_files").allow)
    check("未知工具 → 預設 deny", not p.decide("mcp__x__do_something").allow)
    check("空字串 → deny", not p.decide("").allow)

    # check() 對下單工具拋例外
    raised = False
    try:
        p.check("mcp__okx-trade-mcp__futures_place_order")
    except ToolDenied:
        raised = True
    check("check() 對下單工具拋 ToolDenied", raised)

    # --- PDP 新鮮度 ---
    now = 1_000_000_000.0  # 秒
    now_ms = int(now * 1000)
    fresh = evaluate_freshness(now_ms - 60_000, max_age_sec=5400, now_epoch_sec=now)
    check("60s 前帳本 → 新鮮 allow", fresh.allow)
    stale = evaluate_freshness(now_ms - 3 * 3600 * 1000, max_age_sec=5400, now_epoch_sec=now)
    check("3h 前帳本 → 過期 deny", not stale.allow)
    future = evaluate_freshness(now_ms + 600_000, max_age_sec=5400, now_epoch_sec=now)
    check("時戳在未來 10min → deny（時鐘異常）", not future.allow)
    missing = evaluate_freshness(None, max_age_sec=5400, now_epoch_sec=now)
    check("缺時戳 → deny", not missing.allow)
    lf = ledger_freshness({"generated_at_ms": now_ms - 30_000},
                          max_age_sec=5400, now_epoch_sec=now)
    check("ledger_freshness 取 generated_at_ms → allow", lf.allow)
    lf2 = ledger_freshness({"generated_at": (now - 30)}, max_age_sec=5400, now_epoch_sec=now)
    check("ledger_freshness 退而取 generated_at(秒) → allow", lf2.allow)

    # --- FrozenScores 不可變 + digest ---
    fs = FrozenScores({"strength": 72, "regime": "risk_off"})
    check("FrozenScores 可讀取", fs["strength"] == 72)
    immut = False
    try:
        fs["strength"] = 99  # type: ignore[index]
    except TypeError:
        immut = True
    check("FrozenScores 不可寫（反竄改）", immut)
    check("digest 穩定（同內容同指紋）",
          FrozenScores({"a": 1, "b": 2}).digest == FrozenScores({"b": 2, "a": 1}).digest)
    check("digest 隨內容變", FrozenScores({"a": 1}).digest != FrozenScores({"a": 2}).digest)
    check("verify 正確指紋 → True", fs.verify(fs.digest))
    check("verify 錯誤指紋 → False", not fs.verify("0" * 64))

    # --- 死 schema ---
    good = {"action": "no_op", "rationale": "資料不足，先觀望", "cited_scores": [],
            "tools_requested": []}
    okk, _ = validate_dispatch(good)
    check("合法 no_op 派工 → ok", okk)
    bad_unknown, e1 = validate_dispatch({**good, "evil": 1})
    check("未知欄位 → 拒（死 schema）", not bad_unknown and any("未知" in x for x in e1))
    bad_action, _ = validate_dispatch({**good, "action": "wire_money"})
    check("非法 action → 拒", not bad_action)
    bad_missing, _ = validate_dispatch({"action": "no_op"})
    check("缺必填欄位 → 拒", not bad_missing)
    bad_sess, _ = validate_dispatch({"action": "open_session", "rationale": "x",
                                     "cited_scores": [], "tools_requested": []})
    check("open_session 缺 session_kind → 拒", not bad_sess)

    # --- reflect_and_validate（四門 AND）---
    pdp_ok = evaluate_freshness(now_ms - 60_000, max_age_sec=5400, now_epoch_sec=now)
    pdp_bad = evaluate_freshness(now_ms - 9_999_999, max_age_sec=5400, now_epoch_sec=now)
    dispatch_ok = {"action": "continue_task", "task_id": "51",
                   "rationale": "strength 高，推進下一步",
                   "cited_scores": ["strength"],
                   "tools_requested": ["Read", "mcp__8762a8e2__get_ticker"]}
    v1 = reflect_and_validate(dispatch_ok, frozen_scores=fs, pdp=pdp_ok)
    check("四門全過 → accepted", v1.accepted)
    v2 = reflect_and_validate(dispatch_ok, frozen_scores=fs, pdp=pdp_bad)
    check("帳本過期 → 拒（PDP 門）", not v2.accepted)
    dispatch_fab = {**dispatch_ok, "cited_scores": ["ghost_score"]}
    v3 = reflect_and_validate(dispatch_fab, frozen_scores=fs, pdp=pdp_ok)
    check("引用不存在分數 → 拒（反捏造）", not v3.accepted)
    dispatch_order = {**dispatch_ok,
                      "tools_requested": ["mcp__okx-trade-mcp__futures_place_order"]}
    v4 = reflect_and_validate(dispatch_order, frozen_scores=fs, pdp=pdp_ok)
    check("派工請求下單工具 → 拒（allowlist 門）", not v4.accepted)

    print(f"\noverseer_policy 自測：{ok}/{ok + fail} 通過")
    return fail == 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(0 if _selftest() else 1)
    else:
        print("用法：python -m l3_dispatcher.overseer_policy --selftest")
