# Jev Polymarket Executor

Polymarket trading executor powered by Jev decision engine. Aggregates signals from multiple sources, evaluates markets for mispricing, and executes maker-only orders on Polymarket CLOB.

## Architecture

```
Signal Sources          Decision Engine         Execution Layer
┌─────────────────┐    ┌──────────────────┐    ┌──────────────────┐
│ jev-polymarket   │───▶│                  │───▶│ PolymarketClient │
│ jev-news-sent.   │───▶│  JevExecutor     │───▶│ OrderManager     │
│ jev-crypto-dec.  │───▶│  (OpenRouter)    │───▶│ RiskManager      │
└─────────────────┘    └──────────────────┘    └──────────────────┘
         │                     │                        │
    SignalAggregator    fair_prob, edge           PaperTrader
```

## Quick Start

```bash
cd ~/Projects/jev-polymarket-executor
python3 -m venv .venv
source .venv/bin/activate
pip install requests python-dotenv pyyaml

# Copy and edit .env
cp .env.example .env
# Edit .env with your OpenRouter API key

# Scan markets
python run.py scan

# Run Jev decisions
python run.py decide

# Execute trades (paper mode by default)
python run.py execute

# Monitor positions
python run.py monitor

# Check stats
python run.py stats
```

## Commands

| Command | Description |
|---------|-------------|
| `scan` | Scan Polymarket for tradeable markets (BTC/ETH daily, 5m/15m, political) |
| `decide` | Run Jev decision engine on scanned markets |
| `execute` | Execute trades based on decisions |
| `monitor` | Monitor positions, cancel stale orders |
| `positions` | Show current live and paper positions |
| `stats` | Show P&L, win rate, Brier score |
| `redeem` | Redeem winning positions |

## Configuration

Edit `config.yaml` or use environment variables in `.env`:

- `min_edge`: Minimum edge to trade (default: 8%)
- `max_per_trade`: Max dollars per trade (default: $10)
- `max_exposure`: Max total exposure (default: $100)
- `daily_loss_limit`: Daily loss circuit breaker (default: $50)
- `paper_mode`: Simulate trades (default: true)

## Risk Management

- **Max per trade**: $10
- **Max exposure**: $100 total
- **Daily loss limit**: $50 circuit breaker
- **Kelly sizing**: Quarter-Kelly (0.25) for conservative sizing
- **Circuit breaker**: Stops after 3 consecutive losses
- **Expiry guard**: Skips markets in last 75 seconds
- **Kill switch**: Touch `data/KILL` to halt all trading

## Jev Integration

Uses OpenRouter's Jev decisions endpoint:
- Endpoint: `POST https://openrouter.ai/api/alpha/decisions`
- Model: `typesafe/jev-1.13`
- Questions: fair_probability (0-100), mispriced (yes/no), execute_action, confidence

## File Structure

```
jev-polymarket-executor/
├── polymarket_client.py   # CLOB API client, Gamma API, WebSocket
├── signal_aggregator.py   # Multi-source signal aggregation
├── jev_executor.py        # Jev decision engine
├── order_manager.py       # Order lifecycle, kill switch
├── risk_manager.py        # Position limits, Kelly, circuit breaker
├── market_scanner.py      # Market discovery and filtering
├── paper_trader.py        # Paper trading simulation
├── run.py                 # CLI entrypoint
├── config.yaml            # Configuration
├── .env.example           # Environment template
└── data/                  # Runtime data (positions, trades, decisions)
```

## Signal Sources

Reads from sibling projects:
- `../jev-polymarket/data/decisions.jsonl` (weight: 0.45)
- `../jev-news-sentiment/data/*.jsonl` (weight: 0.30)
- `../jev-crypto-decisions/data/*.jsonl` (weight: 0.25)

## Market Types

- **BTC/ETH Daily**: "Will Bitcoin be above $X at Y date?"
- **5m/15m Up/Down**: Short-term price direction markets
- **Political**: Election and policy outcome markets
- **Crypto**: General crypto market events