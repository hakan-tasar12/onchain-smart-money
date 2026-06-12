"""Consensus accumulation Telegram alerts."""
import logging
import os
import time

import requests

from src.db import get_last_alert_ts, get_recent_token_accumulations, insert_alert

log = logging.getLogger(__name__)

COOLDOWN_SECONDS = 6 * 3600  # minimum gap between alerts for the same contract


def send_telegram(message: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        log.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        return r.json().get("ok", False)
    except Exception as e:
        log.error(f"Telegram send error: {e}")
        return False


def _format_alert(contract: str, symbol: str, wallets: list[str], wallet_labels: dict) -> str:
    from datetime import datetime, timezone
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    contract_short = contract[:6] + "..." + contract[-4:]
    wallet_lines = "\n".join(
        f"  {wallet_labels.get(w, w[:10] + '...')}" for w in wallets
    )
    return (
        f"<b>${symbol}</b> — {len(wallets)} wallets accumulating\n"
        f"<code>{contract_short}</code>\n\n"
        f"{wallet_lines}\n\n"
        f"{now_str}"
    )


def run_alerts(watchlist_path: str = "watchlist.txt") -> int:
    from src.wallet import load_watchlist
    wallets = load_watchlist(watchlist_path)
    wallet_labels = {w["address"].lower(): w["label"] for w in wallets}

    accumulations = get_recent_token_accumulations(hours=1)
    now = int(time.time())
    sent = 0

    for acc in accumulations:
        contract = acc["contract"]
        symbol = acc.get("symbol") or "?"
        wallet_addrs = acc["wallets"].split(",")

        last_ts = get_last_alert_ts(contract)
        if now - last_ts < COOLDOWN_SECONDS:
            log.info(f"Cooldown: {symbol} ({contract[:8]}...) skipped")
            continue

        message = _format_alert(contract, symbol, wallet_addrs, wallet_labels)
        ok = send_telegram(message)
        insert_alert(contract, symbol, wallet_addrs, ok)

        if ok:
            log.info(f"Alert sent: ${symbol} ({len(wallet_addrs)} wallets)")
            sent += 1
        else:
            log.warning(f"Alert failed: ${symbol}")

    return sent


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [alerts] %(message)s")
    count = run_alerts()
    print(f"Alerts sent: {count}")
