"""回放/回測框架。

工作流程：
    historical.generate(symbol, days)  → 產生時序歷史
    replay.run(history, config)        → 跑 L2 引擎逐點評估
    simulator.simulate(fire, future)   → 模擬 FIRE 後的交易結果
    metrics.aggregate(trades)          → hit rate / expectancy / max DD
    report.render(metrics)             → 人類可讀報告

v0：用 mock 歷史驗證 backtest 框架本身對。
真實歷史在 Task 9 之後透過 mi_query_view 拉。
"""
