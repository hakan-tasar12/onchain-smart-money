"""Pure FIFO lot-matching — the accounting core of the PnL engine.

Side-effect free: no database, no network. This is the single source of truth
for *how* realized PnL is computed, so it can be verified against hand-computed
examples (see tests/test_fifo.py). ``pnl.py`` wires this to CoinGecko prices and
SQLite persistence.

All money and amount arithmetic uses ``decimal.Decimal``, not ``float``: FIFO
sums a fill's cost basis across many lots, and binary floating point accumulates
rounding error over those additions. Decimal keeps the accounting exact. Inputs
(amounts, prices) are coerced to Decimal at the boundary via ``str()`` so a stray
float never silently re-introduces that error.

Conventions (must match the engine and the README's Methodology section):
- Buys open lots; sells consume the oldest open lots first (FIFO).
- Realized PnL of a fill = proceeds - cost basis of the consumed lots.
- A sell against an oldest open lot younger than ``wash_seconds`` is treated as a
  wash trade (intraday round-trip / arbitrage) and skipped entirely.
- A sell with no open lot has unknown cost basis: it is recorded as *unmatched*
  (``no_lot``), never counted as zero-cost profit. Selling more than the open
  lots hold records the surplus as ``partial_no_lot``.
- A buy or sell whose USD price is unavailable (``price_fn`` returns ``None``)
  cannot be valued. Inventory is still tracked (the tokens really moved), but any
  matched amount that touches an unpriced lot or an unpriced sell is recorded as
  *unmatched* (``no_price``) rather than realized at a fabricated $0. This keeps a
  missing price from silently inventing profit or loss — it lowers coverage instead.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Iterable

WASH_TRADE_SECONDS = 60
EPS = Decimal("1e-9")  # dust threshold: amounts below this are ignored


def _dec(value) -> Decimal:
    """Coerce a number to Decimal without inheriting float representation error.

    ``Decimal(0.1)`` is 0.1000000000000000055…; ``Decimal(str(0.1))`` is exactly
    0.1. Routing every external float through ``str()`` keeps the accounting clean.
    """
    return value if isinstance(value, Decimal) else Decimal(str(value))


@dataclass
class Lot:
    acquired_ts: int
    cost_per_unit: Decimal | None  # None = bought at an unknown price (unpriced lot)
    amount: Decimal
    remaining: Decimal = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        # None is preserved: an unpriced lot still holds inventory but has no cost basis.
        self.cost_per_unit = None if self.cost_per_unit is None else _dec(self.cost_per_unit)
        self.amount = _dec(self.amount)
        self.remaining = self.amount if self.remaining is None else _dec(self.remaining)


@dataclass
class Fill:
    """A realized (closed) portion of a sell, matched against open lots."""
    close_ts: int
    symbol: str
    matched_amount: Decimal
    cost_basis: Decimal
    proceeds: Decimal

    @property
    def realized_pnl(self) -> Decimal:
        return self.proceeds - self.cost_basis


@dataclass
class Unmatched:
    """A sell (or part of one) that cannot be realized into PnL."""
    ts: int
    symbol: str
    amount: Decimal
    reason: str  # "no_lot" | "partial_no_lot" | "no_price"


@dataclass
class MatchResult:
    lots: list[Lot]
    fills: list[Fill]
    unmatched: list[Unmatched]
    wash_skipped: int = 0

    @property
    def realized_pnl(self) -> Decimal:
        return sum((f.realized_pnl for f in self.fills), Decimal("0"))

    @property
    def lots_created(self) -> int:
        return len(self.lots)

    @property
    def trades_closed(self) -> int:
        return len(self.fills)


def match_fifo(
    transfers: Iterable[dict],
    price_fn: Callable[[int], Decimal],
    wash_seconds: int = WASH_TRADE_SECONDS,
) -> MatchResult:
    """Match a single token's transfers FIFO and return the realized result.

    ``transfers``: dicts with keys ``block_ts`` (int), ``amount`` (signed: >0 buy,
    <0 sell), optional ``id`` (tie-breaker), optional ``symbol``. Processed in
    (block_ts, id) order.
    ``price_fn``: maps a timestamp to a USD unit price (called once per buy and
    once per matched sell). A returned number is coerced to Decimal; ``None`` means
    the price is unknown and the affected amount is recorded as ``no_price``.
    """
    txns = sorted(transfers, key=lambda t: (t["block_ts"], t.get("id", 0)))

    lots: list[Lot] = []
    fills: list[Fill] = []
    unmatched: list[Unmatched] = []
    wash_skipped = 0

    for tx in txns:
        ts = tx["block_ts"]
        signed = _dec(tx["amount"])
        symbol = tx.get("symbol") or "?"
        amount = abs(signed)
        if amount < EPS:
            continue

        if signed > 0:  # buy — open a lot (unpriced if price_fn returns None)
            buy_price = price_fn(ts)
            cost = None if buy_price is None else _dec(buy_price)
            lots.append(Lot(acquired_ts=ts, cost_per_unit=cost, amount=amount))
            continue

        # sell — consume open lots FIFO
        open_lots = [lot for lot in lots if lot.remaining > EPS]
        if not open_lots:
            unmatched.append(Unmatched(ts, symbol, amount, "no_lot"))
            continue

        # wash-trade guard: oldest open lot too fresh -> skip the whole sell
        if ts - open_lots[0].acquired_ts < wash_seconds:
            wash_skipped += 1
            continue

        raw_sell_price = price_fn(ts)
        sell_price = None if raw_sell_price is None else _dec(raw_sell_price)
        remaining_to_sell = amount
        cost_basis = Decimal("0")
        proceeds = Decimal("0")
        priced_amount = Decimal("0")    # consumed against priced lots with a known sell price
        unpriced_amount = Decimal("0")  # consumed, but sell or lot price unknown -> not realizable
        for lot in open_lots:
            if remaining_to_sell <= EPS:
                break
            used = min(lot.remaining, remaining_to_sell)
            # A round trip is realizable only if BOTH ends are priced; otherwise the
            # tokens still leave inventory but the PnL is recorded as no_price.
            if sell_price is None or lot.cost_per_unit is None:
                unpriced_amount += used
            else:
                cost_basis += used * lot.cost_per_unit
                proceeds += used * sell_price
                priced_amount += used
            lot.remaining -= used
            remaining_to_sell -= used

        if priced_amount > EPS:
            fills.append(Fill(ts, symbol, priced_amount, cost_basis, proceeds))
        if unpriced_amount > EPS:
            unmatched.append(Unmatched(ts, symbol, unpriced_amount, "no_price"))
        if remaining_to_sell > EPS:
            unmatched.append(Unmatched(ts, symbol, remaining_to_sell, "partial_no_lot"))

    return MatchResult(lots=lots, fills=fills, unmatched=unmatched, wash_skipped=wash_skipped)
