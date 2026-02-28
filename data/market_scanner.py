"""
Scanner de marchés Polymarket.
Gère la liste des marchés actifs et sert de hub central pour les stratégies.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from core.polymarket_client import PolymarketClient
from core.websocket_manager import OrderbookUpdate, PriceUpdate, WebSocketManager
from data.orderbook_cache import OrderbookCache
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class MarketSnapshot:
    """Snapshot complet d'un marché à un instant T."""
    market_id: str
    question: str
    token_id_yes: str
    token_id_no: str
    ask_yes: Optional[float]
    ask_no: Optional[float]
    bid_yes: Optional[float]
    bid_no: Optional[float]
    liquidity: float
    volume: float
    tags: List[str]
    category: str
    binary_spread: Optional[float]  # ask_yes + ask_no (pour arbitrage)
    last_updated: float = field(default_factory=time.time)

    @property
    def is_arb_opportunity(self) -> bool:
        """Vérifie rapidement si c'est une opportunité d'arbitrage binaire."""
        if self.ask_yes is None or self.ask_no is None:
            return False
        return (self.ask_yes + self.ask_no) < 0.98  # 2% de marge minimum

    @property
    def binary_profit_per_unit(self) -> float:
        if self.ask_yes is None or self.ask_no is None:
            return 0.0
        return max(0.0, 1.0 - self.ask_yes - self.ask_no)


class MarketScanner:
    """
    Scanner central qui maintient un snapshot de TOUS les marchés actifs.
    Mis à jour en temps réel via WebSocket, avec rafraîchissement périodique via API.
    """

    def __init__(
        self,
        client: PolymarketClient,
        ws_manager: Optional[WebSocketManager] = None,
        orderbook_cache: Optional[OrderbookCache] = None,
        refresh_interval: int = 300,  # 5 minutes
    ):
        self.client = client
        self.ws_manager = ws_manager
        self.cache = orderbook_cache or OrderbookCache(ttl_seconds=10.0)
        self.refresh_interval = refresh_interval

        self._markets: Dict[str, Dict] = {}          # market_id -> raw market data
        self._snapshots: Dict[str, MarketSnapshot] = {}
        self._token_to_market: Dict[str, str] = {}   # token_id -> market_id
        self._last_refresh = 0.0
        self._running = False

        # Callbacks pour les opportunités détectées
        self._arb_callbacks: List[Callable[[MarketSnapshot], None]] = []

        # Statistiques
        self._total_scans = 0
        self._arb_detected = 0

        # Brancher le cache aux updates WebSocket
        if ws_manager:
            ws_manager.add_orderbook_callback(self._on_orderbook_update)
            ws_manager.add_price_callback(self._on_price_update)

    def add_arb_callback(self, callback: Callable[[MarketSnapshot], None]) -> None:
        """Enregistre un callback appelé quand une opportunité d'arb est détectée."""
        self._arb_callbacks.append(callback)

    def _on_orderbook_update(self, update: OrderbookUpdate) -> None:
        """Callback appelé à chaque update d'orderbook via WebSocket."""
        self.cache.update_from_ws(
            token_id=update.token_id,
            bids=update.bids,
            asks=update.asks,
        )
        # Mettre à jour le snapshot du marché correspondant
        market_id = self._token_to_market.get(update.token_id)
        if market_id:
            asyncio.create_task(self._update_snapshot(market_id))

    def _on_price_update(self, update: PriceUpdate) -> None:
        """Callback pour les mises à jour de prix individuelles."""
        self.cache.update_price(
            token_id=update.token_id,
            price=update.price,
            size=update.size,
            side=update.side,
        )

    async def start(self) -> None:
        """Démarre le scanner."""
        self._running = True
        await self.refresh_markets()
        asyncio.create_task(self._refresh_loop())
        asyncio.create_task(self._scan_loop())
        logger.info("MarketScanner démarré")

    async def stop(self) -> None:
        self._running = False

    async def _refresh_loop(self) -> None:
        """Rafraîchit la liste des marchés périodiquement."""
        while self._running:
            await asyncio.sleep(self.refresh_interval)
            if self._running:
                await self.refresh_markets()

    async def _scan_loop(self) -> None:
        """Scanne les snapshots pour détecter les opportunités."""
        while self._running:
            await asyncio.sleep(1.0)  # Scan toutes les secondes
            self._total_scans += 1
            try:
                await self._detect_arb_opportunities()
            except Exception as e:
                logger.debug(f"Erreur scan: {e}")

    async def refresh_markets(self) -> None:
        """Recharge tous les marchés actifs depuis l'API."""
        try:
            logger.info("Rafraîchissement de la liste des marchés...")
            raw_markets = await self.client.get_all_active_markets()

            new_markets = {}
            new_token_map = {}

            for market in raw_markets:
                market_id = market.get("conditionId") or market.get("condition_id", "")
                if not market_id:
                    continue

                new_markets[market_id] = market

                for token in market.get("tokens", []):
                    token_id = token.get("token_id", "")
                    if token_id:
                        new_token_map[token_id] = market_id

            self._markets = new_markets
            self._token_to_market = new_token_map
            self._last_refresh = time.time()

            # Souscrire au WebSocket pour tous les tokens
            if self.ws_manager:
                all_token_ids = list(new_token_map.keys())
                await self.ws_manager.subscribe_tokens(all_token_ids)

            logger.info(
                f"Scanner mis à jour: {len(self._markets)} marchés, "
                f"{len(self._token_to_market)} tokens"
            )

            # Construire les snapshots initiaux
            await self._build_all_snapshots()

        except Exception as e:
            logger.error(f"Erreur rafraîchissement marchés: {e}")

    async def _build_all_snapshots(self) -> None:
        """Construit les snapshots initiaux pour tous les marchés."""
        tasks = [self._update_snapshot(mid) for mid in list(self._markets.keys())[:200]]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _update_snapshot(self, market_id: str) -> None:
        """Met à jour le snapshot d'un marché spécifique."""
        market = self._markets.get(market_id)
        if not market:
            return

        tokens = market.get("tokens", [])
        if len(tokens) != 2:
            return

        token_yes = next((t for t in tokens if t.get("outcome") == "Yes"), tokens[0])
        token_no = next((t for t in tokens if t.get("outcome") == "No"), tokens[1])

        tid_yes = token_yes.get("token_id", "")
        tid_no = token_no.get("token_id", "")

        # Tenter de récupérer depuis le cache d'abord
        book_yes = self.cache.get(tid_yes)
        book_no = self.cache.get(tid_no)

        # Si pas en cache, fetch depuis l'API (limité pour éviter de surcharger)
        if not book_yes or not book_no:
            try:
                books = await self.client.get_orderbooks_batch(
                    [t for t in [tid_yes, tid_no] if t]
                )
                if tid_yes in books:
                    self.cache.update(tid_yes, books[tid_yes])
                    book_yes = books[tid_yes]
                if tid_no in books:
                    self.cache.update(tid_no, books[tid_no])
                    book_no = books[tid_no]
            except Exception:
                return

        snap = MarketSnapshot(
            market_id=market_id,
            question=market.get("question", "")[:100],
            token_id_yes=tid_yes,
            token_id_no=tid_no,
            ask_yes=book_yes.best_ask if book_yes else None,
            ask_no=book_no.best_ask if book_no else None,
            bid_yes=book_yes.best_bid if book_yes else None,
            bid_no=book_no.best_bid if book_no else None,
            liquidity=float(market.get("liquidity", 0)),
            volume=float(market.get("volume", 0)),
            tags=market.get("tags", []),
            category=market.get("category", ""),
            binary_spread=(
                (book_yes.best_ask or 0) + (book_no.best_ask or 0)
                if book_yes and book_no else None
            ),
        )

        self._snapshots[market_id] = snap

    async def _detect_arb_opportunities(self) -> None:
        """Passe en revue les snapshots pour détecter les opportunités."""
        for market_id, snap in self._snapshots.items():
            if snap.is_arb_opportunity:
                self._arb_detected += 1
                for callback in self._arb_callbacks:
                    try:
                        callback(snap)
                    except Exception as e:
                        logger.debug(f"Erreur callback arb: {e}")

    def get_snapshot(self, market_id: str) -> Optional[MarketSnapshot]:
        return self._snapshots.get(market_id)

    def get_all_snapshots(self) -> List[MarketSnapshot]:
        return list(self._snapshots.values())

    def get_arb_opportunities(self, min_profit: float = 0.01) -> List[MarketSnapshot]:
        """Retourne tous les marchés avec une opportunité d'arbitrage détectée."""
        return [
            s for s in self._snapshots.values()
            if s.is_arb_opportunity and s.binary_profit_per_unit >= min_profit
        ]

    def get_stats(self) -> Dict[str, Any]:
        return {
            "total_markets": len(self._markets),
            "snapshots": len(self._snapshots),
            "total_scans": self._total_scans,
            "arb_detected_total": self._arb_detected,
            "cache_stats": self.cache.stats(),
            "last_refresh_ago": round(time.time() - self._last_refresh),
        }
