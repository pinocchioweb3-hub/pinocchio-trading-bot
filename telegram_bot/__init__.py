"""Telegram bot 用戶端 + 訊息渲染。

設計：
- v0 用 httpx 走 raw HTTP，不依賴 python-telegram-bot（輕量、快、不需 polling loop）
- HTML 格式（比 MarkdownV2 易處理）
- 訊息格式對應規格範本：[INTRADAY] / [AMBUSH] + 進場/止損/TP1-3 + 證偽條件
"""
