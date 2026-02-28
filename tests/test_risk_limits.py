"""
Tests unitaires pour le module de gestion des risques.
"""

import asyncio
import unittest

from config.settings import RiskConfig
from risk.risk_limits import RiskLimits


def make_config(**kwargs) -> RiskConfig:
    defaults = {
        "max_total_exposure": 10000.0,
        "max_per_market": 1000.0,
        "max_per_trade": 500.0,
        "daily_loss_limit": 500.0,
        "min_profit_threshold": 0.50,
        "kelly_fraction": 0.25,
        "max_open_positions": 5,
    }
    defaults.update(kwargs)
    return RiskConfig(**defaults)


class TestRiskLimits(unittest.IsolatedAsyncioTestCase):

    async def test_trade_allowed_within_limits(self):
        """Un trade dans les limites est autorisé."""
        limits = RiskLimits(make_config())
        result = await limits.can_trade("market_1", 100.0)
        self.assertTrue(result)

    async def test_trade_blocked_when_halted(self):
        """Aucun trade ne passe quand le bot est en pause."""
        limits = RiskLimits(make_config())
        limits._halt("test halt")
        result = await limits.can_trade("market_1", 100.0)
        self.assertFalse(result)

    async def test_trade_blocked_exceeds_total_exposure(self):
        """Trade bloqué si l'exposition totale serait dépassée."""
        config = make_config(max_total_exposure=1000.0)
        limits = RiskLimits(config)
        # Simuler une exposition existante
        limits._status.total_exposure = 950.0
        result = await limits.can_trade("market_1", 100.0)  # 950+100 > 1000
        self.assertFalse(result)

    async def test_trade_blocked_exceeds_per_market(self):
        """Trade bloqué si l'exposition par marché serait dépassée."""
        config = make_config(max_per_market=500.0)
        limits = RiskLimits(config)
        limits._exposure_by_market["market_1"] = 450.0
        result = await limits.can_trade("market_1", 100.0)  # 450+100 > 500
        self.assertFalse(result)

    async def test_trade_blocked_exceeds_per_trade(self):
        """Trade bloqué si la taille dépasse max_per_trade."""
        config = make_config(max_per_trade=500.0)
        limits = RiskLimits(config)
        result = await limits.can_trade("market_1", 600.0)
        self.assertFalse(result)

    async def test_trade_blocked_max_positions(self):
        """Trade bloqué si le nombre max de positions est atteint."""
        config = make_config(max_open_positions=3)
        limits = RiskLimits(config)
        limits._status.open_positions = 3
        result = await limits.can_trade("market_1", 100.0)
        self.assertFalse(result)

    async def test_daily_loss_triggers_halt(self):
        """Le bot se met en pause quand la perte quotidienne est atteinte."""
        config = make_config(daily_loss_limit=100.0)
        limits = RiskLimits(config)
        # Simuler une perte journalière au-dessus du seuil
        limits._status.daily_pnl = -110.0
        result = await limits.can_trade("market_1", 10.0)
        self.assertFalse(result)
        self.assertTrue(limits._status.is_halted)

    async def test_record_trade_updates_exposure(self):
        """L'enregistrement d'un trade met à jour l'exposition."""
        limits = RiskLimits(make_config())
        await limits.record_trade("market_1", 200.0, 5.0)
        self.assertEqual(limits._status.total_exposure, 200.0)
        self.assertEqual(limits._exposure_by_market["market_1"], 200.0)
        self.assertEqual(limits._status.open_positions, 1)

    async def test_close_position_reduces_exposure(self):
        """La fermeture d'une position réduit l'exposition."""
        limits = RiskLimits(make_config())
        await limits.record_trade("market_1", 200.0, 5.0)
        await limits.close_position("market_1", 200.0, 5.0)
        self.assertEqual(limits._status.total_exposure, 0.0)
        self.assertEqual(limits._status.open_positions, 0)

    async def test_resume_clears_halt(self):
        """La reprise manuelle débloque le bot."""
        limits = RiskLimits(make_config())
        limits._halt("test")
        self.assertTrue(limits._status.is_halted)
        limits.resume()
        self.assertFalse(limits._status.is_halted)
        # Vérifier que les trades passent à nouveau
        result = await limits.can_trade("market_1", 100.0)
        self.assertTrue(result)

    async def test_get_summary(self):
        """get_summary retourne les bonnes données."""
        config = make_config(max_total_exposure=1000.0)
        limits = RiskLimits(config)
        await limits.record_trade("market_1", 200.0, 5.0)

        summary = limits.get_summary()
        self.assertEqual(summary["total_exposure"], 200.0)
        self.assertEqual(summary["daily_pnl"], 5.0)
        self.assertEqual(summary["open_positions"], 1)
        self.assertFalse(summary["is_halted"])
        self.assertEqual(summary["pct_exposure_used"], 20.0)  # 200/1000 = 20%


if __name__ == "__main__":
    unittest.main(verbosity=2)
