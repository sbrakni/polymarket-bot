# Analyse de @swisstony & Mega-Prompt pour Bot d'Arbitrage Polymarket

---

## 🔍 Analyse du profil @swisstony

**Statistiques clés :**
- Profit total : ~$4.3M+ (parti de $5)
- ROI : ~740,000%
- Volume total tradé : $430M+
- Nombre de transactions (en décembre seul) : 5,527
- Profit moyen par trade : ~$156
- Style : Arbitrage ultra haute-fréquence "Ant Moving"

**Les 3 stratégies identifiées de swisstony :**

### 1. "Reality Arbitrage" (Stratégie principale — Sports)
Exploite le **décalage temporel de 15-40 secondes** entre les événements sportifs en direct et leur diffusion TV. Le bot reçoit les données en temps réel via des API sportives (données directement des stades/arènes), puis achète des contrats sous-évalués sur Polymarket **avant** que le marché ne réagisse au flux TV retardé.

### 2. Arbitrage binaire intra-marché
Achète simultanément le YES et le NO d'un même marché quand `prix_YES + prix_NO < $1.00`. Le profit garanti est `$1.00 - (prix_YES + prix_NO)` à la résolution. Exécute des dizaines de directions sur un même événement (ex: 23 directions sur un match Jazz vs Clippers).

### 3. Hedging combiné multi-outcomes (Sports)
Sur les marchés sportifs avec de multiples résultats (over/under, spreads, totaux), construit des combinaisons de couverture complexes qui ne se limitent pas au simple YES/NO mais utilisent les corrélations entre les marchés liés.

**⚠️ Points d'attention importants :**
- Certains ordres de couverture ont un total > 1, menant à des pertes inévitables sur ces ordres spécifiques
- La rentabilité repose sur un ratio risque/rendement favorable et un win rate suffisant pour compenser
- Polymarket a introduit des frais dynamiques sur les marchés crypto 15min pour contrer ce type de stratégie
- La liquidité est limitée — les mêmes opportunités ne supportent que de petits montants

---

## 🤖 MEGA-PROMPT — Bot d'Arbitrage Polymarket

Copie et colle le prompt ci-dessous dans une nouvelle conversation Claude pour générer le bot complet :

---

```
Tu es un développeur expert en trading algorithmique, blockchain (Polygon/EVM), et marchés de prédiction. Tu dois créer un bot d'arbitrage complet pour Polymarket en Python, inspiré de la stratégie du wallet @swisstony qui a transformé $5 en $4.3M.

## CONTEXTE TECHNIQUE

### Polymarket
- Marché de prédiction décentralisé sur Polygon (Chain ID 137)
- CLOB (Central Limit Order Book) hybride : matching off-chain, settlement on-chain
- API endpoint : https://clob.polymarket.com
- WebSocket : wss://ws-subscriptions-clob.polymarket.com
- Gamma Markets API pour les métadonnées : https://gamma-api.polymarket.com
- Librairie Python officielle : `py-clob-client` (pip install py-clob-client)
- Tokens : USDC (0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174) sur Polygon
- Fee : ~0.01% par trade + gas Polygon (~$0.007/tx)
- Rate limits : 100 req/min (public), 60 orders/min (trading)
- Les marchés crypto 15min ont des frais dynamiques (taker fees) qui augmentent quand les odds sont proches de 50/50

### Stratégie @swisstony à répliquer
Le bot doit implémenter 3 modules de stratégie :

**MODULE 1 — Reality Arbitrage (Sports)**
- Se connecter à des APIs de données sportives en temps réel (ex: Sportradar, TheOddsAPI, API-Football, ESPN API)
- Recevoir les événements sportifs (scores, plays) AVANT le marché Polymarket
- Détecter quand un événement modifie significativement la probabilité d'un outcome
- Acheter immédiatement le contrat sous-évalué avant que le marché ne s'ajuste
- Latence cible : < 500ms entre l'événement et l'exécution de l'ordre
- Sports ciblés : NBA, NFL, NHL, MLB, Football (soccer), Tennis

**MODULE 2 — Arbitrage Binaire (Intra-marché)**
- Scanner en continu TOUS les marchés actifs via WebSocket
- Pour chaque marché à 2 outcomes : calculer `best_ask_YES + best_ask_NO`
- Si le total < $1.00 (après frais) : acheter les deux côtés simultanément
- Profit garanti = $1.00 - total_cost - fees
- Seuil minimum de profit : configurable (défaut : $0.50 par trade)
- Ignorer les marchés avec liquidité < $1,000

**MODULE 3 — Multi-Outcome Hedging (Sports avancé)**
- Sur les marchés sportifs avec 3+ outcomes (spreads, over/under, moneyline)
- Identifier des combinaisons d'achats qui couvrent tous les scénarios
- Calculer le coût total de la couverture vs le payout garanti
- Exécuter uniquement si la marge est positive après frais
- Gérer les corrélations entre marchés liés (ex: si over/under et spread sur le même match)

## ARCHITECTURE DEMANDÉE

Crée un projet Python complet avec cette structure :

```
polymarket-arb-bot/
├── config/
│   ├── settings.py          # Configuration générale (clés, seuils, paramètres)
│   └── .env.example          # Template des variables d'environnement
├── core/
│   ├── __init__.py
│   ├── polymarket_client.py  # Wrapper autour de py-clob-client
│   ├── websocket_manager.py  # Connexion WebSocket pour les prix en temps réel
│   └── order_executor.py     # Gestion des ordres (market, limit, FOK)
├── strategies/
│   ├── __init__.py
│   ├── base_strategy.py      # Classe abstraite pour toutes les stratégies
│   ├── binary_arbitrage.py   # MODULE 2 — Arbitrage YES+NO < $1
│   ├── reality_arbitrage.py  # MODULE 1 — Exploitation du délai broadcast
│   └── multi_outcome_hedge.py # MODULE 3 — Hedging multi-outcomes
├── data/
│   ├── __init__.py
│   ├── sports_feed.py        # Intégration APIs sportives temps réel
│   ├── market_scanner.py     # Scanner de marchés Polymarket
│   └── orderbook_cache.py    # Cache local des order books
├── risk/
│   ├── __init__.py
│   ├── position_manager.py   # Gestion des positions ouvertes
│   ├── risk_limits.py        # Limites de risque (max exposure, max par marché)
│   └── pnl_tracker.py        # Suivi P&L en temps réel
├── utils/
│   ├── __init__.py
│   ├── logger.py             # Logging structuré
│   ├── notifications.py      # Alertes Telegram/Discord
│   └── metrics.py            # Métriques de performance
├── dashboard/
│   ├── __init__.py
│   └── web_dashboard.py      # Dashboard web simple (Flask/FastAPI)
├── main.py                   # Point d'entrée principal
├── requirements.txt
└── README.md
```

## SPÉCIFICATIONS TECHNIQUES DÉTAILLÉES

### 1. polymarket_client.py
```python
# Doit inclure :
# - Initialisation py-clob-client avec gestion des credentials
# - Méthodes pour : get_markets(), get_orderbook(), place_order(), cancel_order()
# - Retry logic avec exponential backoff
# - Rate limiting intégré (respecter 60 orders/min)
# - Gestion des erreurs spécifiques Polymarket (401, 429, 500)
# - Support des ordres GTC, FOK, et Market
# - Logging de chaque ordre avec timestamp, prix, taille, marché
```

### 2. websocket_manager.py
```python
# Doit inclure :
# - 6 connexions WebSocket parallèles (comme les bots pro)
# - Souscription aux channels : orderbook, trades, market updates
# - Reconnexion automatique avec backoff
# - Callbacks pour chaque type d'événement
# - File d'attente thread-safe pour les updates
# - Heartbeat monitoring
```

### 3. binary_arbitrage.py
```python
# Algorithme :
# 1. Pour chaque marché actif à 2 outcomes :
#    a. Récupérer best_ask pour YES et best_ask pour NO
#    b. total_cost = best_ask_YES + best_ask_NO
#    c. Si total_cost < (1.0 - min_profit_threshold - estimated_fees) :
#       - Calculer la taille optimale (min des liquidités disponibles)
#       - Vérifier les risk limits
#       - Placer les 2 ordres simultanément (asyncio.gather)
#       - Confirmer les fills
#       - Logger l'opération
# 2. Filtres : liquidité > $1000, spread raisonnable, pas de marché en résolution imminente biaisée
```

### 4. reality_arbitrage.py
```python
# Algorithme :
# 1. Se connecter aux flux sportifs en temps réel
# 2. Mapper chaque événement sportif aux marchés Polymarket correspondants
# 3. Quand un événement se produit (but, touchdown, panier, etc.) :
#    a. Calculer le shift de probabilité attendu
#    b. Comparer avec le prix actuel sur Polymarket
#    c. Si écart > seuil (ex: 5% de marge) :
#       - Déterminer la direction (acheter YES ou NO)
#       - Calculer la taille basée sur Kelly Criterion
#       - Exécuter immédiatement (ordre FOK ou Market)
#    d. Optionnel : placer un ordre de sortie si le marché s'ajuste
# 4. Gestion de la latence : mesurer et logger le temps entre l'événement et l'exécution
```

### 5. multi_outcome_hedge.py
```python
# Algorithme :
# 1. Identifier les marchés avec 3+ outcomes liés au même événement
# 2. Pour chaque combinaison possible d'outcomes :
#    a. Calculer le coût total de la couverture
#    b. Déterminer le payout minimum garanti
#    c. Si payout > coût + frais : exécuter
# 3. Optimisation : utiliser la programmation linéaire pour trouver
#    la combinaison optimale d'outcomes à acheter
# 4. Gérer les corrélations (ex: si "Team A wins" ET "over 200.5" sont liés)
```

### 6. Risk Management
```python
# Paramètres configurables :
# - max_total_exposure : montant max total engagé ($)
# - max_per_market : montant max par marché ($)
# - max_per_trade : montant max par trade ($)
# - daily_loss_limit : perte max journalière avant arrêt ($)
# - max_open_positions : nombre max de positions ouvertes
# - min_profit_threshold : profit minimum par trade ($)
# - kelly_fraction : fraction du Kelly Criterion à utiliser (0.25 recommandé)
```

### 7. Notifications (Telegram)
```python
# Envoyer des alertes pour :
# - Chaque trade exécuté (marché, direction, prix, taille, profit estimé)
# - Opportunités d'arbitrage détectées (même si non exécutées)
# - Erreurs critiques
# - Résumé journalier (P&L, nombre de trades, win rate)
# - Alertes de risk limits atteints
```

### 8. Dashboard Web
```python
# Interface simple avec :
# - P&L en temps réel (graphique)
# - Positions ouvertes
# - Historique des trades
# - Métriques : win rate, profit moyen, Sharpe ratio
# - Contrôles : start/stop, ajustement des paramètres
# - Logs en direct
```

## CONFIGURATION (.env.example)
```
# Polymarket
POLYMARKET_PRIVATE_KEY=
POLYMARKET_FUNDER_ADDRESS=
POLYGON_RPC_URL=https://polygon-rpc.com

# Sports Data APIs
SPORTRADAR_API_KEY=
THEODDSAPI_KEY=
ESPN_API_KEY=

# Notifications
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Risk Parameters
MAX_TOTAL_EXPOSURE=10000
MAX_PER_MARKET=1000
MAX_PER_TRADE=500
DAILY_LOSS_LIMIT=500
MIN_PROFIT_THRESHOLD=0.50
KELLY_FRACTION=0.25

# Bot Settings
DRY_RUN=true
LOG_LEVEL=INFO
SCAN_INTERVAL_MS=100
WS_RECONNECT_DELAY_S=5
```

## EXIGENCES DE QUALITÉ

1. **Tout en asyncio** : utiliser aiohttp, asyncio.gather pour la concurrence
2. **Typage strict** : annotations de type partout, dataclasses pour les modèles
3. **Tests** : au minimum des tests unitaires pour les calculs d'arbitrage
4. **Dry-run mode** : pouvoir tester sans exécuter de vrais ordres
5. **Logging structuré** : JSON logging avec contexte (trade_id, market_id, strategy)
6. **Graceful shutdown** : gestion propre de SIGINT/SIGTERM
7. **Idempotence** : chaque ordre doit avoir un client_order_id unique
8. **Monitoring** : métriques Prometheus-compatible (optionnel)

## IMPORTANT — AVERTISSEMENTS À INCLURE DANS LE README

1. Ce bot est à usage éducatif et expérimental
2. Le trading sur les marchés de prédiction comporte des risques significatifs
3. Toujours commencer en mode dry-run
4. Les opportunités d'arbitrage sont de plus en plus rares et compétitives
5. Polymarket peut modifier ses frais et politiques à tout moment
6. Vérifier la légalité dans votre juridiction
7. Ne jamais investir plus que ce que vous pouvez vous permettre de perdre

## LIVRABLE

Génère le code complet de CHAQUE fichier, fonctionnel et prêt à l'emploi. Commence par main.py puis chaque module dans l'ordre. Assure-toi que le code est cohérent entre les fichiers et que les imports sont corrects. Inclus des commentaires explicatifs en français pour les parties stratégiques.
```

---

## 📚 Ressources utiles

| Ressource | Lien |
|-----------|------|
| Polymarket CLOB API Docs | https://docs.polymarket.com |
| py-clob-client (Python) | https://github.com/Polymarket/py-clob-client |
| Gamma Markets API | https://gamma-api.polymarket.com |
| WebSocket endpoint | wss://ws-subscriptions-clob.polymarket.com |
| Bot open-source de référence | https://github.com/Gabagool2-2/polymarket-trading-bot-python |
| Polytrage (alertes Telegram) | https://polymark.et/product/polytrage |
| Polymarket Analytics | https://polymarketanalytics.com/traders |

## ⚠️ Avertissement

Ce document est fourni à titre éducatif. Le trading algorithmique sur les marchés de prédiction comporte des risques importants, notamment la perte de capital, les risques techniques (bugs, latence), et les risques réglementaires. Polymarket adapte activement ses systèmes pour contrer les stratégies d'arbitrage (ex: frais dynamiques sur les marchés crypto 15min). Seul 0.51% des utilisateurs de Polymarket ont gagné plus de $1,000. À utiliser avec prudence et en mode dry-run d'abord.
