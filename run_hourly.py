"""Hourly runner: ingest + alerts.

Invoked by a systemd timer or cron:
  /root/onchain-smart-money/.venv/bin/python run_hourly.py
"""
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/hourly.log"),
    ],
)

from src.alerts import run_alerts
from src.ingest import run_ingest

if __name__ == "__main__":
    results = run_ingest()
    sent = run_alerts()

    new_tokens = sum(r["token_new"] for r in results)
    new_eth = sum(r["eth_new"] for r in results)
    print(f"Hourly run complete — {new_eth} ETH txs, {new_tokens} token txs, {sent} alert(s) sent")
