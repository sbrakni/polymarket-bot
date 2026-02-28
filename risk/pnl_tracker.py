"""
Suivi P&L en temps réel — calcule le Sharpe ratio, drawdown, et autres métriques.
"""

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class TradeRecord:
    """Enregistrement d'un trade complété."""
    trade_id: str
    strategy: str
    market_id: str
    profit: float
    cost: float
    size: float
    timestamp: float = field(default_factory=time.time)
    latency_ms: float = 0.0
    was_dry_run: bool = False


@dataclass
class PnLSnapshot:
    """Snapshot du P&L à un instant T."""
    timestamp: float
    cumulative_pnl: float
    daily_pnl: float
    total_trades: int
    win_rate: float
    sharpe_ratio: float
    max_drawdown: float


class PnLTracker:
    """
    Tracker de performance en temps réel.
    Calcule toutes les métriques nécessaires pour évaluer la stratégie.
    """

    def __init__(self):
        self._trades: List[TradeRecord] = []
        self._daily_trades: Dict[str, List[TradeRecord]] = defaultdict(list)  # date -> trades
        self._snapshots: List[PnLSnapshot] = []
        self._peak_pnl: float = 0.0
        self._max_drawdown: float = 0.0
        self._started_at: float = time.time()
        self._capital_start: float = 0.0

    def set_starting_capital(self, capital: float) -> None:
        self._capital_start = capital

    def record_trade(self, record: TradeRecord) -> None:
        """Enregistre un trade et met à jour les métriques."""
        self._trades.append(record)

        date_key = self._date_key(record.timestamp)
        self._daily_trades[date_key].append(record)

        # Mettre à jour le drawdown
        cum_pnl = self.cumulative_pnl()
        if cum_pnl > self._peak_pnl:
            self._peak_pnl = cum_pnl
        current_drawdown = self._peak_pnl - cum_pnl
        if current_drawdown > self._max_drawdown:
            self._max_drawdown = current_drawdown

        logger.debug(
            f"Trade enregistré: {record.strategy} | profit=${record.profit:.2f} | "
            f"PnL cumulé=${cum_pnl:.2f}"
        )

    def record_from_execution(
        self, execution, strategy: str, was_dry_run: bool = False
    ) -> None:
        """Crée un TradeRecord depuis un ArbitrageExecution."""
        if not execution.success:
            return
        record = TradeRecord(
            trade_id=execution.execution_id,
            strategy=strategy,
            market_id=execution.market_id,
            profit=execution.actual_profit,
            cost=execution.total_cost,
            size=1.0,
            timestamp=execution.timestamp,
            latency_ms=execution.latency_ms,
            was_dry_run=was_dry_run,
        )
        self.record_trade(record)

    # -----------------------------------------------------------------------
    # Métriques de performance
    # -----------------------------------------------------------------------

    def cumulative_pnl(self) -> float:
        return sum(t.profit for t in self._trades)

    def daily_pnl(self, date_key: Optional[str] = None) -> float:
        key = date_key or self._date_key(time.time())
        return sum(t.profit for t in self._daily_trades.get(key, []))

    def total_trades(self) -> int:
        return len(self._trades)

    def winning_trades(self) -> int:
        return sum(1 for t in self._trades if t.profit > 0)

    def losing_trades(self) -> int:
        return sum(1 for t in self._trades if t.profit <= 0)

    def win_rate(self) -> float:
        if not self._trades:
            return 0.0
        return self.winning_trades() / len(self._trades)

    def avg_profit_per_trade(self) -> float:
        if not self._trades:
            return 0.0
        return self.cumulative_pnl() / len(self._trades)

    def avg_win(self) -> float:
        wins = [t.profit for t in self._trades if t.profit > 0]
        return sum(wins) / len(wins) if wins else 0.0

    def avg_loss(self) -> float:
        losses = [t.profit for t in self._trades if t.profit <= 0]
        return sum(losses) / len(losses) if losses else 0.0

    def profit_factor(self) -> float:
        """Ratio profit total / perte totale."""
        gross_profit = sum(t.profit for t in self._trades if t.profit > 0)
        gross_loss = abs(sum(t.profit for t in self._trades if t.profit < 0))
        if gross_loss == 0:
            return float("inf") if gross_profit > 0 else 0.0
        return gross_profit / gross_loss

    def sharpe_ratio(self, risk_free_rate: float = 0.04) -> float:
        """
        Calcule le Sharpe ratio annualisé.
        Basé sur les profits journaliers.
        """
        daily_returns = [
            sum(t.profit for t in trades) / max(self._capital_start, 1.0)
            for trades in self._daily_trades.values()
        ]
        if len(daily_returns) < 2:
            return 0.0

        avg_daily = sum(daily_returns) / len(daily_returns)
        variance = sum((r - avg_daily) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
        std_daily = math.sqrt(variance) if variance > 0 else 0.0001

        # Annualiser (252 jours de trading)
        sharpe = (avg_daily - risk_free_rate / 252) / std_daily * math.sqrt(252)
        return round(sharpe, 2)

    def sortino_ratio(self, risk_free_rate: float = 0.04) -> float:
        """Sharpe ratio basé uniquement sur la volatilité négative."""
        daily_returns = [
            sum(t.profit for t in trades) / max(self._capital_start, 1.0)
            for trades in self._daily_trades.values()
        ]
        if len(daily_returns) < 2:
            return 0.0

        avg_daily = sum(daily_returns) / len(daily_returns)
        downside_returns = [min(0, r - risk_free_rate / 252) for r in daily_returns]
        downside_variance = sum(r ** 2 for r in downside_returns) / len(downside_returns)
        downside_std = math.sqrt(downside_variance) if downside_variance > 0 else 0.0001

        sortino = (avg_daily - risk_free_rate / 252) / downside_std * math.sqrt(252)
        return round(sortino, 2)

    def max_drawdown(self) -> float:
        """Drawdown maximum depuis le pic."""
        return self._max_drawdown

    def max_drawdown_pct(self) -> float:
        if not self._capital_start:
            return 0.0
        return (self._max_drawdown / self._capital_start) * 100

    def roi_pct(self) -> float:
        if not self._capital_start:
            return 0.0
        return (self.cumulative_pnl() / self._capital_start) * 100

    def avg_latency_ms(self) -> float:
        latencies = [t.latency_ms for t in self._trades if t.latency_ms > 0]
        if not latencies:
            return 0.0
        return sum(latencies) / len(latencies)

    def trades_per_day(self) -> float:
        days = max(1.0, (time.time() - self._started_at) / 86400)
        return len(self._trades) / days

    def pnl_by_strategy(self) -> Dict[str, float]:
        result = defaultdict(float)
        for trade in self._trades:
            result[trade.strategy] += trade.profit
        return dict(result)

    def trades_by_strategy(self) -> Dict[str, int]:
        result = defaultdict(int)
        for trade in self._trades:
            result[trade.strategy] += 1
        return dict(result)

    # -----------------------------------------------------------------------
    # Snapshots et export
    # -----------------------------------------------------------------------

    def take_snapshot(self) -> PnLSnapshot:
        snap = PnLSnapshot(
            timestamp=time.time(),
            cumulative_pnl=self.cumulative_pnl(),
            daily_pnl=self.daily_pnl(),
            total_trades=self.total_trades(),
            win_rate=self.win_rate(),
            sharpe_ratio=self.sharpe_ratio(),
            max_drawdown=self.max_drawdown(),
        )
        self._snapshots.append(snap)
        return snap

    def get_equity_curve(self) -> List[Tuple[float, float]]:
        """Retourne la courbe des capitaux: [(timestamp, cumulative_pnl), ...]"""
        curve = []
        cumsum = 0.0
        for trade in sorted(self._trades, key=lambda t: t.timestamp):
            cumsum += trade.profit
            curve.append((trade.timestamp, round(cumsum, 4)))
        return curve

    def get_full_report(self) -> Dict:
        """Rapport complet des performances."""
        run_duration_hours = (time.time() - self._started_at) / 3600

        return {
            "summary": {
                "cumulative_pnl": round(self.cumulative_pnl(), 2),
                "roi_pct": round(self.roi_pct(), 2),
                "total_trades": self.total_trades(),
                "win_rate": round(self.win_rate() * 100, 1),
                "avg_profit_per_trade": round(self.avg_profit_per_trade(), 4),
                "trades_per_day": round(self.trades_per_day(), 1),
                "run_hours": round(run_duration_hours, 1),
            },
            "risk_metrics": {
                "sharpe_ratio": self.sharpe_ratio(),
                "sortino_ratio": self.sortino_ratio(),
                "profit_factor": round(self.profit_factor(), 2),
                "max_drawdown": round(self.max_drawdown(), 2),
                "max_drawdown_pct": round(self.max_drawdown_pct(), 2),
                "avg_win": round(self.avg_win(), 4),
                "avg_loss": round(self.avg_loss(), 4),
            },
            "by_strategy": {
                "pnl": self.pnl_by_strategy(),
                "trades": self.trades_by_strategy(),
            },
            "execution": {
                "avg_latency_ms": round(self.avg_latency_ms(), 1),
            },
            "daily_pnl": {
                date: round(sum(t.profit for t in trades), 2)
                for date, trades in sorted(self._daily_trades.items())
            },
        }

    @staticmethod
    def _date_key(timestamp: float) -> str:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")
