"""
MODULE 1 — Reality Arbitrage (Sports)

Exploite le décalage de 15-40 secondes entre les événements sportifs en direct
et leur reflet sur Polymarket. Reçoit les données des stades/arènes via API
et achète les contrats sous-évalués AVANT que le marché ne réagisse.

Exemples d'événements déclencheurs:
- NBA: panier marqué → shift de probabilité sur "Team X gagne"
- NFL: touchdown → shift sur "Total Points > X"
- Soccer: but marqué → shift sur "Match Nul" vs "Victoire Team A"
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from config import settings
from core.order_executor import ArbitrageExecution, OrderExecutor
from core.polymarket_client import OrderSide, OrderType, PolymarketClient
from data.sports_feed import SportEvent, SportsDataFeed
from risk.risk_limits import RiskLimits
from strategies.base_strategy import BaseStrategy, Opportunity
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class MarketMapping:
    """Mapping entre un événement sportif et les marchés Polymarket correspondants."""
    sport: str
    event_id: str         # ID dans l'API sportive
    home_team: str
    away_team: str
    market_id: str        # ID Polymarket
    question: str
    token_id_yes: str
    token_id_no: str
    market_type: str      # "moneyline", "total", "spread"
    threshold: Optional[float] = None  # Pour over/under
    current_yes_price: float = 0.5
    current_no_price: float = 0.5
    last_updated: float = field(default_factory=time.time)


@dataclass
class ProbabilityModel:
    """Modèle de calcul des probabilités en temps réel selon l'état du jeu."""

    @staticmethod
    def nba_win_probability(
        home_score: int, away_score: int,
        time_remaining_sec: int, home_is_target: bool
    ) -> float:
        """
        Calcule la probabilité de victoire NBA basée sur le score et le temps restant.
        Modèle simplifié basé sur les données historiques NBA.
        """
        if time_remaining_sec <= 0:
            return 1.0 if (home_score > away_score) == home_is_target else 0.0

        score_diff = home_score - away_score
        if not home_is_target:
            score_diff = -score_diff

        # Modèle logistique ajusté par le temps
        # Inspiration: Stern (1994) "A Brownian Motion Model for the Progress of Sports Scores"
        sigma_per_sec = 0.04  # Points par seconde (empirique NBA)
        sigma_remaining = sigma_per_sec * (time_remaining_sec ** 0.5)

        # Probabilité basée sur la loi normale
        import math
        z_score = score_diff / max(sigma_remaining, 0.1)
        prob = 0.5 * (1 + math.erf(z_score / math.sqrt(2)))
        return max(0.01, min(0.99, prob))

    @staticmethod
    def nfl_win_probability(
        home_score: int, away_score: int,
        time_remaining_sec: int, home_is_target: bool
    ) -> float:
        """Probabilité de victoire NFL."""
        if time_remaining_sec <= 0:
            return 1.0 if (home_score > away_score) == home_is_target else 0.0

        score_diff = home_score - away_score
        if not home_is_target:
            score_diff = -score_diff

        import math
        # NFL: plus haute variance par point que NBA
        sigma_per_sec = 0.06
        sigma_remaining = sigma_per_sec * (time_remaining_sec ** 0.5)
        z_score = score_diff / max(sigma_remaining, 0.1)
        prob = 0.5 * (1 + math.erf(z_score / math.sqrt(2)))
        return max(0.01, min(0.99, prob))

    @staticmethod
    def soccer_goal_probability(
        home_goals: int, away_goals: int,
        minutes_remaining: int, home_is_winner: bool
    ) -> float:
        """Probabilité de victoire soccer, basée sur le score et le temps restant."""
        if minutes_remaining <= 0:
            if home_goals > away_goals:
                return 1.0 if home_is_winner else 0.0
            elif away_goals > home_goals:
                return 0.0 if home_is_winner else 1.0
            else:
                return 0.5  # Nul

        import math
        # Modèle de Poisson pour la probabilité de victoire au soccer
        # Taux de buts en Premier League: ~2.7 buts/match = ~0.03/minute
        lambda_per_min = 0.03
        expected_goals = lambda_per_min * minutes_remaining

        score_diff = home_goals - away_goals
        if not home_is_winner:
            score_diff = -score_diff

        # Approximation: si mène de 2+ buts avec < 10min = très probable
        if score_diff >= 2 and minutes_remaining < 10:
            base = 0.97
        elif score_diff >= 1 and minutes_remaining < 15:
            base = 0.90 - (minutes_remaining * 0.01)
        elif score_diff == 0:
            base = 0.35  # Légèrement sous 50% car risque de nul
        elif score_diff < 0:
            base = 0.05 + max(0, (minutes_remaining / 90) * 0.15)
        else:
            base = 0.75 - (minutes_remaining * 0.005)

        return max(0.01, min(0.99, base))


class RealityArbitrageStrategy(BaseStrategy):
    """
    Stratégie Reality Arbitrage.
    Exploite la latence entre les flux sportifs en temps réel et Polymarket.
    """

    name = "REALITY_ARB"

    def __init__(
        self,
        client: PolymarketClient,
        executor: OrderExecutor,
        risk_config,
        sports_feed: Optional[SportsDataFeed] = None,
        risk_limits: Optional[RiskLimits] = None,
    ):
        super().__init__(client, executor, risk_config)
        self.sports_feed = sports_feed
        self.risk_limits = risk_limits
        self._market_mappings: Dict[str, List[MarketMapping]] = {}  # event_id -> marchés
        self._pending_events: List[Tuple[SportEvent, List[MarketMapping]]] = []
        self._prob_model = ProbabilityModel()
        self._execution_latencies: List[float] = []

    async def load_market_mappings(self) -> None:
        """Charge les mappings entre événements sportifs et marchés Polymarket."""
        try:
            markets = await self.client.get_all_active_markets()
            sport_markets = [
                m for m in markets
                if any(tag in m.get("tags", []) for tag in ["Sports", "Basketball", "Football",
                       "NFL", "NBA", "NHL", "MLB", "Soccer", "Tennis"])
            ]

            mappings_count = 0
            for market in sport_markets:
                mapping = self._parse_market_to_mapping(market)
                if mapping:
                    event_key = self._build_event_key(mapping)
                    if event_key not in self._market_mappings:
                        self._market_mappings[event_key] = []
                    self._market_mappings[event_key].append(mapping)
                    mappings_count += 1

            logger.info(
                f"[REALITY_ARB] {mappings_count} marchés sportifs mappés "
                f"sur {len(self._market_mappings)} événements"
            )
        except Exception as e:
            logger.error(f"[REALITY_ARB] Erreur chargement mappings: {e}")

    def _parse_market_to_mapping(self, market: Dict) -> Optional[MarketMapping]:
        """Extrait les informations de mapping depuis les métadonnées d'un marché."""
        question = market.get("question", "")
        tokens = market.get("tokens", [])

        if len(tokens) != 2:
            return None

        token_yes = next((t for t in tokens if t.get("outcome") == "Yes"), tokens[0])
        token_no = next((t for t in tokens if t.get("outcome") == "No"), tokens[1])

        # Détecter le sport et les équipes à partir du titre du marché
        sport = self._detect_sport(question, market.get("tags", []))
        if not sport:
            return None

        teams = self._extract_teams(question)
        if not teams:
            return None

        home_team, away_team = teams

        return MarketMapping(
            sport=sport,
            event_id=f"{sport}_{home_team.lower().replace(' ', '_')}_{away_team.lower().replace(' ', '_')}",
            home_team=home_team,
            away_team=away_team,
            market_id=market.get("conditionId", market.get("condition_id", "")),
            question=question[:200],
            token_id_yes=token_yes.get("token_id", ""),
            token_id_no=token_no.get("token_id", ""),
            market_type=self._detect_market_type(question),
            threshold=self._extract_threshold(question),
        )

    def _detect_sport(self, question: str, tags: List[str]) -> Optional[str]:
        """Détecte le sport depuis le texte du marché."""
        q_lower = question.lower()
        tags_lower = [t.lower() for t in tags]

        if "nba" in tags_lower or "basketball" in tags_lower:
            return "nba"
        if "nfl" in tags_lower or "football" in tags_lower:
            return "nfl"
        if "nhl" in tags_lower or "hockey" in tags_lower:
            return "nhl"
        if "mlb" in tags_lower or "baseball" in tags_lower:
            return "mlb"
        if "soccer" in tags_lower or "epl" in tags_lower or "premier league" in q_lower:
            return "soccer"
        if "tennis" in tags_lower:
            return "tennis"
        return None

    def _extract_teams(self, question: str) -> Optional[Tuple[str, str]]:
        """Extrait les noms d'équipes depuis le titre du marché."""
        import re
        # Pattern: "Will [Team A] beat [Team B]?" ou "[Team A] vs [Team B]"
        patterns = [
            r"Will (.+?) (?:beat|defeat|win against) (.+?)\??$",
            r"(.+?) vs\.? (.+?)(?:\s*-|\?|$)",
            r"(.+?) @ (.+?)(?:\s*-|\?|$)",
        ]
        for pattern in patterns:
            match = re.search(pattern, question, re.IGNORECASE)
            if match:
                home = match.group(1).strip()
                away = match.group(2).strip()
                # Nettoyer les suffixes communs
                for suffix in [" to win", " win", "?"]:
                    home = home.rstrip(suffix)
                    away = away.rstrip(suffix)
                if len(home) > 2 and len(away) > 2:
                    return home, away
        return None

    def _detect_market_type(self, question: str) -> str:
        q = question.lower()
        if "over" in q or "under" in q or "total" in q:
            return "total"
        if "spread" in q or "cover" in q or "+/-" in q:
            return "spread"
        return "moneyline"

    def _extract_threshold(self, question: str) -> Optional[float]:
        """Extrait le seuil numérique pour les marchés over/under."""
        import re
        match = re.search(r"(\d+(?:\.\d+)?)\s*(?:points?|goals?|runs?)", question, re.IGNORECASE)
        if match:
            return float(match.group(1))
        return None

    def _build_event_key(self, mapping: MarketMapping) -> str:
        return mapping.event_id

    async def detect_opportunities(self) -> List[Opportunity]:
        """Détecte les opportunités basées sur les événements sportifs récents."""
        if not self.sports_feed:
            return []

        opportunities = []

        # Charger les mappings si nécessaire
        if not self._market_mappings:
            await self.load_market_mappings()

        # Récupérer les événements sportifs récents
        try:
            recent_events = await self.sports_feed.get_live_events()
        except Exception as e:
            logger.error(f"[REALITY_ARB] Erreur récupération events: {e}")
            return []

        for event in recent_events:
            # Trouver les marchés correspondants
            related_markets = self._find_related_markets(event)

            for mapping in related_markets:
                opp = await self._evaluate_event(event, mapping)
                if opp:
                    opportunities.append(opp)

        return opportunities

    def _find_related_markets(self, event: "SportEvent") -> List[MarketMapping]:
        """Trouve les marchés Polymarket liés à un événement sportif."""
        related = []
        home_lower = event.home_team.lower()
        away_lower = event.away_team.lower()

        for event_key, mappings in self._market_mappings.items():
            for mapping in mappings:
                if mapping.sport != event.sport:
                    continue
                mapping_home = mapping.home_team.lower()
                mapping_away = mapping.away_team.lower()

                # Correspondance approximative des noms d'équipes
                if (self._teams_match(home_lower, mapping_home) and
                        self._teams_match(away_lower, mapping_away)):
                    related.append(mapping)

        return related

    def _teams_match(self, name1: str, name2: str) -> bool:
        """Vérifie si deux noms d'équipes correspondent (tolérance aux variations)."""
        if name1 == name2:
            return True
        # Vérifier si un nom contient l'autre
        if name1 in name2 or name2 in name1:
            return True
        # Comparer les 4 premiers caractères
        if len(name1) >= 4 and len(name2) >= 4 and name1[:4] == name2[:4]:
            return True
        return False

    async def _evaluate_event(
        self, event: "SportEvent", mapping: MarketMapping
    ) -> Optional[Opportunity]:
        """
        Évalue si un événement sportif crée une opportunité sur un marché Polymarket.
        Compare la probabilité calculée avec le prix actuel du marché.
        """
        # Calculer la probabilité théorique basée sur l'état actuel du jeu
        theoretical_prob = self._calculate_win_probability(event, mapping)
        if theoretical_prob is None:
            return None

        # Récupérer les prix actuels du marché
        try:
            books = await self.client.get_orderbooks_batch(
                [mapping.token_id_yes, mapping.token_id_no]
            )
            book_yes = books.get(mapping.token_id_yes)
            book_no = books.get(mapping.token_id_no)

            if not book_yes or not book_no:
                return None

            current_yes_price = book_yes.best_ask or 0.5
            current_no_price = book_no.best_ask or 0.5

        except Exception as e:
            logger.debug(f"[REALITY_ARB] Erreur récupération prix: {e}")
            return None

        # Calculer l'écart entre probabilité théorique et prix marché
        edge_yes = theoretical_prob - current_yes_price
        edge_no = (1 - theoretical_prob) - current_no_price

        best_edge = max(edge_yes, edge_no)
        if best_edge < settings.MIN_PROB_SHIFT:
            return None

        # Déterminer la direction
        if edge_yes > edge_no:
            side = "YES"
            price = current_yes_price
            token_id = mapping.token_id_yes
            edge = edge_yes
        else:
            side = "NO"
            price = current_no_price
            token_id = mapping.token_id_no
            edge = edge_no

        # Calculer la taille optimale via Kelly
        capital = min(self.risk.max_per_trade, self.risk.max_total_exposure * 0.1)
        odds = 1.0 / price
        prob_adjusted = theoretical_prob if side == "YES" else (1 - theoretical_prob)
        size = self.kelly_size(prob_adjusted, odds, capital)

        if size < 1.0:
            return None

        expected_profit = size * (1.0 - price) - size * price * 0.0002  # Frais

        logger.debug(
            f"[REALITY_ARB] Opportunité détectée: {mapping.question[:50]} | "
            f"Théorique: {theoretical_prob:.2%} | Marché: {current_yes_price:.2%} | "
            f"Edge: {best_edge:.2%} | Côté: {side}"
        )

        return Opportunity(
            strategy_name=self.name,
            market_id=mapping.market_id,
            description=(
                f"{event.sport.upper()}: {event.home_team} vs {event.away_team} | "
                f"Score {event.home_score}-{event.away_score} | "
                f"Acheter {side}@{price:.3f} (prob théorique: {theoretical_prob:.1%})"
            ),
            expected_profit=expected_profit,
            expected_profit_pct=edge * 100,
            confidence=min(0.95, 0.6 + edge),
            urgency=0.95,  # Reality arb est toujours urgent
            metadata={
                "event": event,
                "mapping": mapping,
                "side": side,
                "price": price,
                "token_id": token_id,
                "size": size,
                "theoretical_prob": theoretical_prob,
                "edge": edge,
            },
        )

    def _calculate_win_probability(
        self, event: "SportEvent", mapping: MarketMapping
    ) -> Optional[float]:
        """Calcule la probabilité de victoire selon le sport et l'état du jeu."""
        try:
            home_is_target = mapping.market_type == "moneyline"

            if event.sport == "nba":
                return self._prob_model.nba_win_probability(
                    event.home_score, event.away_score,
                    event.time_remaining_seconds or 0,
                    home_is_target=True
                )
            elif event.sport == "nfl":
                return self._prob_model.nfl_win_probability(
                    event.home_score, event.away_score,
                    event.time_remaining_seconds or 0,
                    home_is_target=True
                )
            elif event.sport == "soccer":
                return self._prob_model.soccer_goal_probability(
                    event.home_score, event.away_score,
                    event.time_remaining_seconds // 60 if event.time_remaining_seconds else 45,
                    home_is_winner=True
                )
        except Exception as e:
            logger.debug(f"Erreur calcul probabilité: {e}")
        return None

    async def execute_opportunity(self, opportunity: Opportunity) -> Optional[ArbitrageExecution]:
        """Exécute un trade de Reality Arbitrage."""
        meta = opportunity.metadata
        event: SportEvent = meta["event"]
        mapping: MarketMapping = meta["mapping"]

        # Mesurer la latence depuis la détection
        detect_time = opportunity.detected_at
        latency_ms = (time.time() - detect_time) * 1000

        if latency_ms > settings.MAX_LATENCY_MS:
            logger.warning(
                f"[REALITY_ARB] Latence trop haute ({latency_ms:.0f}ms > {settings.MAX_LATENCY_MS}ms) — abandon"
            )
            return None

        if self.risk_limits and not await self.risk_limits.can_trade(
            market_id=mapping.market_id,
            trade_size=meta["size"] * meta["price"],
        ):
            return None

        side = OrderSide.BUY
        start_ts = time.time()

        try:
            order = await self.executor.execute_single_order(
                market_id=mapping.market_id,
                token_id=meta["token_id"],
                side=side,
                price=meta["price"],
                size=meta["size"],
                order_type=OrderType.FOK,
                tag=f"{event.sport}_{meta['side']}",
            )

            exec_latency = (time.time() - start_ts) * 1000
            self._execution_latencies.append(exec_latency)

            logger.info(
                f"[REALITY_ARB] Trade exécuté: {event.home_team} vs {event.away_team} | "
                f"{meta['side']}@{meta['price']:.3f} × {meta['size']:.1f} | "
                f"Latence: {exec_latency:.0f}ms"
            )

            from core.order_executor import ArbitrageExecution, OrderStatus
            result = ArbitrageExecution(
                strategy=self.name,
                market_id=mapping.market_id,
                expected_profit=opportunity.expected_profit,
                latency_ms=exec_latency,
            )

            if order.status in (OrderStatus.FILLED, OrderStatus.MATCHED):
                result.success = True
                result.actual_profit = opportunity.expected_profit
                if self.risk_limits:
                    await self.risk_limits.record_trade(
                        market_id=mapping.market_id,
                        trade_size=meta["size"] * meta["price"],
                        profit=opportunity.expected_profit,
                    )
            return result

        except Exception as e:
            logger.error(f"[REALITY_ARB] Erreur exécution: {e}")
            return None

    def get_avg_latency_ms(self) -> float:
        if not self._execution_latencies:
            return 0.0
        return sum(self._execution_latencies[-100:]) / len(self._execution_latencies[-100:])
