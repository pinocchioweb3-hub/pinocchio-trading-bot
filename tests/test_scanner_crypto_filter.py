"""來源邊界濾除：OKX 代幣化美股/商品（instCategory!=1）不得進加密掃描宇宙（task#73-C）。

根因：OKX 把代幣化美股（instCategory=='3'：AAPL/NVDA/MU/QQQ…）與商品
（=='4'：CL/XAU/NG…）也上架為 -USDT-SWAP。market_scanner.fetch_market_snapshot
只看 -USDT-SWAP 後綴 → 把這些非加密標的吸進 scanner.db → 污染廣度統計、誤發異常
告警，並經候選池 → 交易層 → fire_tier → 模擬下單／deepdive。它們不在加密 SMC 射程
（美股走獨立 1h 突破引擎、用真實股市數據；商品非射程）。

治本＝用 OKX instruments 的 instCategery=='1' 建純加密允許集，於來源邊界濾除。
本檔鎖住四個純函式 + 一個 async 刷新器的行為，全離線、零真錢、零訊號數學：
  - _extract_crypto_instids：只收 cat-1 USDT 永續，丟代幣化股/商品/反向/缺欄
  - _is_crypto_perp：判定 + 冷啟動 fail-open（空允許集→納入，絕不清空宇宙）
  - is_crypto_base：base 級判定（給 watchlist 候選池）
  - ensure_crypto_allowset：成功填充、失敗保留舊快取（壞回應不得清空）
"""
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import l3_dispatcher.market_scanner as ms


# OKX instruments 樣本：混 cat-1 加密、cat-3 代幣化美股、cat-4 商品、反向、缺欄
_ROWS = [
    {"instId": "BTC-USDT-SWAP", "instCategory": "1"},
    {"instId": "ETH-USDT-SWAP", "instCategory": "1"},
    {"instId": "TAO-USDT-SWAP", "instCategory": "1"},   # 真加密但名字像代號
    {"instId": "HYPE-USDT-SWAP", "instCategory": "1"},  # 真加密（曾誤判風險）
    {"instId": "LAB-USDT-SWAP", "instCategory": "1"},
    {"instId": "MU-USDT-SWAP", "instCategory": "3"},    # 代幣化美股（Micron）
    {"instId": "NVDA-USDT-SWAP", "instCategory": "3"},  # 代幣化美股
    {"instId": "CL-USDT-SWAP", "instCategory": "4"},    # 商品（原油）
    {"instId": "XAU-USDT-SWAP", "instCategory": "4"},   # 商品（黃金）
    {"instId": "BTC-USD-SWAP", "instCategory": "1"},    # 反向（非 USDT 結算）→ 丟
    {"instId": "FOO-USDT-SWAP"},                         # 缺 instCategory → 丟
]


# ── _extract_crypto_instids ──────────────────────────────────────────────
def test_extract_keeps_only_cat1_usdt_swaps():
    got = ms._extract_crypto_instids(_ROWS)
    assert got == {"BTC-USDT-SWAP", "ETH-USDT-SWAP", "TAO-USDT-SWAP",
                   "HYPE-USDT-SWAP", "LAB-USDT-SWAP"}


def test_extract_drops_tokenized_stocks_and_commodities():
    got = ms._extract_crypto_instids(_ROWS)
    assert "MU-USDT-SWAP" not in got       # cat-3 代幣化美股
    assert "NVDA-USDT-SWAP" not in got
    assert "CL-USDT-SWAP" not in got       # cat-4 商品
    assert "XAU-USDT-SWAP" not in got


def test_extract_drops_inverse_and_missing_category():
    got = ms._extract_crypto_instids(_ROWS)
    assert "BTC-USD-SWAP" not in got       # 非 USDT 結算
    assert "FOO-USDT-SWAP" not in got      # 缺 instCategory（保守不收）


def test_extract_empty_on_empty_input():
    assert ms._extract_crypto_instids([]) == set()


# ── _is_crypto_perp ──────────────────────────────────────────────────────
def test_is_crypto_perp_decision_with_allowset():
    allow = {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}
    assert ms._is_crypto_perp("BTC-USDT-SWAP", allow) is True
    assert ms._is_crypto_perp("MU-USDT-SWAP", allow) is False


def test_is_crypto_perp_non_usdt_swap_always_false():
    # 非 -USDT-SWAP 一律 False，連 fail-open 都不適用
    assert ms._is_crypto_perp("BTC-USD-SWAP", set()) is False
    assert ms._is_crypto_perp("BTC-USDT-SWAP", set()) is True  # 空集合→fail-open


def test_is_crypto_perp_failopen_on_empty_allowset():
    # 冷啟動取不到 instruments：寧納勿空（連 MU 也暫放行，呼叫端會印警告）
    assert ms._is_crypto_perp("MU-USDT-SWAP", set()) is True
    assert ms._is_crypto_perp("BTC-USDT-SWAP", set()) is True


# ── is_crypto_base ───────────────────────────────────────────────────────
def test_is_crypto_base_with_populated_set(monkeypatch):
    monkeypatch.setattr(ms, "_CRYPTO_BASES", {"BTC", "ETH", "TAO", "HYPE"})
    assert ms.is_crypto_base("BTC") is True
    assert ms.is_crypto_base("TAO") is True
    assert ms.is_crypto_base("MU") is False
    assert ms.is_crypto_base("CL") is False


def test_is_crypto_base_failopen_on_empty_set(monkeypatch):
    monkeypatch.setattr(ms, "_CRYPTO_BASES", set())
    # 空集合→fail-open（True）：寧納勿空候選池
    assert ms.is_crypto_base("MU") is True
    assert ms.is_crypto_base("BTC") is True


# ── ensure_crypto_allowset（async：成功填充 / 失敗保留舊）────────────────────
class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _OKClient:
    """假 httpx client：回傳 _ROWS（成功路徑）。"""
    async def get(self, url, params=None):
        return _FakeResp({"data": _ROWS})

    async def aclose(self):
        pass


class _BoomClient:
    """假 httpx client：get 直接拋（失敗路徑），驗證保留舊快取。"""
    async def get(self, url, params=None):
        raise RuntimeError("boom: instruments endpoint down")

    async def aclose(self):
        pass


# 只回 2 檔 cat-1（模擬「非空但可疑過小」回應＝未來 API 分頁/截斷）
_TWO_ROWS = [
    {"instId": "BTC-USDT-SWAP", "instCategory": "1"},
    {"instId": "ETH-USDT-SWAP", "instCategory": "1"},
]


class _SmallClient:
    """假 httpx client：回傳可疑過小集合（2 檔），驗證防截斷下限。"""
    async def get(self, url, params=None):
        return _FakeResp({"data": _TWO_ROWS})

    async def aclose(self):
        pass


def _reset_allowset(monkeypatch):
    monkeypatch.setattr(ms, "_CRYPTO_INSTIDS", set())
    monkeypatch.setattr(ms, "_CRYPTO_BASES", set())
    monkeypatch.setattr(ms, "_ALLOWSET_TS", 0.0)


def test_ensure_populates_on_success(monkeypatch):
    _reset_allowset(monkeypatch)
    asyncio.run(ms.ensure_crypto_allowset(client=_OKClient()))
    assert ms._CRYPTO_INSTIDS == {"BTC-USDT-SWAP", "ETH-USDT-SWAP",
                                  "TAO-USDT-SWAP", "HYPE-USDT-SWAP",
                                  "LAB-USDT-SWAP"}
    assert ms._CRYPTO_BASES == {"BTC", "ETH", "TAO", "HYPE", "LAB"}
    assert ms._ALLOWSET_TS > 0


def test_ensure_keeps_old_on_failure(monkeypatch):
    # 預先填一個已知舊集合；刷新失敗時必須原樣保留（壞回應不得清空宇宙）
    monkeypatch.setattr(ms, "_CRYPTO_INSTIDS", {"BTC-USDT-SWAP"})
    monkeypatch.setattr(ms, "_CRYPTO_BASES", {"BTC"})
    monkeypatch.setattr(ms, "_ALLOWSET_TS", 0.0)  # 過期 → 會嘗試刷新
    asyncio.run(ms.ensure_crypto_allowset(client=_BoomClient()))
    assert ms._CRYPTO_INSTIDS == {"BTC-USDT-SWAP"}   # 舊集合保留
    assert ms._CRYPTO_BASES == {"BTC"}


def test_ensure_noop_within_ttl(monkeypatch):
    # TTL 內（剛填過）→ 直接 no-op，不碰 client（傳 Boom 也不該被呼叫）
    import time as _t
    monkeypatch.setattr(ms, "_CRYPTO_INSTIDS", {"BTC-USDT-SWAP"})
    monkeypatch.setattr(ms, "_CRYPTO_BASES", {"BTC"})
    monkeypatch.setattr(ms, "_ALLOWSET_TS", _t.time())  # 剛填，未過期
    asyncio.run(ms.ensure_crypto_allowset(client=_BoomClient()))  # 不該拋
    assert ms._CRYPTO_INSTIDS == {"BTC-USDT-SWAP"}


# ── 防截斷下限（size-sanity guard，task#73-C hardening）─────────────────────
def test_ensure_keeps_old_when_new_set_implausibly_small(monkeypatch):
    # 已有健康集合（4 檔），刷新卻只回 2 檔（< 下限 3）→ 保留舊，不被覆蓋（防截斷）
    monkeypatch.setattr(ms, "_ALLOWSET_MIN_PLAUSIBLE", 3)
    healthy = {"BTC-USDT-SWAP", "ETH-USDT-SWAP",
               "SOL-USDT-SWAP", "XRP-USDT-SWAP"}
    monkeypatch.setattr(ms, "_CRYPTO_INSTIDS", set(healthy))
    monkeypatch.setattr(ms, "_CRYPTO_BASES", {"BTC", "ETH", "SOL", "XRP"})
    monkeypatch.setattr(ms, "_ALLOWSET_TS", 0.0)  # 過期 → 會嘗試刷新
    asyncio.run(ms.ensure_crypto_allowset(client=_SmallClient()))
    assert ms._CRYPTO_INSTIDS == healthy   # 可疑過小 → 保留舊健康集合


def test_ensure_cold_start_accepts_small_set(monkeypatch):
    # 冷啟動（空快取）不套下限：首次填充永不被擋，即使回應很小（fail-open 大原則）
    monkeypatch.setattr(ms, "_ALLOWSET_MIN_PLAUSIBLE", 99)  # 下限遠高於 2
    _reset_allowset(monkeypatch)                            # 空快取＝冷啟動
    asyncio.run(ms.ensure_crypto_allowset(client=_SmallClient()))
    assert ms._CRYPTO_INSTIDS == {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}
