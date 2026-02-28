"""
Tests unitaires pour le tracker P&L.
"""

import time
import unittest

from risk.pnl_tracker import PnLTracker, TradeRecord


def make_trade(strategy: str = "BINARY_ARB", profit: float = 1.0, ts: float = None) -> TradeRecord:
    return TradeRecord(
        trade_id=f"T{profit:.2f}",
        strategy=strategy,
        market_id="market_test",
        profit=profit,
        cost=abs(profit) * 10,
        size=1.0,
        timestamp=ts or time.time(),
        latency_ms=50.0,
    )


class TestPnLTracker(unittest.TestCase):

    def setUp(self):
        self.tracker = PnLTracker()
        self.tracker.set_starting_capital(10000.0)

    def test_cumulative_pnl_empty(self):
        """PnL cumulatif est 0 sans trades."""
        self.assertEqual(self.tracker.cumulative_pnl(), 0.0)

    def test_cumulative_pnl_with_trades(self):
        """PnL cumulatif = somme des profits."""
        self.tracker.record_trade(make_trade(profit=5.0))
        self.tracker.record_trade(make_trade(profit=3.0))
        self.tracker.record_trade(make_trade(profit=-1.0))
        self.assertAlmostEqual(self.tracker.cumulative_pnl(), 7.0)

    def test_win_rate_calculation(self):
        """Win rate = trades positifs / total."""
        self.tracker.record_trade(make_trade(profit=1.0))
        self.tracker.record_trade(make_trade(profit=2.0))
        self.tracker.record_trade(make_trade(profit=-0.5))
        self.assertAlmostEqual(self.tracker.win_rate(), 2/3, places=2)

    def test_win_rate_empty(self):
        """Win rate = 0 sans trades."""
        self.assertEqual(self.tracker.win_rate(), 0.0)

    def test_profit_factor_all_positive(self):
        """Profit factor = infini si aucune perte."""
        self.tracker.record_trade(make_trade(profit=5.0))
        self.tracker.record_trade(make_trade(profit=3.0))
        pf = self.tracker.profit_factor()
        self.assertEqual(pf, float("inf"))

    def test_profit_factor_mixed(self):
        """Profit factor = gains / pertes."""
        self.tracker.record_trade(make_trade(profit=10.0))
        self.tracker.record_trade(make_trade(profit=-2.0))
        pf = self.tracker.profit_factor()
        self.assertAlmostEqual(pf, 5.0, places=1)

    def test_max_drawdown(self):
        """Max drawdown calculé correctement."""
        ts = 1000.0
        self.tracker.record_trade(make_trade(profit=10.0, ts=ts))   # PnL: 10
        self.tracker.record_trade(make_trade(profit=5.0, ts=ts+1))  # PnL: 15
        self.tracker.record_trade(make_trade(profit=-8.0, ts=ts+2)) # PnL: 7 (DD: 8)
        self.tracker.record_trade(make_trade(profit=3.0, ts=ts+3))  # PnL: 10
        self.tracker.record_trade(make_trade(profit=-6.0, ts=ts+4)) # PnL: 4 (DD: 11? Non: peak=15, val=4 -> DD=11)

        dd = self.tracker.max_drawdown()
        self.assertAlmostEqual(dd, 11.0, places=1)

    def test_pnl_by_strategy(self):
        """PnL groupé par stratégie."""
        self.tracker.record_trade(make_trade("BINARY_ARB", 5.0))
        self.tracker.record_trade(make_trade("BINARY_ARB", 3.0))
        self.tracker.record_trade(make_trade("REALITY_ARB", 2.0))

        by_strat = self.tracker.pnl_by_strategy()
        self.assertAlmostEqual(by_strat["BINARY_ARB"], 8.0)
        self.assertAlmostEqual(by_strat["REALITY_ARB"], 2.0)

    def test_avg_profit_per_trade(self):
        """Profit moyen par trade."""
        self.tracker.record_trade(make_trade(profit=4.0))
        self.tracker.record_trade(make_trade(profit=2.0))
        self.assertAlmostEqual(self.tracker.avg_profit_per_trade(), 3.0)

    def test_roi_pct(self):
        """ROI en pourcentage du capital initial."""
        self.tracker.record_trade(make_trade(profit=1000.0))
        # 1000 / 10000 = 10%
        self.assertAlmostEqual(self.tracker.roi_pct(), 10.0, places=1)

    def test_equity_curve_monotone_then_decline(self):
        """La courbe des capitaux reflète les trades dans l'ordre."""
        ts = 1000.0
        self.tracker.record_trade(make_trade(profit=5.0, ts=ts))
        self.tracker.record_trade(make_trade(profit=3.0, ts=ts+1))
        self.tracker.record_trade(make_trade(profit=-2.0, ts=ts+2))

        curve = self.tracker.get_equity_curve()
        self.assertEqual(len(curve), 3)
        self.assertAlmostEqual(curve[0][1], 5.0)
        self.assertAlmostEqual(curve[1][1], 8.0)
        self.assertAlmostEqual(curve[2][1], 6.0)

    def test_full_report_structure(self):
        """get_full_report retourne toutes les clés attendues."""
        self.tracker.record_trade(make_trade(profit=1.0))
        report = self.tracker.get_full_report()

        self.assertIn("summary", report)
        self.assertIn("risk_metrics", report)
        self.assertIn("by_strategy", report)
        self.assertIn("execution", report)
        self.assertIn("daily_pnl", report)

        summary = report["summary"]
        self.assertIn("cumulative_pnl", summary)
        self.assertIn("win_rate", summary)
        self.assertIn("total_trades", summary)

    def test_take_snapshot(self):
        """take_snapshot retourne un snapshot valide."""
        self.tracker.record_trade(make_trade(profit=2.5))
        snap = self.tracker.take_snapshot()
        self.assertAlmostEqual(snap.cumulative_pnl, 2.5)
        self.assertEqual(snap.total_trades, 1)
        self.assertEqual(snap.win_rate, 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
