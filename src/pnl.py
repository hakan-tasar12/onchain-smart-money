"""FIFO PnL engine using CoinGecko daily-close prices. Figures are approximate."""
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from src.db import (
    clear_pnl_data,
    get_pnl_coverage,
    get_pnl_lots,
    get_token_transfers_for_pnl,
    insert_pnl_lot,
    insert_pnl_record,
    insert_unmatched_sell,
    update_lot_remaining,
)
from src.prices import _contract_to_id_map

log = logging.getLogger(__name__)

HIST_CACHE_DIR = Path(".cache/prices/historical")
TWELVE_MONTHS_SECONDS = 365 * 24 * 3600


def _get_historical_price(coin_id: str, ts: int) -> float:
    # Persistent cache is safe here — historical prices don't change.
    HIST_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    date_str = dt.strftime("%d-%m-%Y")  # CoinGecko: DD-MM-YYYY
    cache_file = HIST_CACHE_DIR / f"{coin_id}_{date_str.replace('-', '')}.json"

    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text()).get("usd", 0.0)
        except Exception:
            pass

    for attempt in range(3):
        try:
            r = requests.get(
                f"https://api.coingecko.com/api/v3/coins/{coin_id}/history",
                params={"date": date_str, "localization": "false"},
                timeout=15,
            )
            if r.status_code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            if r.status_code != 200:
                break
            data = r.json()
            price = (data.get("market_data") or {}).get("current_price", {}).get("usd", 0.0)
            cache_file.write_text(json.dumps({"usd": price, "date": date_str}))
            time.sleep(0.6)  # CoinGecko free tier: ~10 req/s
            return price
        except Exception as e:
            log.warning(f"Historical price {coin_id} {date_str}: {e}")
            time.sleep(2)

    return 0.0


def _process_wallet_pnl(wallet_address: str, cmap: dict) -> dict:
    wallet_address = wallet_address.lower()
    # drop derived PnL and recompute from raw transfers
    clear_pnl_data(wallet_address)

    since_ts = int(time.time()) - TWELVE_MONTHS_SECONDS
    transfers = get_token_transfers_for_pnl(wallet_address, since_ts=since_ts)

    contracts = list({t["contract"] for t in transfers})
    stats = {"lots_created": 0, "trades_closed": 0, "total_pnl": 0.0,
             "unmatched": 0, "wash_skipped": 0}

    for contract in contracts:
        coin_id = cmap.get(contract)
        if not coin_id:
            continue  # not in CoinGecko, likely spam — skip

        contract_txns = [t for t in transfers if t["contract"] == contract]
        contract_txns.sort(key=lambda x: (x["block_ts"], x["id"]))

        for tx in contract_txns:
            amount = abs(tx["amount"])
            if amount < 1e-9:
                continue

            symbol = tx.get("symbol") or "?"

            if tx["amount"] > 0:  # IN — buy, open new lot
                price = _get_historical_price(coin_id, tx["block_ts"])
                insert_pnl_lot(
                    wallet_address=wallet_address,
                    contract=contract,
                    acquired_ts=tx["block_ts"],
                    amount=amount,
                    cost_usd_per_unit=price,
                )
                stats["lots_created"] += 1

            else:  # OUT — sell/transfer, consume lots FIFO
                lots = get_pnl_lots(wallet_address, contract)

                if not lots:
                    # no lot found — position was opened before the 12-month window, cost basis unknown
                    insert_unmatched_sell(
                        wallet_address, contract, symbol, tx["block_ts"], amount, "no_lot"
                    )
                    stats["unmatched"] += 1
                    continue

                # Wash-trade guard: oldest open lot < 60 s old (intraday round-trip / arbitrage)
                oldest_acquired = lots[0]["acquired_ts"]
                if (tx["block_ts"] - oldest_acquired) < 60:
                    stats["wash_skipped"] += 1
                    continue

                sell_price = _get_historical_price(coin_id, tx["block_ts"])
                remaining_to_sell = amount
                total_cost = 0.0
                total_proceeds = 0.0

                for lot in lots:
                    if remaining_to_sell <= 1e-9:
                        break
                    used = min(lot["amount_remaining"], remaining_to_sell)
                    total_cost += used * lot["cost_usd_per_unit"]
                    total_proceeds += used * sell_price
                    update_lot_remaining(lot["id"], lot["amount_remaining"] - used)
                    remaining_to_sell -= used

                # Record PnL for the matched portion
                matched_amount = amount - remaining_to_sell
                if matched_amount > 1e-9:
                    realized_pnl = total_proceeds - total_cost
                    insert_pnl_record(
                        wallet_address=wallet_address,
                        contract=contract,
                        symbol=symbol,
                        close_ts=tx["block_ts"],
                        realized_pnl=realized_pnl,
                        cost_basis=total_cost,
                        proceeds=total_proceeds,
                    )
                    stats["trades_closed"] += 1
                    stats["total_pnl"] += realized_pnl

                # sold more than available lots — surplus came from a pre-window position
                if remaining_to_sell > 1e-9:
                    insert_unmatched_sell(
                        wallet_address, contract, symbol, tx["block_ts"],
                        remaining_to_sell, "partial_no_lot"
                    )
                    stats["unmatched"] += 1

    return stats


def run_pnl(watchlist_path: str = "watchlist.txt") -> None:
    from src.wallet import load_watchlist
    cmap = _contract_to_id_map()
    wallets = load_watchlist(watchlist_path)
    for w in wallets:
        log.info(f"PnL: {w['label']}")
        stats = _process_wallet_pnl(w["address"], cmap)
        cov = get_pnl_coverage(w["address"])
        cov_str = f"{cov['coverage']*100:.0f}%" if cov["coverage"] is not None else "—"
        log.info(
            f"  {stats['lots_created']} lots, "
            f"{stats['trades_closed']} closed trades, "
            f"PnL: ${stats['total_pnl']:.2f} | "
            f"coverage {cov_str} "
            f"({stats['unmatched']} unmatched, {stats['wash_skipped']} wash-skipped)"
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [pnl] %(message)s")
    run_pnl()
