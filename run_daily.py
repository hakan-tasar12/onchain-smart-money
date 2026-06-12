"""Daily runner: PnL computation + wallet scoring.

Invoked by the systemd timer at 02:00 UTC:
  /root/onchain-smart-money/.venv/bin/python run_daily.py
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
        logging.FileHandler("logs/daily.log"),
    ],
)

from src.pnl import run_pnl
from src.scoring import run_scoring

if __name__ == "__main__":
    run_pnl()
    run_scoring()
    print("Daily run complete — PnL + scores updated")
