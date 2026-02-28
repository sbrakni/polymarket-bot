"""
Cache local des orderbooks Polymarket.
Évite les appels API répétitifs et permet un accès ultra-rapide aux données.
"""

import time
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, List, Optional

from core.polymarket_client import Orderbook, OrderbookLevel


class OrderbookCache:
    """
    Cache thread-safe des orderbooks avec TTL configurable.
    Mis à jour par le WebSocket en temps réel, avec fallback API.
    """

    def __init__(self, ttl_seconds: float = 5.0):
        self.ttl = ttl_seconds
        self._cache: Dict[str, Orderbook] = {}
        self._timestamps: Dict[str, float] = {}
        self._lock = Lock()
        self._hit_count = 0
        self._miss_count = 0

    def get(self, token_id: str) -> Optional[Orderbook]:
        """Récupère un orderbook du cache si non expiré."""
        with self._lock:
            if token_id not in self._cache:
                self._miss_count += 1
                return None

            age = time.time() - self._timestamps.get(token_id, 0)
            if age > self.ttl:
                self._miss_count += 1
                return None

            self._hit_count += 1
            return self._cache[token_id]

    def update(self, token_id: str, book: Orderbook) -> None:
        """Met à jour l'entrée de cache pour un token."""
        with self._lock:
            self._cache[token_id] = book
            self._timestamps[token_id] = time.time()

    def update_from_ws(self, token_id: str, bids: List[Dict], asks: List[Dict]) -> None:
        """
        Met à jour le cache à partir d'une update WebSocket.
        Format: [{"price": float, "size": float}, ...]
        """
        with self._lock:
            existing = self._cache.get(token_id)
            market_id = existing.market_id if existing else ""

            new_bids = sorted(
                [OrderbookLevel(float(b["price"]), float(b["size"])) for b in bids if float(b["size"]) > 0],
                key=lambda x: x.price, reverse=True
            )
            new_asks = sorted(
                [OrderbookLevel(float(a["price"]), float(a["size"])) for a in asks if float(a["size"]) > 0],
                key=lambda x: x.price
            )

            self._cache[token_id] = Orderbook(
                market_id=market_id,
                token_id=token_id,
                bids=new_bids,
                asks=new_asks,
                timestamp=time.time(),
            )
            self._timestamps[token_id] = time.time()

    def update_price(self, token_id: str, price: float, size: float, side: str) -> None:
        """Met à jour un niveau de prix unique dans le cache (depuis un trade WS)."""
        with self._lock:
            existing = self._cache.get(token_id)
            if not existing:
                return

            if side.upper() == "SELL":
                # Mise à jour du côté ask
                asks = [a for a in existing.asks if a.price != price]
                if size > 0:
                    asks.append(OrderbookLevel(price, size))
                    asks.sort(key=lambda x: x.price)
                existing.asks = asks
            else:
                # Mise à jour du côté bid
                bids = [b for b in existing.bids if b.price != price]
                if size > 0:
                    bids.append(OrderbookLevel(price, size))
                    bids.sort(key=lambda x: x.price, reverse=True)
                existing.bids = bids

            existing.timestamp = time.time()
            self._timestamps[token_id] = time.time()

    def invalidate(self, token_id: str) -> None:
        """Invalide une entrée du cache."""
        with self._lock:
            self._cache.pop(token_id, None)
            self._timestamps.pop(token_id, None)

    def clear_expired(self) -> int:
        """Supprime les entrées expirées. Retourne le nombre de supprimées."""
        now = time.time()
        expired = []
        with self._lock:
            for token_id, ts in self._timestamps.items():
                if now - ts > self.ttl * 3:  # Grace period 3x le TTL
                    expired.append(token_id)
            for token_id in expired:
                self._cache.pop(token_id, None)
                self._timestamps.pop(token_id, None)
        return len(expired)

    def get_best_prices(self, token_id: str) -> Optional[tuple]:
        """Raccourci pour récupérer (best_bid, best_ask) rapidement."""
        book = self.get(token_id)
        if not book:
            return None
        return (book.best_bid, book.best_ask)

    def size(self) -> int:
        return len(self._cache)

    def hit_rate(self) -> float:
        total = self._hit_count + self._miss_count
        if total == 0:
            return 0.0
        return self._hit_count / total

    def stats(self) -> Dict:
        return {
            "size": self.size(),
            "hit_count": self._hit_count,
            "miss_count": self._miss_count,
            "hit_rate": round(self.hit_rate(), 3),
            "ttl_seconds": self.ttl,
        }
