"""OKX 模擬盤安全閘 (demo_guard) — 預設拒絕 (default-deny)。

教訓來源（2026-06-15）：本機同時存在兩條「實盤」下單路徑——
  (1) 連線的 okx-trade-mcp 回報 demo=false（真錢、可下單）；
  (2) .env 的 OKX_TRADE_* 三個槽位皆有值（實盤交易金鑰）。
若任何自動下單只信任一個 `demo=True` 旗標，就可能對真錢下單。

本模組是強制閘：**除非能「正向證明」當前在 OKX 模擬盤，否則一律拒絕。**
任何自動下單路徑都必須先過這個閘——不是「相信沒問題」，而是「證明在模擬盤」。

三層：
  1. 設定層 ensure_demo_env()
     - 實盤金鑰 OKX_TRADE_* 必須全空（有值→拒絕，杜絕誤用真錢）。
     - 必須顯式開 OKX_DEMO_TRADING_ENABLED=1（預設關）。
     - 模擬盤金鑰 OKX_DEMO_* 必須齊備（與實盤金鑰不同，於 OKX「模擬交易」產生）。
  2. 客戶端層 make_demo_exchange()
     - 用 OKX_DEMO_* 建 ccxt okx，set_sandbox_mode(True)，
       並斷言 header x-simulated-trading == '1'（ccxt 4.5 實測即此機制）。
  3. 執行期正向證明 confirm_okx_demo()
     - 對 OKX 發一次私有簽名呼叫。模擬盤金鑰只能在模擬環境通過驗證；
       能成功＝確定在模擬盤。失敗（含實盤金鑰、網路）→ 拒絕。

CLI：
    python -m l4_execution.demo_guard --selftest   # 離線自測（拒絕路徑）
    python -m l4_execution.demo_guard --check      # 帶 .env 真的連 OKX 驗證模擬盤
"""
from __future__ import annotations

import os

REAL_KEYS = ("OKX_TRADE_API_KEY", "OKX_TRADE_API_SECRET", "OKX_TRADE_API_PASSPHRASE")
DEMO_KEYS = ("OKX_DEMO_API_KEY", "OKX_DEMO_API_SECRET", "OKX_DEMO_API_PASSPHRASE")
ENABLE_FLAG = "OKX_DEMO_TRADING_ENABLED"
_TRUTHY = ("1", "true", "yes", "on")


class DemoGuardError(RuntimeError):
    """無法正向確認在模擬盤時拋出；呼叫端必須中止下單。"""


def _val(name: str, env) -> str:
    return (env.get(name) or "").strip()


def ensure_demo_env(env=None) -> tuple[str, str, str]:
    """設定層檢查。通過回傳 (key, secret, passphrase)；否則 raise DemoGuardError。"""
    env = os.environ if env is None else env

    real_set = [k for k in REAL_KEYS if _val(k, env)]
    if real_set:
        raise DemoGuardError(
            f"偵測到實盤交易金鑰已設定：{real_set}。模擬盤模式下 OKX_TRADE_* 必須全空，"
            "以杜絕對真錢下單。請先清空或輪替這些金鑰。"
        )

    if _val(ENABLE_FLAG, env).lower() not in _TRUTHY:
        raise DemoGuardError(
            f"{ENABLE_FLAG} 未開啟（預設關）。確認要啟用模擬盤自動下單時，"
            f"在 .env 設 {ENABLE_FLAG}=1。"
        )

    vals = {k: _val(k, env) for k in DEMO_KEYS}
    missing = [k for k, v in vals.items() if not v]
    if missing:
        raise DemoGuardError(
            f"缺少模擬盤金鑰：{missing}。請到 OKX『模擬交易』環境產生 API key"
            "（與實盤金鑰不同），填入 .env 的 OKX_DEMO_*。"
        )
    return vals[DEMO_KEYS[0]], vals[DEMO_KEYS[1]], vals[DEMO_KEYS[2]]


def make_demo_exchange(env=None):
    """建立『已斷言模擬盤 header』的 ccxt okx 客戶端。未過設定層 → raise。"""
    api_key, secret, passphrase = ensure_demo_env(env)
    import ccxt.async_support as ccxt

    ex = ccxt.okx({
        "apiKey": api_key,
        "secret": secret,
        "password": passphrase,
        "options": {"defaultType": "swap"},
    })
    ex.set_sandbox_mode(True)
    hdr = (ex.headers or {}).get("x-simulated-trading")
    if hdr != "1":
        raise DemoGuardError(
            f"ccxt 未設定模擬盤 header（x-simulated-trading={hdr!r}，預期 '1'）。拒絕下單。"
        )
    return ex


async def confirm_okx_demo(ex) -> bool:
    """執行期正向證明：對 OKX 發一次私有呼叫，成功＝確定在模擬盤。
    回傳 True；任何失敗 → raise DemoGuardError（呼叫端必須中止）。"""
    hdr = (ex.headers or {}).get("x-simulated-trading")
    if hdr != "1":
        raise DemoGuardError(f"header 非模擬盤（{hdr!r}）；拒絕。")
    try:
        # 模擬盤金鑰只能在模擬環境通過簽名驗證；實盤金鑰會驗證失敗。
        await ex.fetch_balance({"type": "swap"})
    except Exception as e:  # noqa: BLE001 — 任何失敗都應保守拒絕
        raise DemoGuardError(
            f"無法用模擬盤金鑰通過 OKX 驗證（{type(e).__name__}: {e}）。"
            "可能金鑰非模擬盤金鑰、或網路問題；拒絕下單。"
        ) from e
    return True


# ---------------------------------------------------------------------------
# 自測 / CLI
# ---------------------------------------------------------------------------
def _selftest() -> bool:
    base_ok = {
        "OKX_TRADE_API_KEY": "", "OKX_TRADE_API_SECRET": "", "OKX_TRADE_API_PASSPHRASE": "",
        "OKX_DEMO_API_KEY": "dk", "OKX_DEMO_API_SECRET": "ds", "OKX_DEMO_API_PASSPHRASE": "dp",
        ENABLE_FLAG: "1",
    }
    cases: list[tuple[bool, str]] = []

    def expect_raise(env, label):
        try:
            ensure_demo_env(env)
            cases.append((False, label + "（應拒絕但通過）"))
        except DemoGuardError:
            cases.append((True, label))

    def expect_pass(env, label):
        try:
            ensure_demo_env(env)
            cases.append((True, label))
        except DemoGuardError as e:
            cases.append((False, label + f"（應通過但拒絕：{e}）"))

    expect_pass(dict(base_ok), "完整 demo 設定→通過")
    e = dict(base_ok); e["OKX_TRADE_API_KEY"] = "real"; expect_raise(e, "實盤金鑰有值→拒絕")
    e = dict(base_ok); e["OKX_TRADE_API_SECRET"] = "real"; expect_raise(e, "實盤 secret 有值→拒絕")
    e = dict(base_ok); e[ENABLE_FLAG] = "0"; expect_raise(e, "開關=0→拒絕")
    e = dict(base_ok); del e[ENABLE_FLAG]; expect_raise(e, "開關缺→拒絕")
    e = dict(base_ok); e["OKX_DEMO_API_SECRET"] = ""; expect_raise(e, "demo 金鑰缺→拒絕")
    e = dict(base_ok); e["OKX_DEMO_API_PASSPHRASE"] = "   "; expect_raise(e, "demo 金鑰空白→拒絕")

    # make_demo_exchange 應設模擬盤 header（不連網）
    try:
        ex = make_demo_exchange(dict(base_ok))
        hdr = (ex.headers or {}).get("x-simulated-trading")
        cases.append((hdr == "1", f"make_demo_exchange header={hdr!r}（預期 '1'）"))
    except Exception as ex_err:  # noqa: BLE001
        cases.append((False, f"make_demo_exchange 失敗：{ex_err}"))

    # make_demo_exchange 在實盤金鑰有值時應拒絕（不連網）
    try:
        bad = dict(base_ok); bad["OKX_TRADE_API_KEY"] = "real"
        make_demo_exchange(bad)
        cases.append((False, "make_demo_exchange 實盤金鑰有值（應拒絕但通過）"))
    except DemoGuardError:
        cases.append((True, "make_demo_exchange 實盤金鑰有值→拒絕"))

    ok = True
    for passed, label in cases:
        print(f"  [{'ok ' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    print("\n自測完成：全部通過 ✅" if ok else "\n自測失敗 ❌")
    return ok


def _check() -> None:
    """帶 .env 真的連 OKX 驗證模擬盤（需 OKX_DEMO_* + 網路）。"""
    import asyncio
    from pathlib import Path

    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    async def run():
        try:
            ex = make_demo_exchange()
        except DemoGuardError as e:
            print(f"❌ 設定層未過：{e}")
            return
        try:
            await confirm_okx_demo(ex)
            print("✅ 正向確認：OKX 模擬盤金鑰驗證成功，header x-simulated-trading=1。可安全進行模擬盤下單。")
        except DemoGuardError as e:
            print(f"❌ 執行期未過：{e}")
        finally:
            await ex.close()

    asyncio.run(run())


if __name__ == "__main__":
    import sys

    if "--check" in sys.argv:
        _check()
    else:
        ok = _selftest()
        sys.exit(0 if ok else 1)
