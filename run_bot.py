"""Entry point for the interactive Telegram bot (long-running service).

Unlike run_hourly / run_daily (one-shot cron jobs), this process stays up and
long-polls Telegram for commands. Deploy it as the ``smartmoney-bot`` systemd
service (see deploy/), which restarts it on failure.
"""
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv()

from src.bot import run_bot  # noqa: E402  (import after load_dotenv)

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [bot] %(message)s",
    )
    try:
        run_bot()
    except KeyboardInterrupt:
        print("\nBot stopped.")
