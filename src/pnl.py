"""FIFO PnL engine using CoinGecko daily-close prices. Figures are approximate."""
import json
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import requests

from src.db import (
    clear_pnl_data,
    get_pnl_coverage,
    get_token_transfers_for_pnl,
    insert_pnl_lot,
    insert_pnl_record,
    insert_unmatched_sell,
)
from src.fifo import EPS, match_fifo
from src.prices import _contract_to_id_map

log = logging.getLogger(__name__)

HIST_CACHE_DIR = Path(".cache/prices/historical")
TWELVE_MONTHS_SECONDS = 365 * 24 * 3600


def _get_historical_price(coin_id: str, ts: int) -> float | None:
    """USD price for a coin on a given day, or None if unavailable.

    Returns None (never a fabricated 0.0) when the price cannot be obtained, so the
    FIFO core records the trade as ``no_price`` instead of inventing PnL. A non-
    positive price is also treated as unavailable: a $0 historical price is never a
    usable value, and this auto-heals any 0.0 that a prior interrupted run cached.
    """
    # Persistent cache is safe here — historical prices don't change.
    HIST_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    date_str = dt.strftime("%d-%m-%Y")  # CoinGecko: DD-MM-YYYY
    cache_file = HIST_CACHE_DIR / f"{coin_id}_{date_str.replace('-', '')}.json"

    if cache_file.exists():
        try:
            usd = json.loads(cache_file.read_text()).get("usd")
            if usd is not None and usd > 0:
                return usd
            # cached miss (None or <=0) -> fall through and try to fetch a real price
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
            price = (data.get("market_data") or {}).get("current_price", {}).get("usd")
            if price is not None and price > 0:
                cache_file.write_text(json.dumps({"usd": price, "date": date_str}))
                time.sleep(0.6)  # CoinGecko free tier: ~10 req/s
                return price
            # CoinGecko had no price for this coin/day — cache the miss so we don't refetch
            cache_file.write_text(json.dumps({"usd": None, "date": date_str}))
            time.sleep(0.6)
            return None
        except Exception as e:
            log.warning(f"Historical price {coin_id} {date_str}: {e}")
            time.sleep(2)

    return None


def _process_wallet_pnl(wallet_address: str, cmap: dict) -> dict:
    wallet_address = wallet_address.lower()
    # drop derived PnL and recompute from raw transfers
    clear_pnl_data(wallet_address)

    since_ts = int(time.time()) - TWELVE_MONTHS_SECONDS
    transfers = get_token_transfers_for_pnl(wallet_address, since_ts=since_ts)

    contracts = list({t["contract"] for t in transfers})
    stats = {"lots_created": 0, "trades_closed": 0, "total_pnl": Decimal("0"),
             "unmatched": 0, "wash_skipped": 0}

    for contract in contracts:
        coin_id = cmap.get(contract)
        if not coin_id:
            continue  # not in CoinGecko, likely spam — skip

        contract_txns = [t for t in transfers if t["contract"] == contract]

        # FIFO matching is delegated to the pure, unit-tested core (src/fifo.py).
        # This module only handles I/O: pricing (CoinGecko) and persistence (SQLite).
        # CoinGecko returns float prices; convert to Decimal at this boundary via
        # str() so the accounting core stays exact. A None price is passed through
        # unchanged so the core records the trade as no_price, never a $0 fill.
        def _price(ts, _cid=coin_id):
            p = _get_historical_price(_cid, ts)
            return None if p is None else Decimal(str(p))

        result = match_fifo(contract_txns, price_fn=_price)

        # Persist open lots (final remaining), realized fills, and unmatched sells.
        for lot in result.lots:
            if lot.remaining > EPS:
                insert_pnl_lot(
                    wallet_address=wallet_address,
                    contract=contract,
                    acquired_ts=lot.acquired_ts,
                    amount=lot.remaining,
                    cost_usd_per_unit=lot.cost_per_unit,
                )
        for fill in result.fills:
            insert_pnl_record(
                wallet_address=wallet_address,
                contract=contract,
                symbol=fill.symbol,
                close_ts=fill.close_ts,
                realized_pnl=fill.realized_pnl,
                cost_basis=fill.cost_basis,
                proceeds=fill.proceeds,
            )
        for unmatched in result.unmatched:
            insert_unmatched_sell(
                wallet_address, contract, unmatched.symbol,
                unmatched.ts, unmatched.amount, unmatched.reason,
            )

        stats["lots_created"] += result.lots_created
        stats["trades_closed"] += result.trades_closed
        stats["total_pnl"] += result.realized_pnl
        stats["unmatched"] += len(result.unmatched)
        stats["wash_skipped"] += result.wash_skipped

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
