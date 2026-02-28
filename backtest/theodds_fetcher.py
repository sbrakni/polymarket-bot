"""
TheOddsAPI data fetcher for backtesting.

Fetches real historical sports results (scores) and saves them locally
to avoid burning through the free-tier quota (500 req/month).

Free-tier endpoints used:
  GET /v4/sports                          — list available sports
  GET /v4/sports/{sport}/scores           — completed games (up to 3 days)
  GET /v4/sports/{sport}/odds             — current odds snapshot (not used in backtest)

Usage:
    from backtest.theodds_fetcher import TheOddsApiFetcher
    fetcher = TheOddsApiFetcher()
    sports = fetcher.get_sports()
    scores = fetcher.get_scores("basketball_nba", days_from=3)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

from utils.logger import get_logger

logger = get_logger(__name__)

BASE_URL = "https://api.the-odds-api.com/v4"
# Cache TTL: 6h for scores (games don't un-resolve), 5m for live odds
_CACHE_TTL_SCORES = 6 * 3600
_CACHE_TTL_ODDS = 300


class TheOddsApiFetcher:
    """
    Thin wrapper around TheOddsAPI with aggressive local JSON caching.
    Every response is saved to data/historical/theodds/<endpoint>.json
    and re-used until it expires.  This preserves the free-tier quota.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        cache_dir: str = "data/historical/theodds",
    ) -> None:
        self.api_key = api_key or os.getenv("THEODDSAPI_KEY", "")
        if not self.api_key:
            raise ValueError(
                "TheOddsAPI key not set. "
                "Set THEODDSAPI_KEY in .env or pass api_key=... explicitly."
            )
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def get_sports(self) -> List[Dict]:
        """Return the list of in-season sports (cached 6h)."""
        return self._cached_get(
            "sports",
            "/sports",
            params={"all": "false"},
            ttl=_CACHE_TTL_SCORES,
        )

    def get_scores(
        self,
        sport_key: str,
        days_from: int = 3,
    ) -> List[Dict]:
        """
        Return completed game scores for the last `days_from` days (max 3 on free tier).
        Each result is a dict with:
          id, sport_key, sport_title, commence_time, completed,
          home_team, away_team, scores: [{name, score}]
        """
        cache_key = f"scores_{sport_key}_d{days_from}"
        return self._cached_get(
            cache_key,
            f"/sports/{sport_key}/scores",
            params={"daysFrom": str(days_from), "dateFormat": "iso"},
            ttl=_CACHE_TTL_SCORES,
        )

    def get_odds(
        self,
        sport_key: str,
        regions: str = "us",
        markets: str = "h2h",
        bookmakers: str = "draftkings",
    ) -> List[Dict]:
        """
        Return current odds snapshot (not for historical backtest, but useful
        for live signal calibration).
        """
        cache_key = f"odds_{sport_key}_{regions}_{markets}"
        return self._cached_get(
            cache_key,
            f"/sports/{sport_key}/odds",
            params={
                "regions": regions,
                "markets": markets,
                "bookmakers": bookmakers,
                "dateFormat": "iso",
                "oddsFormat": "decimal",
            },
            ttl=_CACHE_TTL_ODDS,
        )

    def get_nba_scores(self, days_from: int = 3) -> List[Dict]:
        return self.get_scores("basketball_nba", days_from)

    def get_nfl_scores(self, days_from: int = 3) -> List[Dict]:
        return self.get_scores("americanfootball_nfl", days_from)

    def get_nhl_scores(self, days_from: int = 3) -> List[Dict]:
        return self.get_scores("icehockey_nhl", days_from)

    def get_mlb_scores(self, days_from: int = 3) -> List[Dict]:
        return self.get_scores("baseball_mlb", days_from)

    def get_epl_scores(self, days_from: int = 3) -> List[Dict]:
        return self.get_scores("soccer_epl", days_from)

    def get_all_completed_games(self, days_from: int = 3) -> Dict[str, List[Dict]]:
        """
        Fetch scores for the main sports we care about.
        Returns {sport_key: [game, ...]}.
        Uses 5 API calls; subsequent calls within TTL are free.
        """
        sports = [
            "basketball_nba",
            "americanfootball_nfl",
            "icehockey_nhl",
            "baseball_mlb",
            "soccer_epl",
        ]
        results: Dict[str, List[Dict]] = {}
        for sport in sports:
            try:
                games = self.get_scores(sport, days_from)
                completed = [g for g in games if g.get("completed")]
                results[sport] = completed
                logger.info(
                    "Fetched %d completed %s games", len(completed), sport
                )
            except Exception as exc:
                logger.warning("Could not fetch %s: %s", sport, exc)
                results[sport] = []
        return results

    # ─────────────────────────────────────────────────────────────────────────
    # Caching helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _cache_path(self, key: str) -> Path:
        # Sanitize key for use as a filename
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)
        return self.cache_dir / f"{safe}.json"

    def _load_cache(self, key: str, ttl: int) -> Optional[Any]:
        path = self._cache_path(key)
        if not path.exists():
            return None
        age = time.time() - path.stat().st_mtime
        if age > ttl:
            return None
        try:
            with open(path) as f:
                envelope = json.load(f)
            logger.debug("Cache hit: %s (age %.0fs)", key, age)
            return envelope["data"]
        except Exception:
            return None

    def _save_cache(self, key: str, data: Any) -> None:
        path = self._cache_path(key)
        with open(path, "w") as f:
            json.dump({"data": data, "saved_at": time.time()}, f)

    def _cached_get(
        self,
        cache_key: str,
        endpoint: str,
        params: Optional[Dict[str, str]] = None,
        ttl: int = _CACHE_TTL_SCORES,
    ) -> Any:
        cached = self._load_cache(cache_key, ttl)
        if cached is not None:
            return cached

        data = self._api_get(endpoint, params)
        self._save_cache(cache_key, data)
        return data

    def _api_get(
        self,
        endpoint: str,
        params: Optional[Dict[str, str]] = None,
    ) -> Any:
        p: Dict[str, str] = {"apiKey": self.api_key}
        if params:
            p.update(params)
        qs = urlencode(p)
        url = f"{BASE_URL}{endpoint}?{qs}"
        logger.info("TheOddsAPI → %s%s", endpoint, ("?" + urlencode({k: v for k, v in p.items() if k != "apiKey"})) if len(p) > 1 else "")

        req = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(req, timeout=15) as resp:
                body = resp.read().decode()
                remaining = resp.headers.get("x-requests-remaining", "?")
                used = resp.headers.get("x-requests-used", "?")
                logger.info(
                    "TheOddsAPI: used=%s remaining=%s", used, remaining
                )
                return json.loads(body)
        except HTTPError as exc:
            body = exc.read().decode() if hasattr(exc, "read") else ""
            raise RuntimeError(
                f"TheOddsAPI HTTP {exc.code} for {endpoint}: {body}"
            ) from exc
        except URLError as exc:
            raise RuntimeError(
                f"TheOddsAPI network error for {endpoint}: {exc.reason}"
            ) from exc

    # ─────────────────────────────────────────────────────────────────────────
    # Data helpers for backtesting
    # ─────────────────────────────────────────────────────────────────────────

    def parse_game_result(self, game: Dict) -> Optional[Dict]:
        """
        Convert a raw TheOddsAPI game dict into a clean result dict:
        {
          id, sport, home_team, away_team, home_score, away_score,
          winner, commence_time (ISO str), completed
        }
        Returns None if scores are not available.
        """
        if not game.get("completed"):
            return None
        scores = game.get("scores") or []
        if len(scores) < 2:
            return None

        home = game["home_team"]
        away = game["away_team"]

        home_score: Optional[float] = None
        away_score: Optional[float] = None
        for s in scores:
            try:
                val = float(s["score"])
            except (KeyError, TypeError, ValueError):
                continue
            if s["name"] == home:
                home_score = val
            elif s["name"] == away:
                away_score = val

        if home_score is None or away_score is None:
            return None

        if home_score > away_score:
            winner = home
        elif away_score > home_score:
            winner = away
        else:
            winner = "TIE"

        return {
            "id": game["id"],
            "sport": game["sport_key"],
            "sport_title": game.get("sport_title", ""),
            "home_team": home,
            "away_team": away,
            "home_score": int(home_score),
            "away_score": int(away_score),
            "winner": winner,
            "commence_time": game.get("commence_time", ""),
            "completed": True,
        }

    def get_parsed_results(
        self, sport_key: str, days_from: int = 3
    ) -> List[Dict]:
        """Return list of parsed (clean) game results for a sport."""
        raw = self.get_scores(sport_key, days_from)
        results = []
        for game in raw:
            parsed = self.parse_game_result(game)
            if parsed:
                results.append(parsed)
        return results
