# -*- coding: utf-8 -*-
"""真錢副本產生器（make_live_copy.ps1）的代換表守門測試。零網路、零 OKX 呼叫。

【為什麼需要這一支】
真錢執行器 consume_intents_live.py 不是手寫的，是 make_live_copy.ps1 拿模板
consume_intents.py 做**字串代換**產生的：風險 100U→20U、名義值 3000U→600U、
日熔斷 300U→60U、週熔斷 750U→150U、狀態檔改成 *_live.json、demo 閘反轉。

PowerShell 的 .Replace()「找不到就原樣返回」——不報錯、不回傳筆數。所以只要有人
動了模板裡這幾行的寫法（改個空格、加個註解、換個預設值），對應的代換就會**靜默
沒發生**，產生器照樣印「live copy created OK」，而真錢副本會沿用 demo 級參數：
每筆風險 100U（5 倍）、名義值上限 3000U（5 倍）、日熔斷 300U（5 倍）。
同一物種的失效（靜默 no-op 被當成成功）在本專案已重複出現過三次以上。

本檔把「代換表 vs 模板」的一致性拉進 pytest：模板一改壞錨點，測試當場紅，
不必等到有人下次產生真錢副本才發現。產生器那一端另有 fail-closed（錨點不是剛好
命中 1 次就 throw、且一個字都不寫出去），兩端各守一半。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "tools" / "atk_consumer" / "consume_intents.py"
GENERATOR = ROOT / "tools" / "atk_consumer" / "make_live_copy.ps1"

# 代換表在 make_live_copy.ps1 裡長這樣（單一真相，兩邊都讀它）：
#   @('PROFILE = "demo"', 'PROFILE = "live"', 'profile'),
_SUB_LINE = re.compile(
    r"^\s*@\(\s*'((?:[^']|'')*)'\s*,\s*'((?:[^']|'')*)'\s*,\s*'((?:[^']|'')*)'\s*\)",
    re.M,
)

# 模板裡「一旦沒被代換就會讓真錢副本沿用 demo 級規模」的常數名。
# 新增這類常數卻忘了加代換 → test_no_risk_critical_constant_escapes_the_table 會紅。
_RISK_CRITICAL = re.compile(r"^(PROFILE|.*RISK.*|.*NOTIONAL.*|LEVERAGE|.*STOP_USD)$")

# 必須指向獨立檔案的狀態檔（真錢帳與模擬帳絕不可共用）。
# ⛔ intent_outbox 是**刻意**共用的（訊號來源同一份），故不在此列。
_STATE_FILES = ("atk_consumer_state.json", "atk_positions.json",
                "atk_consumer_health.json")


def _subs():
    """讀出產生器的代換表：[(from, to, label), ...]"""
    text = GENERATOR.read_text(encoding="utf-8")
    return [(a.replace("''", "'"), b.replace("''", "'"), c)
            for a, b, c in _SUB_LINE.findall(text)]


def anchor_hits(template_text: str, anchor: str) -> int:
    """錨點命中次數。後面接數字／小數點的不算命中——純子字串比對會讓
    `LEVERAGE = 5` 命中改成 `LEVERAGE = 50` 的模板，代換後變成 `LEVERAGE = 200`
    （200 倍槓桿上真錢）。產生器用同一條邊界規則。"""
    return len(re.findall(re.escape(anchor) + r"(?![\d.])", template_text))


def missing_anchors(template_text: str, subs) -> list:
    """純函式：回報「在模板中不是剛好命中 1 次」的錨點（產生器該中止的條件）。"""
    return [(a, anchor_hits(template_text, a)) for a, _to, _lab in subs
            if anchor_hits(template_text, a) != 1]


def _module_constants(text: str) -> dict:
    return {m.group(1): m.group(2).strip()
            for m in re.finditer(r"^([A-Z][A-Z0-9_]*)\s*=\s*(.+)$", text, re.M)}


def _num(s):
    m = re.search(r"=\s*(-?\d+(?:\.\d+)?)", s)
    return float(m.group(1)) if m else None


def test_generator_exposes_a_parsable_substitution_table():
    subs = _subs()
    assert len(subs) >= 11, f"代換表只解析到 {len(subs)} 筆——格式壞了或表被拆散"
    labels = [lab for _a, _b, lab in subs]
    assert len(set(labels)) == len(labels), f"代換標籤重複：{labels}"


def test_every_anchor_still_matches_template_exactly_once():
    """每個錨點都必須在模板中剛好命中 1 次；0 次＝靜默沒代換，>1 次＝代換到不該動的地方。"""
    bad = missing_anchors(TEMPLATE.read_text(encoding="utf-8"), _subs())
    assert not bad, f"錨點與模板對不上（錨點, 命中次數）：{bad}"


def test_checker_catches_a_broken_anchor():
    """負向檢定：把模板的預設值改掉，missing_anchors 必須抓到（證明上一支不是虛設）。"""
    text = TEMPLATE.read_text(encoding="utf-8").replace(
        "RISK_USD = 100.0", "RISK_USD = 90.0")
    bad = missing_anchors(text, _subs())
    assert [a for a, _n in bad if a == "RISK_USD = 100.0"], \
        f"改壞錨點後竟然沒被抓到，檢查本身是空的：{bad}"


def test_checker_catches_a_digit_extended_anchor():
    """負向檢定二：模板把 `LEVERAGE = 5` 改成 `LEVERAGE = 50`——純子字串比對會照樣
    命中並產出 `LEVERAGE = 200`（200 倍槓桿上真錢）。邊界規則必須判定為 0 次命中。"""
    text = TEMPLATE.read_text(encoding="utf-8").replace(
        "LEVERAGE = 5 ", "LEVERAGE = 50 ")
    assert anchor_hits(text, "LEVERAGE = 5") == 0, \
        "數字被延長後仍判定命中——邊界規則沒生效，代換會算出離譜槓桿"


def test_no_risk_critical_constant_escapes_the_table():
    """模板新增風險級常數卻沒進代換表 → 真錢副本會沿用 demo 級數值，這裡先擋下。"""
    covered = {a.split("=")[0].strip() for a, _b, _lab in _subs()}
    escaped = [name for name in _module_constants(TEMPLATE.read_text(encoding="utf-8"))
               if _RISK_CRITICAL.match(name) and name not in covered]
    assert not escaped, f"風險級常數沒有對應代換：{escaped}"


def test_state_files_are_redirected_to_separate_live_files():
    """真錢帳的狀態／持倉／健康檔必須另開一份，絕不可與模擬帳共用。"""
    subs = _subs()
    for fname in _STATE_FILES:
        hit = [(a, b) for a, b, _lab in subs if a == fname]
        assert hit, f"{fname} 沒有被改導向真錢專用檔"
        assert hit[0][1] != fname and "_live" in hit[0][1], \
            f"{fname} 的代換目標不是獨立真錢檔：{hit[0][1]}"


def test_live_risk_caps_are_strictly_smaller_than_template():
    """真錢側的每一個風險上限都必須比模板小（副本存在的理由就是縮小暴險）。"""
    for a, b, lab in _subs():
        name = a.split("=")[0].strip()
        if not re.match(r"^(RISK_USD|RISK_USD_CAP|NOTIONAL_CAP_USD|"
                        r"DAILY_STOP_USD|WEEKLY_STOP_USD)$", name):
            continue
        old, new = _num(a), _num(b)
        assert old is not None and new is not None, f"{lab} 解析不出數值"
        assert 0 < new < old, f"{name} 真錢值 {new} 沒有小於模板值 {old}"


def test_profile_flip_and_demo_guard_flip_travel_together():
    """PROFILE 轉 live 就一定要同時反轉 demo 閘；只翻一半＝真錢副本仍要求 demo:true（或反之）。"""
    joined = " ".join(a + "→" + b for a, b, _lab in _subs())
    assert 'PROFILE = "demo"→PROFILE = "live"' in joined, "PROFILE 沒有被轉成 live"
    assert 'prof.get("demo") is True→prof.get("demo") is False' in joined, \
        "demo 閘沒有跟著反轉——⛔ 這兩個代換必須成對存在"
