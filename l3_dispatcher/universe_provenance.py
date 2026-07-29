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

守則：純狀態、零 I/O、零網路、零策略數學；任何情況都不擲出例外。
"""
from __future__ import annotations

# 目前已知的來源值（供讀者對照用，**不做白名單強制**——日後新增來源不該被這支
# 純觀測層擋掉或改寫成錯的值）。
KNOWN_SOURCES: tuple[str, ...] = ("coinglass", "okx_free_fallback")

_LAST_UNIVERSE_SOURCE: str | None = None


def set_universe_source(source: str | None) -> None:
    """記錄本輪 refresh 生效的宇宙來源。非字串／空字串一律當未知（None），永不擲出。"""
    global _LAST_UNIVERSE_SOURCE
    _LAST_UNIVERSE_SOURCE = source if isinstance(source, str) and source else None


def get_universe_source() -> str | None:
    """回最近一次生效的宇宙來源；從未設定過回 None（誠實留空，不預設猜 coinglass）。"""
    return _LAST_UNIVERSE_SOURCE
