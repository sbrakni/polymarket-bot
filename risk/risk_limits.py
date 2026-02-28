"""
Gestion des limites de risque — garde-fou contre les pertes catastrophiques.
Toutes les décisions de trading passent par ce module avant exécution.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from config.settings import RiskConfig
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class RiskStatus:
    """État actuel des risques du bot."""
    total_exposure: float = 0.0
    daily_pnl: float = 0.0
    open_positions: int = 0
    daily_trades: int = 0
    is_halted: bool = False
    halt_reason: Optional[str] = None
    last_reset: float = field(default_factory=time.time)


class RiskLimits:
    """
    Vérificateur de limites de risque.
    Vérifié AVANT chaque trade — si une limite est dépassée, le trade est refusé.
    Peut mettre le bot en pause si les pertes quotidiennes dépassent le seuil.
    """

    def __init__(self, config: RiskConfig):
        self.config = config
        self._status = RiskStatus()
        self._exposure_by_market: Dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._daily_reset_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Démarre le reset automatique des métriques quotidiennes."""
        self._daily_reset_task = asyncio.create_task(self._daily_reset_loop())

    async def stop(self) -> None:
        if self._daily_reset_task:
            self._daily_reset_task.cancel()

    async def _daily_reset_loop(self) -> None:
        """Remet à zéro les métriques quotidiennes à minuit UTC."""
        while True:
            import datetime
            now = datetime.datetime.utcnow()
            # Calculer les secondes jusqu'à minuit UTC
            midnight = (now + datetime.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            seconds_until_midnight = (midnight - now).total_seconds()
            await asyncio.sleep(seconds_until_midnight)
            await self._reset_daily()

    async def _reset_daily(self) -> None:
        """Remet à zéro les compteurs journaliers."""
        async with self._lock:
            old_pnl = self._status.daily_pnl
            self._status.daily_pnl = 0.0
            self._status.daily_trades = 0
            self._status.last_reset = time.time()
            # Réactiver le bot si halté pour limite journalière
            if self._status.is_halted and "daily" in (self._status.halt_reason or ""):
                self._status.is_halted = False
                self._status.halt_reason = None
                logger.info("Bot réactivé après reset journalier")
            logger.info(f"Reset journalier: PnL hier = ${old_pnl:.2f}")

    async def can_trade(self, market_id: str, trade_size: float) -> bool:
        """
        Vérifie toutes les limites de risque avant un trade.
        Retourne True si le trade est autorisé, False sinon.
        """
        async with self._lock:
            # 1. Bot en pause?
            if self._status.is_halted:
                logger.warning(f"Trade refusé — bot en pause: {self._status.halt_reason}")
                return False

            # 2. Perte journalière maximale atteinte?
            if self._status.daily_pnl <= -self.config.daily_loss_limit:
                self._halt(f"Perte journalière de ${abs(self._status.daily_pnl):.2f} dépassée")
                return False

            # 3. Exposition totale maximale?
            if self._status.total_exposure + trade_size > self.config.max_total_exposure:
                logger.warning(
                    f"Trade refusé — exposition totale: "
                    f"${self._status.total_exposure:.0f} + ${trade_size:.0f} > "
                    f"${self.config.max_total_exposure:.0f}"
                )
                return False

            # 4. Exposition par marché?
            market_exposure = self._exposure_by_market.get(market_id, 0.0)
            if market_exposure + trade_size > self.config.max_per_market:
                logger.debug(
                    f"Trade refusé — exposition marché {market_id[:16]}: "
                    f"${market_exposure:.0f} + ${trade_size:.0f} > ${self.config.max_per_market:.0f}"
                )
                return False

            # 5. Taille par trade?
            if trade_size > self.config.max_per_trade:
                logger.warning(
                    f"Trade refusé — taille ${trade_size:.0f} > max ${self.config.max_per_trade:.0f}"
                )
                return False

            # 6. Nombre de positions ouvertes?
            if self._status.open_positions >= self.config.max_open_positions:
                logger.warning(
                    f"Trade refusé — {self._status.open_positions} positions ouvertes "
                    f"(max: {self.config.max_open_positions})"
                )
                return False

            return True

    async def record_trade(
        self, market_id: str, trade_size: float, profit: float
    ) -> None:
        """Enregistre un trade exécuté et met à jour les métriques de risque."""
        async with self._lock:
            self._status.total_exposure += trade_size
            self._status.daily_pnl += profit
            self._status.daily_trades += 1
            self._status.open_positions += 1

            if market_id not in self._exposure_by_market:
                self._exposure_by_market[market_id] = 0.0
            self._exposure_by_market[market_id] += trade_size

            # Vérifier après coup si les pertes dépassent la limite
            if self._status.daily_pnl <= -self.config.daily_loss_limit:
                self._halt(
                    f"daily_loss_limit: PnL journalier = ${self._status.daily_pnl:.2f}"
                )

    async def close_position(self, market_id: str, trade_size: float, pnl: float) -> None:
        """Enregistre la fermeture d'une position."""
        async with self._lock:
            self._status.total_exposure = max(0.0, self._status.total_exposure - trade_size)
            self._status.daily_pnl += pnl
            self._status.open_positions = max(0, self._status.open_positions - 1)

            market_exp = self._exposure_by_market.get(market_id, 0.0)
            self._exposure_by_market[market_id] = max(0.0, market_exp - trade_size)

    def _halt(self, reason: str) -> None:
        """Met le bot en pause avec une raison."""
        if not self._status.is_halted:
            self._status.is_halted = True
            self._status.halt_reason = reason
            logger.critical(f"🛑 BOT EN PAUSE: {reason}")

    def resume(self) -> None:
        """Réactive le bot manuellement."""
        self._status.is_halted = False
        self._status.halt_reason = None
        logger.info("Bot réactivé manuellement")

    def update_config(self, new_config: RiskConfig) -> None:
        """Met à jour la configuration de risque à chaud."""
        self.config = new_config
        logger.info(f"Configuration de risque mise à jour: max_exposure=${new_config.max_total_exposure}")

    @property
    def status(self) -> RiskStatus:
        return self._status

    def get_summary(self) -> Dict:
        return {
            "total_exposure": round(self._status.total_exposure, 2),
            "daily_pnl": round(self._status.daily_pnl, 2),
            "daily_pnl_limit": self.config.daily_loss_limit,
            "open_positions": self._status.open_positions,
            "max_positions": self.config.max_open_positions,
            "daily_trades": self._status.daily_trades,
            "is_halted": self._status.is_halted,
            "halt_reason": self._status.halt_reason,
            "pct_exposure_used": round(
                (self._status.total_exposure / max(self.config.max_total_exposure, 1)) * 100, 1
            ),
        }
