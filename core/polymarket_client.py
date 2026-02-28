"""
Wrapper autour du py-clob-client pour interagir avec le CLOB Polymarket.
Gère l'authentification, le rate limiting, le retry, et le logging de chaque ordre.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional

import aiohttp

from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)


# ===========================================================================
# Modèles de données
# ===========================================================================

class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    FOK = "FOK"    # Fill-or-Kill
    GTC = "GTC"    # Good-till-Cancelled


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    MATCHED = "MATCHED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


@dataclass
class OrderbookLevel:
    price: float
    size: float


@dataclass
class Orderbook:
    market_id: str
    token_id: str
    bids: List[OrderbookLevel] = field(default_factory=list)
    asks: List[OrderbookLevel] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask_size(self) -> float:
        return self.asks[0].size if self.asks else 0.0

    @property
    def best_bid_size(self) -> float:
        return self.bids[0].size if self.bids else 0.0


@dataclass
class Order:
    market_id: str
    token_id: str
    side: OrderSide
    price: float
    size: float
    order_type: OrderType = OrderType.GTC
    client_order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: OrderStatus = OrderStatus.PENDING
    filled_size: float = 0.0
    avg_fill_price: float = 0.0
    created_at: float = field(default_factory=time.time)
    exchange_order_id: Optional[str] = None


@dataclass
class MarketInfo:
    condition_id: str
    question: str
    outcome_yes: str
    outcome_no: str
    token_id_yes: str
    token_id_no: str
    volume: float
    liquidity: float
    active: bool
    closed: bool
    end_date_iso: Optional[str] = None
    category: Optional[str] = None
    tags: List[str] = field(default_factory=list)


# ===========================================================================
# Rate Limiter
# ===========================================================================

class RateLimiter:
    """Token bucket rate limiter pour respecter les limites Polymarket."""

    def __init__(self, max_calls: int, period_seconds: float = 60.0):
        self.max_calls = max_calls
        self.period = period_seconds
        self._calls: List[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.time()
            # Supprimer les appels trop anciens
            self._calls = [t for t in self._calls if now - t < self.period]
            if len(self._calls) >= self.max_calls:
                wait_time = self.period - (now - self._calls[0])
                if wait_time > 0:
                    logger.debug(f"Rate limit: attente de {wait_time:.2f}s")
                    await asyncio.sleep(wait_time)
                self._calls = self._calls[1:]
            self._calls.append(time.time())


# ===========================================================================
# Client principal
# ===========================================================================

class PolymarketClient:
    """
    Client asynchrone pour l'API CLOB de Polymarket.
    Gère authentification, rate limiting, retry avec backoff exponentiel.
    """

    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self.base_url = settings.CLOB_API_URL
        self.gamma_url = settings.GAMMA_API_URL
        self._session: Optional[aiohttp.ClientSession] = None

        # Rate limiters séparés pour les ordres et les requêtes
        self._order_limiter = RateLimiter(settings.MAX_ORDERS_PER_MINUTE)
        self._request_limiter = RateLimiter(settings.MAX_REQUESTS_PER_MINUTE)

        # Clés API dérivées de la clé privée (via py-clob-client)
        self._api_key: Optional[str] = None
        self._api_secret: Optional[str] = None
        self._api_passphrase: Optional[str] = None

        logger.info(f"PolymarketClient initialisé — DRY_RUN={dry_run}")

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def connect(self) -> None:
        """Initialise la session HTTP et récupère les clés API."""
        connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300)
        timeout = aiohttp.ClientTimeout(total=10, connect=5)
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if settings.POLYMARKET_PRIVATE_KEY and not self.dry_run:
            await self._derive_api_credentials()
        logger.info("Session HTTP Polymarket établie")

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _derive_api_credentials(self) -> None:
        """Dérive les clés API L2 à partir de la clé privée EVM via py-clob-client."""
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.constants import POLYGON
            client = ClobClient(
                host=self.base_url,
                key=settings.POLYMARKET_PRIVATE_KEY,
                chain_id=POLYGON,
                funder=settings.POLYMARKET_FUNDER_ADDRESS,
            )
            creds = client.create_or_derive_api_creds()
            self._api_key = creds.api_key
            self._api_secret = creds.api_secret
            self._api_passphrase = creds.api_passphrase
            logger.info("Credentials API L2 dérivées avec succès")
        except Exception as e:
            logger.error(f"Impossible de dériver les credentials: {e}")
            raise

    async def _request(
        self,
        method: str,
        path: str,
        authenticated: bool = False,
        retries: int = 4,
        **kwargs,
    ) -> Any:
        """Requête HTTP avec retry exponentiel et rate limiting."""
        await self._request_limiter.acquire()
        url = f"{self.base_url}{path}"

        headers = {}
        if authenticated and self._api_key:
            # Authentification L2 Polymarket (HMAC signature)
            headers.update(self._build_auth_headers(method, path, kwargs.get("json")))

        for attempt in range(retries):
            try:
                async with self._session.request(
                    method, url, headers=headers, **kwargs
                ) as resp:
                    if resp.status == 429:
                        wait = 2 ** attempt
                        logger.warning(f"Rate limit 429 — attente {wait}s (tentative {attempt+1})")
                        await asyncio.sleep(wait)
                        continue
                    if resp.status in (500, 502, 503, 504):
                        wait = 2 ** attempt
                        logger.warning(f"Erreur serveur {resp.status} — retry dans {wait}s")
                        await asyncio.sleep(wait)
                        continue
                    if resp.status == 401:
                        raise PermissionError("Authentification Polymarket échouée — vérifiez les clés")
                    resp.raise_for_status()
                    return await resp.json()
            except aiohttp.ClientConnectorError as e:
                wait = 2 ** attempt
                logger.warning(f"Erreur connexion (tentative {attempt+1}/{retries}): {e} — retry dans {wait}s")
                if attempt < retries - 1:
                    await asyncio.sleep(wait)
                else:
                    raise

        raise RuntimeError(f"Requête {method} {path} échouée après {retries} tentatives")

    def _build_auth_headers(self, method: str, path: str, body: Any = None) -> Dict[str, str]:
        """Construit les headers d'authentification HMAC pour l'API L2."""
        import hashlib
        import hmac
        import base64
        import json

        timestamp = str(int(time.time() * 1000))
        body_str = json.dumps(body) if body else ""
        message = timestamp + method.upper() + path + body_str

        signature = hmac.new(
            self._api_secret.encode(),
            message.encode(),
            hashlib.sha256,
        ).digest()
        sig_b64 = base64.b64encode(signature).decode()

        return {
            "POLY-API-KEY": self._api_key,
            "POLY-SIGNATURE": sig_b64,
            "POLY-TIMESTAMP": timestamp,
            "POLY-PASSPHRASE": self._api_passphrase,
        }

    # -----------------------------------------------------------------------
    # Marchés
    # -----------------------------------------------------------------------

    async def get_markets(
        self, active_only: bool = True, limit: int = 500, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Récupère la liste des marchés actifs via l'API Gamma."""
        params = {"limit": limit, "offset": offset}
        if active_only:
            params["active"] = "true"
            params["closed"] = "false"

        async with self._session.get(
            f"{self.gamma_url}/markets",
            params=params,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data if isinstance(data, list) else data.get("markets", [])

    async def get_all_active_markets(self) -> List[Dict[str, Any]]:
        """Récupère tous les marchés actifs en paginant."""
        all_markets = []
        offset = 0
        limit = 500
        while True:
            batch = await self.get_markets(active_only=True, limit=limit, offset=offset)
            if not batch:
                break
            all_markets.extend(batch)
            if len(batch) < limit:
                break
            offset += limit
            await asyncio.sleep(0.5)  # Respecter le rate limit
        logger.info(f"Chargé {len(all_markets)} marchés actifs")
        return all_markets

    async def get_orderbook(self, token_id: str) -> Orderbook:
        """Récupère l'orderbook pour un token (YES ou NO)."""
        data = await self._request("GET", f"/book?token_id={token_id}")
        bids = [OrderbookLevel(float(b["price"]), float(b["size"])) for b in data.get("bids", [])]
        asks = [OrderbookLevel(float(a["price"]), float(a["size"])) for a in data.get("asks", [])]
        # Trier : bids décroissant, asks croissant
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)
        return Orderbook(
            market_id=data.get("market", ""),
            token_id=token_id,
            bids=bids,
            asks=asks,
        )

    async def get_orderbooks_batch(self, token_ids: List[str]) -> Dict[str, Orderbook]:
        """Récupère plusieurs orderbooks en parallèle."""
        tasks = [self.get_orderbook(tid) for tid in token_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        books = {}
        for tid, result in zip(token_ids, results):
            if isinstance(result, Exception):
                logger.debug(f"Orderbook error pour {tid}: {result}")
            else:
                books[tid] = result
        return books

    # -----------------------------------------------------------------------
    # Ordres
    # -----------------------------------------------------------------------

    async def place_order(self, order: Order) -> Order:
        """Place un ordre. En mode dry_run, simule l'exécution."""
        if self.dry_run:
            return await self._simulate_order(order)

        await self._order_limiter.acquire()

        payload = {
            "tokenID": order.token_id,
            "side": order.side.value,
            "price": str(round(order.price, 4)),
            "size": str(round(order.size, 2)),
            "orderType": order.order_type.value,
            "clientOrderID": order.client_order_id,
        }

        start_ts = time.time()
        try:
            resp = await self._request("POST", "/order", authenticated=True, json=payload)
            order.exchange_order_id = resp.get("orderID")
            order.status = OrderStatus.MATCHED
            latency_ms = (time.time() - start_ts) * 1000

            logger.info(
                "Ordre placé",
                extra={
                    "trade_id": order.client_order_id,
                    "market_id": order.market_id,
                    "token_id": order.token_id,
                    "side": order.side.value,
                    "price": order.price,
                    "size": order.size,
                    "latency_ms": round(latency_ms, 1),
                    "exchange_id": order.exchange_order_id,
                },
            )
        except Exception as e:
            order.status = OrderStatus.FAILED
            logger.error(f"Échec de l'ordre {order.client_order_id}: {e}")
            raise

        return order

    async def _simulate_order(self, order: Order) -> Order:
        """Simule l'exécution d'un ordre en dry-run."""
        await asyncio.sleep(0.05)  # Simuler la latence réseau
        order.status = OrderStatus.FILLED
        order.filled_size = order.size
        order.avg_fill_price = order.price
        order.exchange_order_id = f"DRY-{order.client_order_id[:8]}"

        logger.info(
            "[DRY-RUN] Ordre simulé",
            extra={
                "trade_id": order.client_order_id,
                "market_id": order.market_id,
                "side": order.side.value,
                "price": order.price,
                "size": order.size,
            },
        )
        return order

    async def cancel_order(self, order_id: str) -> bool:
        """Annule un ordre ouvert."""
        if self.dry_run:
            logger.info(f"[DRY-RUN] Annulation simulée: {order_id}")
            return True
        try:
            await self._request("DELETE", f"/order/{order_id}", authenticated=True)
            logger.info(f"Ordre {order_id} annulé")
            return True
        except Exception as e:
            logger.error(f"Erreur annulation {order_id}: {e}")
            return False

    async def get_open_orders(self) -> List[Dict[str, Any]]:
        """Récupère les ordres ouverts de ce compte."""
        if self.dry_run:
            return []
        return await self._request("GET", "/orders?status=live", authenticated=True)

    async def get_balance(self) -> float:
        """Récupère le solde USDC disponible."""
        if self.dry_run:
            return settings.MAX_TOTAL_EXPOSURE
        try:
            data = await self._request("GET", "/balance-allowance?asset_type=USDC", authenticated=True)
            return float(data.get("balance", 0))
        except Exception as e:
            logger.error(f"Impossible de récupérer le solde: {e}")
            return 0.0

    async def get_trades(self, market_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Récupère l'historique des trades."""
        if self.dry_run:
            return []
        path = "/trades"
        if market_id:
            path += f"?market={market_id}"
        return await self._request("GET", path, authenticated=True)
