"""
Générateur de rapport de démonstration — données synthétiques réalistes.

Lance ce script directement (sans clés API) pour voir immédiatement
à quoi ressemblent les rapports HTML et images PNG.

Usage :
    python -m backtest.demo_report                           # HTML + images
    python -m backtest.demo_report --html-only               # HTML uniquement
    python -m backtest.demo_report --preset aggressive_50000 # Autre capital
    python -m backtest.demo_report --period 2y               # Simulation 2 ans
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# Ajouter le répertoire racine au path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.backtester import BacktestResult, BacktestTrade
from backtest.report_generator import ReportGenerator
from config.settings import RISK_PRESETS


# ─────────────────────────────────────────────────────────────────────────────
# Générateur de données synthétiques calqué sur des backtests réels Polymarket
# ─────────────────────────────────────────────────────────────────────────────

SAMPLE_MARKETS = [
    "Will the Cleveland Guardians win vs Chicago White Sox?",
    "Will Novak Djokovic win the Australian Open 2024?",
    "Will the Lakers beat the Warriors tonight?",
    "Will Bitcoin exceed $60,000 before February 2024?",
    "Will Donald Trump win the New Hampshire primary?",
    "Will the Bills cover the spread vs the Chiefs?",
    "Will LeBron James score over 27.5 points tonight?",
    "Will Sweden win against Denmark in handball?",
    "Will Nvidia's stock close above $600 this week?",
    "Will the ECB cut rates in January 2024?",
    "Will Taylor Swift attend the Super Bowl?",
    "Will Elon Musk post more than 50 tweets today?",
    "Will GPT-5 be released before March 2024?",
    "Will the Celtics win the NBA Championship 2024?",
    "Will the Patriots make the playoffs this season?",
    "Will Lionel Messi win the Ballon d'Or 2024?",
    "Will Apple announce Vision Pro sales above 1M units Q1?",
    "Will the S&P 500 end 2024 above 5000?",
    "Will Rafa Nadal win the French Open 2024?",
    "Will there be a US government shutdown in Q1 2024?",
]


def _generate_synthetic_trades(
    preset_name: str,
    days: int,
    seed: int = 42,
) -> list[BacktestTrade]:
    """
    Génère des trades synthétiques réalistes basés sur les caractéristiques
    de l'arbitrage binaire Polymarket observées en conditions réelles.

    Paramètres calés sur les résultats @swisstony :
    - Profit par trade : $5–$120 (distribution log-normale)
    - Win rate : 97-99% (l'arbitrage binaire est quasi-certain)
    - Trades par jour : 5–30 selon le capital
    - Spreads observés : 0.87–0.98 (YES+NO)
    """
    rng = random.Random(seed)
    risk = RISK_PRESETS[preset_name]
    capital = risk.max_total_exposure

    # Calibrage selon le capital
    base_trades_per_day = max(3, min(25, int(capital / 800)))
    base_profit = max(0.3, min(50, capital / 400))  # Profit moyen par trade

    now = time.time()
    start_ts = now - days * 86400

    trades = []
    ts = start_ts
    cumulative_pnl = 0.0

    # Simuler la croissance progressive de la détection d'opportunités
    # (le bot s'améliore au fil du temps — apprentissage de la cartographie des marchés)
    ramp_up_days = min(30, days // 4)

    for day_idx in range(days):
        ts_day = start_ts + day_idx * 86400

        # Ramp-up: moins de trades au début
        day_factor = min(1.0, (day_idx + 1) / ramp_up_days) if day_idx < ramp_up_days else 1.0

        # Légère saisonnalité : moins de marchés le week-end (moins d'events sportifs)
        dow = datetime.fromtimestamp(ts_day).weekday()
        weekend_factor = 0.6 if dow >= 5 else 1.0

        # Légère variance journalière
        daily_vol = rng.gauss(1.0, 0.25)
        n_trades = max(0, int(base_trades_per_day * day_factor * weekend_factor * daily_vol))

        for _ in range(n_trades):
            # Horodatage dans la journée (trading 06h–22h UTC)
            hour = rng.uniform(6, 22)
            trade_ts = ts_day + hour * 3600

            # Spread YES+NO : distribution réaliste centrée sur 0.93 (7% de marge)
            spread = rng.triangular(0.86, 0.98, 0.93)
            yes_frac = rng.uniform(0.4, 0.6)
            price_yes = round(spread * yes_frac, 4)
            price_no = round(spread * (1 - yes_frac), 4)
            actual_spread = price_yes + price_no

            # Profit brut par contrat
            gross_per_unit = 1.0 - actual_spread
            fees_per_unit = actual_spread * 0.0004
            net_per_unit = gross_per_unit - fees_per_unit

            if net_per_unit < 0.005:
                continue

            # Taille (en contrats) — limitée par le capital disponible
            max_size = risk.max_per_trade / max(actual_spread, 0.01)
            # Distribution log-normale de la taille (parfois gros, souvent petits)
            size = min(max_size, max(1, rng.lognormvariate(
                math.log(max(1, capital / 500)), 0.6
            )))
            size = int(size)

            total_cost = actual_spread * size
            gross_profit = gross_per_unit * size
            fees = fees_per_unit * size
            net_profit = net_per_unit * size

            # Simuler les trades qui échouent (slippage, latence, liquidité)
            # Win rate ~97% pour l'arb binaire
            success = rng.random() < 0.97
            if not success:
                # Trade avorté : pas de profit, parfois une légère perte (slippage)
                net_profit = rng.uniform(-total_cost * 0.01, 0)
                fees = total_cost * 0.0004

            # Résolution : YES ou NO (le profit est identique dans les deux cas pour l'arb binaire)
            resolution = "YES" if rng.random() < 0.5 else "NO"

            trade = BacktestTrade(
                timestamp=trade_ts,
                market_id=f"market_{rng.randint(1000, 9999)}",
                question=rng.choice(SAMPLE_MARKETS),
                strategy="BINARY_ARB",
                price_yes=price_yes,
                price_no=price_no,
                size=size,
                total_cost=round(total_cost, 4),
                gross_profit=round(gross_profit, 4),
                net_profit=round(net_profit, 4),
                fees=round(fees, 4),
                resolution=resolution,
                realized_pnl=round(net_profit, 4),
            )
            trades.append(trade)

    trades.sort(key=lambda t: t.timestamp)
    return trades


def _compute_result(
    trades: list[BacktestTrade],
    preset_name: str,
    start_date: str,
    end_date: str,
) -> BacktestResult:
    """Calcule toutes les métriques à partir des trades synthétiques."""
    from collections import defaultdict

    risk = RISK_PRESETS[preset_name]
    capital = risk.max_total_exposure

    total_profit = sum(t.realized_pnl for t in trades)
    total_fees = sum(t.fees for t in trades)
    total_invested = sum(t.total_cost for t in trades)
    winning = [t for t in trades if t.realized_pnl > 0]
    win_rate = len(winning) / len(trades) * 100 if trades else 0

    # Equity curve
    equity_curve = []
    cum = 0.0
    for t in trades:
        cum += t.realized_pnl
        equity_curve.append((t.timestamp, round(cum, 4)))

    # Drawdown
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
        std = math.sqrt(var) if var > 0 else 1e-9
        sharpe = (avg_r / std) * math.sqrt(252)
    else:
        sharpe = 0.0

    # Profit factor
    gross_wins = sum(t.realized_pnl for t in trades if t.realized_pnl > 0)
    gross_losses = abs(sum(t.realized_pnl for t in trades if t.realized_pnl < 0))
    pf = gross_wins / max(gross_losses, 0.01)

    roi_pct = (total_profit / capital) * 100

    return BacktestResult(
        strategy="BINARY_ARB",
        start_date=start_date,
        end_date=end_date,
        risk_preset=preset_name,
        total_markets_scanned=len(trades) * 12,  # ~12 marchés scannés par trade exécuté
        opportunities_found=int(len(trades) * 1.4),
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
# Entrée principale
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Génère un rapport de backtest de démonstration (données synthétiques réalistes)"
    )
    parser.add_argument(
        "--preset", default="standard_10000",
        choices=list(RISK_PRESETS.keys()),
        help="Preset de capital/risque",
    )
    parser.add_argument(
        "--period", default="1y",
        choices=["3m", "6m", "1y", "2y"],
        help="Période de simulation",
    )
    parser.add_argument(
        "--html-only", action="store_true",
        help="Générer uniquement le rapport HTML (sans les images PNG)",
    )
    parser.add_argument(
        "--images-only", action="store_true",
        help="Générer uniquement les images PNG",
    )
    parser.add_argument(
        "--out", default="data/backtest_results",
        help="Dossier de sortie",
    )
    parser.add_argument("--seed", type=int, default=42, help="Graine aléatoire")
    args = parser.parse_args()

    period_days = {"3m": 90, "6m": 180, "1y": 365, "2y": 730}[args.period]
    risk = RISK_PRESETS[args.preset]
    capital = risk.max_total_exposure

    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=period_days)
    start_str = start_dt.strftime("%Y-%m-%d")
    end_str = end_dt.strftime("%Y-%m-%d")

    print(f"""
╔══════════════════════════════════════════════════════════╗
║  Polymarket Arbitrage Bot — Rapport de démonstration     ║
╠══════════════════════════════════════════════════════════╣
  Preset   : {args.preset}
  Capital  : ${capital:,.0f}
  Période  : {start_str} → {end_str} ({args.period})
  Sortie   : {Path(args.out).resolve()}
╚══════════════════════════════════════════════════════════╝
""")

    print("  ⏳ Génération des données synthétiques...")
    trades = _generate_synthetic_trades(args.preset, period_days, args.seed)
    result = _compute_result(trades, args.preset, start_str, end_str)

    print(f"  ✓ {len(trades):,} trades simulés")
    print(f"  ✓ P&L total : ${result.total_profit:+,.2f}")
    print(f"  ✓ ROI       : {result.roi_pct:+.1f}%")
    print(f"  ✓ Win rate  : {result.win_rate:.1f}%")
    print(f"  ✓ Sharpe    : {result.sharpe_ratio:.2f}")
    print()

    gen = ReportGenerator(result, output_dir=args.out)
    prefix = f"demo_{args.preset}_{args.period}"

    if not args.images_only:
        gen.save_html(f"{prefix}.html")

    if not args.html_only:
        gen.save_images(prefix)

    print()
    print("  Ouvrez le rapport HTML dans votre navigateur pour voir les graphiques interactifs.")


if __name__ == "__main__":
    main()
