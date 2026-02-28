"""
Point d'entrée principal du bot d'arbitrage Polymarket.
Lance tous les modules en parallèle via asyncio et gère le shutdown proprement.
"""

import asyncio
import signal
import sys
import time
from typing import List, Optional

from config import settings
from config.settings import RiskConfig, RISK_PRESETS
from core.order_executor import OrderExecutor
from core.polymarket_client import PolymarketClient
from core.websocket_manager import WebSocketManager
from dashboard.web_dashboard import Dashboard
from data.market_scanner import MarketScanner
from data.sports_feed import SportsDataFeed
from data.orderbook_cache import OrderbookCache
from risk.pnl_tracker import PnLTracker, TradeRecord
from risk.position_manager import PositionManager
from risk.risk_limits import RiskLimits
from strategies.binary_arbitrage import BinaryArbitrageStrategy
from strategies.multi_outcome_hedge import MultiOutcomeHedgeStrategy
from strategies.reality_arbitrage import RealityArbitrageStrategy
from utils.logger import get_logger, setup_logging
from utils.metrics import Metrics
from utils.notifications import TelegramNotifier

logger = get_logger(__name__)


class PolymarketBot:
    """
    Orchestrateur principal du bot d'arbitrage Polymarket.
    Coordonne tous les modules et gère le cycle de vie.
    """

    def __init__(self, risk_preset: Optional[str] = None):
        # Configuration de risque (depuis preset ou settings)
        if risk_preset and risk_preset in RISK_PRESETS:
            self.risk_config = RISK_PRESETS[risk_preset]
            logger.info(f"Configuration de risque: preset '{risk_preset}'")
        else:
            self.risk_config = RiskConfig()
            logger.info("Configuration de risque: valeurs par défaut (.env)")

        self._running = False
        self._started_at = time.time()
        self._tasks: List[asyncio.Task] = []

        # Initialisation des composants principaux
        self.client = PolymarketClient(dry_run=settings.DRY_RUN)
        self.executor = OrderExecutor(self.client)

        # Risk management
        self.risk_limits = RiskLimits(self.risk_config)
        self.position_manager = PositionManager()
        self.pnl_tracker = PnLTracker()
        self.pnl_tracker.set_starting_capital(self.risk_config.max_total_exposure)

        # Données de marché
        self.orderbook_cache = OrderbookCache(ttl_seconds=5.0)
        self.ws_manager = WebSocketManager(
            url=settings.WS_URL,
        )
        self.market_scanner = MarketScanner(
            client=self.client,
            ws_manager=self.ws_manager,
            orderbook_cache=self.orderbook_cache,
        )

        # Flux sportifs
        self.sports_feed = SportsDataFeed()

        # Stratégies
        self.strategies = []
        if settings.ENABLE_BINARY_ARB:
            self.binary_arb = BinaryArbitrageStrategy(
                client=self.client,
                executor=self.executor,
                risk_config=self.risk_config,
                orderbook_cache=self.orderbook_cache,
                risk_limits=self.risk_limits,
            )
            self.strategies.append(self.binary_arb)

        if settings.ENABLE_REALITY_ARB:
            self.reality_arb = RealityArbitrageStrategy(
                client=self.client,
                executor=self.executor,
                risk_config=self.risk_config,
                sports_feed=self.sports_feed,
                risk_limits=self.risk_limits,
            )
            self.strategies.append(self.reality_arb)

        if settings.ENABLE_MULTI_HEDGE:
            self.multi_hedge = MultiOutcomeHedgeStrategy(
                client=self.client,
                executor=self.executor,
                risk_config=self.risk_config,
                risk_limits=self.risk_limits,
            )
            self.strategies.append(self.multi_hedge)

        # Utilitaires
        self.notifier = TelegramNotifier()
        self.metrics = Metrics()

        # Dashboard web
        self.dashboard = Dashboard()
        self.dashboard.set_bot(self)

        logger.info(
            f"Bot initialisé — DRY_RUN={settings.DRY_RUN} | "
            f"Stratégies: {[s.name for s in self.strategies]}"
        )

    async def start(self) -> None:
        """Démarre tous les composants du bot."""
        self._running = True

        # Connexion à l'API Polymarket
        await self.client.connect()
        logger.info("Connexion API Polymarket établie")

        # Vérifier le solde si mode live
        if not settings.DRY_RUN:
            balance = await self.client.get_balance()
            logger.info(f"Solde USDC disponible: ${balance:.2f}")
            if balance < 10:
                logger.warning("Solde insuffisant pour trader!")

        # Démarrer le risk management
        await self.risk_limits.start()

        # Démarrer les WebSockets
        await self.ws_manager.start()
        logger.info("WebSocket Manager démarré")

        # Démarrer le scanner de marchés
        await self.market_scanner.start()

        # Démarrer le flux sportif
        if settings.ENABLE_REALITY_ARB:
            await self.sports_feed.start()

        # Démarrer les notifications
        await self.notifier.start()
        await self.notifier.notify_bot_started(settings.DRY_RUN, self.risk_config.max_total_exposure)

        # Démarrer le dashboard web
        await self.dashboard.start(settings.DASHBOARD_HOST, settings.DASHBOARD_PORT)

        # Lancer les stratégies en parallèle
        for strategy in self.strategies:
            task = asyncio.create_task(strategy.run(), name=f"strategy_{strategy.name}")
            self._tasks.append(task)

        # Tâches de monitoring
        self._tasks.append(asyncio.create_task(self._metrics_loop(), name="metrics"))
        self._tasks.append(asyncio.create_task(self._daily_report_loop(), name="daily_report"))
        self._tasks.append(asyncio.create_task(self.ws_manager.monitor_health(), name="ws_health"))

        logger.info(f"Bot démarré avec {len(self.strategies)} stratégie(s) actives")
        logger.info(f"Dashboard: http://localhost:{settings.DASHBOARD_PORT}")

        # Attendre l'arrêt
        await self._wait_for_shutdown()

    async def stop(self) -> None:
        """Arrête proprement tous les composants."""
        logger.info("Arrêt du bot en cours...")
        self._running = False

        # Arrêter les stratégies
        for strategy in self.strategies:
            await strategy.stop()

        # Annuler les tâches
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        # Arrêter les composants
        await self.ws_manager.stop()
        await self.market_scanner.stop()
        await self.sports_feed.stop()
        await self.risk_limits.stop()
        await self.client.close()

        # Rapport final
        report = self.pnl_tracker.get_full_report()
        logger.info(f"Rapport final: {report['summary']}")
        await self.notifier.notify_daily_summary(report)
        await self.notifier.stop()

        logger.info("Bot arrêté proprement")

    async def _wait_for_shutdown(self) -> None:
        """Attend l'arrêt (signal SIGINT/SIGTERM ou exception)."""
        try:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        except asyncio.CancelledError:
            pass

    async def _metrics_loop(self) -> None:
        """Met à jour les métriques toutes les 10 secondes."""
        while self._running:
            try:
                report = self.pnl_tracker.get_full_report()
                summary = report.get("summary", {})

                self.metrics.set_pnl(summary.get("cumulative_pnl", 0))
                self.metrics.set_open_positions(
                    self.position_manager.get_summary()["open_positions"]
                )
                self.metrics.set_exposure(self.risk_limits.status.total_exposure)
                ws_stats = self.ws_manager.get_stats()
                self.metrics.set_ws_connections(ws_stats["connected"])

            except Exception as e:
                logger.debug(f"Erreur metrics loop: {e}")

            await asyncio.sleep(10)

    async def _daily_report_loop(self) -> None:
        """Envoie un résumé journalier via Telegram."""
        while self._running:
            await asyncio.sleep(3600)  # Toutes les heures
            try:
                report = self.pnl_tracker.get_full_report()
                if report["summary"]["total_trades"] > 0:
                    await self.notifier.notify_daily_summary(report)
            except Exception as e:
                logger.debug(f"Erreur rapport journalier: {e}")

    def get_status(self) -> dict:
        """Retourne l'état actuel du bot."""
        return {
            "running": self._running,
            "dry_run": settings.DRY_RUN,
            "uptime_seconds": time.time() - self._started_at,
            "strategies": [s.get_stats() for s in self.strategies],
            "risk": self.risk_limits.get_summary(),
            "pnl": self.pnl_tracker.get_full_report(),
            "positions": self.position_manager.get_summary(),
            "ws": self.ws_manager.get_stats(),
        }


def setup_signal_handlers(bot: PolymarketBot, loop: asyncio.AbstractEventLoop) -> None:
    """Configure les handlers pour SIGINT (Ctrl+C) et SIGTERM."""
    def handle_shutdown(sig):
        logger.info(f"Signal {sig.name} reçu — arrêt propre en cours...")
        loop.create_task(bot.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig: handle_shutdown(s))
        except NotImplementedError:
            # Windows ne supporte pas add_signal_handler
            pass


async def run_bot(risk_preset: Optional[str] = None) -> None:
    """Lance le bot avec le preset de risque spécifié."""
    bot = PolymarketBot(risk_preset=risk_preset)

    loop = asyncio.get_event_loop()
    setup_signal_handlers(bot, loop)

    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("Interruption clavier détectée")
    except Exception as e:
        logger.critical(f"Erreur fatale: {e}", exc_info=True)
    finally:
        if bot._running:
            await bot.stop()


def main():
    """Point d'entrée CLI."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Polymarket Arbitrage Bot — inspiré de @swisstony"
    )
    parser.add_argument(
        "--preset",
        choices=list(RISK_PRESETS.keys()),
        default=None,
        help="Preset de configuration de risque",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Forcer le mode dry-run (simulation)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Mode live (ATTENTION: utilise de vrais fonds!)",
    )
    parser.add_argument(
        "--log-level",
        default=settings.LOG_LEVEL,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    # Override DRY_RUN si spécifié
    if args.live:
        settings.DRY_RUN = False
        print("⚠️  MODE LIVE ACTIVÉ — Les fonds réels seront utilisés!")
    elif args.dry_run:
        settings.DRY_RUN = True

    # Setup logging
    setup_logging(args.log_level)

    print(f"""
╔══════════════════════════════════════════════════╗
║     POLYMARKET ARBITRAGE BOT v1.0                ║
║     Inspiré de la stratégie @swisstony           ║
╠══════════════════════════════════════════════════╣
║  Mode: {'DRY-RUN (simulation)' if settings.DRY_RUN else '🔴 LIVE (fonds réels)'}
║  Preset: {args.preset or 'défaut (.env)'}
║  Dashboard: http://localhost:{settings.DASHBOARD_PORT}
╚══════════════════════════════════════════════════╝
""")

    if not settings.DRY_RUN and not settings.POLYMARKET_PRIVATE_KEY:
        print("❌ ERREUR: POLYMARKET_PRIVATE_KEY manquante dans .env")
        sys.exit(1)

    # Lancer le bot
    asyncio.run(run_bot(risk_preset=args.preset))


if __name__ == "__main__":
    main()
