"""
Unified Real-Data Backtest Runner.

Combines:
  1. Binary Arbitrage backtest  — uses Polymarket Gamma API (free, no auth)
  2. Reality Arbitrage backtest — uses TheOddsAPI real scores (free tier)

Both use real data, generate HTML + PNG reports, and print a combined summary.

Usage:
    # Full run: both strategies, standard preset, HTML + images
    python -m backtest.run_real_backtest

    # Binary arb only (30-day window)
    python -m backtest.run_real_backtest --strategy binary --days 30

    # Reality arb only (last 3 days of sports results)
    python -m backtest.run_real_backtest --strategy reality --sport nba

    # Custom preset + only HTML report
    python -m backtest.run_real_backtest --preset aggressive_50000 --html-only
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import RISK_PRESETS
from utils.logger import get_logger, setup_logging

logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Binary Arb — real Polymarket data
# ─────────────────────────────────────────────────────────────────────────────

async def run_binary_arb_backtest(
    preset_name: str,
    days: int,
    tags: Optional[List[str]] = None,
    min_volume: float = 1000,
) -> Optional[object]:
    """
    Fetches real Polymarket historical data and runs the binary arb backtest.
    Returns BacktestResult or None on error.
    """
    from backtest.data_fetcher import HistoricalDataFetcher
    from backtest.backtester import BinaryArbitrageBacktester

    risk = RISK_PRESETS[preset_name]
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=days)
    start_str = start_dt.strftime("%Y-%m-%d")
    end_str = end_dt.strftime("%Y-%m-%d")

    print(f"\n  [Binary Arb] Downloading Polymarket data ({start_str} → {end_str})...")

    try:
        async with HistoricalDataFetcher() as fetcher:
            dataset = await fetcher.build_historical_dataset(
                start_date=start_str,
                end_date=end_str,
                min_volume=min_volume,
                tags=tags,
            )

        print(f"  [Binary Arb] {len(dataset)} markets with price history, running simulation...")

        backtester = BinaryArbitrageBacktester(risk_config=risk)
        result = backtester.run(dataset)
        return result

    except Exception as exc:
        print(f"  [Binary Arb] ERROR: {exc}")
        logger.exception("Binary arb backtest failed")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Reality Arb — real TheOddsAPI data
# ─────────────────────────────────────────────────────────────────────────────

def run_reality_arb_backtest(
    preset_name: str,
    sports: Optional[List[str]] = None,
    days_from: int = 3,
) -> Optional[object]:
    """
    Fetches real sports scores from TheOddsAPI and runs the reality arb backtest.
    Returns BacktestResult or None on error.
    """
    from backtest.reality_arb_backtest import RealityArbBacktester, SPORT_KEYS, _WIN_PROB_FN

    risk = RISK_PRESETS[preset_name]

    if sports is None:
        sports = [v for v in SPORT_KEYS.values() if _WIN_PROB_FN.get(v, (None,))[0] is not None]

    print(f"\n  [Reality Arb] Fetching real scores ({days_from} days) for: {', '.join(sports)}")

    try:
        backtester = RealityArbBacktester(
            risk_config=risk,
            days_from=days_from,
        )
        result = backtester.run(sports=sports, preset_name=preset_name)
        return result
    except Exception as exc:
        print(f"  [Reality Arb] ERROR: {exc}")
        logger.exception("Reality arb backtest failed")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Report generation
# ─────────────────────────────────────────────────────────────────────────────

def generate_reports(
    result,
    output_dir: str,
    prefix: str,
    html: bool,
    images: bool,
) -> None:
    from backtest.report_generator import ReportGenerator
    gen = ReportGenerator(result, output_dir=output_dir)
    if html:
        path = gen.save_html(f"{prefix}.html")
        print(f"  Report → {path}")
    if images:
        paths = gen.save_images(prefix)
        for p in paths:
            print(f"  Image  → {p}")


def _print_fallback_note() -> None:
    print("""
  ─────────────────────────────────────────────────────────
  Note: The Polymarket Gamma API may have limited historical
  data or rate-limiting. If no data was returned, try:
    • A shorter --days window (7-30 days works best)
    • Adding --tags Sports to filter relevant markets
    • The demo report: python -m backtest.demo_report
  ─────────────────────────────────────────────────────────
""")


# ─────────────────────────────────────────────────────────────────────────────
# Combined summary printer
# ─────────────────────────────────────────────────────────────────────────────

def print_summary(binary_result, reality_result) -> None:
    print(f"\n{'═'*60}")
    print("  BACKTEST SUMMARY — REAL DATA")
    print(f"{'═'*60}")

    for label, result in [("Binary Arb (Polymarket)", binary_result),
                           ("Reality Arb (TheOddsAPI)", reality_result)]:
        if result is None:
            print(f"\n  {label}: No data available")
            continue
        print(f"\n  {label}")
        print(f"  {'─'*40}")
        print(f"  Period       : {result.start_date} → {result.end_date}")
        print(f"  Markets scanned : {result.total_markets_scanned:,}")
        print(f"  Opportunities   : {result.opportunities_found:,}")
        print(f"  Trades executed : {result.trades_executed:,}")
        print(f"  Total P&L       : ${result.total_profit:+,.2f}")
        print(f"  ROI             : {result.roi_pct:+.2f}%")
        print(f"  Win rate        : {result.win_rate:.1f}%")
        print(f"  Sharpe ratio    : {result.sharpe_ratio:.2f}")
        print(f"  Max drawdown    : ${result.max_drawdown:.2f}")
        print(f"  Profit factor   : {result.profit_factor:.2f}")

    print(f"\n{'═'*60}\n")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

SPORT_CHOICES = ["nba", "nfl", "nhl", "epl", "all"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run real-data backtests (Polymarket + TheOddsAPI)"
    )
    parser.add_argument(
        "--strategy", choices=["binary", "reality", "both"], default="both",
        help="Which strategy to backtest",
    )
    parser.add_argument(
        "--preset", default="standard_10000", choices=list(RISK_PRESETS.keys()),
        help="Risk/capital preset",
    )
    parser.add_argument(
        "--days", type=int, default=30,
        help="Days of Polymarket history for binary arb (7-90 recommended)",
    )
    parser.add_argument(
        "--sport", choices=SPORT_CHOICES, default="all",
        help="Sport for reality arb (default: all supported sports)",
    )
    parser.add_argument(
        "--odds-days", type=int, default=3,
        help="Days of TheOddsAPI scores to use (max 3 on free tier)",
    )
    parser.add_argument(
        "--tags", nargs="+", default=None,
        help="Filter Polymarket markets by tags, e.g. --tags Sports NBA",
    )
    parser.add_argument(
        "--html-only", action="store_true",
        help="Generate HTML report only (no PNG images)",
    )
    parser.add_argument(
        "--images-only", action="store_true",
        help="Generate PNG images only (no HTML)",
    )
    parser.add_argument(
        "--out", default="data/backtest_results",
        help="Output directory for reports",
    )
    args = parser.parse_args()

    do_html = not args.images_only
    do_images = not args.html_only

    setup_logging()
    Path(args.out).mkdir(parents=True, exist_ok=True)

    from backtest.reality_arb_backtest import SPORT_KEYS
    sports_list: Optional[List[str]] = None
    if args.sport != "all":
        sports_list = [SPORT_KEYS[args.sport]]

    binary_result = None
    reality_result = None

    # ── Binary Arb ──────────────────────────────────────────────────────────
    if args.strategy in ("binary", "both"):
        binary_result = asyncio.run(
            run_binary_arb_backtest(
                preset_name=args.preset,
                days=args.days,
                tags=args.tags,
                min_volume=500,
            )
        )
        if binary_result is not None:
            prefix = f"binary_arb_real_{args.preset}_{args.days}d"
            generate_reports(binary_result, args.out, prefix, do_html, do_images)

    # ── Reality Arb ─────────────────────────────────────────────────────────
    if args.strategy in ("reality", "both"):
        reality_result = run_reality_arb_backtest(
            preset_name=args.preset,
            sports=sports_list,
            days_from=args.odds_days,
        )
        if reality_result is not None:
            prefix = f"reality_arb_real_{args.preset}_{args.odds_days}d"
            generate_reports(reality_result, args.out, prefix, do_html, do_images)

    # ── Summary ─────────────────────────────────────────────────────────────
    print_summary(binary_result, reality_result)

    if binary_result is None and reality_result is None:
        print("  No results to display. Check your API connections and try again.")
        sys.exit(1)


if __name__ == "__main__":
    main()
