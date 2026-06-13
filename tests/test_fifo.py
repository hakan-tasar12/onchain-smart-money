"""Hand-computed tests for the FIFO PnL core (src/fifo.py).

Each test states the arithmetic in the docstring so the expected numbers can be
checked by eye — the point is to prove the accounting, not just exercise code.
Because the core computes in Decimal, the expected values are exact: assertions
use ``== Decimal(...)``, not floating-point approximation.
"""
from decimal import Decimal

from src.fifo import match_fifo, Unmatched


def const_price(p):
    """A price function that returns the same price regardless of timestamp."""
    return lambda ts: Decimal(str(p))


def price_at(mapping, default=0):
    """Price function backed by a {timestamp: price} table."""
    return lambda ts: Decimal(str(mapping.get(ts, default)))


def buy(ts, amount, _id=0):
    return {"block_ts": ts, "amount": amount, "id": _id, "symbol": "TKN"}


def sell(ts, amount, _id=0):
    return {"block_ts": ts, "amount": -amount, "id": _id, "symbol": "TKN"}


def test_single_round_trip_profit():
    """Buy 10 @ $1, sell 10 @ $2 -> proceeds 20, cost 10, PnL +10."""
    txns = [buy(0, 10), sell(100, 10)]
    r = match_fifo(txns, price_at({0: 1, 100: 2}))
    assert r.trades_closed == 1
    f = r.fills[0]
    assert f.cost_basis == Decimal("10")
    assert f.proceeds == Decimal("20")
    assert f.realized_pnl == Decimal("10")
    assert r.realized_pnl == Decimal("10")
    assert r.unmatched == []


def test_round_trip_loss():
    """Buy 10 @ $3, sell 10 @ $1 -> PnL -20."""
    r = match_fifo([buy(0, 10), sell(100, 10)], price_at({0: 3, 100: 1}))
    assert r.realized_pnl == Decimal("-20")


def test_fifo_consumes_oldest_first_across_lots():
    """Buy 10 @ $1 (t0), buy 10 @ $2 (t100), sell 15 @ $3 (t200).

    FIFO: 10 from lot1 (cost 10) + 5 from lot2 (cost 10) = cost 20.
    Proceeds 15*3 = 45 -> PnL 25. Lot2 keeps 5 units open.
    """
    txns = [buy(0, 10, 1), buy(100, 10, 2), sell(200, 15, 3)]
    r = match_fifo(txns, price_at({0: 1, 100: 2, 200: 3}))
    f = r.fills[0]
    assert f.cost_basis == Decimal("20")
    assert f.proceeds == Decimal("45")
    assert f.realized_pnl == Decimal("25")
    # lot1 fully consumed, lot2 has 5 remaining
    remaining = sorted(lot.remaining for lot in r.lots)
    assert remaining == [Decimal("0"), Decimal("5")]


def test_oversell_records_partial_no_lot():
    """Buy 5 @ $1, sell 8 @ $2. Match 5 (PnL +5); surplus 3 -> partial_no_lot."""
    r = match_fifo([buy(0, 5), sell(100, 8)], price_at({0: 1, 100: 2}))
    assert r.realized_pnl == Decimal("5")
    assert len(r.unmatched) == 1
    u = r.unmatched[0]
    assert u.reason == "partial_no_lot"
    assert u.amount == Decimal("3")


def test_sell_with_no_lot_is_unmatched_not_free_profit():
    """Sell 10 with no prior buy -> unmatched 'no_lot', never zero-cost profit."""
    r = match_fifo([sell(0, 10)], const_price(2))
    assert r.fills == []
    assert r.realized_pnl == Decimal("0")
    assert r.unmatched == [Unmatched(0, "TKN", Decimal("10"), "no_lot")]


def test_wash_trade_is_skipped_and_lot_untouched():
    """Sell 30s after buy (< 60s) -> wash trade: skipped, lot stays full."""
    r = match_fifo([buy(0, 10), sell(30, 10)], price_at({0: 1, 30: 2}))
    assert r.wash_skipped == 1
    assert r.fills == []
    assert r.lots[0].remaining == Decimal("10")  # not consumed


def test_wash_guard_boundary_at_60s_is_not_a_wash():
    """Exactly 60s is NOT < 60 -> the sell matches normally."""
    r = match_fifo([buy(0, 10), sell(60, 10)], price_at({0: 1, 60: 2}))
    assert r.wash_skipped == 0
    assert r.trades_closed == 1
    assert r.realized_pnl == Decimal("10")


def test_dust_amounts_are_ignored():
    """Sub-1e-9 transfers are noise and must not create lots or fills."""
    r = match_fifo([buy(0, 1e-12), sell(100, 1e-12)], const_price(1))
    assert r.lots_created == 0
    assert r.fills == []
    assert r.unmatched == []


def test_unsorted_input_is_processed_in_time_order():
    """Transfers given out of order are sorted by (block_ts, id) before matching."""
    # Same as the FIFO test but shuffled.
    txns = [sell(200, 15, 3), buy(100, 10, 2), buy(0, 10, 1)]
    r = match_fifo(txns, price_at({0: 1, 100: 2, 200: 3}))
    assert r.realized_pnl == Decimal("25")


def test_price_function_called_per_event():
    """price_fn is called once per buy and once per matched sell (not per lot)."""
    calls = []

    def tracking_price(ts):
        calls.append(ts)
        return Decimal("1")

    match_fifo([buy(0, 10, 1), buy(100, 10, 2), sell(200, 15, 3)], tracking_price)
    # two buys priced + one sell priced = 3 calls
    assert calls == [0, 100, 200]


def test_decimal_avoids_float_accumulation_error():
    """0.1 * 3 in float is 0.30000000000000004; the Decimal core must be exact.

    Buy 0.3 @ $1, then sell it in three 0.1 slices priced at $1 — cost basis and
    proceeds must land on exactly 0.1 each and 0.3 total, with zero residue.
    """
    txns = [buy(0, Decimal("0.3"), 1),
            sell(100, Decimal("0.1"), 2),
            sell(200, Decimal("0.1"), 3),
            sell(300, Decimal("0.1"), 4)]
    r = match_fifo(txns, const_price(1))
    assert r.realized_pnl == Decimal("0")           # bought and sold at $1
    assert sum((f.proceeds for f in r.fills), Decimal("0")) == Decimal("0.3")
    # the lot is fully consumed — no floating-point dust left behind
    assert all(lot.remaining == Decimal("0") for lot in r.lots)


# ── Missing prices (no_price) — a missing price must never invent PnL ───────────

def price_or_none(mapping):
    """Price function that returns None for any timestamp absent from the table."""
    return lambda ts: (None if mapping.get(ts) is None else Decimal(str(mapping[ts])))


def test_sell_with_unknown_price_is_no_price_not_zero_proceeds():
    """Buy @ $1, sell when the sell price is unknown.

    The old engine priced the sell at $0 → a phantom $10 loss. The fix records the
    matched amount as no_price (unrealized), produces no fill, and still consumes
    the lot's inventory.
    """
    r = match_fifo([buy(0, 10), sell(100, 10)], price_or_none({0: 1, 100: None}))
    assert r.fills == []                              # nothing realized
    assert r.realized_pnl == Decimal("0")
    assert [u.reason for u in r.unmatched] == ["no_price"]
    assert r.unmatched[0].amount == Decimal("10")
    assert all(lot.remaining == Decimal("0") for lot in r.lots)  # inventory consumed


def test_unpriced_buy_then_priced_sell_is_no_price():
    """Buy at an unknown price, later sell at a known price.

    Cost basis is unknown, so the round trip cannot be realized — it is no_price,
    not a full-proceeds phantom profit.
    """
    r = match_fifo([buy(0, 10), sell(100, 10)], price_or_none({0: None, 100: 5}))
    assert r.fills == []
    assert [u.reason for u in r.unmatched] == ["no_price"]
    assert r.unmatched[0].amount == Decimal("10")


def test_mixed_priced_and_unpriced_lots_split_fill_and_no_price():
    """Two 10-unit lots — first unpriced, second @ $1 — sold together @ $2.

    Only the priced lot's 10 units realize ($20 proceeds - $10 cost = $10). The
    unpriced lot's 10 units become no_price. One fill + one unmatched.
    """
    txns = [buy(0, 10, 1), buy(100, 10, 2), sell(200, 20, 3)]
    r = match_fifo(txns, price_or_none({0: None, 100: 1, 200: 2}))
    assert len(r.fills) == 1
    assert r.fills[0].matched_amount == Decimal("10")
    assert r.fills[0].realized_pnl == Decimal("10")
    assert [u.reason for u in r.unmatched] == ["no_price"]
    assert r.unmatched[0].amount == Decimal("10")
