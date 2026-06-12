"""Migration test: a pre-Decimal database (REAL money columns) must self-heal.

This guards the subtle failure the Decimal refactor would otherwise have on an
existing deployment: ``CREATE TABLE IF NOT EXISTS`` can't change a column's type,
and a REAL-affinity column silently coerces an inserted Decimal *string* back to
float — so without migration the "stored exactly" guarantee would hold only on a
freshly created database. Needs only stdlib, so it always runs.
"""
import sqlite3
import tempfile
from decimal import Decimal
from pathlib import Path

import src.db as db

# A value binary float cannot represent exactly — the canonical proof of exactness.
HIGH_PRECISION = Decimal("0.10000000000000000001")


def _make_legacy_db(path: Path) -> None:
    """Create a pnl_history table on the OLD schema (REAL money columns)."""
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE pnl_history (
               id INTEGER PRIMARY KEY AUTOINCREMENT, wallet_address TEXT, contract TEXT,
               symbol TEXT, close_ts INTEGER, realized_pnl REAL, cost_basis REAL,
               proceeds REAL, computed_at TEXT)"""
    )
    conn.commit()
    conn.close()


def _column_type(path: Path, table: str, col: str) -> str:
    conn = sqlite3.connect(path)
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    conn.close()
    return {r[1]: (r[2] or "").upper() for r in info}[col]


def test_legacy_real_schema_is_migrated_and_stores_exactly(monkeypatch):
    path = Path(tempfile.mktemp(suffix=".db"))
    _make_legacy_db(path)
    assert _column_type(path, "pnl_history", "realized_pnl") == "REAL"  # precondition

    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()  # runs the migration, then recreates the table as TEXT

    assert _column_type(path, "pnl_history", "realized_pnl") == "TEXT"

    # The whole point: a value float can't hold now round-trips exactly.
    db.insert_pnl_record("0xw", "0xc", "TKN", 1,
                         realized_pnl=HIGH_PRECISION, cost_basis=Decimal("1"), proceeds=Decimal("2"))
    got = db.get_pnl_history("0xw")[0]["realized_pnl"]
    assert got == HIGH_PRECISION


def test_migration_is_idempotent_on_fresh_db(monkeypatch):
    """Running init_db twice on a current-schema DB must not drop/alter anything."""
    path = Path(tempfile.mktemp(suffix=".db"))
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    db.insert_pnl_record("0xw", "0xc", "TKN", 1, Decimal("5"), Decimal("1"), Decimal("6"))
    db.init_db()  # second call: migration sees TEXT, leaves the table (and its row) alone
    assert len(db.get_pnl_history("0xw")) == 1
