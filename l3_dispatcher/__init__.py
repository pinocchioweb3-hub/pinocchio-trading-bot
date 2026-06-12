"""L3 分派層：scheduler → fire_queue → dispatcher → Telegram。

四個 worker 在 asyncio.gather 下並行：
    scheduler   每 N 分鐘掃 watchlist、跑 L2、有 FIRE 排入 queue
    dispatcher  poll queue、渲染訊息、推 Telegram
    heartbeat   定期推 scan summary（看到沒 FIRE 也安心）
    （未來）listener   接 Telegram 按鈕回應（v1）
"""
