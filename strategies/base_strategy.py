"""
Classe de base abstraite pour toutes les stratégies d'arbitrage.
Définit l'interface commune et les méthodes utilitaires partagées.
"""

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config.settings import RiskConfig
from core.order_executor import ArbitrageExecution, OrderExecutor
from core.polymarket_client import PolymarketClient
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class Opportunity:
    """Opportunité d'arbitrage détectée par une stratégie."""
    strategy_name: str
    market_id: str
    description: str
    expected_profit: float
    expected_profit_pct: float
    confidence: float  # 0.0 à 1.0
    urgency: float     # 0.0 (pas urgent) à 1.0 (exécuter immédiatement)
    metadata: Dict[str, Any] = field(default_factory=dict)
    detected_at: float = field(default_factory=time.time)


class BaseStrategy(ABC):
    """
    Classe abstraite de base pour les stratégies d'arbitrage.
    Chaque stratégie implémente detect_opportunities() et execute().
    """

    name: str = "BASE"

    def __init__(
        self,
        client: PolymarketClient,
        executor: OrderExecutor,
        risk_config: RiskConfig,
    ):
        self.client = client
        self.executor = executor
        self.risk = risk_config
        self._running = False
        self._opportunities_found = 0
        self._trades_executed = 0
        self._total_profit = 0.0
        self._errors = 0

    @abstractmethod
    async def detect_opportunities(self) -> List[Opportunity]:
        """Détecte les opportunités d'arbitrage disponibles."""
        ...

    @abstractmethod
    async def execute_opportunity(self, opportunity: Opportunity) -> Optional[ArbitrageExecution]:
        """Exécute une opportunité d'arbitrage détectée."""
        ...

    async def run(self) -> None:
        """Boucle principale de la stratégie."""
        self._running = True
        logger.info(f"[{self.name}] Stratégie démarrée")

        while self._running:
            try:
                opportunities = await self.detect_opportunities()
                if opportunities:
                    self._opportunities_found += len(opportunities)
                    # Trier par profit attendu décroissant
                    opportunities.sort(key=lambda o: o.expected_profit, reverse=True)

                    for opp in opportunities:
                        if not self._running:
                            break
                        result = await self.execute_opportunity(opp)
                        if result and result.success:
                            self._trades_executed += 1
                            self._total_profit += result.actual_profit

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._errors += 1
                logger.error(f"[{self.name}] Erreur dans la boucle principale: {e}")

            await asyncio.sleep(0.1)  # Pause de 100ms entre les scans

    async def stop(self) -> None:
        self._running = False
        logger.info(f"[{self.name}] Stratégie arrêtée")

    def get_stats(self) -> Dict[str, Any]:
        return {
            "strategy": self.name,
            "opportunities_found": self._opportunities_found,
            "trades_executed": self._trades_executed,
            "total_profit": round(self._total_profit, 4),
            "errors": self._errors,
            "success_rate": (
                self._trades_executed / self._opportunities_found
                if self._opportunities_found > 0 else 0.0
            ),
        }

    # -----------------------------------------------------------------------
    # Utilitaires partagés
    # -----------------------------------------------------------------------

    def kelly_size(
        self, prob_win: float, odds_win: float, capital: float
    ) -> float:
        """
        Calcule la taille optimale selon le Critère de Kelly.
        prob_win: probabilité estimée de victoire (0 à 1)
        odds_win: multiplicateur en cas de gain (ex: 2.0 pour doubler)
        capital: capital disponible
        Retourne: taille de la mise en dollars
        """
        if prob_win <= 0 or prob_win >= 1 or odds_win <= 1:
            return 0.0
        # Formule Kelly: f = (p * b - q) / b
        # où p = prob, b = odds-1, q = 1-p
        b = odds_win - 1
        q = 1 - prob_win
        kelly_f = (prob_win * b - q) / b
        kelly_f = max(0.0, kelly_f)

        # Fraction de Kelly (quarter Kelly par défaut pour limiter la variance)
        fractional_kelly = kelly_f * self.risk.kelly_fraction
        size = capital * fractional_kelly

        # Appliquer les limites de risque
        return min(size, self.risk.max_per_trade)

    def calculate_fees(self, total_cost: float, num_legs: int = 2) -> float:
        """Estime les frais pour un trade."""
        # ~0.02% taker fee par jambe
        return total_cost * 0.0002 * num_legs

    def is_profitable_after_fees(
        self, gross_profit: float, total_cost: float, num_legs: int = 2
    ) -> bool:
        """Vérifie si une opportunité est rentable après frais."""
        fees = self.calculate_fees(total_cost, num_legs)
        net_profit = gross_profit - fees
        return net_profit >= self.risk.min_profit_threshold
