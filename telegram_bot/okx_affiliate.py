"""OKX 聯盟（Affiliate）API 客戶端 — 入群閘門的驗證核心（v22-2）。

端點（2026-06 官方文件查證）：
    GET /api/v5/affiliate/invitee/detail?uid=X
        ✓ 是我的被邀請人 → 回 joinTime/depAmt/totalVol/kycTime/affiliateCode...
        ✗ 不是 → code=51621 "The user isn't your invitee"
        ✗ 非聯盟身分 → code=51620 "Only affiliates can perform this action"

需求：正式 Affiliate Program 成員 + Read 權限 API key（3 req/s 上限）。
.env：OKX_AFFILIATE_API_KEY / OKX_AFFILIATE_API_SECRET / OKX_AFFILIATE_PASSPHRASE
    未設定 → 自動進 MOCK 模式（OKX_AFFILIATE_MOCK_UIDS 逗號清單視為有效，
    預設 "8888888"）— 讓入群流程在拿到聯盟資格前就能端到端測試。

安全：key 只需 Read；本模組永不記錄 secret；簽名在本機計算。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

import httpx

OKX_BASE = "https://www.okx.com"
REQ_PATH = "/api/v5/affiliate/invitee/detail"


def _creds() -> tuple[str, str, str] | None:
    k = (os.environ.get("OKX_AFFILIATE_API_KEY") or "").strip()
    s = (os.environ.get("OKX_AFFILIATE_API_SECRET") or "").strip()
    p = (os.environ.get("OKX_AFFILIATE_PASSPHRASE") or "").strip()
    return (k, s, p) if (k and s and p) else None


def is_mock_mode() -> bool:
    return _creds() is None


def _sign(secret: str, ts: str, method: str, path_with_query: str) -> str:
    msg = f"{ts}{method}{path_with_query}"
    mac = hmac.new(secret.encode(), msg.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()


async def verify_invitee(uid: str) -> dict:
    """驗證 UID 是否為我的被邀請人。

    回 {ok: bool, is_invitee: bool, info: dict|None, error: str|None, mock: bool}
    ok=False 代表系統層錯誤（網路/憑證），與「不是被邀請人」不同。
    """
    uid = (uid or "").strip()
    if not uid.isdigit() or not (5 <= len(uid) <= 20):
        return {"ok": True, "is_invitee": False, "info": None,
                "error": "uid_format", "mock": is_mock_mode()}

    creds = _creds()
    if creds is None:
        # MOCK 模式：拿到聯盟 API key 前的端到端測試用
        mock_uids = {u.strip() for u in
                     (os.environ.get("OKX_AFFILIATE_MOCK_UIDS") or "8888888").split(",")}
        hit = uid in mock_uids
        return {"ok": True, "is_invitee": hit,
                "info": {"joinTime": str(int(time.time() * 1000)),
                         "depAmt": "100", "totalVol": "0",
                         "kycTime": "", "mock": True} if hit else None,
                "error": None, "mock": True}

    key, secret, passphrase = creds
    path_q = f"{REQ_PATH}?uid={uid}"
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + \
        f".{int(time.time() * 1000) % 1000:03d}Z"
    headers = {
        "OK-ACCESS-KEY": key,
        "OK-ACCESS-SIGN": _sign(secret, ts, "GET", path_q),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{OKX_BASE}{path_q}", headers=headers)
        j = r.json()
    except Exception as e:
        return {"ok": False, "is_invitee": False, "info": None,
                "error": f"network: {type(e).__name__}", "mock": False}

    code = str(j.get("code", ""))
    if code == "0":
        data = (j.get("data") or [{}])[0]
        return {"ok": True, "is_invitee": True, "info": data,
                "error": None, "mock": False}
    if code == "51621":   # 不是我的被邀請人
        return {"ok": True, "is_invitee": False, "info": None,
                "error": None, "mock": False}
    if code == "51620":   # 非聯盟身分 — 系統配置問題
        return {"ok": False, "is_invitee": False, "info": None,
                "error": "not_affiliate (51620): 帳號尚未取得聯盟資格", "mock": False}
    return {"ok": False, "is_invitee": False, "info": None,
            "error": f"okx_error {code}: {str(j.get('msg', ''))[:100]}", "mock": False}


if __name__ == "__main__":
    import asyncio
    import sys
    from pathlib import Path

    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    uid = sys.argv[1] if len(sys.argv) > 1 else "8888888"
    r = asyncio.run(verify_invitee(uid))
    print(f"mock={r['mock']} ok={r['ok']} is_invitee={r['is_invitee']} "
          f"error={r['error']}")
    if r["info"]:
        safe = {k: v for k, v in r["info"].items()
                if k in ("joinTime", "depAmt", "totalVol", "kycTime", "mock")}
        print(f"info: {safe}")
