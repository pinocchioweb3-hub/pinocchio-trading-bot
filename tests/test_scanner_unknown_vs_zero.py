"""全市場掃描器：「這輪讀不出來」不得折成「確定的市場數字」（同物種第 34 次）。

根因形狀（與 v208/v210/v212/v213 同一族）：
    l3_dispatcher/market_scanner.py:172-174 三支請求一律 `r.json().get("data", [])`
    ——沒有 HTTP status 檢查、沒有 code!='0' 檢查。429/5xx 的錯誤 body 也會被折成
    「交易所回答：沒有資料」。r1(tickers) 失敗時 snap 為空、迴圈 `if snap:` 會整輪跳過
    ＝已 fail-safe；但 **r1 成功而 r2(OI)／r3(資費) 被限流** 時，整批 oi_usd/funding
    變 None，且：

      compute_breadth() 的 `avg_f = sum/len if fundings else 0.0`
        → 「全市場資費一個都讀不到」被寫成 **avg_funding = 0.0（＝資費中性）**，並且
          ① render_breadth_line 印給使用者「均資費 +0.000%」＝一個確定的數字；
          ② macro_confluence.collect 的 `isinstance(af,(int,float))` 收下 0.0 →
             掛成 avg_funding_8h ＝**被計分**的宏觀分量（score_funding），present 判定
             也算它在線；同時 `if "avg_funding_8h" not in out` 那條「退而用 BTC funding
             代理」的設計備援因此**永遠不會啟動**；
          ③ postmortem 把它寫進每筆交易的當下市場環境 → 進復盤資料集。

治本＝只分離「未知」與「答案」：
    - _okx_rows(resp)：None＝這輪讀不出來（非 200／JSON 解不開／code!='0'／缺 data
      鍵／data 非 list）；list＝答案。⛔ 邊界線同 v208/v210/v212/v213：
      {"code":"0","data":[]} 仍是「交易所確認沒有」＝答案，不可為保險打成未知。
    - compute_breadth：一筆可讀資費都沒有 → avg_funding/n_overheat 回 None（未知），
      不再折成 0.0/0。滿窗（有可讀資費）時逐項與舊碼相同（反向側守門）。
    - render_breadth_line：None → 標「讀不出來」，不得印成 +0.000%。

本檔全離線、零真錢、零訊號數學（不碰 strength.py／eval_cvd_divergence）。
"""
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import l3_dispatcher.market_scanner as ms


def _sym(last=100.0, vol=50e6, oi=1e6, funding=0.0002, chg=1.0):
    return {"last": last, "vol24h_usd": vol, "oi_usd": oi,
            "funding": funding, "chg24h_pct": chg}


def _snap(n=40, *, funding=0.0002, oi=1e6):
    """n 檔全部達流動性門檻的快照。funding=None ＝那一輪資費整批讀不出來。"""
    return {f"C{i}": _sym(funding=funding, oi=oi) for i in range(n)}


def _use_tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "DB_PATH", str(tmp_path / "scanner_test.db"))
    ms.init_db()


# ── ① 資費整批讀不出來 → 不得折成「均資費 0.000%」 ─────────────────────────
def test_breadth_avg_funding_is_none_when_nothing_readable(tmp_path, monkeypatch):
    """這是本輪的主症狀：舊碼把「一筆都沒讀到」寫成 avg_funding=0.0（＝中性）。"""
    _use_tmp_db(tmp_path, monkeypatch)
    b = ms.compute_breadth(_snap(funding=None), {}, 1_700_000_000)
    assert b["avg_funding"] is None, (
        f"資費一筆都讀不到卻給出確定的均資費 {b['avg_funding']!r}"
        "（會被印成 +0.000%、被 macro_confluence 計分、並擋掉 BTC funding 備援）")


def test_breadth_overheat_is_none_when_funding_unreadable(tmp_path, monkeypatch):
    """n_overheat 是「資費 ≥0.1% 的檔數」；資費讀不到時它不是 0，是未知。"""
    _use_tmp_db(tmp_path, monkeypatch)
    b = ms.compute_breadth(_snap(funding=None), {}, 1_700_000_000)
    assert b["n_overheat"] is None, (
        f"資費讀不到卻斷言「過熱 {b['n_overheat']} 檔」＝把未知講成『確定沒有過熱』")


def test_breadth_none_avg_funding_survives_db_roundtrip(tmp_path, monkeypatch):
    """未知必須以 NULL 落地，讀回來仍是 None（不能在寫入層被 round() 炸掉或補 0）。"""
    _use_tmp_db(tmp_path, monkeypatch)
    ms.compute_breadth(_snap(funding=None), {}, 1_700_000_000)
    got = ms.get_latest_breadth()
    assert got is not None and got["avg_funding"] is None
    assert got["n_overheat"] is None


def test_render_breadth_line_says_unreadable_not_zero(tmp_path, monkeypatch):
    """使用者看到的那一行：不得出現 +0.000%，必須講明這輪讀不出來。"""
    _use_tmp_db(tmp_path, monkeypatch)
    b = ms.compute_breadth(_snap(funding=None), {}, 1_700_000_000)
    line = ms.render_breadth_line(b)
    assert "0.000%" not in line, f"把未知印成確定的均資費：{line}"
    assert "讀不出來" in line, f"未標明資費缺料：{line}"


# ── ② 反向側守門：讀得到就得跟舊碼逐項相同（不許退化成一律不敢算）────────────
def test_breadth_unchanged_when_funding_readable(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    snap = _snap(n=40, funding=0.0002)
    snap["HOT1"] = _sym(funding=0.0015)      # ≥0.001 ⇒ 過熱
    snap["HOT2"] = _sym(funding=0.0020)
    b = ms.compute_breadth(snap, {}, 1_700_000_000)
    expected = round((0.0002 * 40 + 0.0015 + 0.0020) / 42, 6)
    assert b["avg_funding"] == expected
    assert b["n_overheat"] == 2
    assert b["n_total"] == 42
    line = ms.render_breadth_line(b)
    assert "讀不出來" not in line and "%" in line


def test_breadth_partial_funding_averages_readable_only(tmp_path, monkeypatch):
    """部分可讀：用可讀的算（有答案就給答案），不因為有缺料就整項作廢。"""
    _use_tmp_db(tmp_path, monkeypatch)
    snap = _snap(n=10, funding=None)
    snap["OK1"] = _sym(funding=0.001)
    snap["OK2"] = _sym(funding=0.003)
    b = ms.compute_breadth(snap, {}, 1_700_000_000)
    assert b["avg_funding"] == round((0.001 + 0.003) / 2, 6)
    assert b["n_overheat"] == 2


# ── ③ _okx_rows：未知 vs 確認沒有 ────────────────────────────────────────
class _Resp:
    def __init__(self, status=200, payload=None, boom=False):
        self.status_code = status
        self._payload = payload
        self._boom = boom

    def json(self):
        if self._boom:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


def test_okx_rows_confirmed_empty_stays_an_answer():
    """⛔ 邊界線：交易所明講 code=0 且 data=[] ＝確認沒有，不可打成未知
    （否則每輪正常空回應都變告警＝慢性假警報）。"""
    assert ms._okx_rows(_Resp(200, {"code": "0", "data": []})) == []


def test_okx_rows_success_returns_rows():
    rows = [{"instId": "BTC-USDT-SWAP", "last": "1"}]
    assert ms._okx_rows(_Resp(200, {"code": "0", "data": rows})) == rows


def test_okx_rows_unknown_on_http_error_and_bad_body():
    # 429 限流：舊碼會把錯誤 body 折成「沒有資料」
    assert ms._okx_rows(_Resp(429, {"msg": "Too Many Requests"})) is None
    assert ms._okx_rows(_Resp(500, {})) is None
    # 200 但 OKX 業務碼非 0
    assert ms._okx_rows(_Resp(200, {"code": "50011", "msg": "rate limit"})) is None
    # JSON 解不開（HTML 錯誤頁）
    assert ms._okx_rows(_Resp(200, None, boom=True)) is None
    # 缺 data 鍵／data 不是 list／body 不是 dict
    assert ms._okx_rows(_Resp(200, {"code": "0"})) is None
    assert ms._okx_rows(_Resp(200, {"code": "0", "data": {"a": 1}})) is None
    assert ms._okx_rows(_Resp(200, ["x"])) is None


# ── ④ fetch_market_snapshot_ex：三支請求各自的缺料要講出來 ─────────────────
class _FakeClient:
    def __init__(self, r1, r2, r3):
        self._rs = [r1, r2, r3]
        self._i = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        # gather 順序固定：tickers, open-interest, funding-rate
        for i, key in enumerate(("market/tickers", "open-interest", "funding-rate")):
            if key in url:
                return self._rs[i]
        raise AssertionError(url)


def _patch_client(monkeypatch, r1, r2, r3):
    async def _noop(client=None):
        return None
    monkeypatch.setattr(ms, "ensure_crypto_allowset", _noop)
    monkeypatch.setattr(ms, "_CRYPTO_INSTIDS", {"BTC-USDT-SWAP", "ETH-USDT-SWAP"})
    monkeypatch.setattr(ms.httpx, "AsyncClient",
                        lambda *a, **k: _FakeClient(r1, r2, r3))


_TICKERS = [
    {"instId": "BTC-USDT-SWAP", "last": "100", "open24h": "99", "volCcy24h": "1000000"},
    {"instId": "ETH-USDT-SWAP", "last": "10", "open24h": "10", "volCcy24h": "10000000"},
]


def test_fetch_ex_flags_oi_and_funding_unreadable(monkeypatch):
    _patch_client(
        monkeypatch,
        _Resp(200, {"code": "0", "data": _TICKERS}),
        _Resp(429, {"msg": "Too Many Requests"}),
        _Resp(200, {"code": "50011", "msg": "rate limit"}),
    )
    out, gaps = asyncio.run(ms.fetch_market_snapshot_ex())
    assert set(out) == {"BTC", "ETH"}            # 價格層仍照常產出（不因缺料整輪作廢）
    assert gaps["tickers_unreadable"] is False
    assert gaps["oi_unreadable"] is True
    assert gaps["funding_unreadable"] is True
    assert all(v["funding"] is None for v in out.values())


def test_fetch_ex_confirmed_empty_is_not_a_gap(monkeypatch):
    """交易所確認沒有 OI/資費列 → 不算缺料（否則會變慢性假警報）。"""
    _patch_client(
        monkeypatch,
        _Resp(200, {"code": "0", "data": _TICKERS}),
        _Resp(200, {"code": "0", "data": []}),
        _Resp(200, {"code": "0", "data": []}),
    )
    out, gaps = asyncio.run(ms.fetch_market_snapshot_ex())
    assert gaps["oi_unreadable"] is False
    assert gaps["funding_unreadable"] is False
    assert set(out) == {"BTC", "ETH"}


def test_fetch_ex_tickers_unreadable_yields_empty_snapshot(monkeypatch):
    """r1 讀不出來 → 空快照（迴圈 `if snap:` 會整輪跳過＝既有 fail-safe），
    但必須留痕，不可與「全市場零檔」同形。"""
    _patch_client(
        monkeypatch,
        _Resp(503, {"msg": "service unavailable"}),
        _Resp(200, {"code": "0", "data": []}),
        _Resp(200, {"code": "0", "data": []}),
    )
    out, gaps = asyncio.run(ms.fetch_market_snapshot_ex())
    assert out == {}
    assert gaps["tickers_unreadable"] is True


def test_fetch_market_snapshot_backcompat(monkeypatch):
    """舊呼叫端（unlock_calendar）維持只拿 dict 的介面。"""
    _patch_client(
        monkeypatch,
        _Resp(200, {"code": "0", "data": _TICKERS}),
        _Resp(200, {"code": "0", "data": [
            {"instId": "BTC-USDT-SWAP", "oiUsd": "12345"}]}),
        _Resp(200, {"code": "0", "data": [
            {"instId": "BTC-USDT-SWAP", "fundingRate": "0.0001"}]}),
    )
    out = asyncio.run(ms.fetch_market_snapshot())
    assert out["BTC"]["oi_usd"] == 12345.0
    assert out["BTC"]["funding"] == 0.0001
    assert out["ETH"]["oi_usd"] is None      # 交易所確認沒這列 ＝ 仍是 None（原行為）


# ── ⑤ 下游守門：None 才會讓 macro_confluence 走誠實路徑 ─────────────────────
def test_macro_confluence_treats_none_funding_as_absent():
    """記錄「為何 0.0 有害」：0.0 被當成在線分量並擋掉 BTC funding 備援；
    None 才會讓 `if 'avg_funding_8h' not in out` 那條備援有機會啟動。"""
    from l3_dispatcher import macro_confluence as mc
    assert mc.score_funding(None) == 0.0          # 未知不加不減
    assert mc.score_funding(0.0) == 0.0
    # collect() 的收下條件：0.0 會被收下（＝擋掉備援），None 不會
    assert isinstance(0.0, (int, float)) is True
    assert isinstance(None, (int, float)) is False
