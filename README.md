# onchain-smart-money

Open-source on-chain tracker for Ethereum wallets. Tracks token flows, computes realized PnL with a FIFO engine, scores wallets on four metrics, and sends Telegram alerts when 2+ wallets accumulate the same token.

## How it works

The tracker reads ETH balances and ERC-20 transfers from Etherscan, stores 12 months of history in SQLite, and computes the net position per token to flag each wallet as accumulating or distributing. On top of that, a FIFO PnL engine prices each lot, a daily job scores every wallet, and consensus alerts fire when independent wallets converge on the same token.

## Methodology

PnL and scoring rest on a few explicit conventions — stated here so the numbers
can be read correctly:

- **Pricing.** Tokens are priced by **contract address, not symbol**, so a
  symbol-spoofed scam token can never match a legitimate one. Historical PnL uses
  CoinGecko **daily-close** prices — not the actual swap execution price — so
  realized figures are *approximate*, not exact accounting.
- **FIFO matching.** Buys open lots; sells consume the oldest open lots first.
  Realized PnL = proceeds − cost basis of the consumed lots, over a rolling
  **12-month window**.
- **Unknown cost basis.** If a sell has no matching lot (the position was opened
  before the 12-month window), it is recorded as *unmatched* and **excluded from
  PnL — not set to zero**. The **Coverage %** reported per wallet is the fraction
  of sells with a known cost basis, i.e. how much to trust that wallet's PnL.
- **Wash-trade guard.** Sells against a lot younger than **60 seconds** are
  skipped, to drop intraday round-trips and arbitrage noise.
- **Spam filtering.** Tokens absent from CoinGecko are dropped; a regex catches
  phishing names ("claim", "reward", URL patterns).

## Assumptions & limitations

Stated up front, because what a model *doesn't* do matters as much as what it does:

- Prices are **daily-close approximations**, not execution prices — realized PnL is
  indicative, not reconciled accounting.
- **Gas fees and transaction costs are not modeled.**
- **MEV, sandwiching, and internal contract transfers** are not accounted for.
- Token amounts are handled as **floating-point, not fixed-point `Decimal`** — fine
  for analytics, not suitable for exact financial reconciliation.
- **Daily price granularity** ignores intraday moves; sub-day trades are valued at
  the day's close.
- Positions opened before the 12-month window have unknown cost basis and are
  excluded (see Coverage above).
- This is an **analytics tool, not a trading system, and not financial advice.**

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

## Tests

The FIFO accounting core ([`src/fifo.py`](src/fifo.py)) is pure and side-effect
free, so the PnL math is verified against hand-computed examples — FIFO ordering
across lots, partial fills, the wash-trade guard, and unmatched (pre-window) sells:

```bash
pip install -r requirements-dev.txt
pytest
```

## Roadmap

- [x] A — Watchlist tracker
- [x] B — Smart money dashboard (current)
- [ ] C — Alpha discovery engine

## License

MIT — see [LICENSE](LICENSE).
