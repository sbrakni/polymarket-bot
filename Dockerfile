FROM python:3.11-slim

WORKDIR /app

# Dépendances système
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Dépendances Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code source
COPY . .

# Créer les répertoires de données
RUN mkdir -p logs data/historical data/backtest_results

# Variables d'environnement par défaut
ENV DRY_RUN=true
ENV LOG_LEVEL=INFO
ENV DASHBOARD_PORT=8080
ENV DASHBOARD_HOST=0.0.0.0

EXPOSE 8080

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8080/api/status || exit 1

CMD ["python", "main.py"]
