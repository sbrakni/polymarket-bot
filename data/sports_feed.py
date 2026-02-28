"""
Intégration des flux de données sportives en temps réel.
Supporte TheOddsAPI (gratuit) et Sportradar (premium) pour les événements live.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp

from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class SportEvent:
    """Événement sportif en cours."""
    event_id: str
    sport: str                   # "nba", "nfl", "nhl", "mlb", "soccer", "tennis"
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    status: str                  # "in_progress", "halftime", "final"
    period: str                  # "Q1", "Q2", "H1", "H2", etc.
    time_remaining_seconds: Optional[int] = None
    time_elapsed_seconds: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    last_event: Optional[str] = None   # Description du dernier événement
    fetched_at: float = field(default_factory=time.time)

    @property
    def is_live(self) -> bool:
        return self.status == "in_progress"

    @property
    def total_score(self) -> int:
        return self.home_score + self.away_score

    @property
    def score_diff(self) -> int:
        return abs(self.home_score - self.away_score)


class TheOddsAPIFeed:
    """
    Flux de données sportives via TheOddsAPI.
    Tier gratuit: 500 requêtes/mois → utiliser judicieusement.
    Doc: https://the-odds-api.com/liveapi/guides/v4
    """

    BASE_URL = "https://api.the-odds-api.com/v4"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        return self._session

    async def get_live_scores(self, sport: str) -> List[Dict[str, Any]]:
        """Récupère les scores en direct pour un sport donné."""
        session = await self._get_session()
        url = f"{self.BASE_URL}/sports/{sport}/scores"
        params = {
            "apiKey": self.api_key,
            "daysFrom": 1,
            "dateFormat": "iso",
        }
        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status == 401:
                    logger.error("TheOddsAPI: Clé API invalide")
                elif resp.status == 429:
                    logger.warning("TheOddsAPI: Quota dépassé")
                else:
                    logger.warning(f"TheOddsAPI: Status {resp.status}")
        except Exception as e:
            logger.debug(f"TheOddsAPI erreur: {e}")
        return []

    async def get_live_odds(self, sport: str) -> List[Dict[str, Any]]:
        """Récupère les odds en direct (pour comparer avec Polymarket)."""
        session = await self._get_session()
        url = f"{self.BASE_URL}/sports/{sport}/odds"
        params = {
            "apiKey": self.api_key,
            "regions": "us",
            "markets": "h2h,totals,spreads",
            "oddsFormat": "decimal",
        }
        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            logger.debug(f"TheOddsAPI odds erreur: {e}")
        return []

    def parse_event(self, raw: Dict) -> Optional[SportEvent]:
        """Convertit un événement brut TheOddsAPI en SportEvent."""
        try:
            scores = raw.get("scores") or []
            home_data = next((s for s in scores if s.get("name") == raw.get("home_team")), None)
            away_data = next((s for s in scores if s.get("name") == raw.get("away_team")), None)

            home_score = int(home_data.get("score", 0)) if home_data else 0
            away_score = int(away_data.get("score", 0)) if away_data else 0

            return SportEvent(
                event_id=raw.get("id", ""),
                sport=raw.get("sport_key", "").split("_")[0].lower(),
                home_team=raw.get("home_team", ""),
                away_team=raw.get("away_team", ""),
                home_score=home_score,
                away_score=away_score,
                status="in_progress" if raw.get("completed") is False else "final",
                period=raw.get("period", ""),
                metadata=raw,
            )
        except Exception as e:
            logger.debug(f"Erreur parsing event: {e}")
            return None

    async def close(self):
        if self._session:
            await self._session.close()


class SportradarFeed:
    """
    Flux premium Sportradar — données ultra-temps-réel directes des arènes.
    Latence ~1-2 secondes vs 15-30 secondes pour les flux TV.
    """

    BASE_URL = "https://api.sportradar.com"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5)
            )
        return self._session

    async def get_nba_live(self) -> List[Dict]:
        """Récupère les matchs NBA en cours."""
        session = await self._get_session()
        url = f"{self.BASE_URL}/nba/trial/v8/en/games/day/{self._today()}/schedule.json"
        params = {"api_key": self.api_key}
        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("games", [])
        except Exception as e:
            logger.debug(f"Sportradar NBA erreur: {e}")
        return []

    async def get_nfl_live(self) -> List[Dict]:
        """Récupère les matchs NFL en cours."""
        session = await self._get_session()
        url = f"{self.BASE_URL}/nfl/official/trial/v7/en/games/{self._today()}/schedule.json"
        params = {"api_key": self.api_key}
        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("week", {}).get("games", [])
        except Exception as e:
            logger.debug(f"Sportradar NFL erreur: {e}")
        return []

    def _today(self) -> str:
        from datetime import datetime
        return datetime.utcnow().strftime("%Y/%m/%d")

    def parse_nba_game(self, raw: Dict) -> Optional[SportEvent]:
        """Parse un jeu NBA Sportradar."""
        try:
            home = raw.get("home", {})
            away = raw.get("away", {})
            status = raw.get("status", "")

            if status not in ("inprogress", "halftime"):
                return None

            return SportEvent(
                event_id=raw.get("id", ""),
                sport="nba",
                home_team=home.get("name", ""),
                away_team=away.get("name", ""),
                home_score=home.get("points", 0),
                away_score=away.get("points", 0),
                status="in_progress",
                period=str(raw.get("quarter", "")),
                time_remaining_seconds=raw.get("clock_decimal", 0) * 60 if raw.get("clock_decimal") else None,
                metadata=raw,
            )
        except Exception as e:
            logger.debug(f"Sportradar parse NBA erreur: {e}")
            return None

    async def close(self):
        if self._session:
            await self._session.close()


class SportsDataFeed:
    """
    Orchestrateur des flux sportifs.
    Combine TheOddsAPI (gratuit) et Sportradar (premium).
    Gère le cache et la déduplication des événements.
    """

    def __init__(
        self,
        theodds_key: Optional[str] = None,
        sportradar_key: Optional[str] = None,
        poll_interval_seconds: int = 10,
    ):
        self.theodds_key = theodds_key or settings.THEODDSAPI_KEY
        self.sportradar_key = sportradar_key or settings.SPORTRADAR_API_KEY
        self.poll_interval = poll_interval_seconds

        self._theodds_feed: Optional[TheOddsAPIFeed] = None
        self._sportradar_feed: Optional[SportradarFeed] = None
        self._events_cache: Dict[str, SportEvent] = {}
        self._new_events_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._running = False

        # Initialiser les feeds disponibles
        if self.theodds_key:
            self._theodds_feed = TheOddsAPIFeed(self.theodds_key)
            logger.info("TheOddsAPI feed initialisé")
        if self.sportradar_key:
            self._sportradar_feed = SportradarFeed(self.sportradar_key)
            logger.info("Sportradar feed initialisé")

        if not self._theodds_feed and not self._sportradar_feed:
            logger.warning(
                "Aucun feed sportif configuré! Reality Arbitrage désactivé. "
                "Configurez THEODDSAPI_KEY ou SPORTRADAR_API_KEY dans .env"
            )

    async def start(self) -> None:
        """Démarre le polling des données sportives."""
        self._running = True
        asyncio.create_task(self._poll_loop())
        logger.info(f"SportsDataFeed démarré (interval: {self.poll_interval}s)")

    async def stop(self) -> None:
        self._running = False
        if self._theodds_feed:
            await self._theodds_feed.close()
        if self._sportradar_feed:
            await self._sportradar_feed.close()

    async def _poll_loop(self) -> None:
        """Boucle de polling des événements sportifs."""
        while self._running:
            try:
                await self._fetch_all_events()
            except Exception as e:
                logger.error(f"Erreur polling sports: {e}")
            await asyncio.sleep(self.poll_interval)

    async def _fetch_all_events(self) -> None:
        """Récupère tous les événements depuis les sources disponibles."""
        tasks = []

        if self._theodds_feed:
            for sport in settings.SUPPORTED_SPORTS:
                tasks.append(self._fetch_theodds_sport(sport))

        if self._sportradar_feed:
            tasks.append(self._fetch_sportradar_nba())

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _fetch_theodds_sport(self, sport: str) -> None:
        """Récupère les événements TheOddsAPI pour un sport."""
        try:
            raw_events = await self._theodds_feed.get_live_scores(sport)
            for raw in raw_events:
                event = self._theodds_feed.parse_event(raw)
                if event and event.is_live:
                    await self._process_event(event)
        except Exception as e:
            logger.debug(f"Erreur fetch {sport}: {e}")

    async def _fetch_sportradar_nba(self) -> None:
        """Récupère les matchs NBA depuis Sportradar."""
        try:
            games = await self._sportradar_feed.get_nba_live()
            for game in games:
                event = self._sportradar_feed.parse_nba_game(game)
                if event:
                    await self._process_event(event)
        except Exception as e:
            logger.debug(f"Erreur Sportradar NBA: {e}")

    async def _process_event(self, event: SportEvent) -> None:
        """Traite un événement sportif et détecte les changements significatifs."""
        old_event = self._events_cache.get(event.event_id)

        if old_event:
            # Détecter les changements de score (événements déclencheurs)
            if (event.home_score != old_event.home_score or
                    event.away_score != old_event.away_score):
                event.last_event = (
                    f"Score changé: {old_event.home_score}-{old_event.away_score} → "
                    f"{event.home_score}-{event.away_score}"
                )
                logger.debug(f"Événement score: {event.home_team} {event.home_score} - "
                           f"{event.away_score} {event.away_team}")
                # Mettre en queue pour le traitement immédiat
                try:
                    self._new_events_queue.put_nowait(event)
                except asyncio.QueueFull:
                    pass

        self._events_cache[event.event_id] = event

    async def get_live_events(self) -> List[SportEvent]:
        """Retourne tous les événements sportifs en cours."""
        # Vider la queue des nouveaux événements (changements de score)
        new_events = []
        while not self._new_events_queue.empty():
            try:
                new_events.append(self._new_events_queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        # Si pas de nouveaux événements, retourner tous les events live du cache
        if not new_events:
            return [e for e in self._events_cache.values() if e.is_live]

        return new_events

    async def get_upcoming_events(self, hours_ahead: int = 2) -> List[SportEvent]:
        """Retourne les événements à venir dans les prochaines heures."""
        # Pour l'arbitrage de réalité, on précharge les mappings de marchés
        return list(self._events_cache.values())

    def get_event_by_id(self, event_id: str) -> Optional[SportEvent]:
        return self._events_cache.get(event_id)
