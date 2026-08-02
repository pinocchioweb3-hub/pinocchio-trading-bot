# -*- coding: utf-8 -*-
"""v211：K 線窗口「抓到一半就中斷」不再折成「這段期間就只有這些 K 線」。

同物種（未知→折成確定）第 31 次，這次是**鏡像變體**：前 30 次都是
「查不到 → 折成沒有」，這次是「只抓到部分 → 折成全部」。

落點：l3_dispatcher/paper_audit.fetch_window 的分頁迴圈——傳輸例外 /
HTTP 非 200 / JSON 解不開 / code!=0 一律 `break`，回去的是**部分**收集到的
K 線，與「這個窗口真的就這麼多根」在型別上完全一樣（都是一個 list）。

後果（紅線③相鄰）：audit_one 拿這個殘缺窗口算 high/low，會把
「我沒看到那段」判成「那個價從未被觸及＝疑似捏造」→ 對**紙上樣本**發出
假造假指控。方向與前 30 次相反：不是漏報，是**誤報**。

⛔ 邊界：`{"code":"0","data":[]}` 是交易所明講「更舊沒有了」＝確定，
不可為保險打成未知（否則每次正常抓到底都變告警＝慢性假警報）。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from l3_dispatcher.paper_audit import (  # noqa: E402
    Finding, audit_one, fetch_window, render_audit_report,
)

BAR_MS = 900_000  # 15m


class _Resp:
    def __init__(self, status=200, body=None, bad_json=False):
        self.status_code = status
        self._body = body
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._body


class _Client:
    """依序回傳預先排好的頁面；Exception 實例代表傳輸層爆掉。"""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    async def get(self, path, params=None):
        self.calls.append(params)
        idx = len(self.calls) - 1
        item = self.pages[idx] if idx < len(self.pages) else self.pages[-1]
        if isinstance(item, Exception):
            raise item
        return item


class _Src:
    def __init__(self, pages):
        self.client = _Client(pages)

    def _to_inst(self, symbol):
        return f"{symbol}-USDT-SWAP"


def _rows(n_from_bar, n_to_bar, high, low):
    """產 [ts, o, h, l, c, ...] 降序列（含頭含尾，單位＝第幾根 bar）。"""
    return [[str(i * BAR_MS), "100", str(high), str(low), "100", "0", "0"]
            for i in range(n_from_bar, n_to_bar - 1, -1)]


def _page(rows):
    return _Resp(200, {"code": "0", "data": rows})


# 算術自洽的乾淨多單：tp1+tp2+stop → r = 0.5*1.0 + 0.3*2.0 + 0.2*(-1.0) = 0.9
def _trade(exit_bar):
    return {"id": 77, "symbol": "BTC", "setup": "intraday", "direction": "bull",
            "entry_price": 100, "stop_price": 90, "tp1": 110, "tp2": 120,
            "tp3": 140, "entry_at": 0, "exit_at": exit_bar * BAR_MS,
            "legs_hit": "tp1,tp2,stop", "exit_reason": "stop",
            "realized_r": 0.9, "pnl_usd": 90, "entry_filled_pct": 1.0}


# ── 行為性斷言（不需要新參數；在舊碼上必須以「判成造假」的形狀失敗）──

def test_truncated_window_does_not_accuse_fraud():
    """第 2 頁 HTTP 500 → 只看得到後半段窗口。後半段 high 只到 115，
    帳本聲稱打到的 tp2=120 在**沒看到的那半段**才被觸及。
    舊碼：判 flag「不可達／疑造假」。應為：warn，且不得出現造假指控。"""
    src = _Src([_page(_rows(199, 100, high=115, low=88)), _Resp(500)])
    f = asyncio.run(audit_one(_trade(199), src))
    assert f.verdict == "warn", f"殘缺窗口不可判造假，實得 {f.verdict}：{f.reasons}"
    joined = "；".join(f.reasons)
    assert "不可達" not in joined, f"仍在指控不可達：{joined}"
    assert "中斷" in joined, f"未講出窗口是抓到一半中斷的：{joined}"


def test_complete_window_still_flags_unreachable():
    """反向側：一頁就抓到底（oldest <= start）＝窗口完整 → tp2 不可達仍須 flag。
    這條在舊碼上就該綠，防止修補變成「一律不敢判造假」。"""
    src = _Src([_page(_rows(50, -49, high=115, low=88))])
    f = asyncio.run(audit_one(_trade(50), src))
    assert f.verdict == "flag", f"完整窗口的不可達必須照舊 flag，實得 {f.verdict}"
    assert any("不可達" in r for r in f.reasons)


def test_first_page_transport_error_names_the_break():
    """第一頁就爆 → 零根 K 線。舊碼只講『可能太舊或抓取失敗』，
    把『抓取中斷』與『這段期間本來就沒 K 線』混為一談。"""
    src = _Src([RuntimeError("boom")])
    f = asyncio.run(audit_one(_trade(199), src))
    assert f.verdict == "warn"
    assert any("中斷" in r for r in f.reasons), f.reasons


# ── 結構性斷言（需要新的 out 參數／新欄位）──────────────────────

def test_out_reports_truncation_reason():
    src = _Src([_page(_rows(199, 100, 115, 88)), _Resp(503)])
    out = {}
    got = asyncio.run(fetch_window(src, "BTC", 0, 199 * BAR_MS, "15m", out=out))
    assert out["truncated"] is True
    assert out["reason"] == "http_503"
    assert out["covered"] is False
    assert out["pages"] == 2
    assert len(got) == 100


def test_empty_data_page_is_not_truncation():
    """⛔ 邊界：交易所回 data:[] ＝『更舊沒有了』是**確定**，不是未知。"""
    src = _Src([_page(_rows(199, 100, 115, 88)), _page([])])
    out = {}
    asyncio.run(fetch_window(src, "BTC", 0, 199 * BAR_MS, "15m", out=out))
    assert out["truncated"] is False
    assert out["reason"] is None


def test_bad_json_and_api_code_are_truncation():
    out = {}
    asyncio.run(fetch_window(_Src([_page(_rows(199, 100, 115, 88)),
                                   _Resp(200, bad_json=True)]),
                             "BTC", 0, 199 * BAR_MS, "15m", out=out))
    assert (out["truncated"], out["reason"]) == (True, "bad_json")

    out2 = {}
    asyncio.run(fetch_window(_Src([_page(_rows(199, 100, 115, 88)),
                                   _Resp(200, {"code": "50011", "data": []})]),
                             "BTC", 0, 199 * BAR_MS, "15m", out=out2))
    assert (out2["truncated"], out2["reason"]) == (True, "api_code_50011")


def test_page_limit_is_truncation():
    """翻到頁數上限仍沒回到 start＝也是『沒看到的那段』，不可靜靜當全部。"""
    src = _Src([_page(_rows(999, 900, 115, 88))])  # 每頁都往回 100 根、永遠不到 0
    out = {}
    asyncio.run(fetch_window(src, "BTC", 0, 999 * BAR_MS, "15m",
                             max_pages=2, out=out))
    assert out["truncated"] is True
    assert out["reason"] == "page_limit"


def test_finding_carries_truncation_flag():
    src = _Src([_page(_rows(199, 100, 115, 88)), _Resp(500)])
    f = asyncio.run(audit_one(_trade(199), src))
    assert f.window_truncated is True
    assert "http_500" in (f.window_gap_reason or "")


def test_report_surfaces_truncated_count():
    ok = Finding(trade_id=1, symbol="BTC", setup="intraday", stored_r=0.9,
                 recomputed_r=0.9, verdict="ok", reasons=["算術自洽"])
    cut = Finding(trade_id=2, symbol="ETH", setup="intraday", stored_r=0.9,
                  recomputed_r=0.9, verdict="warn", reasons=["窗口抓取中斷"],
                  window_truncated=True, window_gap_reason="http_500")
    txt = render_audit_report([ok, cut], html=False)
    assert "窗口抓取中斷" in txt
    assert "1" in txt
