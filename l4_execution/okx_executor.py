"""OKX 合約執行器：透過 ccxt 下單。

安全原則：
- 使用獨立的 OKX_TRADE_API_KEY（與唯讀 key 分離）
- 禁勾「提現」權限
- 所有下單帶止損
- 先查餘額再下單，避免超額
- 支援模擬盤（demo=True）測試

用法：
    executor = OKXExecutor(demo=True)  # 模擬盤
    result = await executor.open_position("BTC", "long", leverage=10,
                                           usdt_amount=100, sl_pct=2.0,
                                           tp_prices=[105000, 107000, 110000])
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional

import ccxt.async_support as ccxt

logger = logging.getLogger(__name__)


@dataclass
class TradeResult:
    """下單結果。"""
    success: bool
    order_id: Optional[str] = None
    symbol: str = ""
    side: str = ""
    amount: float = 0.0
    price: float = 0.0
    sl_price: Optional[float] = None
    tp_prices: list[float] | None = None
    error: Optional[str] = None
    raw: dict | None = None


class OKXExecutor:
    """OKX USDT 永續合約執行器。"""

    def __init__(self, demo: bool = True):
        """
        Args:
            demo: True = 模擬盤（唯一允許）；False = 實盤（已停用，直接 raise）

        安全鐵律（2026-06-15 對抗式審查後強化）：
        - **實盤模式永久停用**：真錢執行需使用者另行明確拍板，且自動下單一律走
          l4_execution.demo_trader（過 demo_guard 正向證明）。本類別不再提供任何實盤路徑。
        - 模擬盤一律經 demo_guard.make_demo_exchange 建立（set_sandbox_mode + 斷言
          x-simulated-trading 標頭），並移除舊的「唯讀金鑰退路」（避免誤用實盤金鑰）。
        - 下單前再以 confirm_okx_demo 執行期正向證明（見 _ensure_init）。
        """
        if not demo:
            raise RuntimeError(
                "OKXExecutor 實盤模式已停用。真錢執行需使用者明確拍板；"
                "自動下單一律走 l4_execution.demo_trader（過 demo_guard 模擬盤正向證明）。"
            )
        # 模擬盤：強制走 demo_guard 設定層閘（OKX_TRADE_* 須空 + OKX_DEMO_* 齊備
        # + OKX_DEMO_TRADING_ENABLED=1），不再有唯讀金鑰退路。
        from l4_execution.demo_guard import make_demo_exchange
        self.demo = True
        self._exchange = make_demo_exchange()   # 內含 set_sandbox_mode(True)+標頭斷言
        self._initialized = False

    async def _ensure_init(self) -> None:
        if not self._initialized:
            # 執行期正向證明：發一個只有模擬盤金鑰能過的簽名呼叫，證不出即 raise
            from l4_execution.demo_guard import confirm_okx_demo
            await confirm_okx_demo(self._exchange)
            await self._exchange.load_markets()
            self._initialized = True

    async def close(self) -> None:
        await self._exchange.close()

    # -----------------------------------------------------------------
    # 公開方法
    # -----------------------------------------------------------------
    async def get_balance(self) -> dict:
        """查 USDT 可用餘額。"""
        await self._ensure_init()
        balance = await self._exchange.fetch_balance({"type": "swap"})
        usdt = balance.get("USDT", {})
        return {
            "total": usdt.get("total", 0),
            "free": usdt.get("free", 0),
            "used": usdt.get("used", 0),
        }

    async def get_position(self, symbol: str = "BTC") -> dict | None:
        """查當前持倉。回傳 None = 無倉位。"""
        await self._ensure_init()
        inst_id = f"{symbol}/USDT:USDT"
        positions = await self._exchange.fetch_positions([inst_id])
        for pos in positions:
            contracts = float(pos.get("contracts", 0))
            if contracts > 0:
                return {
                    "symbol": symbol,
                    "side": pos.get("side"),          # "long" / "short"
                    "contracts": contracts,
                    "notional": float(pos.get("notional", 0)),
                    "entry_price": float(pos.get("entryPrice", 0)),
                    "unrealized_pnl": float(pos.get("unrealizedPnl", 0)),
                    "leverage": int(pos.get("leverage", 1)),
                    "liquidation_price": float(pos.get("liquidationPrice", 0)),
                }
        return None

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        """設定槓桿（逐倉模式）。"""
        await self._ensure_init()
        inst_id = f"{symbol}/USDT:USDT"
        try:
            await self._exchange.set_leverage(leverage, inst_id, params={"mgnMode": "isolated"})
            logger.info("set leverage %s %dx", symbol, leverage)
        except ccxt.ExchangeError as e:
            # 部分情況已經是目標槓桿，不需要處理
            if "leverage" in str(e).lower():
                logger.debug("leverage already set: %s", e)
            else:
                raise

    async def open_position(
        self,
        symbol: str,
        side: str,              # "long" / "short"
        leverage: int,
        usdt_margin: float,     # 保證金 USDT
        sl_pct: float,          # 止損距離 %
        tp_prices: list[float] | None = None,
    ) -> TradeResult:
        """開倉 + 設止損。

        Args:
            symbol: "BTC"
            side: "long" or "short"
            leverage: 槓桿倍數
            usdt_margin: 保證金（USDT）
            sl_pct: 止損距離百分比（如 2.0 = 2%）
            tp_prices: 止盈價位列表（可選）
        """
        await self._ensure_init()
        inst_id = f"{symbol}/USDT:USDT"

        # 1. 檢查餘額
        balance = await self.get_balance()
        if balance["free"] < usdt_margin:
            return TradeResult(
                success=False, symbol=symbol, side=side,
                error=f"餘額不足: free={balance['free']:.2f} USDT, "
                      f"需要 {usdt_margin:.2f} USDT",
            )

        # 2. 檢查是否已有持倉
        existing = await self.get_position(symbol)
        if existing and existing["contracts"] > 0:
            return TradeResult(
                success=False, symbol=symbol, side=side,
                error=f"已有 {symbol} {existing['side']} 倉位 "
                      f"({existing['contracts']} 合約), 請先平倉",
            )

        # 3. 設槓桿
        await self.set_leverage(symbol, leverage)

        # 4. 計算合約數量
        ticker = await self._exchange.fetch_ticker(inst_id)
        current_price = ticker["last"]
        notional = usdt_margin * leverage
        # OKX BTC-USDT-SWAP: 1 contract = 0.01 BTC
        market = self._exchange.market(inst_id)
        contract_size = float(market.get("contractSize", 0.01))
        amount = notional / current_price / contract_size
        amount = max(1, round(amount))  # 最少 1 張合約

        # 5. 計算止損價
        if side == "long":
            sl_price = round(current_price * (1 - sl_pct / 100), 2)
        else:
            sl_price = round(current_price * (1 + sl_pct / 100), 2)

        # 6. 下市價單
        order_side = "buy" if side == "long" else "sell"
        try:
            order = await self._exchange.create_order(
                symbol=inst_id,
                type="market",
                side=order_side,
                amount=amount,
                params={
                    "tdMode": "isolated",
                    "posSide": side,
                    "slTriggerPx": str(sl_price),
                    "slOrdPx": "-1",  # 市價止損
                },
            )
            logger.info(
                "OPENED %s %s %s x%d | amount=%s | sl=%.2f | order_id=%s",
                symbol, side, order_side, leverage, amount, sl_price,
                order.get("id"),
            )
        except Exception as e:
            logger.error("下單失敗: %s", e)
            return TradeResult(
                success=False, symbol=symbol, side=side,
                error=str(e),
            )

        fill_price = float(order.get("average", 0) or order.get("price", 0) or current_price)

        # 7. 設止盈單（如果有 tp_prices）
        if tp_prices:
            for i, tp in enumerate(tp_prices):
                try:
                    tp_side = "sell" if side == "long" else "buy"
                    tp_amount = max(1, amount // len(tp_prices))
                    await self._exchange.create_order(
                        symbol=inst_id,
                        type="limit",
                        side=tp_side,
                        amount=tp_amount,
                        price=tp,
                        params={
                            "tdMode": "isolated",
                            "posSide": side,
                            "reduceOnly": True,
                        },
                    )
                    logger.info("TP%d set: %s @ %.2f (amount=%s)", i + 1, tp_side, tp, tp_amount)
                except Exception as e:
                    logger.warning("設 TP%d 失敗: %s", i + 1, e)

        return TradeResult(
            success=True,
            order_id=order.get("id"),
            symbol=symbol,
            side=side,
            amount=amount * contract_size,
            price=fill_price,
            sl_price=sl_price,
            tp_prices=tp_prices,
            raw=order,
        )

    async def close_position(self, symbol: str = "BTC") -> TradeResult:
        """市價平倉。"""
        await self._ensure_init()
        inst_id = f"{symbol}/USDT:USDT"

        pos = await self.get_position(symbol)
        if not pos or pos["contracts"] == 0:
            return TradeResult(
                success=False, symbol=symbol, side="",
                error="無持倉可平",
            )

        close_side = "sell" if pos["side"] == "long" else "buy"
        try:
            order = await self._exchange.create_order(
                symbol=inst_id,
                type="market",
                side=close_side,
                amount=pos["contracts"],
                params={
                    "tdMode": "isolated",
                    "posSide": pos["side"],
                    "reduceOnly": True,
                },
            )
            logger.info(
                "CLOSED %s %s | contracts=%s | order_id=%s",
                symbol, pos["side"], pos["contracts"], order.get("id"),
            )
            return TradeResult(
                success=True,
                order_id=order.get("id"),
                symbol=symbol,
                side=f"close_{pos['side']}",
                amount=pos["contracts"],
                price=float(order.get("average", 0) or 0),
                raw=order,
            )
        except Exception as e:
            logger.error("平倉失敗: %s", e)
            return TradeResult(
                success=False, symbol=symbol, side=f"close_{pos['side']}",
                error=str(e),
            )

    async def cancel_all_orders(self, symbol: str = "BTC") -> int:
        """取消所有掛單。回傳取消數量。"""
        await self._ensure_init()
        inst_id = f"{symbol}/USDT:USDT"
        try:
            orders = await self._exchange.fetch_open_orders(inst_id)
            for o in orders:
                await self._exchange.cancel_order(o["id"], inst_id)
            logger.info("cancelled %d orders for %s", len(orders), symbol)
            return len(orders)
        except Exception as e:
            logger.error("取消掛單失敗: %s", e)
            return 0
