"""universe_provenance.py — 加密強度宇宙的來源留痕（零依賴葉模組，純觀測）。

為什麼需要這支獨立小模組（而不是把值塞在 watchlist、讓 plan_snapshot 直接 import）：
    v141 起 CoinGlass 訂閱到期（2026-07-08，帳單證實）→ 宇宙空時自動降級用免費 OKX
    大宗源。活線量到 `topN_agreement=0.0`——換源實質換掉了選出來的標的，於是「免費源
    時代」的加密樣本與 CG 時代的樣本**不可直接合併統計**。但進場當下若沒把來源凍進
    plan_snapshot，日後帳上就分不出這筆單出自哪個宇宙——這正是紅線③留痕要防的事，
    而且**只能前向累積、補不了**（封存快照永不回填）。

    plan_snapshot.py 有一條鐵則：純資料組裝、不 import 策略模組（有 CI ast 護欄擋）。
    watchlist 會轉帶 strength 數學，plan_snapshot 直接 import 它就弄髒了這條界線。
    所以把「來源」這顆狀態放在這支**零依賴葉模組**：watchlist 寫、plan_snapshot 讀，
    兩邊都不必認識對方，兩條鐵則都保住。

語意：
    set_universe_source()  由 watchlist.refresh 在每輪決定來源後呼叫。
    get_universe_source()  回「最近一次 refresh 生效的來源」＝進場那刻的宇宙來源。
    尚未 refresh 過（剛啟動）→ None ＝誠實留空，絕不猜（紅線③）。

v144(監督員r29)：留痕之上補「統計消費者」。
    v142 只把來源凍進快照，但沒有任何統計層讀它——優化器 120 天窗仍把兩代宇宙的
    樣本混在同一桶裡算晉升（實測 2026-07-31：unknown 152 筆 vs okx_free_fallback
    5 筆）。有訊號無消費者＝留痕形同不存在，且一旦樣本跨過 n≥30 就會用「上一代
    宇宙的成交行為」替這一代參數背書。以下三支純函式即該消費者的共用底座：
    generation_of_row / cohort_mix / active_generation，由 auto_optimizer 與
    entry_policy_optimizer 呼叫。此處不做過濾決策（那是各優化器的策略），只回事實。

    ⚠️ 「unknown」是一個**正當的世代標籤**，不是缺失值的同義詞：v142 之前的樣本
    真的無從得知出自哪一代（快照永不回填），故它自成一代，不併入任何已知來源。

守則：純狀態、零 I/O、零網路、零策略數學；任何情況都不擲出例外。
"""
from __future__ import annotations

import json

# 目前已知的來源值（供讀者對照用，**不做白名單強制**——日後新增來源不該被這支
# 純觀測層擋掉或改寫成錯的值）。
KNOWN_SOURCES: tuple[str, ...] = ("coinglass", "okx_free_fallback")

# v142 之前（無留痕）的樣本世代標籤——自成一代，永不併入已知來源。
UNKNOWN_GENERATION = "unknown"

_LAST_UNIVERSE_SOURCE: str | None = None


def set_universe_source(source: str | None) -> None:
    """記錄本輪 refresh 生效的宇宙來源。非字串／空字串一律當未知（None），永不擲出。"""
    global _LAST_UNIVERSE_SOURCE
    _LAST_UNIVERSE_SOURCE = source if isinstance(source, str) and source else None


def get_universe_source() -> str | None:
    """回最近一次生效的宇宙來源；從未設定過回 None（誠實留空，不預設猜 coinglass）。"""
    return _LAST_UNIVERSE_SOURCE


# ── 數據面世代（v178）──────────────────────────────────────────────
# 修復數據面（CVD備援/BTC閘自算/備援上桌）不會改 universe_source，但會實質改變
# 「訊號是在多少資訊下做出的」——修復前後樣本混算＝v144 同物種污染在數據面維度
# 重演。dp 版本每次數據面能力變更時遞增；缺欄＝dp1（修復前舊樣本，缺席自成一代）。
DATA_PLANE = "dp2"          # dp2 = v178 起（CVD備援+BTC閘+備援上桌）
_LEGACY_DATA_PLANE = "dp1"  # v178 前（數據面殘缺的兩桶版）


def data_plane_of_row(row: dict) -> str:
    """一筆樣本出自哪個數據面版本。缺鍵/壞資料一律回 dp1（誠實：舊版沒標記）。"""
    try:
        snap = row.get("plan_snapshot")
        if isinstance(snap, str):
            snap = json.loads(snap or "") or {}
        if not isinstance(snap, dict):
            return _LEGACY_DATA_PLANE
        dp = snap.get("data_plane")
        return dp if isinstance(dp, str) and dp else _LEGACY_DATA_PLANE
    except Exception:  # noqa: BLE001
        return _LEGACY_DATA_PLANE


# ── 統計層消費者的共用底座（純函式，無狀態、不擲出） ──────────────────
def generation_of_row(row: dict) -> str:
    """一筆已平倉紙上單出自哪一代宇宙。

    讀 plan_snapshot.universe_source；缺鍵／None／壞 JSON／非加密路徑（美股恆 None）
    一律回 UNKNOWN_GENERATION——「不知道」是誠實答案，絕不猜成 coinglass（紅線③）。
    """
    try:
        snap = row.get("plan_snapshot")
        if isinstance(snap, str):
            snap = json.loads(snap or "") or {}
        if not isinstance(snap, dict):
            return UNKNOWN_GENERATION
        src = snap.get("universe_source")
        return src if isinstance(src, str) and src else UNKNOWN_GENERATION
    except Exception:  # noqa: BLE001 — 純觀測層永不因壞資料炸掉優化器
        return UNKNOWN_GENERATION


def cohort_mix(rows) -> dict[str, int]:
    """{世代: 筆數}——給報告與稽核看「這批樣本混了幾代」。"""
    mix: dict[str, int] = {}
    for r in rows or []:
        g = generation_of_row(r)
        mix[g] = mix.get(g, 0) + 1
    return mix


def active_generation(rows=None) -> str:
    """「現在生產中的是哪一代宇宙」——晉升證據必須來自這一代。

    順位：
      1. 本進程最近一次 refresh 生效的來源（daemon 內即時真相）。
      2. 退回樣本中「最近一筆有留痕者」的來源（獨立進程／離線報告用；entry_at 排序）。
      3. 都拿不到 → UNKNOWN_GENERATION（此時全庫多半也都是無留痕樣本，等於維持現況）。
    """
    cur = get_universe_source()
    if cur:
        return cur
    best_ts, best_gen = None, UNKNOWN_GENERATION
    for r in rows or []:
        g = generation_of_row(r)
        if g == UNKNOWN_GENERATION:
            continue
        try:
            ts = int(r.get("entry_at") or 0)
        except Exception:  # noqa: BLE001
            ts = 0
        if best_ts is None or ts >= best_ts:
            best_ts, best_gen = ts, g
    return best_gen
