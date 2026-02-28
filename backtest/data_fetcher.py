"""
Téléchargement des données historiques Polymarket pour le backtesting.
Utilise l'API Gamma pour les marchés et les données de prix historiques.
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from utils.logger import get_logger

logger = get_logger(__name__)

DATA_DIR = Path("data/historical")


@dataclass
class HistoricalMarket:
    """Données historiques d'un marché Polymarket."""
    market_id: str
    question: str
    start_date: str
    end_date: str
    resolved: bool
    resolution: Optional[str]  # "YES" ou "NO"
    volume: float
    liquidity: float
    tags: List[str]
    token_id_yes: str
    token_id_no: str
    price_history: List[Dict] = field(default_factory=list)  # [{timestamp, yes_price, no_price}]


class HistoricalDataFetcher:
    """
    Télécharge et met en cache les données historiques Polymarket.
    Les données sont sauvegardées localement pour éviter de re-télécharger.
    """

    GAMMA_API = "https://gamma-api.polymarket.com"
    CLOB_API = "https://clob.polymarket.com"

    def __init__(self, cache_dir: str = "data/historical"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers={"User-Agent": "PolymarketBacktester/1.0"},
        )
        return self

    async def __aexit__(self, *args):
        if self._session:
            await self._session.close()

    async def fetch_resolved_markets(
        self,
        start_date: str,  # Format: "2024-01-01"
        end_date: str,    # Format: "2024-12-31"
        tags: Optional[List[str]] = None,
        limit: int = 2000,
    ) -> List[Dict]:
        """
        Télécharge les marchés résolus entre deux dates.
        Utilisés pour backtester la stratégie d'arbitrage binaire.
        """
        cache_file = self.cache_dir / f"markets_{start_date}_{end_date}.json"

        if cache_file.exists():
            logger.info(f"Chargement depuis cache: {cache_file}")
            with open(cache_file) as f:
                return json.load(f)

        logger.info(f"Téléchargement des marchés {start_date} → {end_date}...")
        markets = []
        offset = 0

        while True:
            try:
                url = f"{self.GAMMA_API}/markets"
                params = {
                    "limit": min(limit, 500),
                    "offset": offset,
                    "closed": "true",
                    "start_date_min": start_date,
                    "end_date_max": end_date,
                }
                if tags:
                    params["tag"] = tags[0]

                async with self._session.get(url, params=params) as resp:
                    if resp.status != 200:
                        logger.warning(f"API erreur {resp.status}")
                        break
                    data = await resp.json()
                    batch = data if isinstance(data, list) else data.get("markets", [])

                    if not batch:
                        break
                    markets.extend(batch)
                    if len(batch) < 500:
                        break
                    offset += 500
                    await asyncio.sleep(0.5)

            except Exception as e:
                logger.error(f"Erreur fetch marchés: {e}")
                break

        # Sauvegarder en cache
        with open(cache_file, "w") as f:
            json.dump(markets, f)

        logger.info(f"Téléchargé {len(markets)} marchés résolus")
        return markets

    async def fetch_price_history(
        self, token_id: str, fidelity: int = 60
    ) -> List[Dict]:
        """
        Récupère l'historique de prix pour un token.
        fidelity: résolution en minutes (1, 5, 15, 60, 1440)
        """
        cache_file = self.cache_dir / f"prices_{token_id[:12]}_{fidelity}m.json"

        if cache_file.exists():
            age = time.time() - cache_file.stat().st_mtime
            if age < 86400:  # Cache valide 24h
                with open(cache_file) as f:
                    return json.load(f)

        try:
            url = f"{self.CLOB_API}/prices-history"
            params = {
                "market": token_id,
                "interval": f"{fidelity}m",
                "fidelity": fidelity,
            }
            async with self._session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    history = data.get("history", [])

                    with open(cache_file, "w") as f:
                        json.dump(history, f)

                    return history
        except Exception as e:
            logger.debug(f"Erreur prix historique {token_id[:12]}: {e}")

        return []

    async def build_historical_dataset(
        self,
        start_date: str,
        end_date: str,
        min_volume: float = 1000,
        tags: Optional[List[str]] = None,
    ) -> List[HistoricalMarket]:
        """
        Construit un dataset complet avec marchés + historique de prix.
        """
        logger.info(f"Construction dataset: {start_date} → {end_date}")

        raw_markets = await self.fetch_resolved_markets(start_date, end_date, tags)
        logger.info(f"Marchés bruts: {len(raw_markets)}")

        dataset = []

        for i, market in enumerate(raw_markets):
            try:
                volume = float(market.get("volume", 0))
                if volume < min_volume:
                    continue

                tokens = market.get("tokens", [])
                if len(tokens) != 2:
                    continue

                token_yes = next((t for t in tokens if t.get("outcome") == "Yes"), tokens[0])
                token_no = next((t for t in tokens if t.get("outcome") == "No"), tokens[1])

                tid_yes = token_yes.get("token_id", "")
                tid_no = token_no.get("token_id", "")

                # Récupérer l'historique de prix
                hist_yes, hist_no = await asyncio.gather(
                    self.fetch_price_history(tid_yes),
                    self.fetch_price_history(tid_no),
                    return_exceptions=True,
                )

                if isinstance(hist_yes, Exception) or isinstance(hist_no, Exception):
                    continue

                # Construire l'historique combiné
                price_history = self._merge_price_histories(hist_yes, hist_no)

                # Déterminer la résolution
                resolution = None
                if market.get("resolved"):
                    yes_res = token_yes.get("winner", False)
                    resolution = "YES" if yes_res else "NO"

                hm = HistoricalMarket(
                    market_id=market.get("conditionId", ""),
                    question=market.get("question", "")[:200],
                    start_date=market.get("startDate", ""),
                    end_date=market.get("endDate", ""),
                    resolved=market.get("resolved", False),
                    resolution=resolution,
                    volume=volume,
                    liquidity=float(market.get("liquidity", 0)),
                    tags=market.get("tags", []),
                    token_id_yes=tid_yes,
                    token_id_no=tid_no,
                    price_history=price_history,
                )
                dataset.append(hm)

                if (i + 1) % 100 == 0:
                    logger.info(f"Traité {i+1}/{len(raw_markets)} marchés...")
                    await asyncio.sleep(0.1)

            except Exception as e:
                logger.debug(f"Erreur traitement marché {i}: {e}")

        logger.info(f"Dataset construit: {len(dataset)} marchés avec historique de prix")
        return dataset

    def _merge_price_histories(
        self, hist_yes: List[Dict], hist_no: List[Dict]
    ) -> List[Dict]:
        """Fusionne les historiques YES et NO en timeline commune."""
        yes_map = {p["t"]: float(p.get("p", 0.5)) for p in hist_yes}
        no_map = {p["t"]: float(p.get("p", 0.5)) for p in hist_no}

        all_timestamps = sorted(set(yes_map.keys()) | set(no_map.keys()))

        result = []
        last_yes = 0.5
        last_no = 0.5

        for ts in all_timestamps:
            if ts in yes_map:
                last_yes = yes_map[ts]
            if ts in no_map:
                last_no = no_map[ts]

            result.append({
                "timestamp": ts,
                "yes_price": last_yes,
                "no_price": last_no,
                "total": last_yes + last_no,
            })

        return result

    def save_dataset(self, dataset: List[HistoricalMarket], filename: str) -> None:
        """Sauvegarde le dataset en JSON."""
        path = self.cache_dir / filename
        data = [
            {
                "market_id": m.market_id,
                "question": m.question,
                "start_date": m.start_date,
                "end_date": m.end_date,
                "resolved": m.resolved,
                "resolution": m.resolution,
                "volume": m.volume,
                "liquidity": m.liquidity,
                "tags": m.tags,
                "token_id_yes": m.token_id_yes,
                "token_id_no": m.token_id_no,
                "price_history": m.price_history,
            }
            for m in dataset
        ]
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        logger.info(f"Dataset sauvegardé: {path} ({len(data)} marchés)")

    def load_dataset(self, filename: str) -> List[HistoricalMarket]:
        """Charge un dataset depuis un fichier JSON."""
        path = self.cache_dir / filename
        with open(path) as f:
            data = json.load(f)
        return [
            HistoricalMarket(**m)
            for m in data
        ]
