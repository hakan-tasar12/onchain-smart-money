"""One-time cache warm-up: fetch every wallet's token price series with the
per-wallet budget lifted, so the real+negative price cache is fully populated in a
single pass. After this, scheduled daily runs hit the cache and finish fast with
full coverage. Safe to re-run (idempotent; clear_pnl_data resets per wallet)."""
import logging
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])

from src import pnl
pnl.PER_WALLET_BUDGET = 10 ** 9  # effectively unbounded for this one-time warm-up

if __name__ == "__main__":
    start = time.time()
    pnl.run_pnl()
    print(f"PREWARM DONE in {time.time() - start:.0f}s", flush=True)
