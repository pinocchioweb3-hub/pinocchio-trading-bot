"""統一錯誤格式。

反模式（BigData Boutique #5）：絕對不吞 API 錯誤細節。
所有錯誤都回傳結構化 dict，內含足以讓 LLM 重試或換來源的資訊。
"""
from __future__ import annotations


def make_error(
    *,
    tool: str,
    symbol: str | None,
    source: str | None,
    code: str,
    message: str,
    suggestion: str | None = None,
    upstream_status: int | None = None,
    upstream_body: str | None = None,
) -> dict:
    """產生標準化錯誤回應。

    code 常用值：
        SOURCE_UNAVAILABLE   來源整個掛了
        SYMBOL_UNKNOWN       symbol 對照不到
        RATE_LIMITED         被限流
        STALE_DATA           有資料但 ts 太舊
        VALIDATION           輸入參數驗證失敗
        BACKEND_NOT_READY    後端尚未啟用（v0 stub）
    """
    out = {
        "error": True,
        "tool": tool,
        "code": code,
        "message": message,
    }
    if symbol is not None:
        out["symbol"] = symbol
    if source is not None:
        out["source"] = source
    if suggestion is not None:
        out["suggestion"] = suggestion
    if upstream_status is not None:
        out["upstream_status"] = upstream_status
    if upstream_body is not None:
        # 截短避免 LLM token 爆炸，但保留足以診斷的尾端
        out["upstream_body"] = upstream_body[:1000]
    return out
