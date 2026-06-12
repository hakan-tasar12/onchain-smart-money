"""Etherscan to SQLite ingest. Runs hourly (invoked by run_hourly.py)."""
import logging
import time

from src.db import init_db, insert_eth_transfer, insert_token_transfer, upsert_wallet
from src.etherscan import get_token_transfers_since, get_transactions_since
from src.wallet import is_spam_token, load_watchlist

log = logging.getLogger(__name__)

WINDOW_SECONDS = 365 * 24 * 3600  # 12-month lookback window


def ingest_wallet(address: str, label: str) -> dict:
    """Fetch the last 12 months of transfers from Etherscan and write to DB.
    Safe to re-run — duplicates are silently ignored by the UNIQUE constraint.
    """
    address = address.lower()
    upsert_wallet(address, label)
    since_ts = int(time.time()) - WINDOW_SECONDS

    eth_new = 0
    try:
        txns = get_transactions_since(address, since_ts)
        for tx in txns:
            value_eth = int(tx.get("value", 0)) / 1e18
            direction_sign = 1.0 if tx.get("to", "").lower() == address else -1.0
            ok = insert_eth_transfer(
                wallet_address=address,
                tx_hash=tx.get("hash", ""),
                block_number=int(tx.get("blockNumber", 0)),
                block_ts=int(tx.get("timeStamp", 0)),
                value_eth=round(value_eth * direction_sign, 8),
            )
            if ok:
                eth_new += 1
    except Exception as e:
        log.warning(f"ETH transfers error for {address[:10]}...: {e}")

    token_new = 0
    spam_skipped = 0
    try:
        token_txns = get_token_transfers_since(address, since_ts)
        for tx in token_txns:
            symbol = tx.get("tokenSymbol", "")
            name = tx.get("tokenName", "")
            if is_spam_token(symbol, name):
                spam_skipped += 1
                continue
            decimals = int(tx.get("tokenDecimal", 18) or 18)
            amount = int(tx.get("value", 0)) / (10 ** decimals)
            direction_sign = 1.0 if tx.get("to", "").lower() == address else -1.0
            ok = insert_token_transfer(
                wallet_address=address,
                tx_hash=tx.get("hash", ""),
                block_number=int(tx.get("blockNumber", 0)),
                block_ts=int(tx.get("timeStamp", 0)),
                contract=tx.get("contractAddress", "").lower(),
                symbol=symbol,
                token_name=name,
                decimals=decimals,
                amount=round(amount * direction_sign, 8),
            )
            if ok:
                token_new += 1
    except Exception as e:
        log.warning(f"Token transfers error for {address[:10]}...: {e}")

    return {"eth_new": eth_new, "token_new": token_new, "spam_skipped": spam_skipped}


def run_ingest(watchlist_path: str = "watchlist.txt") -> list[dict]:
    init_db()
    wallets = load_watchlist(watchlist_path)
    results = []
    for w in wallets:
        log.info(f"Ingesting {w['label']} ({w['address'][:10]}...)")
        summary = ingest_wallet(w["address"], w["label"])
        summary["label"] = w["label"]
        results.append(summary)
        time.sleep(0.5)

    total_eth = sum(r["eth_new"] for r in results)
    total_token = sum(r["token_new"] for r in results)
    total_spam = sum(r["spam_skipped"] for r in results)
    log.info(
        f"Ingest complete: {total_eth} new ETH txs, "
        f"{total_token} new token txs, {total_spam} spam skipped"
    )
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [ingest] %(message)s")
    run_ingest()
