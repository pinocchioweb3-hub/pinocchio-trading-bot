"""L2 觸發引擎：純函式、無 LLM、確定性。

職責：吃 MarketSnapshot → 吐 TriggerDecision（FIRE or HOLD + direction）。
**不算倉位、不下單**——那是 L4 的事。

核心對外介面：
    from l2_trigger.engine import evaluate
    from l2_trigger.configs.intraday import INTRADAY_DEFAULT
    decision = evaluate(snapshot, INTRADAY_DEFAULT)
"""
