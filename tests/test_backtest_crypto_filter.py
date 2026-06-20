"""來源邊界 parity：回測流動性池也須濾掉 OKX 代幣化美股/商品（task#73-C MEDIUM）。

backtest_session._liquid_symbols 與 watchlist 同樣讀 scanner.db（經 _market_candidates），
若不濾，冷啟動 fail-open 窗內落入 scanner.db 的代幣化美股/商品會被拉進歷史回測宇宙。
雖屬唯讀歷史路徑（零真錢、零訊號影響、且 CoinGlass 抓不到 MU 歷史多半自然跳過），仍與
watchlist 主閘對齊做 parity 濾除。fail-open：分類快取空→不過濾（is_crypto_base 回 True）。

全離線：monkeypatch _market_candidates 與 _CRYPTO_BASES，零網路、零真錢。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import l3_dispatcher.market_scanner as ms
import l3_dispatcher.watchlist as wl
from backtest import backtest_session as bs


def test_liquid_symbols_filters_noncrypto(monkeypatch):
    # scanner.db 池含代幣化美股/商品；允許集健康 → 濾掉 MU/NVDA/CL，保序
    monkeypatch.setattr(
        wl, "_market_candidates",
        lambda **k: ["BTC", "ETH", "MU", "NVDA", "SOL", "CL"])
    monkeypatch.setattr(ms, "_CRYPTO_BASES", {"BTC", "ETH", "SOL"})
    got = bs._liquid_symbols(cap=10)
    assert got == ["BTC", "ETH", "SOL"]


def test_liquid_symbols_failopen_empty_cache(monkeypatch):
    # 冷啟動（允許集空）→ fail-open 不過濾（寧納勿空，下輪刷新後自癒）
    monkeypatch.setattr(
        wl, "_market_candidates", lambda **k: ["BTC", "ETH", "MU"])
    monkeypatch.setattr(ms, "_CRYPTO_BASES", set())
    got = bs._liquid_symbols(cap=10)
    assert got == ["BTC", "ETH", "MU"]


def test_liquid_symbols_all_noncrypto_falls_back_to_majors(monkeypatch):
    # 極端：池全為非加密 + 允許集健康 → 濾成空 → fallback 主流幣（永不回空）
    monkeypatch.setattr(
        wl, "_market_candidates", lambda **k: ["MU", "NVDA", "CL"])
    monkeypatch.setattr(ms, "_CRYPTO_BASES", {"BTC", "ETH", "SOL"})
    got = bs._liquid_symbols(cap=4)
    assert got == ["BTC", "ETH", "SOL", "BNB"]   # 落回 fallback 名單前 4
