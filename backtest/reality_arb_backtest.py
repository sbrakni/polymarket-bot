"""
Reality Arbitrage Backtest — données réelles TheOddsAPI.

Simule la stratégie d'arbitrage de latence sportive à partir des vrais
résultats de matchs récupérés via TheOddsAPI, puis croise avec les marchés
Polymarket correspondants.

Comme TheOddsAPI free-tier donne uniquement les scores finaux (pas
play-by-play), nous simulons l'évolution de la probabilité au fil du match
avec le même modèle logistique utilisé en live par `reality_arbitrage.py`,
et nous estimons combien d'opportunités auraient été détectées.

Usage:
    python -m backtest.reality_arb_backtest --sport nba --days 3
    python -m backtest.reality_arb_backtest --all-sports --days 3
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.backtester import BacktestResult, BacktestTrade
from backtest.theodds_fetcher import TheOddsApiFetcher
from config.settings import RISK_PRESETS, RiskConfig
from utils.logger import get_logger

logger = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Probability models (mirrors strategies/reality_arbitrage.py)
# ─────────────────────────────────────────────────────────────────────────────

def _win_prob_nba(score_diff: float, minutes_remaining: float) -> float:
    """P(home win | current score diff, time remaining) — logistic model."""
    if minutes_remaining <= 0:
        return 1.0 if score_diff > 0 else (0.5 if score_diff == 0 else 0.0)
    # Roughly: 1-point advantage = 3% when 10min remain, 6% when 2min remain
    k = 0.15 / math.sqrt(max(minutes_remaining, 0.5))
    return 1.0 / (1.0 + math.exp(-k * score_diff))


def _win_prob_nfl(score_diff: float, minutes_remaining: float) -> float:
    k = 0.08 / math.sqrt(max(minutes_remaining, 0.5))
    return 1.0 / (1.0 + math.exp(-k * score_diff))


def _win_prob_nhl(score_diff: float, minutes_remaining: float) -> float:
    k = 0.35 / math.sqrt(max(minutes_remaining, 0.5))
    return 1.0 / (1.0 + math.exp(-k * score_diff))


def _win_prob_soccer(score_diff: float, minutes_remaining: float) -> float:
    k = 0.25 / math.sqrt(max(minutes_remaining, 0.5))
    return 1.0 / (1.0 + math.exp(-k * score_diff))


_WIN_PROB_FN = {
    "basketball_nba": (_win_prob_nba, 48.0),     # 48 minutes per game
    "americanfootball_nfl": (_win_prob_nfl, 60.0),
    "icehockey_nhl": (_win_prob_nhl, 60.0),
    "baseball_mlb": (None, None),                 # Not suitable for this model
    "soccer_epl": (_win_prob_soccer, 90.0),
}


# ─────────────────────────────────────────────────────────────────────────────
# Simulated game progression from final score
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class GameMoment:
    """A simulated game state at a specific time point."""
    minute: float           # elapsed minutes
    home_score: float
    away_score: float
    prob_home_win: float    # probability at this moment
    is_decisive: bool       # True if a score change just happened


def simulate_game_progression(
    sport_key: str,
    home_score_final: int,
    away_score_final: int,
    n_snapshots: int = 20,
) -> List[GameMoment]:
    """
    Reconstruct a plausible game progression from the final score.

    Since we only have the final score (TheOddsAPI free tier), we:
    1. Divide the game into `n_snapshots` equally-spaced time points
    2. Linearly interpolate scoring (crude but reasonable for backtesting)
    3. Mark snapshots where scoring changes as "decisive moments"
    4. Calculate win probability at each snapshot using our logistic model

    Returns a list of GameMoment objects ordered by time.
    """
    prob_fn, total_minutes = _WIN_PROB_FN.get(sport_key, (None, None))
    if prob_fn is None or total_minutes is None:
        return []

    moments = []
    for i in range(n_snapshots + 1):
        frac = i / n_snapshots
        elapsed = frac * total_minutes
        remaining = total_minutes - elapsed

        # Linear interpolation of score (simplified)
        h = round(home_score_final * frac)
        a = round(away_score_final * frac)

        prob = prob_fn(h - a, remaining)

        # Mark as decisive if score changes here (approximate)
        prev_h = round(home_score_final * ((i - 1) / n_snapshots)) if i > 0 else 0
        prev_a = round(away_score_final * ((i - 1) / n_snapshots)) if i > 0 else 0
        is_decisive = (h != prev_h or a != prev_a) and i > 0

        moments.append(GameMoment(
            minute=elapsed,
            home_score=h,
            away_score=a,
            prob_home_win=prob,
            is_decisive=is_decisive,
        ))

    return moments


# ─────────────────────────────────────────────────────────────────────────────
# Backtest core
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RealityArbOpportunity:
    """Detected arbitrage opportunity from a game moment."""
    game_id: str
    sport: str
    home_team: str
    away_team: str
    minute: float
    model_prob: float           # Our model probability
    implied_market_prob: float  # What Polymarket was likely showing
    edge: float                 # model_prob - implied_market_prob
    side: str                   # "YES" or "NO"
    estimated_profit: float     # in $ for the given trade size


class RealityArbBacktester:
    """
    Backtests the reality arbitrage strategy using real TheOddsAPI data.

    For each completed game:
    1. Simulate the game progression from the final score
    2. At each decisive moment, estimate what Polymarket's market would have shown
       (using a "reaction lag model" — markets update 15–40s after reality)
    3. Calculate the edge between our model probability and the stale market price
    4. If edge >= MIN_PROB_SHIFT → count as a captured opportunity
    5. Aggregate into BacktestResult
    """

    MIN_EDGE = 0.05       # Minimum edge to trade (5%)
    LAG_SECONDS = 25.0    # Average market reaction lag (seconds)
    FEE_RATE = 0.0004     # Taker fees (0.04%)

    def __init__(
        self,
        risk_config: RiskConfig,
        api_key: Optional[str] = None,
        days_from: int = 3,
    ) -> None:
        self.risk = risk_config
        self.fetcher = TheOddsApiFetcher(api_key=api_key)
        self.days_from = days_from

    def run(
        self,
        sports: Optional[List[str]] = None,
        preset_name: str = "standard_10000",
    ) -> BacktestResult:
        """Run the full backtest and return a BacktestResult."""

        if sports is None:
            sports = [k for k in _WIN_PROB_FN if _WIN_PROB_FN[k][0] is not None]

        all_games: List[Dict] = []
        for sport in sports:
            try:
                results = self.fetcher.get_parsed_results(sport, self.days_from)
                all_games.extend(results)
                logger.info("Sport %s: %d completed games", sport, len(results))
            except Exception as exc:
                logger.warning("Skipping %s: %s", sport, exc)

        logger.info("Total games to analyze: %d", len(all_games))

        trades: List[BacktestTrade] = []
        total_markets_scanned = 0
        opportunities_found = 0

        for game in all_games:
            total_markets_scanned += 1
            game_trades, opps = self._backtest_game(game)
            trades.extend(game_trades)
            opportunities_found += opps

        # Derive start/end dates from game data
        if all_games:
            times = [g["commence_time"] for g in all_games if g.get("commence_time")]
            start_date = min(times)[:10] if times else datetime.now().strftime("%Y-%m-%d")
            end_date = max(times)[:10] if times else start_date
        else:
            end_date = datetime.now().strftime("%Y-%m-%d")
            start_date = end_date

        return _compute_result_from_trades(
            trades=trades,
            strategy="REALITY_ARB",
            start_date=start_date,
            end_date=end_date,
            risk_preset=preset_name,
            capital=self.risk.max_total_exposure,
            total_markets_scanned=total_markets_scanned,
            opportunities_found=opportunities_found,
        )

    def _backtest_game(self, game: Dict) -> Tuple[List[BacktestTrade], int]:
        """Simulate trading opportunities for a single completed game."""
        sport = game["sport"]
        home_score = game["home_score"]
        away_score = game["away_score"]
        winner = game["winner"]

        moments = simulate_game_progression(sport, home_score, away_score)
        if not moments:
            return [], 0

        trades: List[BacktestTrade] = []
        opportunities = 0
        prev_prob: Optional[float] = None

        # Approximate timestamp from commence_time
        try:
            game_ts = datetime.fromisoformat(
                game["commence_time"].replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            game_ts = time.time() - 86400

        for moment in moments:
            if not moment.is_decisive:
                prev_prob = moment.prob_home_win
                continue

            # During the LAG window, the market still shows the OLD probability.
            # We model: market_prob ≈ probability BEFORE this score change.
            if prev_prob is None:
                prev_prob = 0.5

            stale_market_prob = prev_prob
            true_prob = moment.prob_home_win

            edge_home = true_prob - stale_market_prob
            if abs(edge_home) >= self.MIN_EDGE:
                opportunities += 1
                side = "YES" if edge_home > 0 else "NO"
                abs_edge = abs(edge_home)

                # Trade: buy the mispriced side
                # At stale_market_prob, the YES token is priced at stale_market_prob
                # After market corrects, it moves to true_prob
                price_yes = stale_market_prob
                price_no = 1.0 - stale_market_prob

                # Size: use Kelly fraction of max_per_trade
                kelly_size = self.risk.max_per_trade * self.risk.kelly_fraction * abs_edge
                trade_size = min(kelly_size, self.risk.max_per_trade)
                trade_size = max(trade_size, 1.0)

                # Number of contracts
                price_of_side = price_yes if side == "YES" else price_no
                n_contracts = int(trade_size / max(price_of_side, 0.01))

                total_cost = price_of_side * n_contracts
                fees = total_cost * self.FEE_RATE

                # Did the bet resolve correctly?
                # YES bet wins if home_team won; NO bet wins if away_team won
                home_won = winner == game["home_team"]
                bet_won = (side == "YES" and home_won) or (side == "NO" and not home_won)

                if bet_won:
                    gross_profit = (1.0 - price_of_side) * n_contracts
                    net_profit = gross_profit - fees
                    resolution = "YES" if home_won else "NO"
                else:
                    # Lost: entire stake is gone (binary market)
                    gross_profit = -total_cost
                    net_profit = -total_cost - fees
                    resolution = "YES" if home_won else "NO"

                trade_ts = game_ts + (moment.minute / 90.0) * 5400  # approx seconds into game

                trade = BacktestTrade(
                    timestamp=trade_ts,
                    market_id=game["id"],
                    question=f"Will {game['home_team']} beat {game['away_team']}?",
                    strategy="REALITY_ARB",
                    price_yes=round(price_yes, 4),
                    price_no=round(price_no, 4),
                    size=n_contracts,
                    total_cost=round(total_cost, 4),
                    gross_profit=round(gross_profit, 4),
                    net_profit=round(net_profit, 4),
                    fees=round(fees, 4),
                    resolution=resolution,
                    realized_pnl=round(net_profit, 4),
                )
                trades.append(trade)

            prev_prob = moment.prob_home_win

        return trades, opportunities


# ─────────────────────────────────────────────────────────────────────────────
# Metrics computation (shared with binary arb backtest)
# ─────────────────────────────────────────────────────────────────────────────

def _compute_result_from_trades(
    trades: List[BacktestTrade],
    strategy: str,
    start_date: str,
    end_date: str,
    risk_preset: str,
    capital: float,
    total_markets_scanned: int,
    opportunities_found: int,
) -> BacktestResult:
    """Compute all metrics from a list of trades."""
    import math as _math
    from collections import defaultdict

    if not trades:
        return BacktestResult(
            strategy=strategy,
            start_date=start_date,
            end_date=end_date,
            risk_preset=risk_preset,
            total_markets_scanned=total_markets_scanned,
            opportunities_found=opportunities_found,
            trades_executed=0,
            total_invested=0,
            total_profit=0,
            total_fees=0,
            win_rate=0,
            avg_profit_per_trade=0,
            max_drawdown=0,
            sharpe_ratio=0,
            profit_factor=0,
            roi_pct=0,
        )

    total_profit = sum(t.realized_pnl for t in trades)
    total_fees = sum(t.fees for t in trades)
    total_invested = sum(t.total_cost for t in trades)
    winning = [t for t in trades if t.realized_pnl > 0]
    win_rate = len(winning) / len(trades) * 100

    # Equity curve
    equity_curve = []
    cum = 0.0
    for t in sorted(trades, key=lambda x: x.timestamp):
        cum += t.realized_pnl
        equity_curve.append((t.timestamp, round(cum, 4)))

    # Max drawdown
    peak = 0.0
    max_dd = 0.0
    for _, pnl in equity_curve:
        if pnl > peak:
            peak = pnl
        dd = peak - pnl
        if dd > max_dd:
            max_dd = dd

    # Sharpe
    daily: defaultdict = defaultdict(float)
    for t in trades:
        day = int(t.timestamp // 86400)
        daily[day] += t.realized_pnl
    daily_returns = [v / capital for v in daily.values()]
    if len(daily_returns) >= 2:
        avg_r = sum(daily_returns) / len(daily_returns)
        var = sum((r - avg_r) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
        std = _math.sqrt(var) if var > 0 else 1e-9
        sharpe = (avg_r / std) * _math.sqrt(252)
    else:
        sharpe = 0.0

    # Profit factor
    gross_wins = sum(t.realized_pnl for t in trades if t.realized_pnl > 0)
    gross_losses = abs(sum(t.realized_pnl for t in trades if t.realized_pnl < 0))
    pf = gross_wins / max(gross_losses, 0.01)

    roi_pct = (total_profit / capital) * 100

    return BacktestResult(
        strategy=strategy,
        start_date=start_date,
        end_date=end_date,
        risk_preset=risk_preset,
        total_markets_scanned=total_markets_scanned,
        opportunities_found=opportunities_found,
        trades_executed=len(trades),
        total_invested=round(total_invested, 2),
        total_profit=round(total_profit, 2),
        total_fees=round(total_fees, 2),
        win_rate=round(win_rate, 1),
        avg_profit_per_trade=round(total_profit / max(len(trades), 1), 4),
        max_drawdown=round(max_dd, 2),
        sharpe_ratio=round(sharpe, 2),
        profit_factor=round(pf, 2),
        roi_pct=round(roi_pct, 2),
        trades=trades,
        equity_curve=equity_curve,
    )


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

SPORT_KEYS = {
    "nba": "basketball_nba",
    "nfl": "americanfootball_nfl",
    "nhl": "icehockey_nhl",
    "mlb": "baseball_mlb",
    "epl": "soccer_epl",
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reality Arb Backtest using real TheOddsAPI data"
    )
    parser.add_argument(
        "--sport", choices=list(SPORT_KEYS.keys()),
        help="Sport to backtest (omit for --all-sports)",
    )
    parser.add_argument(
        "--all-sports", action="store_true",
        help="Backtest all supported sports",
    )
    parser.add_argument(
        "--days", type=int, default=3,
        help="Days of historical scores to use (max 3 on free tier)",
    )
    parser.add_argument(
        "--preset", default="standard_10000",
        choices=list(RISK_PRESETS.keys()),
        help="Risk preset",
    )
    parser.add_argument(
        "--html", action="store_true",
        help="Generate HTML report",
    )
    parser.add_argument(
        "--images", action="store_true",
        help="Generate PNG images",
    )
    parser.add_argument(
        "--out", default="data/backtest_results",
        help="Output directory for reports",
    )
    args = parser.parse_args()

    if not args.sport and not args.all_sports:
        parser.error("Specify --sport <sport> or --all-sports")

    sports: List[str] = []
    if args.all_sports:
        sports = [v for k, v in SPORT_KEYS.items() if _WIN_PROB_FN.get(v, (None,))[0] is not None]
    else:
        sports = [SPORT_KEYS[args.sport]]

    risk = RISK_PRESETS[args.preset]
    backtester = RealityArbBacktester(risk_config=risk, days_from=args.days)

    print(f"\n  Reality Arb Backtest — {', '.join(sports)}")
    print(f"  Preset: {args.preset} | Days: {args.days}\n")

    result = backtester.run(sports=sports, preset_name=args.preset)

    print(f"  Games analyzed : {result.total_markets_scanned}")
    print(f"  Opportunities  : {result.opportunities_found}")
    print(f"  Trades executed: {result.trades_executed}")
    print(f"  P&L total      : ${result.total_profit:+,.2f}")
    print(f"  ROI            : {result.roi_pct:+.2f}%")
    print(f"  Win rate       : {result.win_rate:.1f}%")
    print(f"  Sharpe ratio   : {result.sharpe_ratio:.2f}")
    print(f"  Max drawdown   : ${result.max_drawdown:.2f}")
    print()

    if args.html or args.images:
        from backtest.report_generator import ReportGenerator
        gen = ReportGenerator(result, output_dir=args.out)
        prefix = f"reality_arb_{args.preset}_{args.days}d"
        if args.html:
            gen.save_html(f"{prefix}.html")
        if args.images:
            gen.save_images(prefix)


if __name__ == "__main__":
    main()
