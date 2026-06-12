"""L4 執行層：OKX 合約自動下單。

職責：吃 TriggerDecision → 在 OKX 開倉/平倉/設止損止盈。
**不做決策**——方向與倉位由 L2 + leverage.py 決定。
"""
