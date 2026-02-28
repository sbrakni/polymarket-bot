"""
Gestionnaire des positions ouvertes.
Suit toutes les positions en cours et calcule le P&L non réalisé.
"""

import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class Position:
    """Une position ouverte sur un marché Polymarket."""
    position_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    market_id: str = ""
    question: str = ""
    strategy: str = ""

    # Positions YES/NO (pour l'arbitrage binaire, les deux sont non-nuls)
    size_yes: float = 0.0
    cost_yes: float = 0.0
    size_no: float = 0.0
    cost_no: float = 0.0

    # Pour les positions simples (Reality Arb)
    side: str = "YES"  # "YES" ou "NO"
    size: float = 0.0
    entry_price: float = 0.0
    current_price: float = 0.0

    # Métriques
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    is_closed: bool = False
    opened_at: float = field(default_factory=time.time)
    closed_at: Optional[float] = None

    # Pour les arbitrages: profit garanti à résolution
    guaranteed_profit: float = 0.0  # Pour les arb binaires parfaits

    @property
    def total_cost(self) -> float:
        if self.cost_yes or self.cost_no:
            return self.cost_yes + self.cost_no
        return self.size * self.entry_price

    @property
    def age_hours(self) -> float:
        return (time.time() - self.opened_at) / 3600

    def update_current_price(self, price_yes: float = 0.0, price_no: float = 0.0) -> None:
        """Met à jour les prix actuels et recalcule le P&L non réalisé."""
        if self.size_yes > 0 and self.size_no > 0:
            # Arbitrage binaire: valeur actuelle = taille × (prix_yes + prix_no)
            current_value = self.size_yes * price_yes + self.size_no * price_no
            self.unrealized_pnl = current_value - self.total_cost
        elif self.size > 0:
            self.current_price = price_yes if self.side == "YES" else price_no
            self.unrealized_pnl = self.size * (self.current_price - self.entry_price)

    def close(self, realized_pnl: float) -> None:
        """Ferme la position avec le P&L réalisé."""
        self.realized_pnl = realized_pnl
        self.is_closed = True
        self.closed_at = time.time()
        self.unrealized_pnl = 0.0


class PositionManager:
    """
    Gestionnaire central de toutes les positions ouvertes.
    Maintient un registre des positions et calcule les métriques agrégées.
    """

    def __init__(self):
        self._positions: Dict[str, Position] = {}  # position_id -> Position
        self._market_positions: Dict[str, List[str]] = {}  # market_id -> [position_ids]

    def open_position(
        self,
        market_id: str,
        question: str,
        strategy: str,
        size_yes: float = 0.0,
        cost_yes: float = 0.0,
        size_no: float = 0.0,
        cost_no: float = 0.0,
        side: str = "YES",
        size: float = 0.0,
        entry_price: float = 0.0,
        guaranteed_profit: float = 0.0,
    ) -> Position:
        """Ouvre et enregistre une nouvelle position."""
        pos = Position(
            market_id=market_id,
            question=question,
            strategy=strategy,
            size_yes=size_yes,
            cost_yes=cost_yes,
            size_no=size_no,
            cost_no=cost_no,
            side=side,
            size=size,
            entry_price=entry_price,
            guaranteed_profit=guaranteed_profit,
        )

        self._positions[pos.position_id] = pos

        if market_id not in self._market_positions:
            self._market_positions[market_id] = []
        self._market_positions[market_id].append(pos.position_id)

        logger.info(
            f"Position ouverte [{pos.position_id}]: {strategy} sur {market_id[:16]} | "
            f"Profit garanti: ${guaranteed_profit:.2f}"
        )
        return pos

    def close_position(self, position_id: str, realized_pnl: float) -> Optional[Position]:
        """Ferme une position avec le P&L réalisé."""
        pos = self._positions.get(position_id)
        if not pos:
            logger.warning(f"Position {position_id} introuvable")
            return None
        pos.close(realized_pnl)
        logger.info(
            f"Position fermée [{position_id}]: PnL = ${realized_pnl:.2f} | "
            f"Durée: {pos.age_hours:.1f}h"
        )
        return pos

    def get_open_positions(self) -> List[Position]:
        return [p for p in self._positions.values() if not p.is_closed]

    def get_positions_for_market(self, market_id: str) -> List[Position]:
        ids = self._market_positions.get(market_id, [])
        return [self._positions[i] for i in ids if i in self._positions]

    def get_all_positions(self, include_closed: bool = False) -> List[Position]:
        if include_closed:
            return list(self._positions.values())
        return self.get_open_positions()

    def total_unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.get_open_positions())

    def total_realized_pnl(self) -> float:
        return sum(p.realized_pnl for p in self._positions.values() if p.is_closed)

    def total_guaranteed_profit(self) -> float:
        """Profit total garanti (pour les arb binaires déjà exécutés)."""
        return sum(p.guaranteed_profit for p in self.get_open_positions())

    def total_exposure(self) -> float:
        return sum(p.total_cost for p in self.get_open_positions())

    def get_summary(self) -> Dict:
        open_pos = self.get_open_positions()
        closed_pos = [p for p in self._positions.values() if p.is_closed]

        return {
            "open_positions": len(open_pos),
            "closed_positions": len(closed_pos),
            "total_exposure": round(self.total_exposure(), 2),
            "unrealized_pnl": round(self.total_unrealized_pnl(), 2),
            "realized_pnl": round(self.total_realized_pnl(), 2),
            "guaranteed_profit": round(self.total_guaranteed_profit(), 2),
            "by_strategy": self._positions_by_strategy(),
        }

    def _positions_by_strategy(self) -> Dict[str, int]:
        counts = {}
        for pos in self.get_open_positions():
            counts[pos.strategy] = counts.get(pos.strategy, 0) + 1
        return counts
