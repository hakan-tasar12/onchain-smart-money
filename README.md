# onchain-smart-money

Open-source on-chain tracker for Ethereum wallets. Tracks token flows, computes realized PnL with a FIFO engine, scores wallets on four metrics, and sends Telegram alerts when 2+ wallets accumulate the same token. Built with Claude.

## How it works

Project A reads ETH balances and token transfers from Etherscan, computes net position per token, and flags each wallet as accumulating or distributing.

Project B (current) stores 12 months of transfers in SQLite, runs a FIFO PnL engine on top, scores each wallet daily, and fires consensus alerts.

## PnL note

Prices come from CoinGecko daily-close data, not the actual swap price, so the numbers are approximate. If a wallet opened a position before the 12-month window, the cost basis is unknown — those sells are excluded from PnL, not set to zero. Coverage % shows how reliable each wallet's data is.

Spam tokens are filtered by contract address (anything not on CoinGecko gets dropped) and a regex that catches phishing names like "claim", "reward", and URL patterns.

## Setup

```bash
git clone https://github.com/your-username/onchain-smart-money.git
cd onchain-smart-money
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Add your API keys to `.env`, then add wallet addresses to `watchlist.txt`.

## Running

```bash
streamlit run app.py     # dashboard at http://localhost:8501
python run_hourly.py     # ingest + alerts
python run_daily.py      # PnL + scoring (first run: 5-10 min)
```

VPS scheduler: `bash deploy/setup_systemd.sh`

## Files

| File | What it does |
|---|---|
| `src/etherscan.py` | Etherscan API wrapper |
| `src/prices.py` | CoinGecko price lookup by contract address |
| `src/wallet.py` | Net position, spam filter |
| `src/db.py` | SQLite init and CRUD |
| `src/ingest.py` | Etherscan to SQLite |
| `src/pnl.py` | FIFO PnL engine |
| `src/scoring.py` | Wallet scoring |
| `src/alerts.py` | Telegram alerts |
| `run_hourly.py` | Ingest + alerts |
| `run_daily.py` | PnL + scoring |
| `app.py` | Streamlit dashboard |

## Stack

Python, SQLite, Etherscan API, CoinGecko API, pandas, Streamlit, Telegram Bot API

## Roadmap

- [x] A — Watchlist tracker
- [x] B — Smart money dashboard (current)
- [ ] C — Alpha discovery engine
