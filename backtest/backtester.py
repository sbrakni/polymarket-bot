"""
Moteur de backtesting pour les stratégies d'arbitrage Polymarket.
Simule l'exécution des stratégies sur des données historiques réelles.

Usage:
    python -m backtest.backtester --start 2024-01-01 --end 2024-12-31 --strategy binary_arb
"""

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backtest.data_fetcher import HistoricalDataFetcher, HistoricalMarket
from config.settings import RiskConfig, RISK_PRESETS
from utils.logger import get_logger, setup_logging

logger = get_logger(__name__)


@dataclass
class BacktestTrade:
    """Trade simulé lors du backtesting."""
    timestamp: float
    market_id: str
    question: str
    strategy: str
    price_yes: float
    price_no: float
    size: float
    total_cost: float
    gross_profit: float
    net_profit: float
    fees: float
    resolution: Optional[str]
    realized_pnl: float = 0.0  # Calculé après résolution


@dataclass
class BacktestResult:
    """Résultats complets d'un backtest."""
    strategy: str
    start_date: str
    end_date: str
    risk_preset: str
    total_markets_scanned: int
    opportunities_found: int
    trades_executed: int
    total_invested: float
    total_profit: float
    total_fees: float
    win_rate: float
    avg_profit_per_trade: float
    max_drawdown: float
    sharpe_ratio: float
    profit_factor: float
    roi_pct: float
    trades: List[BacktestTrade] = field(default_factory=list)
    equity_curve: List[Tuple[float, float]] = field(default_factory=list)


class BinaryArbitrageBacktester:
    """
    Backteste la stratégie d'arbitrage binaire sur données historiques.

    Logique:
    - Pour chaque point temporel dans l'historique de prix d'un marché:
      1. Si price_YES + price_NO < 1.0 - fees - min_edge → simuler un trade
      2. À la résolution du marché: réaliser le profit ($1 par contrat acheté)
    """

    def __init__(self, risk_config: RiskConfig, fees_rate: float = 0.0004):
        self.risk = risk_config
        self.fees_rate = fees_rate  # 0.04% total (2 jambes × 0.02%)

    def backtest_market(self, market: HistoricalMarket) -> List[BacktestTrade]:
        """Backteste un marché individuel sur tout son historique."""
        if not market.price_history:
            return []
        if not market.resolved or not market.resolution:
            return []
        if market.liquidity < 1000:
            return []

        trades = []
        traded_timestamps = set()

        for snapshot in market.price_history:
            ts = snapshot["timestamp"]
            price_yes = snapshot["yes_price"]
            price_no = snapshot["no_price"]
            total_cost = price_yes + price_no

            # Calculer le profit brut par contrat
            gross_profit = 1.0 - total_cost
            fees = total_cost * self.fees_rate
            net_profit = gross_profit - fees

            # Vérifier si c'est une opportunité
            if net_profit < self.risk.min_profit_threshold / 100:
                # Profit trop faible
                continue

            if price_yes <= 0 or price_no <= 0:
                continue
            if price_yes >= 1 or price_no >= 1:
                continue

            # Éviter de trader plusieurs fois dans le même marché dans la même heure
            hour_key = ts // 3600
            if hour_key in traded_timestamps:
                continue
            traded_timestamps.add(hour_key)

            # Calculer la taille (limitée par le capital)
            max_size = self.risk.max_per_trade / max(total_cost, 0.01)
            size = min(max_size, 100)  # Maximum 100 contrats par trade

            if size < 1:
                continue

            trade = BacktestTrade(
                timestamp=ts,
                market_id=market.market_id,
                question=market.question[:100],
                strategy="BINARY_ARB",
                price_yes=price_yes,
                price_no=price_no,
                size=size,
                total_cost=total_cost * size,
                gross_profit=gross_profit * size,
                net_profit=net_profit * size,
                fees=fees * size,
                resolution=market.resolution,
                realized_pnl=net_profit * size,  # Garanti à résolution
            )
            trades.append(trade)

        return trades

    def run(self, dataset: List[HistoricalMarket]) -> BacktestResult:
        """Lance le backtest sur tout le dataset."""
        all_trades = []

        for market in dataset:
            market_trades = self.backtest_market(market)
            all_trades.extend(market_trades)

        # Trier par timestamp
        all_trades.sort(key=lambda t: t.timestamp)

        # Calculer les métriques
        return self._compute_metrics(all_trades, dataset)

    def _compute_metrics(
        self, trades: List[BacktestTrade], dataset: List[HistoricalMarket]
    ) -> BacktestResult:
        if not trades:
            return BacktestResult(
                strategy="BINARY_ARB",
                start_date="", end_date="",
                risk_preset="", total_markets_scanned=len(dataset),
                opportunities_found=0, trades_executed=0,
                total_invested=0, total_profit=0, total_fees=0,
                win_rate=0, avg_profit_per_trade=0, max_drawdown=0,
                sharpe_ratio=0, profit_factor=0, roi_pct=0,
            )

        total_profit = sum(t.realized_pnl for t in trades)
        total_fees = sum(t.fees for t in trades)
        total_invested = sum(t.total_cost for t in trades)
        winning = [t for t in trades if t.realized_pnl > 0]
        win_rate = len(winning) / len(trades)

        # Courbe des capitaux
        equity_curve = []
        cum_pnl = 0.0
        for t in trades:
            cum_pnl += t.realized_pnl
            equity_curve.append((t.timestamp, round(cum_pnl, 4)))

        # Drawdown
        peak = 0.0
        max_dd = 0.0
        for _, pnl in equity_curve:
            if pnl > peak:
                peak = pnl
            dd = peak - pnl
            if dd > max_dd:
                max_dd = dd

        # Sharpe ratio (approximé sur les profits journaliers)
        import math
        from collections import defaultdict
        daily = defaultdict(float)
        for t in trades:
            day = int(t.timestamp // 86400)
            daily[day] += t.realized_pnl

        daily_returns = list(daily.values())
        if len(daily_returns) >= 2:
            avg = sum(daily_returns) / len(daily_returns)
            variance = sum((r - avg) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
            std = math.sqrt(variance) if variance > 0 else 0.0001
            sharpe = (avg / std) * math.sqrt(252)
        else:
            sharpe = 0.0

        # Profit factor
        gross_wins = sum(t.realized_pnl for t in trades if t.realized_pnl > 0)
        gross_losses = abs(sum(t.realized_pnl for t in trades if t.realized_pnl < 0))
        profit_factor = gross_wins / max(gross_losses, 0.01)

        start_ts = trades[0].timestamp if trades else 0
        end_ts = trades[-1].timestamp if trades else 0

        from datetime import datetime
        start_date = datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d") if start_ts else ""
        end_date = datetime.fromtimestamp(end_ts).strftime("%Y-%m-%d") if end_ts else ""

        roi_pct = (total_profit / max(self.risk.max_total_exposure, 1)) * 100

        return BacktestResult(
            strategy="BINARY_ARB",
            start_date=start_date,
            end_date=end_date,
            risk_preset="custom",
            total_markets_scanned=len(dataset),
            opportunities_found=len(trades),
            trades_executed=len(trades),
            total_invested=round(total_invested, 2),
            total_profit=round(total_profit, 2),
            total_fees=round(total_fees, 2),
            win_rate=round(win_rate * 100, 1),
            avg_profit_per_trade=round(total_profit / len(trades), 4),
            max_drawdown=round(max_dd, 2),
            sharpe_ratio=round(sharpe, 2),
            profit_factor=round(profit_factor, 2),
            roi_pct=round(roi_pct, 2),
            trades=trades,
            equity_curve=equity_curve,
        )


class BacktestRunner:
    """
    Interface principale pour lancer des backtests.
    Supporte plusieurs stratégies et périodes.
    """

    async def run_binary_arb_backtest(
        self,
        start_date: str,
        end_date: str,
        risk_preset: str = "standard_10000",
        min_volume: float = 5000,
        tags: Optional[List[str]] = None,
    ) -> BacktestResult:
        """
        Lance un backtest complet de la stratégie Binary Arbitrage.

        Args:
            start_date: Date de début (YYYY-MM-DD)
            end_date: Date de fin (YYYY-MM-DD)
            risk_preset: Preset de risque (conservative_100, moderate_1000, standard_10000, aggressive_50000)
            min_volume: Volume minimum des marchés à inclure
            tags: Tags de marchés à filtrer (ex: ["Sports", "NBA"])
        """
        risk_config = RISK_PRESETS.get(risk_preset, RISK_PRESETS["standard_10000"])

        logger.info(f"Démarrage backtest: {start_date} → {end_date} | Preset: {risk_preset}")

        async with HistoricalDataFetcher() as fetcher:
            dataset = await fetcher.build_historical_dataset(
                start_date=start_date,
                end_date=end_date,
                min_volume=min_volume,
                tags=tags,
            )

        if not dataset:
            logger.error("Aucune donnée disponible pour le backtest")
            return None

        backtester = BinaryArbitrageBacktester(risk_config)
        result = backtester.run(dataset)
        result.risk_preset = risk_preset
        result.start_date = start_date
        result.end_date = end_date

        return result

    def print_report(self, result: BacktestResult) -> None:
        """Affiche le rapport de backtest formaté."""
        duration = f"{result.start_date} → {result.end_date}"
        print(f"\n{'='*60}")
        print(f"  RAPPORT DE BACKTEST — {result.strategy}")
        print(f"{'='*60}")
        print(f"  Période: {duration}")
        print(f"  Preset de risque: {result.risk_preset}")
        print(f"{'─'*60}")
        print(f"  Marchés scannés:     {result.total_markets_scanned:>10,}")
        print(f"  Opportunités:        {result.opportunities_found:>10,}")
        print(f"  Trades exécutés:     {result.trades_executed:>10,}")
        print(f"{'─'*60}")
        print(f"  P&L Total:           ${result.total_profit:>10,.2f}")
        print(f"  Frais payés:         ${result.total_fees:>10,.2f}")
        print(f"  ROI:                 {result.roi_pct:>9.1f}%")
        print(f"{'─'*60}")
        print(f"  Win Rate:            {result.win_rate:>9.1f}%")
        print(f"  Profit moyen/trade:  ${result.avg_profit_per_trade:>10.4f}")
        print(f"  Profit Factor:       {result.profit_factor:>10.2f}")
        print(f"{'─'*60}")
        print(f"  Sharpe Ratio:        {result.sharpe_ratio:>10.2f}")
        print(f"  Max Drawdown:        ${result.max_drawdown:>10.2f}")
        print(f"{'='*60}\n")

    def save_report(self, result: BacktestResult, filename: str) -> None:
        """Sauvegarde le rapport de backtest en JSON."""
        Path("data/backtest_results").mkdir(parents=True, exist_ok=True)
        path = f"data/backtest_results/{filename}"

        report = {
            "strategy": result.strategy,
            "period": f"{result.start_date} to {result.end_date}",
            "risk_preset": result.risk_preset,
            "summary": {
                "markets_scanned": result.total_markets_scanned,
                "opportunities": result.opportunities_found,
                "trades": result.trades_executed,
                "total_profit": result.total_profit,
                "total_fees": result.total_fees,
                "roi_pct": result.roi_pct,
                "win_rate": result.win_rate,
                "avg_profit_per_trade": result.avg_profit_per_trade,
                "sharpe_ratio": result.sharpe_ratio,
                "max_drawdown": result.max_drawdown,
                "profit_factor": result.profit_factor,
            },
            "equity_curve": result.equity_curve[:1000],  # Limiter la taille
            "sample_trades": [
                {
                    "market": t.question[:60],
                    "timestamp": t.timestamp,
                    "yes_price": t.price_yes,
                    "no_price": t.price_no,
                    "size": t.size,
                    "profit": t.realized_pnl,
                }
                for t in result.trades[:100]  # 100 premiers trades
            ],
        }

        with open(path, "w") as f:
            json.dump(report, f, indent=2)
        logger.info(f"Rapport sauvegardé: {path}")


async def main():
    """Point d'entrée CLI pour le backtesting."""
    setup_logging("INFO")

    parser = argparse.ArgumentParser(description="Backtest Polymarket Arbitrage Bot")
    parser.add_argument("--start", default="2024-01-01", help="Date de début (YYYY-MM-DD)")
    parser.add_argument("--end", default="2024-12-31", help="Date de fin (YYYY-MM-DD)")
    parser.add_argument("--strategy", default="binary_arb", help="Stratégie à backtester")
    parser.add_argument("--preset", default="standard_10000",
                       choices=list(RISK_PRESETS.keys()),
                       help="Preset de configuration de risque")
    parser.add_argument("--min-volume", type=float, default=5000,
                       help="Volume minimum des marchés")
    parser.add_argument("--tags", nargs="*", help="Tags de marchés à filtrer")
    parser.add_argument("--save", help="Sauvegarder le rapport dans un fichier JSON")
    args = parser.parse_args()

    runner = BacktestRunner()

    if args.strategy == "binary_arb":
        result = await runner.run_binary_arb_backtest(
            start_date=args.start,
            end_date=args.end,
            risk_preset=args.preset,
            min_volume=args.min_volume,
            tags=args.tags,
        )

        if result:
            runner.print_report(result)
            if args.save:
                runner.save_report(result, args.save)
        else:
            print("Aucun résultat — vérifiez les paramètres")
    else:
        print(f"Stratégie '{args.strategy}' non supportée. Options: binary_arb")


if __name__ == "__main__":
    asyncio.run(main())
