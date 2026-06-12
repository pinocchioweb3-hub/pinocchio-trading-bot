"""market-intel-mcp: 本地 stdio MCP 伺服器，給 L2 觸發引擎與 L3 Claude 共用。

所有工具 readOnlyHint=True、無下單/轉帳/寫入。
唯一資料庫存取為 mi_query_view，**僅允許白名單 view + 參數綁定**。

後端切換（環境變數 MARKET_INTEL_BACKEND）：
    mock      → 用 sources.mock（v0 預設）
    coinglass → 用 sources.coinglass（Task 9 啟用）
    local     → 用 sources.local（TimescaleDB，Task 10 啟用）
    auto      → coinglass + local 並行，缺料自動 fallback 到 mock
"""
__version__ = "0.1.0"
