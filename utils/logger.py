"""
Logging structuré en JSON pour toutes les composantes du bot.
Chaque log inclut le contexte trade_id, strategy, market_id.
"""

import logging
import sys
from typing import Optional

from config import settings


class JsonFormatter(logging.Formatter):
    """Formateur JSON pour les logs structurés."""

    def format(self, record: logging.LogRecord) -> str:
        import json
        import datetime

        log_obj = {
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Ajouter les champs d'extra context si présents
        extra_fields = [
            "trade_id", "market_id", "token_id", "strategy",
            "profit", "latency_ms", "price", "size", "side",
            "exchange_id",
        ]
        for field in extra_fields:
            if hasattr(record, field):
                log_obj[field] = getattr(record, field)

        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_obj, ensure_ascii=False)


class HumanFormatter(logging.Formatter):
    """Formateur lisible pour la console."""

    LEVEL_COLORS = {
        "DEBUG": "\033[36m",    # Cyan
        "INFO": "\033[32m",     # Vert
        "WARNING": "\033[33m",  # Jaune
        "ERROR": "\033[31m",    # Rouge
        "CRITICAL": "\033[35m", # Magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.LEVEL_COLORS.get(record.levelname, "")
        level = f"{color}{record.levelname:8}{self.RESET}"
        name = f"\033[90m{record.name:30}\033[0m"
        msg = record.getMessage()

        # Ajouter les champs extra importants inline
        extras = []
        for field in ["trade_id", "strategy", "profit", "latency_ms"]:
            if hasattr(record, field):
                extras.append(f"{field}={getattr(record, field)}")

        extra_str = f" [{', '.join(extras)}]" if extras else ""

        import datetime
        ts = datetime.datetime.utcnow().strftime("%H:%M:%S.%f")[:-3]

        return f"\033[90m{ts}\033[0m {level} {name} {msg}{extra_str}"


def setup_logging(log_level: str = "INFO", json_output: bool = False) -> None:
    """Configure le logging global du bot."""
    level = getattr(logging, log_level.upper(), logging.INFO)

    # Supprimer les handlers existants
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    # Handler console
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)

    if json_output:
        console_handler.setFormatter(JsonFormatter())
    else:
        console_handler.setFormatter(HumanFormatter())

    root.addHandler(console_handler)

    # Handler fichier JSON (toujours)
    import os
    os.makedirs("logs", exist_ok=True)
    from logging.handlers import RotatingFileHandler
    file_handler = RotatingFileHandler(
        "logs/bot.json",
        maxBytes=50 * 1024 * 1024,  # 50MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)

    # Réduire le bruit des librairies tierces
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Retourne un logger configuré pour un module."""
    return logging.getLogger(name)
