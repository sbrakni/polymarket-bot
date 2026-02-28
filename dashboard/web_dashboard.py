"""
Dashboard web en temps réel via FastAPI + Server-Sent Events.
Accessible à http://localhost:8080 après démarrage du bot.
"""

import asyncio
import json
import time
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)


class Dashboard:
    """Dashboard web pour monitorer le bot en temps réel."""

    def __init__(self):
        self.app = FastAPI(title="Polymarket Arbitrage Bot", version="1.0.0")
        self._bot_ref = None  # Référence au bot principal
        self._setup_routes()

    def set_bot(self, bot) -> None:
        """Injecte la référence au bot principal pour accéder aux métriques."""
        self._bot_ref = bot

    def _setup_routes(self) -> None:
        app = self.app

        @app.get("/", response_class=HTMLResponse)
        async def root():
            return self._render_dashboard()

        @app.get("/api/status")
        async def api_status():
            return self._get_status()

        @app.get("/api/metrics")
        async def api_metrics():
            if not self._bot_ref:
                return {"error": "Bot not connected"}
            return self._get_all_metrics()

        @app.get("/api/positions")
        async def api_positions():
            if not self._bot_ref:
                return []
            return self._get_positions()

        @app.get("/api/trades")
        async def api_trades():
            if not self._bot_ref:
                return []
            return self._get_recent_trades()

        @app.get("/api/equity-curve")
        async def api_equity_curve():
            if not self._bot_ref:
                return []
            return self._get_equity_curve()

        @app.get("/api/stream")
        async def stream_updates(request: Request):
            """Server-Sent Events pour les mises à jour temps réel."""
            return StreamingResponse(
                self._event_generator(request),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

        @app.post("/api/control/start")
        async def control_start():
            if self._bot_ref:
                # Réactiver si en pause
                if hasattr(self._bot_ref, "risk_limits"):
                    self._bot_ref.risk_limits.resume()
            return {"status": "started"}

        @app.post("/api/control/stop")
        async def control_stop():
            if self._bot_ref:
                await self._bot_ref.stop()
            return {"status": "stopped"}

        @app.post("/api/control/risk")
        async def update_risk(config: Dict[str, Any]):
            """Met à jour la configuration de risque à chaud."""
            if self._bot_ref and hasattr(self._bot_ref, "risk_limits"):
                from config.settings import RiskConfig
                new_config = RiskConfig(**config)
                self._bot_ref.risk_limits.update_config(new_config)
            return {"status": "updated"}

    async def _event_generator(self, request: Request):
        """Génère les événements SSE pour le dashboard temps réel."""
        while True:
            if await request.is_disconnected():
                break
            try:
                data = json.dumps(self._get_all_metrics())
                yield f"data: {data}\n\n"
            except Exception as e:
                logger.debug(f"SSE error: {e}")
            await asyncio.sleep(2.0)  # Update toutes les 2 secondes

    def _get_status(self) -> Dict[str, Any]:
        if not self._bot_ref:
            return {"status": "disconnected"}
        return {
            "status": "running" if self._bot_ref._running else "stopped",
            "dry_run": settings.DRY_RUN,
            "uptime_seconds": time.time() - getattr(self._bot_ref, "_started_at", time.time()),
        }

    def _get_all_metrics(self) -> Dict[str, Any]:
        if not self._bot_ref:
            return {}
        bot = self._bot_ref
        result = {
            "timestamp": time.time(),
            "status": "running" if bot._running else "stopped",
            "dry_run": settings.DRY_RUN,
        }
        if hasattr(bot, "pnl_tracker"):
            result["pnl"] = bot.pnl_tracker.get_full_report()
        if hasattr(bot, "risk_limits"):
            result["risk"] = bot.risk_limits.get_summary()
        if hasattr(bot, "position_manager"):
            result["positions"] = bot.position_manager.get_summary()
        if hasattr(bot, "ws_manager"):
            result["websocket"] = bot.ws_manager.get_stats()
        if hasattr(bot, "market_scanner"):
            result["scanner"] = bot.market_scanner.get_stats()
        return result

    def _get_positions(self) -> list:
        if not self._bot_ref or not hasattr(self._bot_ref, "position_manager"):
            return []
        positions = self._bot_ref.position_manager.get_open_positions()
        return [
            {
                "id": p.position_id,
                "market_id": p.market_id[:16],
                "question": p.question[:80],
                "strategy": p.strategy,
                "total_cost": round(p.total_cost, 2),
                "unrealized_pnl": round(p.unrealized_pnl, 4),
                "guaranteed_profit": round(p.guaranteed_profit, 4),
                "age_hours": round(p.age_hours, 2),
            }
            for p in positions
        ]

    def _get_recent_trades(self) -> list:
        if not self._bot_ref or not hasattr(self._bot_ref, "pnl_tracker"):
            return []
        trades = self._bot_ref.pnl_tracker._trades[-50:]
        return [
            {
                "id": t.trade_id,
                "strategy": t.strategy,
                "market_id": t.market_id[:16],
                "profit": round(t.profit, 4),
                "latency_ms": round(t.latency_ms, 1),
                "timestamp": t.timestamp,
                "dry_run": t.was_dry_run,
            }
            for t in reversed(trades)
        ]

    def _get_equity_curve(self) -> list:
        if not self._bot_ref or not hasattr(self._bot_ref, "pnl_tracker"):
            return []
        return self._bot_ref.pnl_tracker.get_equity_curve()[-500:]

    def _render_dashboard(self) -> str:
        """Retourne le HTML du dashboard."""
        return DASHBOARD_HTML

    async def start(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        """Démarre le serveur web en arrière-plan."""
        config = uvicorn.Config(
            self.app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        asyncio.create_task(server.serve())
        logger.info(f"Dashboard démarré sur http://{host}:{port}")


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Arbitrage Bot</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0f1117; color: #e2e8f0; }
  .header { background: #1a1d27; padding: 16px 24px; border-bottom: 1px solid #2d3748;
            display: flex; align-items: center; gap: 12px; }
  .header h1 { font-size: 1.25rem; font-weight: 600; }
  .status-badge { padding: 4px 10px; border-radius: 20px; font-size: 0.75rem; font-weight: 600; }
  .status-running { background: #065f46; color: #6ee7b7; }
  .status-stopped { background: #7f1d1d; color: #fca5a5; }
  .dry-run { background: #1e3a5f; color: #93c5fd; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
          gap: 16px; padding: 24px; }
  .card { background: #1a1d27; border: 1px solid #2d3748; border-radius: 12px; padding: 20px; }
  .card-title { font-size: 0.75rem; color: #94a3b8; text-transform: uppercase;
                letter-spacing: 0.05em; margin-bottom: 8px; }
  .card-value { font-size: 2rem; font-weight: 700; }
  .positive { color: #34d399; }
  .negative { color: #f87171; }
  .neutral { color: #93c5fd; }
  .chart-container { background: #1a1d27; border: 1px solid #2d3748; border-radius: 12px;
                     padding: 20px; margin: 0 24px 24px; }
  .chart-title { font-size: 1rem; font-weight: 600; margin-bottom: 16px; }
  .table-container { background: #1a1d27; border: 1px solid #2d3748; border-radius: 12px;
                     margin: 0 24px 24px; overflow: hidden; }
  table { width: 100%; border-collapse: collapse; font-size: 0.875rem; }
  th { background: #2d3748; padding: 12px 16px; text-align: left; font-size: 0.75rem;
       text-transform: uppercase; color: #94a3b8; }
  td { padding: 10px 16px; border-bottom: 1px solid #2d3748; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: #1e2132; }
  .controls { display: flex; gap: 12px; margin: 0 24px 24px; }
  .btn { padding: 10px 20px; border-radius: 8px; border: none; cursor: pointer;
         font-weight: 600; font-size: 0.875rem; transition: opacity 0.2s; }
  .btn:hover { opacity: 0.8; }
  .btn-green { background: #065f46; color: #6ee7b7; }
  .btn-red { background: #7f1d1d; color: #fca5a5; }
  .section-title { padding: 0 24px; margin-bottom: 12px; font-size: 1rem; font-weight: 600;
                   color: #94a3b8; }
</style>
</head>
<body>
<div class="header">
  <h1>🤖 Polymarket Arbitrage Bot</h1>
  <span id="status-badge" class="status-badge">Connexion...</span>
  <span id="dry-run-badge" style="display:none" class="status-badge dry-run">DRY-RUN</span>
  <span id="uptime" style="margin-left:auto; font-size:0.875rem; color:#94a3b8"></span>
</div>

<div class="grid">
  <div class="card">
    <div class="card-title">P&L Cumulatif</div>
    <div class="card-value" id="pnl">$0.00</div>
  </div>
  <div class="card">
    <div class="card-title">Trades Totaux</div>
    <div class="card-value neutral" id="total-trades">0</div>
  </div>
  <div class="card">
    <div class="card-title">Win Rate</div>
    <div class="card-value" id="win-rate">0%</div>
  </div>
  <div class="card">
    <div class="card-title">Sharpe Ratio</div>
    <div class="card-value neutral" id="sharpe">0.00</div>
  </div>
  <div class="card">
    <div class="card-title">Positions Ouvertes</div>
    <div class="card-value neutral" id="open-positions">0</div>
  </div>
  <div class="card">
    <div class="card-title">Exposition Totale</div>
    <div class="card-value neutral" id="exposure">$0</div>
  </div>
  <div class="card">
    <div class="card-title">Max Drawdown</div>
    <div class="card-value negative" id="drawdown">$0.00</div>
  </div>
  <div class="card">
    <div class="card-title">Trades/Jour</div>
    <div class="card-value neutral" id="trades-per-day">0</div>
  </div>
</div>

<div class="chart-container">
  <div class="chart-title">📈 Courbe des Capitaux (P&L Cumulatif)</div>
  <canvas id="equity-chart" height="300"></canvas>
</div>

<div class="controls">
  <button class="btn btn-green" onclick="controlBot('start')">▶ Reprendre</button>
  <button class="btn btn-red" onclick="controlBot('stop')">⏹ Arrêter</button>
</div>

<div class="section-title">📋 Trades Récents</div>
<div class="table-container">
  <table>
    <thead>
      <tr>
        <th>ID</th><th>Stratégie</th><th>Marché</th>
        <th>Profit</th><th>Latence</th><th>Heure</th>
      </tr>
    </thead>
    <tbody id="trades-tbody">
      <tr><td colspan="6" style="text-align:center;color:#94a3b8">Aucun trade...</td></tr>
    </tbody>
  </table>
</div>

<script>
let equityChart = null;
const equityData = { labels: [], datasets: [{ label: 'P&L ($)', data: [],
  borderColor: '#34d399', backgroundColor: 'rgba(52,211,153,0.1)',
  fill: true, tension: 0.3, pointRadius: 0 }] };

function initChart() {
  const ctx = document.getElementById('equity-chart').getContext('2d');
  equityChart = new Chart(ctx, {
    type: 'line', data: equityData,
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { display: false },
        y: { grid: { color: '#2d3748' }, ticks: { color: '#94a3b8',
             callback: v => '$' + v.toFixed(2) } }
      },
      animation: { duration: 200 }
    }
  });
}

function fmt(v, prefix='$') {
  const n = parseFloat(v) || 0;
  return prefix + (n >= 0 ? '' : '') + n.toFixed(2);
}

function setColored(id, value) {
  const el = document.getElementById(id);
  el.textContent = fmt(value);
  el.className = 'card-value ' + (value >= 0 ? 'positive' : 'negative');
}

function updateDashboard(data) {
  const pnl = data.pnl || {};
  const summary = pnl.summary || {};
  const risk_metrics = pnl.risk_metrics || {};
  const risk = data.risk || {};
  const positions = data.positions || {};

  // Status badge
  const statusEl = document.getElementById('status-badge');
  statusEl.textContent = data.status === 'running' ? '● ACTIF' : '○ ARRÊTÉ';
  statusEl.className = 'status-badge ' + (data.status === 'running' ? 'status-running' : 'status-stopped');

  if (data.dry_run) {
    document.getElementById('dry-run-badge').style.display = 'inline';
  }

  const uptime = data.uptime_seconds || 0;
  const h = Math.floor(uptime/3600), m = Math.floor((uptime%3600)/60), s = Math.floor(uptime%60);
  document.getElementById('uptime').textContent = `Uptime: ${h}h ${m}m ${s}s`;

  setColored('pnl', summary.cumulative_pnl || 0);
  document.getElementById('total-trades').textContent = summary.total_trades || 0;

  const wr = summary.win_rate || 0;
  const wrEl = document.getElementById('win-rate');
  wrEl.textContent = wr.toFixed(1) + '%';
  wrEl.className = 'card-value ' + (wr >= 50 ? 'positive' : 'negative');

  document.getElementById('sharpe').textContent = (risk_metrics.sharpe_ratio || 0).toFixed(2);
  document.getElementById('open-positions').textContent = positions.open_positions || 0;
  document.getElementById('exposure').textContent = '$' + (risk.total_exposure || 0).toFixed(0);
  document.getElementById('drawdown').textContent = '-$' + (risk_metrics.max_drawdown || 0).toFixed(2);
  document.getElementById('trades-per-day').textContent = (summary.trades_per_day || 0).toFixed(1);
}

function updateTrades(trades) {
  if (!trades || !trades.length) return;
  const tbody = document.getElementById('trades-tbody');
  tbody.innerHTML = trades.slice(0, 20).map(t => {
    const profit = parseFloat(t.profit) || 0;
    const color = profit >= 0 ? '#34d399' : '#f87171';
    const sign = profit >= 0 ? '+' : '';
    const dt = new Date(t.timestamp * 1000).toLocaleTimeString();
    const dr = t.dry_run ? ' <small style="color:#93c5fd">[DR]</small>' : '';
    return `<tr>
      <td><code>${t.id}</code></td>
      <td>${t.strategy}${dr}</td>
      <td><code>${t.market_id}...</code></td>
      <td style="color:${color}">${sign}$${profit.toFixed(4)}</td>
      <td>${t.latency_ms.toFixed(0)}ms</td>
      <td>${dt}</td>
    </tr>`;
  }).join('');
}

function updateEquityCurve(curve) {
  if (!curve || !curve.length) return;
  equityData.labels = curve.map((_, i) => i);
  equityData.datasets[0].data = curve.map(p => p[1]);
  equityChart && equityChart.update('none');
}

async function controlBot(action) {
  await fetch('/api/control/' + action, { method: 'POST' });
}

// Server-Sent Events pour les mises à jour temps réel
function connectSSE() {
  const es = new EventSource('/api/stream');
  es.onmessage = e => {
    try {
      const data = JSON.parse(e.data);
      updateDashboard(data);
    } catch(err) {}
  };
  es.onerror = () => { setTimeout(connectSSE, 3000); es.close(); };
}

// Rafraîchissement des trades et de la courbe toutes les 5s
async function refreshExtras() {
  try {
    const [trades, curve] = await Promise.all([
      fetch('/api/trades').then(r => r.json()),
      fetch('/api/equity-curve').then(r => r.json()),
    ]);
    updateTrades(trades);
    updateEquityCurve(curve);
  } catch(e) {}
}

initChart();
connectSSE();
refreshExtras();
setInterval(refreshExtras, 5000);
</script>
</body>
</html>
"""
