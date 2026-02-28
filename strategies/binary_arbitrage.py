"""
MODULE 2 — Arbitrage Binaire (Intra-marché)

Stratégie principale de @swisstony : acheter simultanément YES et NO
quand price_YES + price_NO < $1.00 (après frais).
Profit garanti à la résolution du marché.

Exemple: YES@$0.47 + NO@$0.48 = $0.95 total → profit de $0.05 par contrat
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from config import settings
from core.order_executor import ArbitrageExecution, OrderExecutor
from core.polymarket_client import MarketInfo, Orderbook, PolymarketClient
from data.orderbook_cache import OrderbookCache
from risk.risk_limits import RiskLimits
from strategies.base_strategy import BaseStrategy, Opportunity
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class BinaryOpportunity:
    """Opportunité d'arbitrage binaire spécifique."""
    market_id: str
    question: str
    token_id_yes: str
    token_id_no: str
    ask_yes: float
    ask_no: float
    total_cost: float
    gross_profit: float  # par contrat
    net_profit: float    # après frais
    available_size: float
    max_profit: float    # profit total disponible
    liquidity_score: float
    detected_at: float = field(default_factory=time.time)


class BinaryArbitrageStrategy(BaseStrategy):
    """
    Stratégie d'arbitrage binaire YES+NO.

    Logique:
    1. Scanner tous les marchés actifs à 2 outcomes
    2. Pour chaque marché: best_ask_YES + best_ask_NO
    3. Si total < 1.00 - frais - seuil_min_profit → exécuter
    4. Taille = min(liquidité_YES, liquidité_NO, max_per_trade)
    """

    name = "BINARY_ARB"

    def __init__(
        self,
        client: PolymarketClient,
        executor: OrderExecutor,
        risk_config,
        orderbook_cache: Optional["OrderbookCache"] = None,
        risk_limits: Optional["RiskLimits"] = None,
    ):
        super().__init__(client, executor, risk_config)
        self.cache = orderbook_cache
        self.risk_limits = risk_limits
        self._markets: List[Dict] = []
        self._last_market_refresh = 0.0
        self._market_refresh_interval = 300  # Rafraîchir la liste des marchés toutes les 5min
        self._scanned_count = 0
        self._opportunities_per_scan: List[int] = []

    async def refresh_markets(self) -> None:
        """Rafraîchit la liste des marchés actifs."""
        now = time.time()
        if now - self._last_market_refresh < self._market_refresh_interval:
            return
        try:
            raw_markets = await self.client.get_all_active_markets()
            # Filtrer: binaires uniquement (2 tokens), liquidité suffisante
            self._markets = [
                m for m in raw_markets
                if len(m.get("tokens", [])) == 2
                and float(m.get("liquidity", 0)) >= settings.MIN_LIQUIDITY
                and not m.get("closed", False)
            ]
            self._last_market_refresh = now
            logger.info(f"[BINARY_ARB] {len(self._markets)} marchés éligibles chargés")
        except Exception as e:
            logger.error(f"[BINARY_ARB] Erreur rafraîchissement marchés: {e}")

    async def scan_market(self, market: Dict) -> Optional[BinaryOpportunity]:
        """Vérifie si un marché présente une opportunité d'arbitrage binaire."""
        try:
            tokens = market.get("tokens", [])
            if len(tokens) != 2:
                return None

            # Identifier les tokens YES et NO
            token_yes = next((t for t in tokens if t.get("outcome") == "Yes"), tokens[0])
            token_no = next((t for t in tokens if t.get("outcome") == "No"), tokens[1])

            tid_yes = token_yes.get("token_id", "")
            tid_no = token_no.get("token_id", "")

            if not tid_yes or not tid_no:
                return None

            # Récupérer les orderbooks (depuis le cache ou l'API)
            if self.cache:
                book_yes = self.cache.get(tid_yes)
                book_no = self.cache.get(tid_no)
                if not book_yes or not book_no:
                    # Rafraîchir depuis l'API
                    books = await self.client.get_orderbooks_batch([tid_yes, tid_no])
                    book_yes = books.get(tid_yes)
                    book_no = books.get(tid_no)
                    if book_yes:
                        self.cache.update(tid_yes, book_yes)
                    if book_no:
                        self.cache.update(tid_no, book_no)
            else:
                books = await self.client.get_orderbooks_batch([tid_yes, tid_no])
                book_yes = books.get(tid_yes)
                book_no = books.get(tid_no)

            if not book_yes or not book_no:
                return None

            ask_yes = book_yes.best_ask
            ask_no = book_no.best_ask

            if ask_yes is None or ask_no is None:
                return None
            if ask_yes <= 0 or ask_no <= 0:
                return None

            total_cost = ask_yes + ask_no

            # Frais estimés: ~0.04% (0.02% × 2 jambes)
            fees_per_unit = total_cost * 0.0004

            gross_profit = 1.0 - total_cost
            net_profit = gross_profit - fees_per_unit

            # Seuil minimum de profit
            if net_profit < settings.MIN_BINARY_EDGE:
                return None

            # Calculer la taille disponible (min des deux côtés)
            available_size = min(
                book_yes.best_ask_size,
                book_no.best_ask_size,
                self.risk.max_per_trade / max(total_cost, 0.01),
            )

            if available_size < 1.0:  # Minimum 1 contrat
                return None

            max_profit = net_profit * available_size

            # Vérifier le seuil de profit total
            if max_profit < self.risk.min_profit_threshold:
                return None

            liquidity = float(market.get("liquidity", 0))
            liquidity_score = min(1.0, liquidity / 100_000)

            return BinaryOpportunity(
                market_id=market.get("conditionId", market.get("condition_id", "")),
                question=market.get("question", "")[:100],
                token_id_yes=tid_yes,
                token_id_no=tid_no,
                ask_yes=ask_yes,
                ask_no=ask_no,
                total_cost=total_cost,
                gross_profit=gross_profit,
                net_profit=net_profit,
                available_size=available_size,
                max_profit=max_profit,
                liquidity_score=liquidity_score,
            )

        except Exception as e:
            logger.debug(f"[BINARY_ARB] Erreur scan marché {market.get('conditionId', '?')}: {e}")
            return None

    async def detect_opportunities(self) -> List[Opportunity]:
        """Scanne tous les marchés et retourne les opportunités triées."""
        await self.refresh_markets()

        if not self._markets:
            return []

        # Scanner par batch pour éviter de surcharger l'API
        batch_size = 50
        all_opps = []

        for i in range(0, len(self._markets), batch_size):
            batch = self._markets[i:i + batch_size]
            results = await asyncio.gather(
                *[self.scan_market(m) for m in batch],
                return_exceptions=True,
            )

            for result in results:
                if isinstance(result, BinaryOpportunity):
                    all_opps.append(result)

            # Petite pause entre les batches
            if i + batch_size < len(self._markets):
                await asyncio.sleep(0.1)

        self._scanned_count += len(self._markets)
        self._opportunities_per_scan.append(len(all_opps))

        if all_opps:
            logger.info(
                f"[BINARY_ARB] {len(all_opps)} opportunités sur {len(self._markets)} marchés scannés"
            )

        # Convertir en objets Opportunity génériques
        return [
            Opportunity(
                strategy_name=self.name,
                market_id=opp.market_id,
                description=f"YES@{opp.ask_yes:.3f} + NO@{opp.ask_no:.3f} = {opp.total_cost:.3f} → profit ${opp.net_profit:.4f}/contrat",
                expected_profit=opp.max_profit,
                expected_profit_pct=(opp.net_profit / opp.total_cost) * 100,
                confidence=0.99,  # Arbitrage pur = quasi-certitude
                urgency=min(1.0, opp.net_profit * 10),  # Plus rentable = plus urgent
                metadata={"binary_opp": opp},
            )
            for opp in sorted(all_opps, key=lambda x: x.max_profit, reverse=True)
        ]

    async def execute_opportunity(self, opportunity: Opportunity) -> Optional[ArbitrageExecution]:
        """Exécute un arbitrage binaire."""
        opp: BinaryOpportunity = opportunity.metadata["binary_opp"]

        # Vérifier les limites de risque
        if self.risk_limits:
            if not await self.risk_limits.can_trade(
                market_id=opp.market_id,
                trade_size=opp.available_size * opp.total_cost,
            ):
                logger.debug(f"[BINARY_ARB] Trade refusé par risk limits: {opp.market_id[:16]}")
                return None

        # Re-vérifier les prix en temps réel avant d'exécuter
        # (les prix peuvent avoir changé depuis la détection)
        try:
            books = await self.client.get_orderbooks_batch([opp.token_id_yes, opp.token_id_no])
            book_yes = books.get(opp.token_id_yes)
            book_no = books.get(opp.token_id_no)

            if not book_yes or not book_no:
                return None

            current_ask_yes = book_yes.best_ask
            current_ask_no = book_no.best_ask

            if current_ask_yes is None or current_ask_no is None:
                return None

            current_total = current_ask_yes + current_ask_no
            current_net = 1.0 - current_total - (current_total * 0.0004)

            if current_net < settings.MIN_BINARY_EDGE:
                logger.debug(
                    f"[BINARY_ARB] Opportunité disparue pour {opp.market_id[:16]}: "
                    f"total={current_total:.4f}"
                )
                return None

            # Calculer la taille finale
            size = min(
                book_yes.best_ask_size,
                book_no.best_ask_size,
                self.risk.max_per_trade / max(current_total, 0.01),
            )
            size = max(1.0, int(size))  # Au moins 1 contrat, arrondir

        except Exception as e:
            logger.error(f"[BINARY_ARB] Erreur re-vérification: {e}")
            return None

        logger.info(
            f"[BINARY_ARB] Exécution: {opp.question[:60]} | "
            f"YES@{current_ask_yes:.3f} + NO@{current_ask_no:.3f} × {size:.0f} | "
            f"Profit: ${current_net * size:.2f}"
        )

        result = await self.executor.execute_binary_arbitrage(
            market_id=opp.market_id,
            token_id_yes=opp.token_id_yes,
            token_id_no=opp.token_id_no,
            price_yes=current_ask_yes,
            price_no=current_ask_no,
            size=size,
            strategy=self.name,
        )

        if result.success and self.risk_limits:
            await self.risk_limits.record_trade(
                market_id=opp.market_id,
                trade_size=size * current_total,
                profit=result.actual_profit,
            )

        return result

    def get_detailed_stats(self) -> Dict:
        stats = self.get_stats()
        stats.update({
            "markets_tracked": len(self._markets),
            "total_scanned": self._scanned_count,
            "avg_opportunities_per_scan": (
                sum(self._opportunities_per_scan[-100:]) / len(self._opportunities_per_scan[-100:])
                if self._opportunities_per_scan else 0
            ),
        })
        return stats
