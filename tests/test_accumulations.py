"""Consensus-accumulation query must exclude stablecoins.

A wallet receiving USDC is parking cash, not making a directional bet, so it must
never surface as a "consensus accumulation" in /movers or alerts. A real token
bought by the same wallets must still surface.
"""
import tempfile
import time
from pathlib import Path

import src.db as db

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"  # stablecoin
PEPE = "0x6982508145454ce325ddbe47a25d4ec3d2311933"  # real token (directional)


def _buy(addr, contract, sym, ts):
    db.insert_token_transfer(addr, f"{addr}-{contract}", 1, ts, contract, sym,
                             sym, 18, 1000.0)


def test_stablecoins_excluded_real_token_kept(monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", Path(tempfile.mktemp(suffix=".db")))
    db.init_db()
    now = int(time.time())
    # Two distinct wallets accumulate each token within the last hour.
    for w in ("0xaaa1", "0xbbb2"):
        _buy(w, USDC, "USDC", now - 60)
        _buy(w, PEPE, "PEPE", now - 60)

    accs = db.get_recent_token_accumulations(hours=1)
    contracts = {a["contract"] for a in accs}

    assert PEPE in contracts                    # directional token surfaces
    assert USDC not in contracts                # stablecoin filtered out


def test_stablecoin_only_run_yields_nothing(monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", Path(tempfile.mktemp(suffix=".db")))
    db.init_db()
    now = int(time.time())
    for w in ("0xaaa1", "0xbbb2"):
        _buy(w, USDC, "USDC", now - 60)
    assert db.get_recent_token_accumulations(hours=1) == []
