"""
Métriques de performance compatibles Prometheus.
Expose un endpoint /metrics pour le monitoring externe.
"""

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from utils.logger import get_logger

logger = get_logger(__name__)

# Tentative d'import Prometheus (optionnel)
try:
    from prometheus_client import Counter, Gauge, Histogram, Summary, start_http_server
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    logger.info("prometheus_client non installé — métriques internes uniquement")


class Metrics:
    """
    Collecteur de métriques compatible Prometheus.
    Fonctionne en mode standalone sans Prometheus si la librairie n'est pas disponible.
    """

    def __init__(self, prometheus_port: Optional[int] = None):
        self._prometheus_port = prometheus_port
        self._internal: Dict[str, Any] = defaultdict(float)
        self._counters: Dict[str, float] = defaultdict(float)
        self._gauges: Dict[str, float] = defaultdict(float)
        self._histograms: Dict[str, List[float]] = defaultdict(list)

        if PROMETHEUS_AVAILABLE and prometheus_port:
            self._setup_prometheus()
            try:
                start_http_server(prometheus_port)
                logger.info(f"Prometheus metrics sur port {prometheus_port}")
            except Exception as e:
                logger.warning(f"Impossible de démarrer Prometheus HTTP: {e}")

    def _setup_prometheus(self):
        """Initialise les métriques Prometheus."""
        self._prom_trades_total = Counter(
            "polymarket_trades_total",
            "Nombre total de trades",
            ["strategy"]
        )
        self._prom_profit_total = Counter(
            "polymarket_profit_usd_total",
            "Profit cumulatif en USD",
            ["strategy"]
        )
        self._prom_opportunities = Counter(
            "polymarket_opportunities_total",
            "Opportunités détectées",
            ["strategy"]
        )
        self._prom_latency = Histogram(
            "polymarket_trade_latency_ms",
            "Latence d'exécution des trades (ms)",
            ["strategy"],
            buckets=[50, 100, 200, 500, 1000, 2000, 5000]
        )
        self._prom_pnl = Gauge(
            "polymarket_pnl_usd",
            "P&L actuel"
        )
        self._prom_positions = Gauge(
            "polymarket_open_positions",
            "Nombre de positions ouvertes"
        )
        self._prom_exposure = Gauge(
            "polymarket_exposure_usd",
            "Exposition totale en USD"
        )
        self._prom_ws_connections = Gauge(
            "polymarket_ws_connections",
            "Connexions WebSocket actives"
        )

    def record_trade(self, strategy: str, profit: float, latency_ms: float = 0) -> None:
        """Enregistre un trade exécuté."""
        self._counters[f"trades_{strategy}"] += 1
        self._counters[f"profit_{strategy}"] += profit
        self._counters["trades_total"] += 1
        self._counters["profit_total"] += profit

        if latency_ms > 0:
            self._histograms[f"latency_{strategy}"].append(latency_ms)

        if PROMETHEUS_AVAILABLE and hasattr(self, "_prom_trades_total"):
            try:
                self._prom_trades_total.labels(strategy=strategy).inc()
                if profit != 0:
                    self._prom_profit_total.labels(strategy=strategy).inc(profit)
                if latency_ms > 0:
                    self._prom_latency.labels(strategy=strategy).observe(latency_ms)
            except Exception:
                pass

    def record_opportunity(self, strategy: str) -> None:
        self._counters[f"opportunities_{strategy}"] += 1
        if PROMETHEUS_AVAILABLE and hasattr(self, "_prom_opportunities"):
            try:
                self._prom_opportunities.labels(strategy=strategy).inc()
            except Exception:
                pass

    def set_pnl(self, pnl: float) -> None:
        self._gauges["pnl"] = pnl
        if PROMETHEUS_AVAILABLE and hasattr(self, "_prom_pnl"):
            try:
                self._prom_pnl.set(pnl)
            except Exception:
                pass

    def set_open_positions(self, count: int) -> None:
        self._gauges["open_positions"] = count
        if PROMETHEUS_AVAILABLE and hasattr(self, "_prom_positions"):
            try:
                self._prom_positions.set(count)
            except Exception:
                pass

    def set_exposure(self, exposure: float) -> None:
        self._gauges["exposure"] = exposure
        if PROMETHEUS_AVAILABLE and hasattr(self, "_prom_exposure"):
            try:
                self._prom_exposure.set(exposure)
            except Exception:
                pass

    def set_ws_connections(self, count: int) -> None:
        self._gauges["ws_connections"] = count
        if PROMETHEUS_AVAILABLE and hasattr(self, "_prom_ws_connections"):
            try:
                self._prom_ws_connections.set(count)
            except Exception:
                pass

    def get_snapshot(self) -> Dict[str, Any]:
        """Retourne un snapshot des métriques actuelles."""
        return {
            "counters": dict(self._counters),
            "gauges": dict(self._gauges),
            "histograms": {
                k: {
                    "count": len(v),
                    "avg": sum(v) / len(v) if v else 0,
                    "p50": sorted(v)[len(v) // 2] if v else 0,
                    "p95": sorted(v)[int(len(v) * 0.95)] if v else 0,
                    "p99": sorted(v)[int(len(v) * 0.99)] if v else 0,
                }
                for k, v in self._histograms.items()
            },
        }
