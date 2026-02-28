"""
Générateur de rapports de backtest.

Produit deux types de sorties :
  1. Rapport HTML standalone — graphiques interactifs Chart.js, entièrement auto-contenu
  2. Images PNG — via matplotlib, pour l'archivage ou le partage hors navigateur

Usage programmatique :
    from backtest.report_generator import ReportGenerator
    gen = ReportGenerator(result, output_dir="data/backtest_results")
    gen.save_html("rapport_2024.html")
    gen.save_images("rapport_2024")   # → equity_curve.png, monthly_pnl.png …
"""

from __future__ import annotations

import base64
import io
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backtest.backtester import BacktestResult, BacktestTrade
from backtest.results_analyzer import ResultsAnalyzer
from utils.logger import get_logger

logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ts_to_date(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _monthly(trades: List[BacktestTrade]) -> Dict[str, Dict]:
    data: Dict[str, Dict] = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0})
    for t in trades:
        key = datetime.fromtimestamp(t.timestamp).strftime("%Y-%m")
        data[key]["pnl"] += t.realized_pnl
        data[key]["trades"] += 1
        if t.realized_pnl > 0:
            data[key]["wins"] += 1
    for v in data.values():
        v["pnl"] = round(v["pnl"], 2)
        v["win_rate"] = round(v["wins"] / max(v["trades"], 1) * 100, 1)
    return dict(sorted(data.items()))


def _capital_scaling(result: BacktestResult) -> Dict[float, Dict]:
    capitals = [100, 500, 1_000, 5_000, 10_000, 25_000, 50_000, 100_000]
    base = 10_000
    out = {}
    for cap in capitals:
        factor = cap / base
        # La liquidité Polymarket sature au-delà de ~$30k déployés simultanément
        # Appliquer une atténuation pour les gros capitaux
        liquidity_penalty = min(1.0, (30_000 / cap) ** 0.3) if cap > 30_000 else 1.0
        effective_factor = factor * liquidity_penalty
        out[cap] = {
            "capital": cap,
            "profit": round(result.total_profit * effective_factor, 2),
            "roi_pct": round(result.roi_pct * liquidity_penalty, 2),
            "drawdown": round(result.max_drawdown * effective_factor, 2),
            "note": "Liquidité saturée — rendements réduits" if cap > 30_000 else "",
        }
    return out


def _spread_buckets(trades: List[BacktestTrade]) -> Tuple[List[str], List[int]]:
    """Distribution des spreads YES+NO au moment du trade."""
    edges = [i / 100 for i in range(85, 101)]  # 0.85 à 1.00 par 0.01
    counts = [0] * (len(edges) - 1)
    for t in trades:
        spread = t.price_yes + t.price_no
        for i in range(len(edges) - 1):
            if edges[i] <= spread < edges[i + 1]:
                counts[i] += 1
                break
    labels = [f"{edges[i]:.2f}–{edges[i+1]:.2f}" for i in range(len(edges) - 1)]
    return labels, counts


def _profit_buckets(trades: List[BacktestTrade]) -> Tuple[List[str], List[int]]:
    """Distribution des profits par trade."""
    profits = [t.realized_pnl for t in trades]
    if not profits:
        return [], []
    lo, hi = min(profits), max(profits)
    if lo == hi:
        return [f"{lo:.2f}"], [len(profits)]
    step = (hi - lo) / 15
    edges = [lo + i * step for i in range(16)]
    counts = [0] * 15
    for p in profits:
        idx = min(int((p - lo) / step), 14)
        counts[idx] += 1
    labels = [f"{edges[i]:.2f}" for i in range(15)]
    return labels, counts


def _drawdown_series(equity: List[Tuple[float, float]]) -> List[float]:
    """Série du drawdown courant (pic - valeur) pour chaque point de la courbe."""
    peak = 0.0
    series = []
    for _, pnl in equity:
        if pnl > peak:
            peak = pnl
        series.append(round(peak - pnl, 4))
    return series


# ─────────────────────────────────────────────────────────────────────────────
# Générateur principal
# ─────────────────────────────────────────────────────────────────────────────

class ReportGenerator:
    """
    Génère un rapport HTML riche et/ou des images PNG à partir d'un BacktestResult.
    """

    def __init__(self, result: BacktestResult, output_dir: str = "data/backtest_results"):
        self.result = result
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Pré-calculer toutes les données dérivées
        self._monthly = _monthly(result.trades)
        self._scaling = _capital_scaling(result)
        self._spread_labels, self._spread_counts = _spread_buckets(result.trades)
        self._profit_labels, self._profit_counts = _profit_buckets(result.trades)
        self._drawdown_series = _drawdown_series(result.equity_curve)

        # Courbe des capitaux — dater les points
        self._equity_dates = [
            _ts_to_date(ts) for ts, _ in result.equity_curve
        ] if result.equity_curve else []
        self._equity_values = [pnl for _, pnl in result.equity_curve]

    # ──────────────────────────────────────────────────────────────────────────
    # HTML
    # ──────────────────────────────────────────────────────────────────────────

    def save_html(self, filename: str = "backtest_report.html") -> Path:
        """Génère et sauvegarde le rapport HTML standalone."""
        path = self.output_dir / filename
        html = self._build_html()
        path.write_text(html, encoding="utf-8")
        logger.info(f"Rapport HTML sauvegardé : {path}")
        print(f"\n  ✅ Rapport HTML : {path.resolve()}")
        return path

    def _build_html(self) -> str:
        r = self.result

        # Sérialiser les données JSON pour Chart.js
        equity_dates_json = json.dumps(self._equity_dates)
        equity_values_json = json.dumps(self._equity_values)
        drawdown_json = json.dumps(self._drawdown_series)

        monthly_labels = json.dumps(list(self._monthly.keys()))
        monthly_pnls = json.dumps([v["pnl"] for v in self._monthly.values()])
        monthly_trades = json.dumps([v["trades"] for v in self._monthly.values()])
        monthly_colors = json.dumps(
            ["rgba(52,211,153,0.8)" if v["pnl"] >= 0 else "rgba(248,113,113,0.8)"
             for v in self._monthly.values()]
        )

        scaling_labels = json.dumps([f"${c:,.0f}" for c in self._scaling])
        scaling_profits = json.dumps([v["profit"] for v in self._scaling.values()])
        scaling_rois = json.dumps([v["roi_pct"] for v in self._scaling.values()])

        spread_labels_json = json.dumps(self._spread_labels)
        spread_counts_json = json.dumps(self._spread_counts)

        profit_labels_json = json.dumps(self._profit_labels)
        profit_counts_json = json.dumps(self._profit_counts)

        # Tableau des trades (50 derniers)
        trades_rows = self._build_trades_table_rows()

        # Tableau scaling
        scaling_rows = self._build_scaling_table_rows()

        # Indicateur de rentabilité
        pnl_color = "#34d399" if r.total_profit >= 0 else "#f87171"
        pnl_sign = "+" if r.total_profit >= 0 else ""
        roi_badge = "positive" if r.roi_pct >= 0 else "negative"

        # Badge Sharpe
        sharpe_color = "#34d399" if r.sharpe_ratio >= 1 else ("#facc15" if r.sharpe_ratio >= 0 else "#f87171")

        generated_at = datetime.now().strftime("%d/%m/%Y à %H:%M")

        return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Backtest — {r.strategy} ({r.start_date} → {r.end_date})</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3.0.1/dist/chartjs-plugin-annotation.min.js"></script>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'Segoe UI',system-ui,sans-serif;background:#0f1117;color:#e2e8f0;min-height:100vh}}
  a{{color:#60a5fa}}
  /* ── Header ── */
  .header{{background:linear-gradient(135deg,#1e2130 0%,#1a1d27 100%);
           border-bottom:1px solid #2d3748;padding:28px 32px 20px}}
  .header-top{{display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:12px}}
  .header h1{{font-size:1.5rem;font-weight:700;letter-spacing:-.02em}}
  .header h1 span{{color:#60a5fa}}
  .badge{{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;
          border-radius:20px;font-size:.8rem;font-weight:600;letter-spacing:.02em}}
  .badge-green{{background:#064e3b;color:#34d399;border:1px solid #065f46}}
  .badge-red{{background:#7f1d1d;color:#f87171;border:1px solid #991b1b}}
  .badge-blue{{background:#1e3a5f;color:#93c5fd;border:1px solid #1d4ed8}}
  .badge-yellow{{background:#451a03;color:#fbbf24;border:1px solid #92400e}}
  .header-meta{{font-size:.8rem;color:#64748b;margin-top:6px}}
  /* ── KPI grid ── */
  .kpi-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
             gap:16px;padding:24px 32px}}
  .kpi{{background:#1a1d27;border:1px solid #2d3748;border-radius:14px;
        padding:20px 22px;position:relative;overflow:hidden}}
  .kpi::before{{content:'';position:absolute;top:0;left:0;right:0;height:3px;
                background:var(--accent,#3b82f6)}}
  .kpi-label{{font-size:.72rem;color:#94a3b8;text-transform:uppercase;
              letter-spacing:.06em;margin-bottom:6px}}
  .kpi-value{{font-size:2rem;font-weight:800;line-height:1}}
  .kpi-sub{{font-size:.78rem;color:#64748b;margin-top:4px}}
  .c-green{{color:#34d399;--accent:#34d399}}
  .c-red{{color:#f87171;--accent:#f87171}}
  .c-blue{{color:#60a5fa;--accent:#60a5fa}}
  .c-yellow{{color:#fbbf24;--accent:#fbbf24}}
  .c-purple{{color:#a78bfa;--accent:#a78bfa}}
  .c-cyan{{color:#22d3ee;--accent:#22d3ee}}
  /* ── Section ── */
  .section{{margin:0 32px 32px}}
  .section-title{{font-size:1.05rem;font-weight:600;color:#94a3b8;
                  margin-bottom:14px;display:flex;align-items:center;gap:8px}}
  .section-title::after{{content:'';flex:1;height:1px;background:#2d3748}}
  /* ── Chart card ── */
  .chart-card{{background:#1a1d27;border:1px solid #2d3748;border-radius:14px;padding:24px}}
  .chart-row{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}
  @media(max-width:900px){{.chart-row{{grid-template-columns:1fr}}}}
  /* ── Table ── */
  .table-wrap{{overflow-x:auto;border-radius:14px;border:1px solid #2d3748}}
  table{{width:100%;border-collapse:collapse;font-size:.85rem}}
  thead th{{background:#1e2130;padding:12px 16px;text-align:left;
            font-size:.72rem;text-transform:uppercase;color:#64748b;
            letter-spacing:.06em;white-space:nowrap}}
  tbody td{{padding:10px 16px;border-bottom:1px solid #1e2130;white-space:nowrap}}
  tbody tr:last-child td{{border-bottom:none}}
  tbody tr:hover td{{background:#1e2130}}
  .tag{{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.72rem;font-weight:600}}
  .tag-green{{background:#064e3b;color:#34d399}}
  .tag-red{{background:#7f1d1d;color:#f87171}}
  .tag-blue{{background:#1e3a5f;color:#93c5fd}}
  /* ── Scaling table ── */
  .scale-row-highlight{{background:#1e2130 !important}}
  /* ── Footer ── */
  footer{{text-align:center;padding:24px;color:#334155;font-size:.78rem;border-top:1px solid #1e2130;margin-top:20px}}
  /* ── Warn box ── */
  .warn{{background:#1c1a0a;border:1px solid #713f12;border-radius:10px;
         padding:14px 18px;font-size:.82rem;color:#fbbf24;margin-bottom:24px}}
  .warn strong{{color:#f59e0b}}
</style>
</head>
<body>

<!-- ═══════════════════════ HEADER ═══════════════════════ -->
<div class="header">
  <div class="header-top">
    <div>
      <h1>📊 Backtest — <span>{r.strategy}</span></h1>
      <div class="header-meta">
        Période : <strong>{r.start_date}</strong> → <strong>{r.end_date}</strong> &nbsp;|&nbsp;
        Preset risque : <strong>{r.risk_preset}</strong> &nbsp;|&nbsp;
        Généré le {generated_at}
      </div>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <span class="badge {'badge-green' if r.total_profit >= 0 else 'badge-red'}">
        {pnl_sign}${r.total_profit:,.2f} P&L
      </span>
      <span class="badge {'badge-green' if r.roi_pct >= 0 else 'badge-red'}">
        {pnl_sign}{r.roi_pct:.1f}% ROI
      </span>
      <span class="badge badge-blue">
        {r.trades_executed:,} trades
      </span>
    </div>
  </div>
</div>

<!-- ═══════════════════════ KPIs ═══════════════════════ -->
<div class="kpi-grid">
  <div class="kpi c-green">
    <div class="kpi-label">P&L Total</div>
    <div class="kpi-value">{pnl_sign}${r.total_profit:,.2f}</div>
    <div class="kpi-sub">Sur capital ${self._scaling.get(10000, {}).get('capital', 10000):,}</div>
  </div>
  <div class="kpi c-blue">
    <div class="kpi-label">ROI Période</div>
    <div class="kpi-value">{pnl_sign}{r.roi_pct:.1f}%</div>
    <div class="kpi-sub">{r.start_date} → {r.end_date}</div>
  </div>
  <div class="kpi c-cyan">
    <div class="kpi-label">Trades exécutés</div>
    <div class="kpi-value">{r.trades_executed:,}</div>
    <div class="kpi-sub">sur {r.total_markets_scanned:,} marchés scannés</div>
  </div>
  <div class="kpi {'c-green' if r.win_rate >= 50 else 'c-red'}">
    <div class="kpi-label">Win Rate</div>
    <div class="kpi-value">{r.win_rate:.1f}%</div>
    <div class="kpi-sub">Arb binaire ≈ quasi-certitude</div>
  </div>
  <div class="kpi c-purple">
    <div class="kpi-label">Sharpe Ratio</div>
    <div class="kpi-value" style="color:{sharpe_color}">{r.sharpe_ratio:.2f}</div>
    <div class="kpi-sub">{'Excellent (> 1)' if r.sharpe_ratio >= 1 else ('Correct (> 0)' if r.sharpe_ratio >= 0 else 'Faible (< 0)')}</div>
  </div>
  <div class="kpi c-red">
    <div class="kpi-label">Max Drawdown</div>
    <div class="kpi-value">-${r.max_drawdown:,.2f}</div>
    <div class="kpi-sub">Pire perte de creux à pic</div>
  </div>
  <div class="kpi c-yellow">
    <div class="kpi-label">Profit Factor</div>
    <div class="kpi-value">{r.profit_factor:.2f}x</div>
    <div class="kpi-sub">Gains bruts / Pertes brutes</div>
  </div>
  <div class="kpi c-blue">
    <div class="kpi-label">Profit moyen / trade</div>
    <div class="kpi-value">${r.avg_profit_per_trade:.3f}</div>
    <div class="kpi-sub">Frais payés : ${r.total_fees:,.2f}</div>
  </div>
</div>

<!-- ═══════════════════════ EQUITY CURVE ═══════════════════════ -->
<div class="section">
  <div class="section-title">📈 Courbe des Capitaux (Cumulative P&L)</div>
  <div class="chart-card">
    <canvas id="equityChart" height="110"></canvas>
  </div>
</div>

<!-- ═══════════════════════ DRAWDOWN ═══════════════════════ -->
<div class="section">
  <div class="section-title">📉 Drawdown (creux depuis le pic)</div>
  <div class="chart-card">
    <canvas id="drawdownChart" height="70"></canvas>
  </div>
</div>

<!-- ═══════════════════════ MONTHLY P&L + TRADES ═══════════════════════ -->
<div class="section">
  <div class="section-title">📅 Performance Mensuelle</div>
  <div class="chart-row">
    <div class="chart-card">
      <div style="font-size:.85rem;color:#94a3b8;margin-bottom:10px">P&L par mois ($)</div>
      <canvas id="monthlyPnlChart" height="220"></canvas>
    </div>
    <div class="chart-card">
      <div style="font-size:.85rem;color:#94a3b8;margin-bottom:10px">Nombre de trades par mois</div>
      <canvas id="monthlyTradesChart" height="220"></canvas>
    </div>
  </div>
</div>

<!-- ═══════════════════════ DISTRIBUTIONS ═══════════════════════ -->
<div class="section">
  <div class="section-title">🔍 Distributions</div>
  <div class="chart-row">
    <div class="chart-card">
      <div style="font-size:.85rem;color:#94a3b8;margin-bottom:10px">
        Distribution des spreads YES+NO au moment du trade
      </div>
      <canvas id="spreadChart" height="220"></canvas>
    </div>
    <div class="chart-card">
      <div style="font-size:.85rem;color:#94a3b8;margin-bottom:10px">
        Distribution des profits par trade ($)
      </div>
      <canvas id="profitHistChart" height="220"></canvas>
    </div>
  </div>
</div>

<!-- ═══════════════════════ SCALING ═══════════════════════ -->
<div class="section">
  <div class="section-title">💰 Scalabilité — Capital Investi vs Profit Projeté</div>
  <div class="chart-card" style="margin-bottom:20px">
    <canvas id="scalingChart" height="90"></canvas>
  </div>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Capital investi</th>
          <th>Profit projeté</th>
          <th>ROI estimé</th>
          <th>Max Drawdown</th>
          <th>Note</th>
        </tr>
      </thead>
      <tbody>
        {scaling_rows}
      </tbody>
    </table>
  </div>
</div>

<!-- ═══════════════════════ AVERTISSEMENT ═══════════════════════ -->
<div class="section">
  <div class="warn">
    <strong>⚠️ Avertissement :</strong>
    Les performances passées ne garantissent pas les performances futures.
    Ce backtest est une simulation sur données historiques réelles Polymarket, mais sans tenir compte
    de la compétition entre bots, des changements de frais dynamiques, ou de la latence réelle d'exécution.
    Toujours tester en <strong>DRY-RUN</strong> avant de déployer du capital réel.
  </div>
</div>

<!-- ═══════════════════════ TRADES TABLE ═══════════════════════ -->
<div class="section">
  <div class="section-title">📋 Derniers Trades Simulés</div>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>#</th><th>Date</th><th>Question (extrait)</th>
          <th>Prix YES</th><th>Prix NO</th><th>Spread</th>
          <th>Taille</th><th>Profit net</th><th>Résolution</th>
        </tr>
      </thead>
      <tbody>
        {trades_rows}
      </tbody>
    </table>
  </div>
</div>

<footer>
  Polymarket Arbitrage Bot — Rapport généré le {generated_at} &nbsp;|&nbsp;
  Données : Polymarket Gamma API &nbsp;|&nbsp; ⚠️ Usage éducatif uniquement
</footer>

<script>
Chart.defaults.color = '#94a3b8';
Chart.defaults.borderColor = '#2d3748';
Chart.defaults.font.family = "'Segoe UI', system-ui, sans-serif";

const eqDates  = {equity_dates_json};
const eqValues = {equity_values_json};
const ddValues = {drawdown_json};

// ── Equity Curve ──
new Chart(document.getElementById('equityChart'), {{
  type: 'line',
  data: {{
    labels: eqDates,
    datasets: [{{
      label: 'P&L cumulatif ($)',
      data: eqValues,
      borderColor: '#34d399',
      backgroundColor: (ctx) => {{
        const g = ctx.chart.ctx.createLinearGradient(0,0,0,300);
        g.addColorStop(0, 'rgba(52,211,153,0.18)');
        g.addColorStop(1, 'rgba(52,211,153,0.01)');
        return g;
      }},
      fill: true, tension: 0.35, pointRadius: 0, borderWidth: 2
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: true,
    plugins: {{
      legend: {{display: false}},
      tooltip: {{callbacks: {{label: ctx => ' $' + ctx.parsed.y.toFixed(2)}}}}
    }},
    scales: {{
      x: {{ticks: {{maxTicksLimit: 12, maxRotation: 0}}}},
      y: {{
        ticks: {{callback: v => '$' + v.toLocaleString('fr-FR', {{minimumFractionDigits:0}})}},
        grid: {{color: '#1e2130'}}
      }}
    }}
  }}
}});

// ── Drawdown ──
new Chart(document.getElementById('drawdownChart'), {{
  type: 'line',
  data: {{
    labels: eqDates,
    datasets: [{{
      label: 'Drawdown ($)',
      data: ddValues.map(v => -v),
      borderColor: '#f87171',
      backgroundColor: 'rgba(248,113,113,0.12)',
      fill: true, tension: 0.3, pointRadius: 0, borderWidth: 1.5
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: true,
    plugins: {{legend: {{display: false}},
      tooltip: {{callbacks: {{label: ctx => ' -$' + Math.abs(ctx.parsed.y).toFixed(2)}}}}
    }},
    scales: {{
      x: {{ticks: {{maxTicksLimit: 12, maxRotation: 0}}}},
      y: {{ticks: {{callback: v => '-$' + Math.abs(v).toFixed(0)}}, grid: {{color: '#1e2130'}}}}
    }}
  }}
}});

// ── Monthly P&L ──
new Chart(document.getElementById('monthlyPnlChart'), {{
  type: 'bar',
  data: {{
    labels: {monthly_labels},
    datasets: [{{
      label: 'P&L ($)',
      data: {monthly_pnls},
      backgroundColor: {monthly_colors},
      borderRadius: 4, borderSkipped: false
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{legend: {{display: false}},
      tooltip: {{callbacks: {{label: ctx => ' $' + ctx.parsed.y.toFixed(2)}}}}
    }},
    scales: {{
      x: {{ticks: {{maxRotation: 45}}}},
      y: {{ticks: {{callback: v => '$' + v}}, grid: {{color: '#1e2130'}}}}
    }}
  }}
}});

// ── Monthly Trades ──
new Chart(document.getElementById('monthlyTradesChart'), {{
  type: 'bar',
  data: {{
    labels: {monthly_labels},
    datasets: [{{
      label: 'Trades',
      data: {monthly_trades},
      backgroundColor: 'rgba(96,165,250,0.7)',
      borderRadius: 4, borderSkipped: false
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{legend: {{display: false}}}},
    scales: {{
      x: {{ticks: {{maxRotation: 45}}}},
      y: {{ticks: {{stepSize: 1}}, grid: {{color: '#1e2130'}}}}
    }}
  }}
}});

// ── Spread distribution ──
new Chart(document.getElementById('spreadChart'), {{
  type: 'bar',
  data: {{
    labels: {spread_labels_json},
    datasets: [{{
      label: 'Nombre de trades',
      data: {spread_counts_json},
      backgroundColor: 'rgba(167,139,250,0.75)',
      borderRadius: 3, borderSkipped: false
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{legend: {{display: false}},
      tooltip: {{callbacks: {{title: ctx => 'Spread: ' + ctx[0].label}}}}
    }},
    scales: {{
      x: {{ticks: {{maxRotation: 60, font: {{size: 10}}}}}},
      y: {{grid: {{color: '#1e2130'}}}}
    }}
  }}
}});

// ── Profit distribution ──
new Chart(document.getElementById('profitHistChart'), {{
  type: 'bar',
  data: {{
    labels: {profit_labels_json},
    datasets: [{{
      label: 'Fréquence',
      data: {profit_counts_json},
      backgroundColor: (ctx) => {{
        const v = parseFloat({profit_labels_json}[ctx.dataIndex] || 0);
        return v >= 0 ? 'rgba(52,211,153,0.75)' : 'rgba(248,113,113,0.75)';
      }},
      borderRadius: 3, borderSkipped: false
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{legend: {{display: false}},
      tooltip: {{callbacks: {{title: ctx => 'Profit: $' + ctx[0].label}}}}
    }},
    scales: {{
      x: {{ticks: {{maxRotation: 60, font: {{size: 10}}}}}},
      y: {{grid: {{color: '#1e2130'}}}}
    }}
  }}
}});

// ── Capital Scaling ──
new Chart(document.getElementById('scalingChart'), {{
  type: 'bar',
  data: {{
    labels: {scaling_labels},
    datasets: [
      {{
        label: 'Profit projeté ($)',
        data: {scaling_profits},
        backgroundColor: 'rgba(52,211,153,0.75)',
        borderRadius: 5, yAxisID: 'y'
      }},
      {{
        label: 'ROI (%)',
        data: {scaling_rois},
        type: 'line',
        borderColor: '#fbbf24',
        backgroundColor: 'transparent',
        pointBackgroundColor: '#fbbf24',
        tension: 0.3, yAxisID: 'y2', borderWidth: 2
      }}
    ]
  }},
  options: {{
    responsive: true, maintainAspectRatio: true,
    plugins: {{
      legend: {{labels: {{color: '#94a3b8', padding: 16}}}},
      tooltip: {{mode: 'index', intersect: false}}
    }},
    scales: {{
      x: {{}},
      y: {{
        position: 'left',
        ticks: {{callback: v => '$' + v.toLocaleString('fr-FR', {{maximumFractionDigits: 0}})}},
        grid: {{color: '#1e2130'}}
      }},
      y2: {{
        position: 'right',
        ticks: {{callback: v => v.toFixed(1) + '%'}},
        grid: {{drawOnChartArea: false}}
      }}
    }}
  }}
}});
</script>
</body>
</html>"""

    def _build_trades_table_rows(self) -> str:
        """HTML des lignes du tableau des derniers trades."""
        trades = self.result.trades[-100:]  # 100 derniers
        rows = []
        for i, t in enumerate(reversed(trades), 1):
            spread = t.price_yes + t.price_no
            profit_class = "c-green" if t.realized_pnl >= 0 else "c-red"
            sign = "+" if t.realized_pnl >= 0 else ""
            res_badge = (
                f'<span class="tag tag-green">YES</span>' if t.resolution == "YES"
                else f'<span class="tag tag-red">NO</span>' if t.resolution == "NO"
                else '<span class="tag tag-blue">?</span>'
            )
            rows.append(f"""
        <tr>
          <td style="color:#64748b">{i}</td>
          <td>{_ts_to_date(t.timestamp)}</td>
          <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis"
              title="{t.question}">{t.question[:55]}…</td>
          <td>${t.price_yes:.4f}</td>
          <td>${t.price_no:.4f}</td>
          <td style="color:{'#fbbf24' if spread < 0.95 else '#94a3b8'}">${spread:.4f}</td>
          <td>{t.size:.0f}</td>
          <td class="{profit_class}"><strong>{sign}${t.realized_pnl:.4f}</strong></td>
          <td>{res_badge}</td>
        </tr>""")
        return "".join(rows)

    def _build_scaling_table_rows(self) -> str:
        rows = []
        for cap, data in self._scaling.items():
            highlight = ' class="scale-row-highlight"' if cap == 10_000 else ""
            note_html = f'<span style="color:#64748b;font-size:.8rem">{data["note"]}</span>' if data["note"] else "—"
            sign = "+" if data["profit"] >= 0 else ""
            roi_color = "#34d399" if data["roi_pct"] >= 0 else "#f87171"
            rows.append(f"""
        <tr{highlight}>
          <td><strong>${cap:>10,}</strong>{'&nbsp;★' if cap == 10_000 else ''}</td>
          <td style="color:#34d399"><strong>{sign}${data['profit']:,.2f}</strong></td>
          <td style="color:{roi_color}">{sign}{data['roi_pct']:.1f}%</td>
          <td style="color:#f87171">-${data['drawdown']:,.2f}</td>
          <td>{note_html}</td>
        </tr>""")
        return "".join(rows)

    # ──────────────────────────────────────────────────────────────────────────
    # PNG via matplotlib
    # ──────────────────────────────────────────────────────────────────────────

    def save_images(self, prefix: str = "backtest") -> List[Path]:
        """
        Génère et sauvegarde des images PNG pour chaque graphique.
        Retourne la liste des fichiers créés.
        """
        try:
            import matplotlib
            matplotlib.use("Agg")  # Pas d'affichage GUI
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
        except ImportError:
            logger.warning("matplotlib non installé. Installez : pip install matplotlib")
            print("  ⚠️  pip install matplotlib  pour générer les images PNG")
            return []

        paths = []
        style = self._mpl_style()
        plt.rcParams.update(style)

        paths.append(self._img_equity_curve(plt, prefix))
        paths.append(self._img_monthly_pnl(plt, prefix))
        paths.append(self._img_capital_scaling(plt, prefix))
        paths.append(self._img_spread_distribution(plt, prefix))
        paths.append(self._img_profit_distribution(plt, prefix))
        paths.append(self._img_drawdown(plt, prefix))

        plt.close("all")

        print(f"\n  ✅ {len(paths)} images PNG dans : {self.output_dir.resolve()}/")
        for p in paths:
            print(f"     · {p.name}")
        return paths

    @staticmethod
    def _mpl_style() -> Dict[str, Any]:
        return {
            "figure.facecolor": "#0f1117",
            "axes.facecolor": "#1a1d27",
            "axes.edgecolor": "#2d3748",
            "axes.labelcolor": "#94a3b8",
            "axes.titlecolor": "#e2e8f0",
            "xtick.color": "#64748b",
            "ytick.color": "#64748b",
            "grid.color": "#1e2130",
            "grid.linestyle": "--",
            "grid.alpha": 0.6,
            "text.color": "#e2e8f0",
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.titlesize": 13,
            "axes.titlepad": 14,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "figure.dpi": 150,
        }

    def _save_fig(self, fig, name: str) -> Path:
        path = self.output_dir / name
        fig.savefig(path, bbox_inches="tight", facecolor=fig.get_facecolor())
        return path

    def _img_equity_curve(self, plt, prefix: str) -> Path:
        import matplotlib.pyplot as plt
        from datetime import datetime

        if not self._equity_values:
            return self.output_dir / f"{prefix}_equity_curve.png"

        fig, ax = plt.subplots(figsize=(12, 5))
        dates = [datetime.fromtimestamp(ts) for ts, _ in self.result.equity_curve]
        values = self._equity_values

        ax.plot(dates, values, color="#34d399", linewidth=1.8, zorder=3)
        ax.fill_between(dates, values, alpha=0.15, color="#34d399")
        ax.axhline(0, color="#2d3748", linewidth=0.8)

        ax.set_title(f"Courbe des Capitaux — {self.result.strategy} ({self.result.start_date} → {self.result.end_date})")
        ax.set_ylabel("P&L cumulatif ($)")
        ax.yaxis.set_major_formatter(lambda x, _: f"${x:,.0f}")
        ax.grid(True)

        import matplotlib.dates as mdates
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
        fig.autofmt_xdate()

        # Annoter le max
        max_val = max(values)
        max_idx = values.index(max_val)
        ax.annotate(
            f"  Pic: ${max_val:,.2f}",
            xy=(dates[max_idx], max_val),
            color="#34d399", fontsize=9,
            arrowprops=dict(arrowstyle="->", color="#34d399", lw=1),
            xytext=(dates[max_idx], max_val * 0.9),
        )

        fig.tight_layout()
        path = self._save_fig(fig, f"{prefix}_equity_curve.png")
        plt.close(fig)
        return path

    def _img_monthly_pnl(self, plt, prefix: str) -> Path:
        import matplotlib.pyplot as plt

        months = list(self._monthly.keys())
        pnls = [v["pnl"] for v in self._monthly.values()]
        colors = ["#34d399" if p >= 0 else "#f87171" for p in pnls]

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
        fig.subplots_adjust(hspace=0.08)

        # Bar chart P&L
        bars = ax1.bar(months, pnls, color=colors, width=0.7, zorder=3)
        ax1.axhline(0, color="#2d3748", linewidth=0.8)
        ax1.set_title("Performance Mensuelle")
        ax1.set_ylabel("P&L mensuel ($)")
        ax1.yaxis.set_major_formatter(lambda x, _: f"${x:,.0f}")
        ax1.grid(True, axis="y")
        for bar, val in zip(bars, pnls):
            ax1.text(
                bar.get_x() + bar.get_width() / 2,
                val + (max(pnls) * 0.02 if val >= 0 else min(pnls) * 0.02),
                f"${val:,.0f}", ha="center", va="bottom" if val >= 0 else "top",
                fontsize=7.5, color="#e2e8f0"
            )

        # Trades par mois
        trades_per_month = [v["trades"] for v in self._monthly.values()]
        ax2.bar(months, trades_per_month, color="#3b82f6", alpha=0.75, width=0.7, zorder=3)
        ax2.set_ylabel("Nb trades")
        ax2.grid(True, axis="y")
        plt.xticks(rotation=45, ha="right", fontsize=8)

        path = self._save_fig(fig, f"{prefix}_monthly_pnl.png")
        plt.close(fig)
        return path

    def _img_capital_scaling(self, plt, prefix: str) -> Path:
        import matplotlib.pyplot as plt
        import numpy as np

        capitals = list(self._scaling.keys())
        profits = [v["profit"] for v in self._scaling.values()]
        rois = [v["roi_pct"] for v in self._scaling.values()]
        labels = [f"${c:,}" for c in capitals]

        fig, ax1 = plt.subplots(figsize=(11, 5))
        ax2 = ax1.twinx()

        x = np.arange(len(labels))
        bars = ax1.bar(x, profits, color="#34d399", alpha=0.8, width=0.55, label="Profit projeté ($)", zorder=3)
        line, = ax2.plot(x, rois, "o-", color="#fbbf24", linewidth=2, markersize=6, label="ROI (%)", zorder=4)

        ax1.set_xticks(x)
        ax1.set_xticklabels(labels, rotation=30, ha="right")
        ax1.set_ylabel("Profit projeté ($)", color="#34d399")
        ax1.yaxis.set_major_formatter(lambda v, _: f"${v:,.0f}")
        ax2.set_ylabel("ROI (%)", color="#fbbf24")
        ax2.yaxis.set_major_formatter(lambda v, _: f"{v:.1f}%")
        ax1.set_title("Scalabilité — Capital Investi vs Profit Projeté")

        # Légende
        lines = [bars, line]
        labels_legend = [b.get_label() for b in [bars]] + [line.get_label()]
        ax1.legend([bars, line], ["Profit projeté ($)", "ROI (%)"],
                   loc="upper left", facecolor="#1a1d27", edgecolor="#2d3748")

        # Zone de saturation liquidité
        sat_idx = next((i for i, c in enumerate(capitals) if c > 30_000), None)
        if sat_idx:
            ax1.axvspan(sat_idx - 0.5, len(capitals) - 0.5, alpha=0.07, color="#f87171",
                        label="Liquidité saturée")
            ax1.text(sat_idx, max(profits) * 0.95, "  ⚠️ Liquidité saturée",
                     color="#f87171", fontsize=8.5, va="top")

        ax1.grid(True, axis="y", zorder=0)
        fig.tight_layout()
        path = self._save_fig(fig, f"{prefix}_capital_scaling.png")
        plt.close(fig)
        return path

    def _img_spread_distribution(self, plt, prefix: str) -> Path:
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(10, 5))
        ax.bar(self._spread_labels, self._spread_counts, color="#a78bfa", alpha=0.8, zorder=3)
        ax.set_title("Distribution des Spreads YES+NO au moment du trade")
        ax.set_xlabel("Spread (YES + NO)")
        ax.set_ylabel("Nombre de trades")
        ax.tick_params(axis="x", rotation=60, labelsize=8)
        ax.grid(True, axis="y")

        # Ligne seuil rentabilité
        if self._spread_labels:
            ax.axvline(x=len(self._spread_labels) - 1, color="#f87171",
                       linestyle="--", linewidth=1, label="= $1.00 (seuil)")
            ax.legend(facecolor="#1a1d27", edgecolor="#2d3748")

        fig.tight_layout()
        path = self._save_fig(fig, f"{prefix}_spread_distribution.png")
        plt.close(fig)
        return path

    def _img_profit_distribution(self, plt, prefix: str) -> Path:
        import matplotlib.pyplot as plt

        colors = ["#34d399" if float(l) >= 0 else "#f87171"
                  for l in (self._profit_labels or ["0"])]

        fig, ax = plt.subplots(figsize=(10, 5))
        ax.bar(self._profit_labels, self._profit_counts, color=colors, alpha=0.85, zorder=3)
        ax.set_title("Distribution des Profits par Trade")
        ax.set_xlabel("Profit net par trade ($)")
        ax.set_ylabel("Fréquence")
        ax.tick_params(axis="x", rotation=60, labelsize=8)
        ax.grid(True, axis="y")
        ax.axvline(x=0, color="#94a3b8", linewidth=0.8, linestyle="--")

        fig.tight_layout()
        path = self._save_fig(fig, f"{prefix}_profit_distribution.png")
        plt.close(fig)
        return path

    def _img_drawdown(self, plt, prefix: str) -> Path:
        import matplotlib.pyplot as plt
        from datetime import datetime

        if not self.result.equity_curve:
            return self.output_dir / f"{prefix}_drawdown.png"

        dates = [datetime.fromtimestamp(ts) for ts, _ in self.result.equity_curve]
        dd = [-v for v in self._drawdown_series]

        fig, ax = plt.subplots(figsize=(12, 4))
        ax.fill_between(dates, dd, alpha=0.4, color="#f87171")
        ax.plot(dates, dd, color="#f87171", linewidth=1.2)
        ax.set_title("Drawdown (depuis le pic)")
        ax.set_ylabel("Drawdown ($)")
        ax.yaxis.set_major_formatter(lambda x, _: f"-${abs(x):,.0f}")

        import matplotlib.dates as mdates
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
        fig.autofmt_xdate()
        ax.grid(True)

        fig.tight_layout()
        path = self._save_fig(fig, f"{prefix}_drawdown.png")
        plt.close(fig)
        return path
