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
    cost_per_unit: Decimal
    amount: Decimal
    remaining: Decimal = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.cost_per_unit = _dec(self.cost_per_unit)
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
    """A sell (or part of one) with no known cost basis."""
    ts: int
    symbol: str
    amount: Decimal
    reason: str  # "no_lot" | "partial_no_lot"


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
    once per matched sell). Its return value is coerced to Decimal.
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

        if signed > 0:  # buy — open a lot
            lots.append(Lot(acquired_ts=ts, cost_per_unit=_dec(price_fn(ts)), amount=amount))
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

        sell_price = _dec(price_fn(ts))
        remaining_to_sell = amount
        cost_basis = Decimal("0")
        proceeds = Decimal("0")
        for lot in open_lots:
            if remaining_to_sell <= EPS:
                break
            used = min(lot.remaining, remaining_to_sell)
            cost_basis += used * lot.cost_per_unit
            proceeds += used * sell_price
            lot.remaining -= used
            remaining_to_sell -= used

        matched = amount - remaining_to_sell
        if matched > EPS:
            fills.append(Fill(ts, symbol, matched, cost_basis, proceeds))
        if remaining_to_sell > EPS:
            unmatched.append(Unmatched(ts, symbol, remaining_to_sell, "partial_no_lot"))

    return MatchResult(lots=lots, fills=fills, unmatched=unmatched, wash_skipped=wash_skipped)
