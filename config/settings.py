"""
Configuration centrale du bot d'arbitrage Polymarket.
Toutes les valeurs sont chargées depuis les variables d'environnement (.env).
"""

import os
from dataclasses import dataclass, field
from decimal import Decimal
from dotenv import load_dotenv

load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    val = os.environ.get(key, str(default)).lower()
    return val in ("true", "1", "yes", "on")


# ===========================================================================
# Polymarket / Polygon
# ===========================================================================
POLYMARKET_PRIVATE_KEY: str = _env("POLYMARKET_PRIVATE_KEY")
POLYMARKET_FUNDER_ADDRESS: str = _env("POLYMARKET_FUNDER_ADDRESS")
POLYGON_RPC_URL: str = _env("POLYGON_RPC_URL", "https://polygon-rpc.com")

# CLOB API endpoints
CLOB_API_URL: str = "https://clob.polymarket.com"
GAMMA_API_URL: str = "https://gamma-api.polymarket.com"
WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Polymarket fee estimate (per leg): ~0.01% maker, ~0.02% taker
POLYMARKET_TAKER_FEE: float = 0.0002  # 0.02%
POLYMARKET_MAKER_FEE: float = 0.0001  # 0.01%

# ===========================================================================
# Sports Data APIs
# ===========================================================================
THEODDSAPI_KEY: str = _env("THEODDSAPI_KEY")
SPORTRADAR_API_KEY: str = _env("SPORTRADAR_API_KEY")
API_FOOTBALL_KEY: str = _env("API_FOOTBALL_KEY")

# Sports supported for reality arbitrage
SUPPORTED_SPORTS = ["basketball_nba", "americanfootball_nfl", "icehockey_nhl",
                    "baseball_mlb", "soccer_epl", "tennis_atp"]

# ===========================================================================
# Notifications
# ===========================================================================
TELEGRAM_BOT_TOKEN: str = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID: str = _env("TELEGRAM_CHAT_ID")

# ===========================================================================
# Risk Parameters
# ===========================================================================
MAX_TOTAL_EXPOSURE: float = _env_float("MAX_TOTAL_EXPOSURE", 10000.0)
MAX_PER_MARKET: float = _env_float("MAX_PER_MARKET", 1000.0)
MAX_PER_TRADE: float = _env_float("MAX_PER_TRADE", 500.0)
DAILY_LOSS_LIMIT: float = _env_float("DAILY_LOSS_LIMIT", 500.0)
MIN_PROFIT_THRESHOLD: float = _env_float("MIN_PROFIT_THRESHOLD", 0.50)
KELLY_FRACTION: float = _env_float("KELLY_FRACTION", 0.25)
MAX_OPEN_POSITIONS: int = _env_int("MAX_OPEN_POSITIONS", 50)

# ===========================================================================
# Bot Settings
# ===========================================================================
DRY_RUN: bool = _env_bool("DRY_RUN", True)
LOG_LEVEL: str = _env("LOG_LEVEL", "INFO")
SCAN_INTERVAL_MS: int = _env_int("SCAN_INTERVAL_MS", 100)
WS_RECONNECT_DELAY_S: int = _env_int("WS_RECONNECT_DELAY_S", 5)
DASHBOARD_PORT: int = _env_int("DASHBOARD_PORT", 8080)
DASHBOARD_HOST: str = _env("DASHBOARD_HOST", "0.0.0.0")

# ===========================================================================
# Strategy Toggles
# ===========================================================================
ENABLE_BINARY_ARB: bool = _env_bool("ENABLE_BINARY_ARB", True)
ENABLE_REALITY_ARB: bool = _env_bool("ENABLE_REALITY_ARB", True)
ENABLE_MULTI_HEDGE: bool = _env_bool("ENABLE_MULTI_HEDGE", True)

# ===========================================================================
# Binary Arbitrage Tuning
# ===========================================================================
MIN_LIQUIDITY: float = _env_float("MIN_LIQUIDITY", 1000.0)
MIN_BINARY_EDGE: float = _env_float("MIN_BINARY_EDGE", 0.02)  # 2 cents per $1

# ===========================================================================
# Reality Arbitrage Tuning
# ===========================================================================
MIN_PROB_SHIFT: float = _env_float("MIN_PROB_SHIFT", 0.05)  # 5%
MAX_LATENCY_MS: int = _env_int("MAX_LATENCY_MS", 500)

# ===========================================================================
# Database
# ===========================================================================
DATABASE_URL: str = _env("DATABASE_URL", "sqlite:///data/polymarket_bot.db")

# ===========================================================================
# Rate Limits (Polymarket)
# ===========================================================================
MAX_ORDERS_PER_MINUTE: int = 60
MAX_REQUESTS_PER_MINUTE: int = 100


@dataclass
class RiskConfig:
    """Configuration de gestion des risques — peut être modifiée à l'exécution."""
    max_total_exposure: float = MAX_TOTAL_EXPOSURE
    max_per_market: float = MAX_PER_MARKET
    max_per_trade: float = MAX_PER_TRADE
    daily_loss_limit: float = DAILY_LOSS_LIMIT
    min_profit_threshold: float = MIN_PROFIT_THRESHOLD
    kelly_fraction: float = KELLY_FRACTION
    max_open_positions: int = MAX_OPEN_POSITIONS

    def scale_for_capital(self, capital: float) -> "RiskConfig":
        """Ajuste les paramètres proportionnellement au capital disponible."""
        factor = capital / 10_000  # basé sur configuration par défaut de $10k
        return RiskConfig(
            max_total_exposure=capital * 0.8,
            max_per_market=capital * 0.1,
            max_per_trade=capital * 0.05,
            daily_loss_limit=capital * 0.05,
            min_profit_threshold=max(0.10, capital * 0.00005),
            kelly_fraction=self.kelly_fraction,
            max_open_positions=int(50 * factor),
        )


# Preset de configuration pour différents niveaux de capital
RISK_PRESETS = {
    "conservative_100": RiskConfig(
        max_total_exposure=80, max_per_market=10, max_per_trade=5,
        daily_loss_limit=5, min_profit_threshold=0.10, kelly_fraction=0.1,
        max_open_positions=10
    ),
    "moderate_1000": RiskConfig(
        max_total_exposure=800, max_per_market=100, max_per_trade=50,
        daily_loss_limit=50, min_profit_threshold=0.25, kelly_fraction=0.25,
        max_open_positions=25
    ),
    "standard_10000": RiskConfig(
        max_total_exposure=8000, max_per_market=1000, max_per_trade=500,
        daily_loss_limit=500, min_profit_threshold=0.50, kelly_fraction=0.25,
        max_open_positions=50
    ),
    "aggressive_50000": RiskConfig(
        max_total_exposure=40000, max_per_market=5000, max_per_trade=2500,
        daily_loss_limit=2500, min_profit_threshold=1.00, kelly_fraction=0.33,
        max_open_positions=100
    ),
}
