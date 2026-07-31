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


def render_live(template_text: str, subs) -> str:
    """純函式：把代換表整張套到模板上，得出「真錢副本應該長的樣子」。
    邊界規則與產生器一致（錨點後接數字／小數點不算命中）。"""
    s = template_text
    for a, b, _lab in subs:
        s = re.sub(re.escape(a) + r"(?![\d.])", lambda _m, b=b: b, s)
    return s


def live_copy_defects(live_text: str, subs) -> list:
    """純函式：逐筆回報真錢副本裡「代換沒生效」之處。
    (label, 症狀, 字串)；demo 值還在＝縮小暴險沒發生，真錢值不在場＝代換到一半。"""
    bad = []
    for a, b, lab in subs:
        if re.search(re.escape(a) + r"(?![\d.])", live_text):
            bad.append((lab, "demo 值仍在真錢副本裡", a))
        if not re.search(re.escape(b) + r"(?![\d.])", live_text):
            bad.append((lab, "真錢值不在場", b))
    return bad


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


# ---------------------------------------------------------------------------
# 以下針對「機器上實際在跑的那一份真錢副本」。上面幾支只驗『代換表 vs 模板』，
# 驗不到落地檔：模板修好、卻忘了重新產生副本（v154/v155/v156 每一輪都要多跑一次
# make_live_copy.ps1 -GenerateOnly），真錢執行器就繼續跑舊碼而沒有任何人會知道。
# verify_live.ps1 第 2 段本來想擋這個，但它只硬編查 11 處中的 5 處、而且只印
# MISSING 不改 exit code——「印了警告但回傳成功」在本專案已是重複出現的失效物種。
# ---------------------------------------------------------------------------

LIVE_COPY = ROOT / "tools" / "atk_consumer" / "consume_intents_live.py"

_NO_LIVE_COPY = (
    f"本機沒有 {LIVE_COPY.name}（尚未產生過真錢副本）——本檢查不適用，"
    "⛔ 不可解讀為『副本已驗過』"
)


def test_live_copy_is_exactly_the_current_template_rendered():
    """落地的真錢副本必須逐字等於「現行模板套上代換表」的結果。

    不等於的兩種成因都要擋：(a) 模板改了但沒重新產生副本＝真錢在跑舊碼；
    (b) 有人直接手改副本＝下次重新產生時那些手改會被無聲蓋掉。
    修法一律是重跑 `make_live_copy.ps1 -GenerateOnly`（它自己也是 fail-closed）。
    """
    if not LIVE_COPY.exists():
        import pytest
        pytest.skip(_NO_LIVE_COPY)
    subs = _subs()
    live = LIVE_COPY.read_text(encoding="utf-8-sig")
    defects = live_copy_defects(live, subs)
    assert not defects, f"真錢副本的代換沒生效：{defects}"
    expect = render_live(TEMPLATE.read_text(encoding="utf-8-sig"), subs)
    assert live == expect, (
        "真錢副本與現行模板不一致（副本已過期或被手改）——"
        "真錢執行器正在跑的不是模板上這份碼。請跑 make_live_copy.ps1 -GenerateOnly"
    )


def test_drift_checker_catches_a_stale_live_copy():
    """負向檢定：模板新增一行安全檢查、副本停留在舊版（＝忘了重新產生）。
    此時 11 處代換全部好端端在場，defects 是空的——只有逐字比對抓得到。"""
    subs = _subs()
    template = TEMPLATE.read_text(encoding="utf-8-sig")
    stale = render_live(template, subs)
    fresh = render_live(template + "\n# 模板新增的安全檢查\n", subs)
    assert not live_copy_defects(stale, subs), \
        "這個情境本來就該讓逐筆檢查全過（不然證明不了逐字比對的必要性）"
    assert stale != fresh, "副本落後模板一整段修改卻沒被抓到——逐字比對是空的"


def test_drift_checker_catches_a_half_substituted_live_copy():
    """負向檢定二：副本裡 RISK_USD 沒被代換（沿用 demo 級 100U＝5 倍暴險）。"""
    subs = _subs()
    half = render_live(TEMPLATE.read_text(encoding="utf-8-sig"), subs).replace(
        "RISK_USD = 20.0", "RISK_USD = 100.0")
    labels = [lab for lab, _sym, _s in live_copy_defects(half, subs)]
    assert "risk 1R 20U" in labels, \
        f"副本沿用 demo 級風險值竟沒被抓到，檢查本身是空的：{labels}"
