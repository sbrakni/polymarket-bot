"""
Alertes Telegram pour les événements importants du bot.
Envoie des notifications pour les trades, erreurs, et résumés journaliers.
"""

import asyncio
import time
from typing import Any, Dict, Optional

import aiohttp

from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)


class TelegramNotifier:
    """
    Envoi de notifications via l'API Telegram Bot.
    Gère le rate limiting (1 message/seconde max pour Telegram).
    """

    TELEGRAM_API = "https://api.telegram.org"
    MIN_INTERVAL = 1.0  # Secondes entre les messages

    def __init__(
        self,
        bot_token: str = "",
        chat_id: str = "",
    ):
        self.bot_token = bot_token or settings.TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id or settings.TELEGRAM_CHAT_ID
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_send = 0.0
        self._message_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._running = False
        self._enabled = bool(self.bot_token and self.chat_id)

        if not self._enabled:
            logger.info("Notifications Telegram désactivées (clés manquantes)")

    async def start(self) -> None:
        if not self._enabled:
            return
        self._session = aiohttp.ClientSession()
        self._running = True
        asyncio.create_task(self._send_loop())
        logger.info("Notificateur Telegram démarré")

    async def stop(self) -> None:
        self._running = False
        if self._session:
            await self._session.close()

    async def _send_loop(self) -> None:
        """Traite la file d'attente des messages à envoyer."""
        while self._running:
            try:
                message = await asyncio.wait_for(self._message_queue.get(), timeout=1.0)
                await self._send_direct(message)
                await asyncio.sleep(self.MIN_INTERVAL)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.debug(f"Erreur envoi Telegram: {e}")

    async def _send_direct(self, text: str) -> bool:
        """Envoie directement un message Telegram."""
        if not self._session or not self._enabled:
            return False

        url = f"{self.TELEGRAM_API}/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            async with self._session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return True
                elif resp.status == 429:
                    logger.warning("Telegram rate limit — pause 30s")
                    await asyncio.sleep(30)
                else:
                    logger.warning(f"Telegram erreur {resp.status}")
        except Exception as e:
            logger.debug(f"Telegram send error: {e}")
        return False

    async def send(self, text: str) -> None:
        """Ajoute un message à la file d'attente."""
        if not self._enabled:
            return
        try:
            self._message_queue.put_nowait(text)
        except asyncio.QueueFull:
            logger.debug("File Telegram pleine — message ignoré")

    # -----------------------------------------------------------------------
    # Messages spécialisés
    # -----------------------------------------------------------------------

    async def notify_trade(
        self,
        strategy: str,
        market: str,
        description: str,
        profit: float,
        latency_ms: float = 0,
        dry_run: bool = False,
    ) -> None:
        """Notification pour un trade exécuté."""
        prefix = "🧪 <i>[DRY-RUN]</i> " if dry_run else "✅ "
        sign = "+" if profit >= 0 else ""
        text = (
            f"{prefix}<b>Trade exécuté</b>\n"
            f"📊 Stratégie: <code>{strategy}</code>\n"
            f"🏪 Marché: {market[:60]}\n"
            f"📝 {description[:100]}\n"
            f"💰 Profit: <b>{sign}${profit:.2f}</b>\n"
            f"⚡ Latence: {latency_ms:.0f}ms"
        )
        await self.send(text)

    async def notify_opportunity(
        self,
        strategy: str,
        market: str,
        profit: float,
        reason: str = "",
    ) -> None:
        """Notification pour une opportunité détectée (non exécutée)."""
        text = (
            f"👀 <b>Opportunité détectée</b>\n"
            f"📊 Stratégie: <code>{strategy}</code>\n"
            f"🏪 Marché: {market[:60]}\n"
            f"💰 Profit potentiel: <b>${profit:.2f}</b>\n"
            f"ℹ️ {reason[:100]}"
        )
        await self.send(text)

    async def notify_error(self, component: str, error: str) -> None:
        """Notification pour une erreur critique."""
        text = (
            f"🚨 <b>Erreur critique</b>\n"
            f"🔧 Composant: <code>{component}</code>\n"
            f"❌ {error[:200]}"
        )
        await self.send(text)

    async def notify_risk_halt(self, reason: str) -> None:
        """Notification quand le bot est stoppé par les limites de risque."""
        text = (
            f"🛑 <b>BOT EN PAUSE</b>\n"
            f"Raison: {reason}\n"
            f"Action: vérifiez les paramètres de risque"
        )
        await self.send(text)

    async def notify_daily_summary(self, report: Dict[str, Any]) -> None:
        """Résumé journalier envoyé chaque soir."""
        summary = report.get("summary", {})
        risk = report.get("risk_metrics", {})

        profit = summary.get("cumulative_pnl", 0)
        trades = summary.get("total_trades", 0)
        win_rate = summary.get("win_rate", 0)
        sharpe = risk.get("sharpe_ratio", 0)
        drawdown = risk.get("max_drawdown", 0)

        sign = "📈" if profit >= 0 else "📉"
        text = (
            f"{sign} <b>Résumé journalier</b>\n"
            f"{'─' * 30}\n"
            f"💰 P&L total: <b>${profit:+.2f}</b>\n"
            f"📊 Trades: {trades} | Win rate: {win_rate:.1f}%\n"
            f"📉 Max drawdown: ${drawdown:.2f}\n"
            f"📐 Sharpe: {sharpe:.2f}\n"
        )

        by_strategy = report.get("by_strategy", {}).get("pnl", {})
        if by_strategy:
            text += "\n<b>Par stratégie:</b>\n"
            for strat, pnl in by_strategy.items():
                emoji = "✅" if pnl >= 0 else "❌"
                text += f"  {emoji} {strat}: ${pnl:+.2f}\n"

        await self.send(text)

    async def notify_bot_started(self, dry_run: bool, capital: float) -> None:
        """Notification au démarrage du bot."""
        mode = "🧪 DRY-RUN" if dry_run else "🔴 LIVE"
        text = (
            f"🤖 <b>Bot démarré</b>\n"
            f"Mode: {mode}\n"
            f"Capital: ${capital:.0f}\n"
            f"Heure: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}"
        )
        await self.send(text)
