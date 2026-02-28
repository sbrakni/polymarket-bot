"""
Gestionnaire d'exécution des ordres.
Coordonne les exécutions simultanées (arbitrage), gère les confirmations et le rollback.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from core.polymarket_client import Order, OrderSide, OrderStatus, OrderType, PolymarketClient
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ArbitrageExecution:
    """Résultat d'une exécution d'arbitrage (2 ordres simultanés)."""
    execution_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    strategy: str = ""
    market_id: str = ""
    order_yes: Optional[Order] = None
    order_no: Optional[Order] = None
    total_cost: float = 0.0
    expected_profit: float = 0.0
    actual_profit: float = 0.0
    success: bool = False
    error: Optional[str] = None
    latency_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)


class OrderExecutor:
    """
    Exécute les ordres avec gestion des erreurs, rollback, et confirmation.
    Spécialement optimisé pour l'arbitrage binaire (exécution simultanée des 2 jambes).
    """

    def __init__(self, client: PolymarketClient):
        self.client = client
        self._executions: List[ArbitrageExecution] = []
        self._pending_orders: Dict[str, Order] = {}

    async def execute_binary_arbitrage(
        self,
        market_id: str,
        token_id_yes: str,
        token_id_no: str,
        price_yes: float,
        price_no: float,
        size: float,
        strategy: str = "BINARY_ARB",
    ) -> ArbitrageExecution:
        """
        Exécute les deux jambes d'un arbitrage binaire simultanément.
        Achète YES et NO en parallèle pour minimiser le slippage.
        Profit garanti = $1.00 - price_yes - price_no - fees
        """
        exec_id = str(uuid.uuid4())[:8]
        start_ts = time.time()

        order_yes = Order(
            market_id=market_id,
            token_id=token_id_yes,
            side=OrderSide.BUY,
            price=price_yes,
            size=size,
            order_type=OrderType.FOK,  # Fill-or-Kill pour garantir l'exécution totale
            client_order_id=f"ARB-{exec_id}-YES",
        )
        order_no = Order(
            market_id=market_id,
            token_id=token_id_no,
            side=OrderSide.BUY,
            price=price_no,
            size=size,
            order_type=OrderType.FOK,
            client_order_id=f"ARB-{exec_id}-NO",
        )

        total_cost = (price_yes + price_no) * size
        fees = total_cost * 0.0004  # ~0.04% total (2 jambes × 0.02%)
        expected_profit = size - total_cost - fees

        logger.info(
            f"[{exec_id}] Arbitrage binaire: YES@{price_yes:.3f} + NO@{price_no:.3f} "
            f"× {size:.2f} = coût {total_cost:.2f}, profit attendu ${expected_profit:.2f}"
        )

        result = ArbitrageExecution(
            execution_id=exec_id,
            strategy=strategy,
            market_id=market_id,
            order_yes=order_yes,
            order_no=order_no,
            total_cost=total_cost,
            expected_profit=expected_profit,
        )

        try:
            # Exécution simultanée des 2 ordres via asyncio.gather
            yes_result, no_result = await asyncio.gather(
                self.client.place_order(order_yes),
                self.client.place_order(order_no),
                return_exceptions=True,
            )

            # Vérifier les résultats
            yes_ok = not isinstance(yes_result, Exception) and yes_result.status in (
                OrderStatus.FILLED, OrderStatus.MATCHED
            )
            no_ok = not isinstance(no_result, Exception) and no_result.status in (
                OrderStatus.FILLED, OrderStatus.MATCHED
            )

            if yes_ok and no_ok:
                result.success = True
                result.actual_profit = expected_profit
                result.order_yes = yes_result
                result.order_no = no_result
                logger.info(
                    f"[{exec_id}] Arbitrage réussi! Profit: ${expected_profit:.2f}",
                    extra={"strategy": strategy, "market_id": market_id, "profit": expected_profit}
                )
            elif yes_ok and not no_ok:
                # Jambe YES remplie mais NO a échoué — risque de perte
                logger.error(f"[{exec_id}] Jambe NO échouée! Tentative de rollback YES...")
                if yes_result.exchange_order_id:
                    await self.client.cancel_order(yes_result.exchange_order_id)
                result.error = f"Jambe NO échouée: {no_result}"
            elif no_ok and not yes_ok:
                logger.error(f"[{exec_id}] Jambe YES échouée! Tentative de rollback NO...")
                if no_result.exchange_order_id:
                    await self.client.cancel_order(no_result.exchange_order_id)
                result.error = f"Jambe YES échouée: {yes_result}"
            else:
                result.error = f"Les deux jambes ont échoué"
                logger.error(f"[{exec_id}] Échec total de l'arbitrage")

        except Exception as e:
            result.error = str(e)
            logger.error(f"[{exec_id}] Exception durant l'arbitrage: {e}")

        result.latency_ms = (time.time() - start_ts) * 1000
        self._executions.append(result)
        return result

    async def execute_single_order(
        self,
        market_id: str,
        token_id: str,
        side: OrderSide,
        price: float,
        size: float,
        order_type: OrderType = OrderType.FOK,
        tag: str = "",
    ) -> Order:
        """Exécute un ordre unique (utilisé pour Reality Arbitrage)."""
        order = Order(
            market_id=market_id,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            order_type=order_type,
            client_order_id=f"RA-{str(uuid.uuid4())[:8]}-{tag}",
        )
        return await self.client.place_order(order)

    async def execute_multi_hedge(
        self,
        market_id: str,
        legs: List[Tuple[str, float, float]],  # (token_id, price, size)
        strategy: str = "MULTI_HEDGE",
    ) -> ArbitrageExecution:
        """
        Exécute un hedge multi-outcomes (3+ jambes).
        legs: liste de (token_id, price, size)
        """
        exec_id = str(uuid.uuid4())[:8]
        start_ts = time.time()

        orders = [
            Order(
                market_id=market_id,
                token_id=tid,
                side=OrderSide.BUY,
                price=price,
                size=size,
                order_type=OrderType.FOK,
                client_order_id=f"MH-{exec_id}-{i}",
            )
            for i, (tid, price, size) in enumerate(legs)
        ]

        total_cost = sum(price * size for _, price, size in legs)
        fees = total_cost * 0.0004
        # Pour multi-outcome: payout = 1.0 * size si on couvre tout
        avg_size = sum(size for _, _, size in legs) / len(legs)
        expected_profit = avg_size - total_cost - fees

        result = ArbitrageExecution(
            execution_id=exec_id,
            strategy=strategy,
            market_id=market_id,
            total_cost=total_cost,
            expected_profit=expected_profit,
        )

        try:
            results = await asyncio.gather(
                *[self.client.place_order(o) for o in orders],
                return_exceptions=True,
            )

            failures = [(i, r) for i, r in enumerate(results) if isinstance(r, Exception)]
            if failures:
                # Rollback les ordres réussis
                successful = [r for r in results if not isinstance(r, Exception) and r.exchange_order_id]
                for order in successful:
                    await self.client.cancel_order(order.exchange_order_id)
                result.error = f"{len(failures)} jambe(s) échouée(s)"
                logger.error(f"[{exec_id}] Multi-hedge échoué: {failures}")
            else:
                result.success = True
                result.actual_profit = expected_profit
                logger.info(f"[{exec_id}] Multi-hedge réussi! Profit: ${expected_profit:.2f}")

        except Exception as e:
            result.error = str(e)

        result.latency_ms = (time.time() - start_ts) * 1000
        self._executions.append(result)
        return result

    def get_recent_executions(self, limit: int = 50) -> List[ArbitrageExecution]:
        return self._executions[-limit:]

    def get_success_rate(self) -> float:
        if not self._executions:
            return 0.0
        return sum(1 for e in self._executions if e.success) / len(self._executions)

    def get_total_profit(self) -> float:
        return sum(e.actual_profit for e in self._executions if e.success)
