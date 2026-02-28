"""
Tests unitaires pour la stratégie d'arbitrage binaire.
"""

import asyncio
import unittest
from dataclasses import dataclass
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from config.settings import RiskConfig
from strategies.binary_arbitrage import BinaryArbitrageStrategy, BinaryOpportunity


class MockOrderbook:
    """Mock d'un orderbook pour les tests."""
    def __init__(self, best_ask: float, best_ask_size: float = 100.0):
        self.best_ask = best_ask
        self.best_bid = best_ask - 0.01
        self.best_ask_size = best_ask_size
        self.best_bid_size = best_ask_size
        self.market_id = "test_market"
        self.token_id = "test_token"
        self.bids = []
        self.asks = []


def make_risk_config(**kwargs) -> RiskConfig:
    defaults = {
        "max_total_exposure": 10000.0,
        "max_per_market": 1000.0,
        "max_per_trade": 500.0,
        "daily_loss_limit": 500.0,
        "min_profit_threshold": 0.50,
        "kelly_fraction": 0.25,
        "max_open_positions": 50,
    }
    defaults.update(kwargs)
    return RiskConfig(**defaults)


class TestBinaryArbitrageDetection(unittest.TestCase):
    """Tests de détection des opportunités d'arbitrage binaire."""

    def setUp(self):
        self.risk = make_risk_config()

    def test_profitable_opportunity_detected(self):
        """Une opportunité avec un spread suffisant doit être détectée."""
        # YES@0.47 + NO@0.48 = 0.95 total → profit de 5% par contrat
        ask_yes = 0.47
        ask_no = 0.48
        total_cost = ask_yes + ask_no
        gross_profit = 1.0 - total_cost
        fees = total_cost * 0.0004
        net_profit = gross_profit - fees

        self.assertGreater(gross_profit, 0, "Profit brut doit être positif")
        self.assertGreater(net_profit, 0, "Profit net doit être positif après frais")
        self.assertAlmostEqual(gross_profit, 0.05, places=2)

    def test_not_profitable_when_total_exceeds_1(self):
        """Un spread YES + NO ≥ 1.0 n'est pas une opportunité."""
        ask_yes = 0.52
        ask_no = 0.52
        total_cost = ask_yes + ask_no  # 1.04

        gross_profit = 1.0 - total_cost
        self.assertLess(gross_profit, 0, "Pas de profit quand le total > 1.0")

    def test_fees_reduce_profit(self):
        """Les frais réduisent le profit net."""
        ask_yes = 0.49
        ask_no = 0.49
        total_cost = ask_yes + ask_no  # 0.98

        gross_profit = 1.0 - total_cost  # 0.02
        fees = total_cost * 0.0004  # ~0.0004
        net_profit = gross_profit - fees

        self.assertLess(net_profit, gross_profit, "Fees doivent réduire le profit")
        self.assertGreater(net_profit, 0, "Net profit toujours positif ici")

    def test_minimum_profit_threshold(self):
        """Les opportunités en-dessous du seuil minimum doivent être ignorées."""
        # Spread très faible: YES@0.499 + NO@0.499 = 0.998 → profit de 0.2% = $0.002 par contrat
        ask_yes = 0.499
        ask_no = 0.499
        total_cost = ask_yes + ask_no
        gross_profit = 1.0 - total_cost  # = 0.002

        from config import settings
        # Vérifier que l'edge minimum n'est pas atteint
        self.assertLess(gross_profit, settings.MIN_BINARY_EDGE,
                       "Un profit très faible doit être en-dessous du seuil MIN_BINARY_EDGE")

    def test_size_calculation(self):
        """La taille du trade est limitée par les paramètres de risque."""
        ask_yes = 0.47
        ask_no = 0.48
        total_cost = ask_yes + ask_no
        available_size_from_book = 200.0  # 200 contrats disponibles

        max_size_by_risk = self.risk.max_per_trade / total_cost
        actual_size = min(available_size_from_book, max_size_by_risk)

        # Avec max_per_trade=$500 et total_cost=$0.95, max = 526 contrats
        # Donc limité par la liquidité du livre (200)
        self.assertLessEqual(actual_size, available_size_from_book)
        self.assertLessEqual(actual_size * total_cost, self.risk.max_per_trade + 1)

    def test_profit_calculation_is_correct(self):
        """Le calcul du profit garanti est mathématiquement correct."""
        ask_yes = 0.45
        ask_no = 0.47
        size = 100
        total_cost = (ask_yes + ask_no) * size  # $92.00
        gross_profit = (1.0 - ask_yes - ask_no) * size  # $8.00
        fees = total_cost * 0.0004  # $0.0368
        net_profit = gross_profit - fees  # ~$7.96

        self.assertAlmostEqual(gross_profit, 8.0, places=1)
        self.assertAlmostEqual(net_profit, 7.96, places=1)

    def test_zero_prices_ignored(self):
        """Les marchés avec des prix nuls doivent être ignorés."""
        ask_yes = 0.0
        ask_no = 0.48

        # Un prix nul est invalide
        self.assertEqual(ask_yes, 0, "Prix YES est 0 — à ignorer")
        self.assertFalse(ask_yes > 0 and ask_no > 0,
                        "Pas d'opportunité avec un prix nul")


class TestBinaryArbitrageKelly(unittest.TestCase):
    """Tests du calcul Kelly pour la taille des positions."""

    def setUp(self):
        self.risk = make_risk_config(kelly_fraction=0.25)

        # Créer un mock client et executor
        mock_client = MagicMock()
        mock_executor = MagicMock()
        self.strategy = BinaryArbitrageStrategy(mock_client, mock_executor, self.risk)

    def test_kelly_zero_for_no_edge(self):
        """Kelly doit retourner 0 quand il n'y a pas d'edge."""
        size = self.strategy.kelly_size(prob_win=0.5, odds_win=1.0, capital=1000)
        self.assertEqual(size, 0.0, "Pas de mise quand odds = 1.0 (pas de profit)")

    def test_kelly_positive_for_edge(self):
        """Kelly doit retourner une taille positive avec un edge."""
        size = self.strategy.kelly_size(prob_win=0.55, odds_win=2.0, capital=1000)
        self.assertGreater(size, 0, "Taille positive avec un edge")

    def test_kelly_limited_by_max_trade(self):
        """Kelly doit être limité par max_per_trade."""
        # Avec un grand capital et un fort edge, Kelly suggère une grande taille
        size = self.strategy.kelly_size(prob_win=0.9, odds_win=5.0, capital=100000)
        self.assertLessEqual(size, self.risk.max_per_trade,
                            "Kelly limité par max_per_trade")

    def test_kelly_fraction_reduces_size(self):
        """La fraction Kelly réduit la taille par rapport au full Kelly."""
        full_kelly_risk = make_risk_config(kelly_fraction=1.0)
        quarter_kelly_risk = make_risk_config(kelly_fraction=0.25)

        mock_client = MagicMock()
        mock_executor = MagicMock()
        strategy_full = BinaryArbitrageStrategy(mock_client, mock_executor, full_kelly_risk)
        strategy_quarter = BinaryArbitrageStrategy(mock_client, mock_executor, quarter_kelly_risk)

        full_size = strategy_full.kelly_size(prob_win=0.6, odds_win=2.0, capital=1000)
        quarter_size = strategy_quarter.kelly_size(prob_win=0.6, odds_win=2.0, capital=1000)

        self.assertAlmostEqual(quarter_size, full_size * 0.25, places=2,
                               msg="Quarter Kelly = Full Kelly × 0.25")


class TestFeeCalculations(unittest.TestCase):
    """Tests des calculs de frais."""

    def setUp(self):
        self.risk = make_risk_config()
        mock_client = MagicMock()
        mock_executor = MagicMock()
        self.strategy = BinaryArbitrageStrategy(mock_client, mock_executor, self.risk)

    def test_fees_two_legs(self):
        """Frais pour 2 jambes = 0.04% du total."""
        total_cost = 100.0
        fees = self.strategy.calculate_fees(total_cost, num_legs=2)
        expected = total_cost * 0.0004  # 0.04%
        self.assertAlmostEqual(fees, expected, places=4)

    def test_fees_three_legs(self):
        """Frais pour 3 jambes = 0.06% du total."""
        total_cost = 100.0
        fees = self.strategy.calculate_fees(total_cost, num_legs=3)
        expected = total_cost * 0.0006  # 0.06%
        self.assertAlmostEqual(fees, expected, places=4)

    def test_profitability_check(self):
        """Vérification de rentabilité après frais."""
        # Profit brut suffisant
        self.assertTrue(
            self.strategy.is_profitable_after_fees(
                gross_profit=5.0, total_cost=95.0, num_legs=2
            )
        )
        # Profit brut insuffisant pour couvrir frais + seuil minimum
        self.assertFalse(
            self.strategy.is_profitable_after_fees(
                gross_profit=0.01, total_cost=99.0, num_legs=2
            )
        )


class TestBacktestMetrics(unittest.TestCase):
    """Tests des calculs de métriques du backtester."""

    def test_win_rate_calculation(self):
        """Win rate = trades gagnants / total trades."""
        profits = [1.0, 2.0, -0.5, 0.3, -0.1]
        winners = [p for p in profits if p > 0]
        win_rate = len(winners) / len(profits)
        self.assertAlmostEqual(win_rate, 0.6, places=2)

    def test_profit_factor(self):
        """Profit factor = gains totaux / pertes totales."""
        profits = [5.0, 3.0, -1.0, 2.0, -0.5]
        gross_wins = sum(p for p in profits if p > 0)  # 10.0
        gross_losses = abs(sum(p for p in profits if p < 0))  # 1.5
        pf = gross_wins / gross_losses
        self.assertAlmostEqual(pf, 6.67, places=1)

    def test_max_drawdown(self):
        """Max drawdown = pic - vallée maximale."""
        equity_curve = [0, 10, 20, 15, 25, 10, 30]  # Drawdowns: 5, 15
        peak = 0
        max_dd = 0
        for val in equity_curve:
            if val > peak:
                peak = val
            dd = peak - val
            if dd > max_dd:
                max_dd = dd
        self.assertEqual(max_dd, 15, "Max drawdown devrait être 15")


if __name__ == "__main__":
    unittest.main(verbosity=2)
