"""
Gestionnaire de connexions WebSocket pour les prix Polymarket en temps réel.
Maintient 6 connexions parallèles et gère la reconnexion automatique.
"""

import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class PriceUpdate:
    """Mise à jour de prix reçue via WebSocket."""
    token_id: str
    price: float
    size: float
    side: str  # "BUY" ou "SELL"
    timestamp: float = field(default_factory=time.time)
    market_id: str = ""


@dataclass
class OrderbookUpdate:
    """Mise à jour de l'orderbook reçue via WebSocket."""
    token_id: str
    market_id: str
    bids: List[Dict[str, float]] = field(default_factory=list)
    asks: List[Dict[str, float]] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


# Type des callbacks
PriceCallback = Callable[[PriceUpdate], None]
OrderbookCallback = Callable[[OrderbookUpdate], None]


class WebSocketConnection:
    """Une connexion WebSocket individuelle avec reconnexion automatique."""

    def __init__(
        self,
        connection_id: int,
        url: str,
        on_price_update: Optional[PriceCallback] = None,
        on_orderbook_update: Optional[OrderbookCallback] = None,
        reconnect_delay: int = settings.WS_RECONNECT_DELAY_S,
    ):
        self.id = connection_id
        self.url = url
        self.on_price_update = on_price_update
        self.on_orderbook_update = on_orderbook_update
        self.reconnect_delay = reconnect_delay

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._subscriptions: Set[str] = set()
        self._running = False
        self._connected = False
        self._last_heartbeat = time.time()
        self._message_count = 0

    @property
    def connected(self) -> bool:
        return self._connected

    async def subscribe(self, token_ids: List[str]) -> None:
        """Souscrit aux updates de prix pour une liste de tokens."""
        new_ids = set(token_ids) - self._subscriptions
        if not new_ids:
            return

        if self._ws and self._connected:
            sub_msg = {
                "type": "subscribe",
                "channel": "price_change",
                "markets": list(new_ids),
            }
            await self._ws.send(json.dumps(sub_msg))

        self._subscriptions.update(new_ids)
        logger.debug(f"[WS-{self.id}] Souscrit à {len(new_ids)} nouveaux tokens")

    async def run(self) -> None:
        """Boucle principale de connexion avec reconnexion automatique."""
        self._running = True
        delay = self.reconnect_delay

        while self._running:
            try:
                async with websockets.connect(
                    self.url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                    max_size=10 * 1024 * 1024,  # 10MB max message
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    delay = self.reconnect_delay  # Reset le délai sur succès
                    logger.info(f"[WS-{self.id}] Connexion établie: {self.url}")

                    # Re-souscrire aux tokens précédents après reconnexion
                    if self._subscriptions:
                        await self.subscribe(list(self._subscriptions))

                    await self._listen(ws)

            except (ConnectionClosed, WebSocketException) as e:
                self._connected = False
                logger.warning(f"[WS-{self.id}] Connexion perdue: {e} — reconnexion dans {delay}s")
            except Exception as e:
                self._connected = False
                logger.error(f"[WS-{self.id}] Erreur inattendue: {e}")

            if self._running:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)  # Backoff exponentiel plafonné à 60s

    async def _listen(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Écoute et traite les messages entrants."""
        async for raw_message in ws:
            try:
                self._message_count += 1
                self._last_heartbeat = time.time()
                await self._process_message(raw_message)
            except Exception as e:
                logger.debug(f"[WS-{self.id}] Erreur traitement message: {e}")

    async def _process_message(self, raw: str) -> None:
        """Parse et dispatche les messages WebSocket."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = data.get("type", "")

        if msg_type == "price_change":
            update = PriceUpdate(
                token_id=data.get("asset_id", ""),
                price=float(data.get("price", 0)),
                size=float(data.get("size", 0)),
                side=data.get("side", ""),
                market_id=data.get("market", ""),
            )
            if self.on_price_update:
                self.on_price_update(update)

        elif msg_type == "book":
            update = OrderbookUpdate(
                token_id=data.get("asset_id", ""),
                market_id=data.get("market", ""),
                bids=[{"price": float(b[0]), "size": float(b[1])} for b in data.get("bids", [])],
                asks=[{"price": float(a[0]), "size": float(a[1])} for a in data.get("asks", [])],
            )
            if self.on_orderbook_update:
                self.on_orderbook_update(update)

        elif msg_type == "heartbeat":
            logger.debug(f"[WS-{self.id}] Heartbeat reçu")

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()


class WebSocketManager:
    """
    Gestionnaire de 6 connexions WebSocket parallèles.
    Distribue les souscriptions entre les connexions pour équilibrer la charge.
    Les bots professionnels utilisent plusieurs connexions pour éviter les limites de souscription.
    """

    NUM_CONNECTIONS = 6

    def __init__(
        self,
        url: str = settings.WS_URL,
        on_price_update: Optional[PriceCallback] = None,
        on_orderbook_update: Optional[OrderbookCallback] = None,
    ):
        self.url = url
        self._connections: List[WebSocketConnection] = []
        self._tasks: List[asyncio.Task] = []
        self._token_to_connection: Dict[str, int] = {}  # token_id -> connection index
        self._running = False

        # Callbacks centralisés
        self._price_callbacks: List[PriceCallback] = []
        self._orderbook_callbacks: List[OrderbookCallback] = []

        if on_price_update:
            self._price_callbacks.append(on_price_update)
        if on_orderbook_update:
            self._orderbook_callbacks.append(on_orderbook_update)

        # Créer les 6 connexions
        for i in range(self.NUM_CONNECTIONS):
            conn = WebSocketConnection(
                connection_id=i,
                url=url,
                on_price_update=self._dispatch_price,
                on_orderbook_update=self._dispatch_orderbook,
            )
            self._connections.append(conn)

    def add_price_callback(self, callback: PriceCallback) -> None:
        self._price_callbacks.append(callback)

    def add_orderbook_callback(self, callback: OrderbookCallback) -> None:
        self._orderbook_callbacks.append(callback)

    def _dispatch_price(self, update: PriceUpdate) -> None:
        """Dispatch les mises à jour de prix à tous les callbacks enregistrés."""
        for cb in self._price_callbacks:
            try:
                cb(update)
            except Exception as e:
                logger.debug(f"Erreur callback price: {e}")

    def _dispatch_orderbook(self, update: OrderbookUpdate) -> None:
        """Dispatch les mises à jour d'orderbook à tous les callbacks."""
        for cb in self._orderbook_callbacks:
            try:
                cb(update)
            except Exception as e:
                logger.debug(f"Erreur callback orderbook: {e}")

    async def start(self) -> None:
        """Lance toutes les connexions WebSocket en parallèle."""
        self._running = True
        self._tasks = [
            asyncio.create_task(conn.run(), name=f"ws-{conn.id}")
            for conn in self._connections
        ]
        logger.info(f"WebSocketManager démarré avec {self.NUM_CONNECTIONS} connexions")

    async def subscribe_tokens(self, token_ids: List[str]) -> None:
        """
        Distribue les tokens entre les connexions en round-robin.
        Chaque connexion gère un sous-ensemble équilibré.
        """
        new_tokens = [t for t in token_ids if t not in self._token_to_connection]

        for i, token_id in enumerate(new_tokens):
            conn_idx = i % self.NUM_CONNECTIONS
            self._token_to_connection[token_id] = conn_idx
            await self._connections[conn_idx].subscribe([token_id])

        if new_tokens:
            logger.info(f"Souscrit à {len(new_tokens)} nouveaux tokens via {self.NUM_CONNECTIONS} connexions")

    async def stop(self) -> None:
        """Arrête toutes les connexions proprement."""
        self._running = False
        for conn in self._connections:
            await conn.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("WebSocketManager arrêté")

    def get_stats(self) -> Dict[str, Any]:
        """Retourne les statistiques de connexion."""
        return {
            "num_connections": self.NUM_CONNECTIONS,
            "connected": sum(1 for c in self._connections if c.connected),
            "total_subscriptions": len(self._token_to_connection),
            "messages_received": sum(c._message_count for c in self._connections),
        }

    async def monitor_health(self) -> None:
        """Surveille la santé des connexions et alerte si une est morte trop longtemps."""
        while self._running:
            await asyncio.sleep(30)
            now = time.time()
            for conn in self._connections:
                if conn._connected and (now - conn._last_heartbeat) > 60:
                    logger.warning(
                        f"[WS-{conn.id}] Pas de message depuis {now - conn._last_heartbeat:.0f}s"
                    )
