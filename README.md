# Polymarket Arbitrage Bot

**Inspiré de la stratégie @swisstony — $5 → $4.3M sur Polymarket**

Bot d'arbitrage algorithmique complet pour [Polymarket](https://polymarket.com), implémentant les 3 stratégies identifiées chez le trader @swisstony. Conçu pour être déployé avec différents niveaux de capital, avec backtesting sur données réelles.

---

## Table des matières

1. [Stratégies implémentées](#stratégies)
2. [Architecture technique](#architecture)
3. [Démarrage rapide](#démarrage-rapide)
4. [Configuration](#configuration)
5. [Gestion des risques](#gestion-des-risques)
6. [Backtesting](#backtesting)
7. [Dashboard](#dashboard)
8. [Scalabilité par capital](#scalabilité)
9. [Structure du projet](#structure)
10. [Avertissements](#avertissements)

---

## Stratégies

### Module 1 — Reality Arbitrage (Sports)

**Principe :** Le bot reçoit les données sportives en temps réel via API (Sportradar/TheOddsAPI), **15 à 40 secondes avant** que les marchés Polymarket ne réagissent aux flux TV retardés.

**Mécanisme :**
1. Connexion aux APIs sportives temps réel (NBA, NFL, NHL, MLB, Soccer, Tennis)
2. Détection d'un événement significatif (but, touchdown, panier, etc.)
3. Calcul du shift de probabilité théorique (modèle logistique basé sur score + temps restant)
4. Comparaison avec le prix actuel du marché Polymarket
5. Si l'écart est ≥ 5% → achat immédiat via ordre FOK (Fill-or-Kill)
6. Le marché se corrige dans les 15-40 secondes → profit

**Latence cible :** < 500ms entre l'événement et l'exécution.

**Sports supportés :** NBA, NFL, NHL, MLB, Soccer (EPL), Tennis ATP

---

### Module 2 — Arbitrage Binaire Intra-marché (Stratégie principale)

**Principe :** Sur tout marché Polymarket à 2 outcomes (YES/NO), si `prix_YES + prix_NO < $1.00`, acheter les deux côtés garantit un profit à la résolution.

```
Exemple:
  YES @ $0.47 + NO @ $0.48 = $0.95 total
  Profit garanti = $1.00 - $0.95 - fees ≈ $0.04 par contrat
  Avec 500 contrats: profit ≈ $20 par trade
```

**Mécanisme :**
1. Scanner en continu tous les marchés actifs via WebSocket (6 connexions parallèles)
2. Pour chaque marché: `best_ask_YES + best_ask_NO`
3. Si total < 0.98 (après 2% de marge pour les frais): exécuter
4. Exécution simultanée des 2 ordres via `asyncio.gather`
5. Profit réalisé à la résolution du marché (peut prendre des heures ou des semaines)

**Paramètres clés :**
- `MIN_BINARY_EDGE = 0.02` (minimum 2 cents de profit par contrat)
- `MIN_LIQUIDITY = $1,000` (ignorer les marchés illiquides)
- Frais: ~0.04% total (2 jambes × 0.02% taker)

---

### Module 3 — Multi-Outcome Hedging (Sports avancé)

**Principe :** Sur les marchés sportifs avec plusieurs outcomes liés au même événement (moneyline + total + spread), construire des combinaisons de positions qui couvrent tous les scénarios avec un profit garanti.

**Mécanisme :**
1. Grouper les marchés par événement sportif
2. Construire la matrice payout × scénario
3. Utiliser la programmation linéaire (scipy) pour optimiser les tailles
4. Exécuter si le payout minimum dépasse le coût total + frais

---

## Architecture

```
polymarket-arb-bot/
├── config/
│   ├── settings.py          # Configuration centrale (chargée depuis .env)
│   └── .env.example         # Template des variables d'environnement
├── core/
│   ├── polymarket_client.py  # Wrapper API CLOB avec retry + rate limiting
│   ├── websocket_manager.py  # 6 connexions WS parallèles pour les prix temps réel
│   └── order_executor.py     # Exécution simultanée des ordres (asyncio.gather)
├── strategies/
│   ├── base_strategy.py      # Classe abstraite commune (Kelly, fees, interface)
│   ├── binary_arbitrage.py   # Module 2: YES+NO < $1
│   ├── reality_arbitrage.py  # Module 1: latence sportive
│   └── multi_outcome_hedge.py # Module 3: hedge multi-outcomes
├── data/
│   ├── sports_feed.py        # TheOddsAPI + Sportradar (polling + cache)
│   ├── market_scanner.py     # Scanner central de tous les marchés actifs
│   └── orderbook_cache.py    # Cache thread-safe des orderbooks (TTL: 5s)
├── risk/
│   ├── position_manager.py   # Registre des positions ouvertes
│   ├── risk_limits.py        # Vérificateur de limites (avant chaque trade)
│   └── pnl_tracker.py        # P&L, Sharpe, Sortino, drawdown, win rate
├── utils/
│   ├── logger.py             # JSON logging structuré + console colorée
│   ├── notifications.py      # Alertes Telegram (rate-limited)
│   └── metrics.py            # Métriques Prometheus (optionnel)
├── dashboard/
│   └── web_dashboard.py      # FastAPI + SSE + Chart.js (http://localhost:8080)
├── backtest/
│   ├── data_fetcher.py       # Téléchargement données historiques Polymarket
│   ├── backtester.py         # Moteur de backtesting + CLI
│   └── results_analyzer.py   # Analyse comparative + scalabilité par capital
├── tests/
│   ├── test_binary_arbitrage.py
│   ├── test_risk_limits.py
│   └── test_pnl_tracker.py
├── main.py                   # Point d'entrée avec presets de capital
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

### Stack technique

| Composant | Technologie |
|-----------|-------------|
| Runtime asynchrone | `asyncio` (Python 3.11+) |
| HTTP client | `aiohttp` |
| WebSocket | `websockets` |
| API Polymarket | `py-clob-client` (officiel) |
| Dashboard | `FastAPI` + `uvicorn` + `Chart.js` |
| Calcul scientifique | `numpy` + `scipy` |
| Analyse de données | `pandas` |
| Notifications | Telegram Bot API |
| Monitoring | `prometheus-client` (optionnel) |
| Déploiement | Docker / docker-compose |

---

## Démarrage rapide

### Prérequis

- Python 3.11+
- Un wallet Polygon avec des USDC
- (Optionnel) Clé API TheOddsAPI (gratuit: 500 req/mois) ou Sportradar (premium)
- (Optionnel) Bot Telegram pour les notifications

### Installation

```bash
# Cloner le projet
git clone <repo>
cd polymarket-arb-bot

# Créer l'environnement virtuel
python -m venv venv
source venv/bin/activate  # Linux/macOS
# ou: venv\Scripts\activate  # Windows

# Installer les dépendances
pip install -r requirements.txt

# Configurer l'environnement
cp config/.env.example .env
nano .env  # Remplir les valeurs
```

### Lancer en mode dry-run (simulation)

```bash
# Mode simulation — aucun fonds réel utilisé
python main.py

# Avec un preset de capital spécifique
python main.py --preset moderate_1000  # Pour $1,000 de capital
python main.py --preset standard_10000  # Pour $10,000 de capital
```

### Lancer le backtesting

```bash
# Backtester la stratégie binary arb sur 2024 (toutes catégories)
python -m backtest.backtester --start 2024-01-01 --end 2024-12-31 --preset standard_10000

# Backtester sur les marchés sportifs uniquement
python -m backtest.backtester --start 2024-01-01 --end 2024-12-31 --tags Sports NBA

# Sauvegarder les résultats
python -m backtest.backtester --start 2024-01-01 --end 2024-12-31 --save results_2024.json
```

### Lancer les tests

```bash
python -m pytest tests/ -v
```

### Déploiement Docker

```bash
# Copier et configurer .env
cp config/.env.example .env

# Build et lancer
docker-compose up -d

# Voir les logs
docker-compose logs -f bot
```

---

## Configuration

### Variables d'environnement critiques

```env
# Polymarket (REQUIS pour le mode live)
POLYMARKET_PRIVATE_KEY=0x...   # Clé privée de votre wallet Polygon
POLYMARKET_FUNDER_ADDRESS=0x... # Adresse du wallet avec les USDC

# Mode simulation (TOUJOURS commencer ici)
DRY_RUN=true
```

### Paramètres de risque

| Paramètre | Défaut | Description |
|-----------|--------|-------------|
| `MAX_TOTAL_EXPOSURE` | $10,000 | Capital total maximum exposé |
| `MAX_PER_MARKET` | $1,000 | Exposition max par marché |
| `MAX_PER_TRADE` | $500 | Taille max par trade individuel |
| `DAILY_LOSS_LIMIT` | $500 | Perte max/jour avant arrêt auto |
| `MIN_PROFIT_THRESHOLD` | $0.50 | Profit minimum par trade |
| `KELLY_FRACTION` | 0.25 | Fraction du Kelly Criterion (Quarter Kelly) |

### Activation des stratégies

```env
ENABLE_BINARY_ARB=true    # Arbitrage YES+NO < $1 (recommandé en premier)
ENABLE_REALITY_ARB=true   # Sports latency arb (nécessite clés API sportives)
ENABLE_MULTI_HEDGE=true   # Multi-outcome hedging
```

---

## Gestion des risques

Le module de risk management est le **garde-fou central** du système. Avant chaque trade:

1. **Bot en pause ?** → Refusé immédiatement
2. **Perte journalière ≥ `DAILY_LOSS_LIMIT` ?** → Bot mis en pause automatiquement
3. **Exposition totale + nouveau trade > `MAX_TOTAL_EXPOSURE` ?** → Refusé
4. **Exposition sur ce marché + nouveau trade > `MAX_PER_MARKET` ?** → Refusé
5. **Taille du trade > `MAX_PER_TRADE` ?** → Refusé
6. **Positions ouvertes ≥ `MAX_OPEN_POSITIONS` ?** → Refusé

En cas de panne de l'API ou de réseau, le bot tente 4 fois avec backoff exponentiel (2s, 4s, 8s, 16s) avant d'abandonner le trade.

---

## Backtesting

Le module de backtesting utilise les **données historiques réelles de Polymarket** via l'API Gamma (gratuite).

### Méthodologie

1. Téléchargement des marchés résolus entre deux dates
2. Récupération des historiques de prix (résolution: 1h par défaut)
3. Simulation de la stratégie: pour chaque snapshot temporel, vérifier si `YES+NO < 1`
4. Si opportunité: simuler l'achat aux prix de l'époque
5. À la résolution: calculer le profit réalisé

### Métriques de backtest

- **P&L total** et **ROI%** sur la période
- **Win Rate** (pour l'arb binaire, théoriquement proche de 100%)
- **Sharpe Ratio** annualisé
- **Max Drawdown** et **Profit Factor**
- **Décomposition mensuelle** des performances
- **Analyse de scalabilité**: projections pour différents niveaux de capital

### Données de cache

Les données téléchargées sont mises en cache dans `data/historical/` pour éviter de re-télécharger.

---

## Dashboard

Dashboard web temps réel accessible à `http://localhost:8080`

**Fonctionnalités:**
- P&L cumulatif et courbe des capitaux (Chart.js)
- Positions ouvertes avec P&L non réalisé
- Historique des 50 derniers trades
- Métriques: win rate, Sharpe, drawdown, exposition
- Contrôles: start/stop, mise à jour des paramètres de risque
- Server-Sent Events (SSE) pour les mises à jour en temps réel (toutes les 2s)

---

## Scalabilité

Le bot est conçu pour fonctionner avec **différents niveaux de capital**. Les presets de risque sont préconfigurés:

| Preset | Capital | Max/trade | Exposition max | Perte max/jour |
|--------|---------|-----------|----------------|----------------|
| `conservative_100` | $100 | $5 | $80 | $5 |
| `moderate_1000` | $1,000 | $50 | $800 | $50 |
| `standard_10000` | $10,000 | $500 | $8,000 | $500 |
| `aggressive_50000` | $50,000 | $2,500 | $40,000 | $2,500 |

**Lancer avec un preset:**
```bash
python main.py --preset conservative_100
python main.py --preset moderate_1000
```

**Important sur la scalabilité:**
- La liquidité des marchés Polymarket est limitée. Au-delà de ~$50k d'exposition, les opportunités deviennent rares.
- @swisstony a atteint $4.3M en scalant progressivement et en réinvestissant les profits.
- Commencer petit, valider la stratégie, puis augmenter le capital.

---

## Structure du projet (détail)

### Flux de données

```
APIs sportives ──────────┐
(TheOddsAPI/Sportradar)  │
                         ▼
                   SportsDataFeed
                         │
                         ▼
                 RealityArbitrageStrategy
                         │
Polymarket WS ────────────┐
(6 connexions)           │
                         ▼
                  WebSocketManager
                         │
                         ▼
                  OrderbookCache ──── MarketScanner
                         │                  │
                         ▼                  ▼
              BinaryArbitrageStrategy  MultiOutcomeHedge
                         │
                         ▼
                   RiskLimits (vérification avant chaque trade)
                         │
                         ▼
                   OrderExecutor (asyncio.gather pour les 2 jambes)
                         │
                         ▼
                 PolymarketClient (py-clob-client)
                         │
                         ▼
              Polygon Blockchain (CLOB + Settlement)
                         │
                         ▼
           PnLTracker + PositionManager + Metrics
                         │
               ┌──────────┴──────────┐
               ▼                     ▼
         Dashboard Web         Notifications
         (FastAPI/SSE)         (Telegram)
```

### Modèles de données principaux

- `Order`: Un ordre à placer (token_id, side, price, size, type)
- `Orderbook`: État du carnet d'ordres (bids + asks)
- `MarketInfo`: Métadonnées d'un marché (question, tokens, volume)
- `SportEvent`: Événement sportif en cours (score, temps, équipes)
- `Position`: Position ouverte (coût, P&L, profit garanti)
- `ArbitrageExecution`: Résultat d'une exécution (succès, profit, latence)
- `TradeRecord`: Trade enregistré dans le tracker P&L

---

## Avertissements

> ⚠️ **Ce bot est fourni à titre éducatif et expérimental.**

1. **Risque de perte de capital**: Le trading algorithmique comporte des risques significatifs. Ne jamais investir plus que ce que vous pouvez vous permettre de perdre.

2. **Toujours commencer en mode dry-run**: Tester et valider la stratégie pendant au moins 2 semaines en simulation avant de passer en mode live.

3. **Les opportunités d'arbitrage sont rares et compétitives**: Polymarket a introduit des frais dynamiques sur certains marchés. La concurrence entre bots est intense.

4. **Risques techniques**: Bugs, latence réseau, pannes API — un trade partiellement exécuté peut créer une position non couverte.

5. **Polymarket peut modifier ses règles**: Les frais, les policies, et les endpoints API peuvent changer sans préavis.

6. **Conformité légale**: Vérifier la légalité du trading sur les marchés de prédiction dans votre juridiction. Illégal dans certains pays.

7. **Limites de liquidité**: Seul 0.51% des utilisateurs de Polymarket ont gagné plus de $1,000. Les opportunités ne supportent que de petits montants.

8. **Fiscalité**: Les gains peuvent être imposables selon votre pays de résidence.

---

## Ressources

| Ressource | Lien |
|-----------|------|
| Polymarket CLOB API | https://docs.polymarket.com |
| py-clob-client (Python) | https://github.com/Polymarket/py-clob-client |
| Gamma Markets API | https://gamma-api.polymarket.com |
| WebSocket endpoint | wss://ws-subscriptions-clob.polymarket.com |
| TheOddsAPI (gratuit) | https://the-odds-api.com |
| Sportradar (premium) | https://sportradar.com |

---

## Licence

Ce projet est distribué sous licence MIT à titre éducatif. Utilisation commerciale sous votre propre responsabilité.
