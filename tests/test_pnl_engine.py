"""Integration tests for the wired PnL engine (src/pnl.py) against a real SQLite DB.

These complement the pure unit tests in test_fifo.py: they prove the engine
correctly *persists* what the FIFO core computes — realized fills into
pnl_history, leftovers into unmatched_sells, and open lots into pnl_lots — and
that money values round-trip through the TEXT columns as exact Decimals.

Skipped automatically if the project's runtime deps (e.g. requests, pulled in by
src.pnl) are not installed, so `pytest` stays green on a minimal install.
"""
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("requests")  # src.pnl imports the runtime stack

import src.db as db  # noqa: E402
import src.pnl as pnl  # noqa: E402

WALLET = "0xwallet"
CONTRACT = "0xtoken"
CMAP = {CONTRACT: "tokencoin"}
PRICES = {0: 1.0, 30: 2.0, 60: 2.0, 100: 2.0, 200: 3.0}


def _tx(ts, amount, _id=0, sym="TKN"):
    return {"contract": CONTRACT, "block_ts": ts, "amount": amount, "id": _id, "symbol": sym}


@pytest.fixture
def engine(monkeypatch):
    """Fresh temp DB + deterministic prices. Returns a runner taking transfers."""
    monkeypatch.setattr(db, "DB_PATH", Path(tempfile.mktemp(suffix=".db")))
    db.init_db()
    # The engine prices via _get_price_series (one fetch per coin) -> _price_on_day
    # (lookup per trade). These tiny timestamps would collide on a single calendar
    # day, so we stub the lookup to resolve by exact timestamp instead — the point
    # of these tests is persistence wiring, not the day-bucketing (see
    # test_pnl_prices.py for that). The series just needs to be truthy.
    monkeypatch.setattr(pnl, "_get_price_series",
                        lambda cid, frm, to, deadline=None: {"_": 1.0})
    monkeypatch.setattr(pnl, "_price_on_day", lambda series, ts: PRICES[ts])

    def run(transfers):
        monkeypatch.setattr(pnl, "get_token_transfers_for_pnl",
                            lambda w, since_ts=0: transfers)
        return pnl._process_wallet_pnl(WALLET, CMAP)

    return run


def test_multi_lot_fifo_persists_one_fill_and_open_lot(engine):
    """buy 10@1, buy 10@2, sell 15@3 -> PnL +25 recorded; 5 units left open."""
    stats = engine([_tx(0, 10, 1), _tx(100, 10, 2), _tx(200, -15, 3)])
    assert stats["total_pnl"] == Decimal("25")

    hist = db.get_pnl_history(WALLET)
    assert len(hist) == 1
    assert hist[0]["realized_pnl"] == Decimal("25")
    assert hist[0]["cost_basis"] == Decimal("20")
    assert hist[0]["proceeds"] == Decimal("45")

    lots = db.get_pnl_lots(WALLET, CONTRACT)
    assert len(lots) == 1
    assert lots[0]["amount_remaining"] == Decimal("5")
    assert db.get_pnl_coverage(WALLET)["coverage"] == 1.0  # no unmatched


def test_oversell_persists_unmatched_remainder(engine):
    """buy 5@1, sell 8@2 -> PnL +5; surplus 3 recorded as partial_no_lot."""
    engine([_tx(0, 5, 1), _tx(100, -8, 2)])
    assert db.get_pnl_history(WALLET)[0]["realized_pnl"] == Decimal("5")
    cov = db.get_pnl_coverage(WALLET)
    assert cov["matched"] == 1 and cov["unmatched"] == 1


def test_wash_trade_records_nothing(engine):
    """Sell 30s after buy -> wash trade: no fill, no unmatched, lot untouched."""
    stats = engine([_tx(0, 10, 1), _tx(30, -10, 2)])
    assert stats["wash_skipped"] == 1
    assert db.get_pnl_history(WALLET) == []
    lots = db.get_pnl_lots(WALLET, CONTRACT)
    assert lots[0]["amount_remaining"] == Decimal("10")


def test_recompute_is_idempotent(engine):
    """Running twice (clear_pnl_data resets) yields the same single fill, not duplicates."""
    transfers = [_tx(0, 10, 1), _tx(200, -10, 2)]
    engine(transfers)
    engine(transfers)
    assert len(db.get_pnl_history(WALLET)) == 1
