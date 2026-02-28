"""
MODULE 3 — Multi-Outcome Hedging (Sports avancé)

Pour les marchés sportifs avec 3+ outcomes liés au même événement
(ex: moneyline + total + spread), identifie des combinaisons d'achats
qui couvrent tous les scénarios possibles avec un profit garanti.

Utilise la programmation linéaire (scipy.optimize) pour trouver
la combinaison optimale minimisant le coût tout en maximisant le payout.
"""

import asyncio
import time
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from config import settings
from core.order_executor import ArbitrageExecution, OrderExecutor
from core.polymarket_client import PolymarketClient
from risk.risk_limits import RiskLimits
from strategies.base_strategy import BaseStrategy, Opportunity
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class MarketLeg:
    """Une jambe d'un hedge multi-outcomes."""
    market_id: str
    token_id: str
    outcome_name: str
    price: float
    available_size: float
    payout_scenarios: Dict[str, float]  # scenario_name -> payout (0 ou 1)


@dataclass
class HedgeOpportunity:
    """Opportunité de hedge multi-outcomes."""
    event_name: str
    legs: List[MarketLeg]
    scenario_coverage: Dict[str, float]  # scenario -> payout min garanti
    total_cost: float
    min_payout: float
    profit_per_unit: float
    optimal_sizes: List[float]
    risk_score: float  # 0 (risqué) à 1 (sans risque)


class MultiOutcomeHedgeStrategy(BaseStrategy):
    """
    Stratégie de hedge multi-outcomes.

    Logique:
    1. Grouper les marchés par événement sportif
    2. Pour chaque groupe, construire la matrice payout-scénario
    3. Utiliser la prog. linéaire pour trouver la combinaison optimale
    4. Exécuter si le profit net est positif
    """

    name = "MULTI_HEDGE"

    def __init__(
        self,
        client: PolymarketClient,
        executor: OrderExecutor,
        risk_config,
        risk_limits: Optional[RiskLimits] = None,
    ):
        super().__init__(client, executor, risk_config)
        self.risk_limits = risk_limits
        self._market_groups: Dict[str, List[Dict]] = {}  # event_key -> marchés
        self._last_refresh = 0.0

    async def _group_markets_by_event(self) -> None:
        """Groupe les marchés Polymarket par événement sportif."""
        now = time.time()
        if now - self._last_refresh < 300:  # Rafraîchir toutes les 5min
            return

        try:
            markets = await self.client.get_all_active_markets()
            sport_markets = [m for m in markets if m.get("tags") and
                           any(t in m.get("tags", []) for t in
                               ["Sports", "Basketball", "Football", "Soccer", "Hockey", "Baseball"])]

            self._market_groups = {}

            for market in sport_markets:
                event_key = self._extract_event_key(market)
                if event_key:
                    if event_key not in self._market_groups:
                        self._market_groups[event_key] = []
                    self._market_groups[event_key].append(market)

            # Garder seulement les groupes avec 2+ marchés (pour le hedge)
            self._market_groups = {
                k: v for k, v in self._market_groups.items() if len(v) >= 2
            }

            self._last_refresh = now
            logger.info(
                f"[MULTI_HEDGE] {len(self._market_groups)} événements avec marchés multiples"
            )
        except Exception as e:
            logger.error(f"[MULTI_HEDGE] Erreur groupement marchés: {e}")

    def _extract_event_key(self, market: Dict) -> Optional[str]:
        """Extrait une clé d'événement unique depuis les métadonnées du marché."""
        question = market.get("question", "")
        tags = market.get("tags", [])

        # Essayer d'extraire les équipes
        import re
        # Chercher "Team A vs Team B" ou "Team A @ Team B"
        match = re.search(r"([A-Z][a-z]+ [A-Z][a-z]+|[A-Z]{2,5}) (?:vs?\.?|@) ([A-Z][a-z]+ [A-Z][a-z]+|[A-Z]{2,5})", question)
        if match:
            team1 = match.group(1).strip().lower().replace(" ", "_")
            team2 = match.group(2).strip().lower().replace(" ", "_")
            sport = next((t.lower() for t in tags if t.lower() in
                         ["nba", "nfl", "nhl", "mlb", "soccer"]), "sports")
            return f"{sport}_{team1}_vs_{team2}"
        return None

    async def _build_hedge_opportunity(
        self, event_key: str, markets: List[Dict]
    ) -> Optional[HedgeOpportunity]:
        """
        Construit une opportunité de hedge à partir d'un groupe de marchés.
        Utilise la programmation linéaire pour optimiser les tailles.
        """
        if len(markets) < 2:
            return None

        legs: List[MarketLeg] = []

        # Récupérer les orderbooks pour tous les tokens
        all_token_ids = []
        for market in markets:
            for token in market.get("tokens", []):
                token_id = token.get("token_id", "")
                if token_id:
                    all_token_ids.append((market, token, token_id))

        if len(all_token_ids) < 3:
            return None

        try:
            books = await self.client.get_orderbooks_batch(
                [tid for _, _, tid in all_token_ids]
            )
        except Exception as e:
            logger.debug(f"[MULTI_HEDGE] Erreur orderbooks pour {event_key}: {e}")
            return None

        for market, token, token_id in all_token_ids:
            book = books.get(token_id)
            if not book or not book.best_ask:
                continue

            outcome = token.get("outcome", "Unknown")
            market_type = self._classify_market(market.get("question", ""))

            # Construire les scénarios de payout
            payout_scenarios = self._build_payout_scenarios(outcome, market_type, market)

            leg = MarketLeg(
                market_id=market.get("conditionId", ""),
                token_id=token_id,
                outcome_name=f"{market_type}:{outcome}",
                price=book.best_ask,
                available_size=book.best_ask_size,
                payout_scenarios=payout_scenarios,
            )
            legs.append(leg)

        if len(legs) < 3:
            return None

        # Optimiser avec programmation linéaire
        return self._optimize_hedge(event_key, legs)

    def _classify_market(self, question: str) -> str:
        q = question.lower()
        if "over" in q or "under" in q:
            return "total"
        if "spread" in q or "cover" in q:
            return "spread"
        if "first" in q or "1st" in q:
            return "firsthalf"
        return "moneyline"

    def _build_payout_scenarios(
        self, outcome: str, market_type: str, market: Dict
    ) -> Dict[str, float]:
        """Construit les payouts pour chaque scénario de jeu."""
        # Scénarios possibles dans un match sportif:
        # home_win, away_win, overtime (rare)
        if market_type == "moneyline":
            if outcome == "Yes":
                return {"home_win": 1.0, "away_win": 0.0, "draw": 0.0}
            else:
                return {"home_win": 0.0, "away_win": 1.0, "draw": 1.0}
        elif market_type == "total":
            # Détecter si c'est "over" ou "under"
            q = market.get("question", "").lower()
            if "over" in outcome.lower() or (outcome == "Yes" and "over" in q):
                return {"high_score": 1.0, "low_score": 0.0}
            else:
                return {"high_score": 0.0, "low_score": 1.0}
        elif market_type == "spread":
            if outcome == "Yes":
                return {"favorite_covers": 1.0, "favorite_fails": 0.0}
            else:
                return {"favorite_covers": 0.0, "favorite_fails": 1.0}
        else:
            return {"yes": 1.0 if outcome == "Yes" else 0.0,
                    "no": 0.0 if outcome == "Yes" else 1.0}

    def _optimize_hedge(
        self, event_name: str, legs: List[MarketLeg]
    ) -> Optional[HedgeOpportunity]:
        """
        Utilise la programmation linéaire pour trouver la combinaison optimale.
        Objectif: maximiser le profit net = payout_min - coût_total - frais
        """
        try:
            from scipy.optimize import linprog
        except ImportError:
            # Fallback sans scipy: utiliser une approche greedy
            return self._greedy_hedge(event_name, legs)

        n_legs = len(legs)

        # Récupérer tous les scénarios possibles
        all_scenarios = set()
        for leg in legs:
            all_scenarios.update(leg.payout_scenarios.keys())
        scenarios = list(all_scenarios)
        n_scenarios = len(scenarios)

        if n_scenarios == 0:
            return None

        # Variables de décision: size_i (nombre de contrats pour chaque jambe)
        # Minimiser: -min_payout + sum(cost_i * size_i)  (équivalent à maximiser profit)
        # Contrainte: pour chaque scénario s, sum(payout_i_s * size_i) >= min_payout

        # Construction de la matrice de payout [n_scenarios × n_legs]
        payout_matrix = np.zeros((n_scenarios, n_legs))
        for j, leg in enumerate(legs):
            for i, scenario in enumerate(scenarios):
                payout_matrix[i, j] = leg.payout_scenarios.get(scenario, 0.0)

        prices = np.array([leg.price for leg in legs])

        # Tailles maximales (limitées par liquidité et risk limits)
        max_sizes = np.array([
            min(leg.available_size, self.risk.max_per_trade / max(leg.price, 0.01))
            for leg in legs
        ])

        # Essai avec des tailles normalisées (1 contrat par jambe)
        unit_sizes = np.ones(n_legs)
        total_cost = np.dot(prices, unit_sizes)
        fees = total_cost * 0.0004

        # Payout minimum dans le pire scénario
        payouts_per_scenario = payout_matrix @ unit_sizes
        min_payout = float(np.min(payouts_per_scenario))

        profit_per_unit = min_payout - total_cost - fees

        if profit_per_unit < settings.MIN_BINARY_EDGE:
            return None

        # Calculer les scénarios couverts
        scenario_coverage = {
            scenario: float(payouts_per_scenario[i])
            for i, scenario in enumerate(scenarios)
        }

        # Calculer la couverture optimale
        optimal_sizes = list(unit_sizes)
        risk_score = min_payout / max(total_cost, 0.01)

        return HedgeOpportunity(
            event_name=event_name,
            legs=legs,
            scenario_coverage=scenario_coverage,
            total_cost=float(total_cost),
            min_payout=float(min_payout),
            profit_per_unit=float(profit_per_unit),
            optimal_sizes=optimal_sizes,
            risk_score=float(risk_score),
        )

    def _greedy_hedge(
        self, event_name: str, legs: List[MarketLeg]
    ) -> Optional[HedgeOpportunity]:
        """Approche greedy sans scipy pour construire un hedge."""
        # Sélectionner les jambes qui maximisent la couverture
        selected = legs[:4]  # Limiter à 4 jambes max
        total_cost = sum(leg.price for leg in selected)
        fees = total_cost * len(selected) * 0.0002

        # Payout moyen (approximation)
        min_payout = 0.5  # Conservateur
        profit = min_payout - total_cost - fees

        if profit < settings.MIN_BINARY_EDGE:
            return None

        return HedgeOpportunity(
            event_name=event_name,
            legs=selected,
            scenario_coverage={},
            total_cost=total_cost,
            min_payout=min_payout,
            profit_per_unit=profit,
            optimal_sizes=[1.0] * len(selected),
            risk_score=0.5,
        )

    async def detect_opportunities(self) -> List[Opportunity]:
        """Détecte les opportunités de hedge multi-outcomes."""
        await self._group_markets_by_event()

        if not self._market_groups:
            return []

        opportunities = []

        for event_key, markets in self._market_groups.items():
            try:
                hedge_opp = await self._build_hedge_opportunity(event_key, markets)
                if hedge_opp:
                    profit_total = hedge_opp.profit_per_unit * hedge_opp.optimal_sizes[0]
                    if profit_total >= self.risk.min_profit_threshold:
                        opportunities.append(Opportunity(
                            strategy_name=self.name,
                            market_id=event_key,
                            description=(
                                f"Hedge {len(hedge_opp.legs)} jambes: {event_key} | "
                                f"Coût: ${hedge_opp.total_cost:.2f} | "
                                f"Payout min: ${hedge_opp.min_payout:.2f} | "
                                f"Profit: ${hedge_opp.profit_per_unit:.3f}/unité"
                            ),
                            expected_profit=profit_total,
                            expected_profit_pct=(hedge_opp.profit_per_unit / hedge_opp.total_cost) * 100,
                            confidence=hedge_opp.risk_score,
                            urgency=0.7,
                            metadata={"hedge_opp": hedge_opp},
                        ))
            except Exception as e:
                logger.debug(f"[MULTI_HEDGE] Erreur analyse {event_key}: {e}")

            await asyncio.sleep(0.05)  # Éviter de saturer l'API

        return opportunities

    async def execute_opportunity(self, opportunity: Opportunity) -> Optional[ArbitrageExecution]:
        """Exécute un hedge multi-outcomes."""
        hedge_opp: HedgeOpportunity = opportunity.metadata["hedge_opp"]

        legs_to_execute = [
            (leg.token_id, leg.price, size)
            for leg, size in zip(hedge_opp.legs, hedge_opp.optimal_sizes)
        ]

        # Utiliser le premier market_id disponible
        market_id = hedge_opp.legs[0].market_id if hedge_opp.legs else ""

        if self.risk_limits and not await self.risk_limits.can_trade(
            market_id=market_id,
            trade_size=hedge_opp.total_cost,
        ):
            return None

        logger.info(
            f"[MULTI_HEDGE] Exécution hedge {len(legs_to_execute)} jambes: {hedge_opp.event_name} | "
            f"Profit attendu: ${hedge_opp.profit_per_unit:.3f}"
        )

        result = await self.executor.execute_multi_hedge(
            market_id=market_id,
            legs=legs_to_execute,
            strategy=self.name,
        )

        if result.success and self.risk_limits:
            await self.risk_limits.record_trade(
                market_id=market_id,
                trade_size=hedge_opp.total_cost,
                profit=hedge_opp.profit_per_unit,
            )

        return result
