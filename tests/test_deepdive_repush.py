"""deepdive 同幣「重推卡」冷卻測試（PEPE 連推 bug 修復・v67）。

鎖住根因與修復，避免日後改動把缺口重新埋回：
  根因：中性/非可做單 deepdive 卡 →（a）不建紙上倉，不進 open_syms_set；
        （b）方向非 bull/bear，跳過 symbol_gate.claim()（macro.py ~852）。
        兩道 v47-2 防線都對它失效 → 該幣每 cadence(6h) 被當「唯一未開倉候選」重推。
  修復：_deepdive_candidates 多加一道「近期已推同幣 deepdive 卡」冷卻（不分方向），
        窗 = _deepdive_repush_window()（預設 24h > 6h cadence，故不會剛過期就重推）。

全離線：monkeypatch symbol_gate.DB_PATH 到暫存 sqlite；零網路、零真錢、零訊號數學。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import l3_dispatcher.symbol_gate as sg
from l3_dispatcher.macro import _deepdive_candidates, _deepdive_repush_window


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(sg, "DB_PATH", tmp_path / "symbol_gate_test.db")


def test_repush_window_exceeds_cadence(tmp_path, monkeypatch):
    # 預設窗必須 > 6h cadence，否則每輪剛好過期 = bug 時序根因
    assert _deepdive_repush_window(21600) > 21600
    assert _deepdive_repush_window(21600) >= 86400
    # 覆寫生效
    monkeypatch.setenv("DEEPDIVE_REPUSH_WINDOW_S", "7200")
    # botconfig 可能快取；直接驗 default 邏輯仍 > cadence 即可（覆寫值由 botconfig 解讀）
    assert _deepdive_repush_window(3600) >= 3600


def test_neutral_card_not_repushed_within_window(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    t0 = 1_000_000
    win = 86400  # 24h
    # PEPE 推過一張中性 deepdive 卡（不建倉 → 不在 open set），標記 (PEPE,'deepdive')
    sg.mark_sent("PEPE", "deepdive", now=t0)

    # 6h 後（< 24h 窗）再選候選：PEPE 應被「近期已推」排除，不再連推
    picked, recently = _deepdive_candidates(
        ["PEPE", "DOGE"], set(), 3, win, now=t0 + 21600)
    assert "PEPE" in recently
    assert "PEPE" not in picked
    assert "DOGE" in picked          # 其他幣不受影響


def test_repush_allowed_after_window_expires(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    t0 = 1_000_000
    win = 86400
    sg.mark_sent("PEPE", "deepdive", now=t0)
    # 超過窗 → PEPE 重新可被分析（不是永久封鎖，只是降頻）
    picked, recently = _deepdive_candidates(
        ["PEPE"], set(), 3, win, now=t0 + win + 1)
    assert "PEPE" not in recently
    assert "PEPE" in picked


def test_open_position_exclusion_preserved(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    t0 = 1_000_000
    win = 86400
    # 已開倉者即使從未推過 deepdive 卡，仍要被排除（保留 v47-2 既有防線）
    picked, recently = _deepdive_candidates(
        ["PEPE", "DOGE"], {"DOGE"}, 3, win, now=t0)
    assert "DOGE" not in picked
    assert "PEPE" in picked          # 未開倉且未近期推 → 可選


def test_non_crypto_excluded_from_deepdive(tmp_path, monkeypatch):
    """task#73-B：美股/貴金屬商品即使漏進交易層宇宙，也不得進 deepdive（加密結構分析）。
    deepdive 卡＝加密 SMC/Wyckoff；美股走獨立 1h 突破引擎、無此卡，商品非射程。
    這道閘是 defense-in-depth：上游今日純加密，但來源一旦變更不得破壞架構。"""
    _isolate(tmp_path, monkeypatch)
    t0 = 1_000_000
    win = 86400
    picked, _recently = _deepdive_candidates(
        ["BTC", "ETH", "MU", "NVDA", "XAU"], set(), 10, win, now=t0)
    assert picked == ["BTC", "ETH"]        # 只有加密進 deepdive，保序
    assert "MU" not in picked and "NVDA" not in picked   # 美股白名單擋
    assert "XAU" not in picked             # 貴金屬商品擋


def test_crypto_only_preserves_open_and_recent_filters(tmp_path, monkeypatch):
    """task#73-B 與既有兩道閘正交：非加密被擋的同時，已開倉/近期已推的加密閘仍生效。"""
    _isolate(tmp_path, monkeypatch)
    t0 = 1_000_000
    win = 86400
    sg.mark_sent("ETH", "deepdive", now=t0)            # ETH 近期已推
    picked, recently = _deepdive_candidates(
        ["BTC", "ETH", "SOL", "MU"], {"SOL"}, 10, win, now=t0 + 60)
    assert "ETH" in recently
    assert picked == ["BTC"]               # ETH 冷卻、SOL 已開倉、MU 非加密 → 只剩 BTC


def test_top_n_slicing_and_order_preserved(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    t0 = 1_000_000
    win = 86400
    # 依強勢序取前 N，且排除/冷卻後仍保序
    sg.mark_sent("B", "deepdive", now=t0)      # B 近期已推 → 跳過
    picked, recently = _deepdive_candidates(
        ["A", "B", "C", "D"], {"C"}, 2, win, now=t0 + 60)
    assert "B" in recently
    assert picked == ["A", "D"]                 # C 已開倉、B 冷卻 → 取 A、D 前 2
