"""
Analyse et visualisation des résultats de backtest.
Génère des graphiques et des statistiques avancées.
"""

import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backtest.backtester import BacktestResult, BacktestTrade
from utils.logger import get_logger

logger = get_logger(__name__)


class ResultsAnalyzer:
    """
    Analyse approfondie des résultats de backtest.
    Calcule des métriques avancées et génère des rapports comparatifs.
    """

    def __init__(self, results: List[BacktestResult]):
        self.results = results

    def compare_presets(self) -> Dict[str, Any]:
        """Compare les performances entre différents presets de risque."""
        comparison = {}
        for result in self.results:
            comparison[result.risk_preset] = {
                "total_profit": result.total_profit,
                "roi_pct": result.roi_pct,
                "win_rate": result.win_rate,
                "sharpe": result.sharpe_ratio,
                "max_drawdown": result.max_drawdown,
                "trades": result.trades_executed,
                "profit_factor": result.profit_factor,
            }
        return comparison

    def monthly_breakdown(self, result: BacktestResult) -> Dict[str, Dict]:
        """Décompose les performances mois par mois."""
        monthly = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0})

        for trade in result.trades:
            from datetime import datetime
            dt = datetime.fromtimestamp(trade.timestamp)
            key = dt.strftime("%Y-%m")
            monthly[key]["pnl"] += trade.realized_pnl
            monthly[key]["trades"] += 1
            if trade.realized_pnl > 0:
                monthly[key]["wins"] += 1

        # Calculer le win rate mensuel
        for key, data in monthly.items():
            data["win_rate"] = (
                data["wins"] / data["trades"] * 100 if data["trades"] > 0 else 0
            )
            data["pnl"] = round(data["pnl"], 2)

        return dict(sorted(monthly.items()))

    def price_spread_analysis(self, result: BacktestResult) -> Dict:
        """Analyse la distribution des spreads YES+NO au moment du trade."""
        spreads = [t.price_yes + t.price_no for t in result.trades]
        if not spreads:
            return {}

        spreads.sort()
        n = len(spreads)

        return {
            "min": round(min(spreads), 4),
            "max": round(max(spreads), 4),
            "avg": round(sum(spreads) / n, 4),
            "p25": round(spreads[n // 4], 4),
            "p50": round(spreads[n // 2], 4),
            "p75": round(spreads[3 * n // 4], 4),
            "distribution": self._bucket_distribution(spreads, 0.85, 1.00, 15),
        }

    def profit_distribution(self, result: BacktestResult) -> Dict:
        """Analyse la distribution des profits par trade."""
        profits = [t.realized_pnl for t in result.trades]
        if not profits:
            return {}

        profits.sort()
        n = len(profits)

        return {
            "min": round(min(profits), 4),
            "max": round(max(profits), 4),
            "avg": round(sum(profits) / n, 4),
            "std": round(self._std(profits), 4),
            "p25": round(profits[n // 4], 4),
            "p50": round(profits[n // 2], 4),
            "p75": round(profits[3 * n // 4], 4),
            "p95": round(profits[int(n * 0.95)], 4),
        }

    def capital_scaling_analysis(
        self, base_result: BacktestResult, capital_levels: List[float]
    ) -> Dict[float, Dict]:
        """
        Analyse comment les performances scalent avec le capital.
        Important pour l'utilisateur qui veut savoir combien il peut gagner
        avec différents montants de capital.
        """
        analysis = {}
        for capital in capital_levels:
            # Approximation linéaire (arbitrage pur = profit proportionnel au capital)
            factor = capital / 10_000  # Basé sur le preset standard
            projected = {
                "capital": capital,
                "projected_profit": round(base_result.total_profit * factor, 2),
                "projected_roi_pct": round(base_result.roi_pct, 2),  # ROI % identique
                "estimated_trades": int(base_result.trades_executed * min(factor, 1.0)),
                "max_drawdown": round(base_result.max_drawdown * factor, 2),
                "sharpe": base_result.sharpe_ratio,
                "note": "Estimation approximative — la liquidité limite la scalabilité",
            }
            analysis[capital] = projected
        return analysis

    def print_monthly_report(self, result: BacktestResult) -> None:
        """Affiche le rapport mensuel formaté."""
        monthly = self.monthly_breakdown(result)
        print(f"\n{'─'*50}")
        print(f"  PERFORMANCE MENSUELLE — {result.strategy}")
        print(f"{'─'*50}")
        print(f"  {'Mois':<10} {'P&L':>10} {'Trades':>8} {'Win%':>8}")
        print(f"{'─'*50}")

        for month, data in monthly.items():
            sign = "+" if data["pnl"] >= 0 else ""
            print(
                f"  {month:<10} {sign}${data['pnl']:>8.2f} "
                f"{data['trades']:>8} {data['win_rate']:>7.0f}%"
            )

        print(f"{'─'*50}\n")

    def print_scaling_report(self, base_result: BacktestResult) -> None:
        """Affiche l'analyse de scalabilité par capital."""
        capitals = [100, 500, 1000, 5000, 10000, 50000, 100000]
        scaling = self.capital_scaling_analysis(base_result, capitals)

        print(f"\n{'─'*65}")
        print(f"  ANALYSE DE SCALABILITÉ — Capital vs Profit Estimé")
        print(f"{'─'*65}")
        print(f"  {'Capital':>12} {'Profit/an':>12} {'ROI%':>8} {'Drawdown':>12}")
        print(f"{'─'*65}")

        for capital, data in scaling.items():
            print(
                f"  ${capital:>10,.0f}  ${data['projected_profit']:>10,.2f}"
                f"  {data['projected_roi_pct']:>6.1f}%  ${data['max_drawdown']:>10.2f}"
            )

        print(f"{'─'*65}")
        print(f"  ⚠️  Note: Basé sur {base_result.start_date} → {base_result.end_date}")
        print(f"  ⚠️  La liquidité des marchés limite la scalabilité au-delà de ~$50k\n")

    @staticmethod
    def _std(values: List[float]) -> float:
        if len(values) < 2:
            return 0.0
        avg = sum(values) / len(values)
        variance = sum((v - avg) ** 2 for v in values) / (len(values) - 1)
        return math.sqrt(variance)

    @staticmethod
    def _bucket_distribution(
        values: List[float], min_val: float, max_val: float, buckets: int
    ) -> Dict[str, int]:
        bucket_size = (max_val - min_val) / buckets
        counts = defaultdict(int)
        for v in values:
            bucket_idx = int((v - min_val) / bucket_size)
            bucket_idx = max(0, min(bucket_idx, buckets - 1))
            bucket_label = f"{min_val + bucket_idx * bucket_size:.3f}"
            counts[bucket_label] += 1
        return dict(sorted(counts.items()))
