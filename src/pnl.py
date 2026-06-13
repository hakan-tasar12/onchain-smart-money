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

SERIES_CACHE_DIR = Path(".cache/prices/series")
TWELVE_MONTHS_SECONDS = 365 * 24 * 3600
SERIES_TTL = 24 * 3600              # refetch a coin's daily series at most once a day
_MIN_CALL_GAP = 2.0                 # min seconds between CoinGecko calls (global throttle)
PER_WALLET_BUDGET = 180            # max seconds of new price fetching per wallet

# One in-memory series cache per process so a coin shared across many wallets
# (WETH, USDC, ...) is fetched at most once per run, not once per wallet.
_series_mem: dict[str, dict[str, float]] = {}
_last_call_ts = 0.0


def _day_str(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _throttle() -> None:
    """Space out CoinGecko calls to stay under the free-tier rate limit."""
    global _last_call_ts
    gap = time.time() - _last_call_ts
    if gap < _MIN_CALL_GAP:
        time.sleep(_MIN_CALL_GAP - gap)
    _last_call_ts = time.time()


def _fetch_price_series(coin_id: str, from_ts: int, to_ts: int) -> dict[str, float]:
    """Fetch a coin's daily USD price series in ONE market_chart/range call.

    Replaces the old per-day /coins/{id}/history endpoint (one HTTP call per
    calendar day) with a single range call that returns every daily point for the
    window. For wallets with hundreds of trade-days this collapses thousands of
    sequential calls — and their rate-limit stalls — into one. Returns a
    {YYYY-MM-DD: price} map; an empty map on failure (→ all prices None → no_price).

    For ranges over 90 days CoinGecko free returns daily granularity, which is
    exactly the daily-close basis the PnL methodology already documents.
    """
    for attempt in range(4):
        try:
            _throttle()
            r = requests.get(
                f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart/range",
                params={"vs_currency": "usd", "from": from_ts, "to": to_ts},
                timeout=30,
            )
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code != 200:
                log.warning(f"Price series {coin_id}: HTTP {r.status_code}")
                return {}
            series: dict[str, float] = {}
            for ms, price in r.json().get("prices", []):
                if price and price > 0:
                    # Keep the last point seen for each UTC day (the daily close).
                    series[_day_str(int(ms / 1000))] = price
            return series
        except Exception as e:
            log.warning(f"Price series {coin_id}: {e}")
            time.sleep(2)
    return {}


def _get_price_series(
    coin_id: str, from_ts: int, to_ts: int, deadline: float | None = None
) -> dict[str, float]:
    """Daily price series for a coin, served from memory → disk (24h) → network.

    Cached reads (memory/disk) are always served. A network fetch is skipped once
    the per-wallet ``deadline`` has passed, so a single heavy wallet cannot stall
    the whole run — its un-cached coins simply resolve to no_price this time.
    """
    if coin_id in _series_mem:
        return _series_mem[coin_id]

    SERIES_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = SERIES_CACHE_DIR / f"{coin_id}.json"
    if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < SERIES_TTL:
        try:
            series = json.loads(cache_file.read_text())
            _series_mem[coin_id] = series
            return series
        except Exception:
            pass

    if deadline is not None and time.time() > deadline:
        return {}  # out of budget — don't start a new network fetch

    # Pad the window by a day each side so trades near the edges still resolve.
    series = _fetch_price_series(coin_id, from_ts - 86400, to_ts + 86400)
    if series:  # only persist a real series; a failed fetch retries next run
        try:
            cache_file.write_text(json.dumps(series))
        except Exception:
            pass
    _series_mem[coin_id] = series
    return series


def _price_on_day(series: dict[str, float], ts: int) -> float | None:
    """Look up the daily price for a timestamp, or None if the day is missing."""
    usd = series.get(_day_str(ts))
    return usd if usd and usd > 0 else None


def _process_wallet_pnl(wallet_address: str, cmap: dict) -> dict:
    wallet_address = wallet_address.lower()
    # drop derived PnL and recompute from raw transfers
    clear_pnl_data(wallet_address)

    since_ts = int(time.time()) - TWELVE_MONTHS_SECONDS
    transfers = get_token_transfers_for_pnl(wallet_address, since_ts=since_ts)

    contracts = list({t["contract"] for t in transfers})
    stats = {"lots_created": 0, "trades_closed": 0, "total_pnl": Decimal("0"),
             "unmatched": 0, "wash_skipped": 0, "truncated": False}

    deadline = time.time() + PER_WALLET_BUDGET

    for contract in contracts:
        coin_id = cmap.get(contract)
        if not coin_id:
            continue  # not in CoinGecko, likely spam — skip

        contract_txns = [t for t in transfers if t["contract"] == contract]

        # Fetch this coin's whole daily price series in one call (cached across
        # wallets), then price every trade by lookup — no network inside the loop.
        # Past the per-wallet deadline, un-cached coins resolve to no_price so the
        # overall run can't be held hostage by one wallet with hundreds of tokens.
        tss = [t["block_ts"] for t in contract_txns]
        if time.time() > deadline:
            stats["truncated"] = True
        series = _get_price_series(coin_id, min(tss), max(tss), deadline=deadline)

        # FIFO matching is delegated to the pure, unit-tested core (src/fifo.py).
        # This module only handles I/O: pricing (CoinGecko) and persistence (SQLite).
        # Prices are floats; convert to Decimal at this boundary via str() so the
        # accounting core stays exact. A None price (missing day) is passed through
        # unchanged so the core records the trade as no_price, never a $0 fill.
        def _price(ts, _s=series):
            p = _price_on_day(_s, ts)
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
    processed = 0
    truncated = 0
    for w in wallets:
        log.info(f"PnL: {w['label']}")
        stats = _process_wallet_pnl(w["address"], cmap)
        cov = get_pnl_coverage(w["address"])
        cov_str = f"{cov['coverage']*100:.0f}%" if cov["coverage"] is not None else "—"
        budget_note = "  ⚠ price budget hit — partial coverage" if stats["truncated"] else ""
        log.info(
            f"  {stats['lots_created']} lots, "
            f"{stats['trades_closed']} closed trades, "
            f"PnL: ${stats['total_pnl']:.2f} | "
            f"coverage {cov_str} "
            f"({stats['unmatched']} unmatched, {stats['wash_skipped']} wash-skipped)"
            f"{budget_note}"
        )
        processed += 1
        truncated += 1 if stats["truncated"] else 0
    log.info(
        f"PnL run complete: {processed} wallets processed"
        + (f", {truncated} hit the per-wallet price budget" if truncated else "")
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [pnl] %(message)s")
    run_pnl()
