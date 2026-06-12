"""SQLite initialisation and CRUD helpers."""
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

DB_PATH = Path("data/smart_money.db")

# Money/amount columns are stored as TEXT (the Decimal's string form), not REAL,
# so values round-trip exactly: float storage would re-introduce the rounding
# error the Decimal accounting in src/fifo.py exists to avoid. These helpers
# serialise on the way in and rehydrate on the way out.


def _money_str(value) -> str:
    """Serialise a Decimal/number to its canonical string for TEXT storage."""
    return str(value if isinstance(value, Decimal) else Decimal(str(value)))


def _money_keys(row: dict, *keys: str) -> dict:
    """Rehydrate the given TEXT columns of a row dict back into Decimals."""
    for k in keys:
        if row.get(k) is not None:
            row[k] = Decimal(row[k])
    return row


@contextmanager
def get_conn():
    # sqlite3's context manager commits but doesn't close the file descriptor.
    # Explicit close() here prevents "Too many open files" on long runs.
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS wallets (
                address     TEXT PRIMARY KEY,
                label       TEXT NOT NULL,
                added_at    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS token_transfers (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet_address  TEXT NOT NULL,
                tx_hash         TEXT NOT NULL,
                block_number    INTEGER NOT NULL,
                block_ts        INTEGER NOT NULL,
                contract        TEXT NOT NULL,
                symbol          TEXT,
                token_name      TEXT,
                decimals        INTEGER DEFAULT 18,
                amount          REAL NOT NULL,
                UNIQUE(wallet_address, tx_hash, contract)
            );

            CREATE TABLE IF NOT EXISTS eth_transfers (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet_address  TEXT NOT NULL,
                tx_hash         TEXT NOT NULL,
                block_number    INTEGER NOT NULL,
                block_ts        INTEGER NOT NULL,
                value_eth       REAL NOT NULL,
                UNIQUE(wallet_address, tx_hash)
            );

            CREATE TABLE IF NOT EXISTS pnl_lots (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet_address      TEXT NOT NULL,
                contract            TEXT NOT NULL,
                acquired_ts         INTEGER NOT NULL,
                amount_remaining    TEXT NOT NULL,   -- Decimal string (exact)
                cost_usd_per_unit   TEXT NOT NULL    -- Decimal string (exact)
            );

            CREATE TABLE IF NOT EXISTS pnl_history (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet_address  TEXT NOT NULL,
                contract        TEXT NOT NULL,
                symbol          TEXT,
                close_ts        INTEGER NOT NULL,
                realized_pnl    TEXT NOT NULL,       -- Decimal string (exact)
                cost_basis      TEXT NOT NULL,       -- Decimal string (exact)
                proceeds        TEXT NOT NULL,       -- Decimal string (exact)
                computed_at     TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS unmatched_sells (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet_address  TEXT NOT NULL,
                contract        TEXT NOT NULL,
                symbol          TEXT,
                close_ts        INTEGER NOT NULL,
                amount          TEXT NOT NULL,       -- Decimal string (exact)
                reason          TEXT NOT NULL,
                computed_at     TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scores (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet_address  TEXT NOT NULL,
                win_rate        REAL,
                realized_pnl    REAL,
                early_entry     REAL,
                diversity       REAL,
                composite       REAL,
                trade_count     INTEGER,
                computed_at     TEXT NOT NULL,
                UNIQUE(wallet_address, computed_at)
            );

            CREATE TABLE IF NOT EXISTS alerts_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                contract        TEXT NOT NULL,
                symbol          TEXT,
                wallets_json    TEXT NOT NULL,
                alert_type      TEXT NOT NULL,
                sent_at         TEXT NOT NULL,
                telegram_ok     INTEGER DEFAULT 0
            );
        """)


# ── Wallet ────────────────────────────────────────────────────────────────────

def upsert_wallet(address: str, label: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO wallets (address, label, added_at) VALUES (?, ?, ?)",
            (address.lower(), label, datetime.now(timezone.utc).isoformat()),
        )


def get_all_wallets() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM wallets").fetchall()
    return [dict(r) for r in rows]


# ── Token transfers ───────────────────────────────────────────────────────────

def insert_token_transfer(
    wallet_address: str, tx_hash: str, block_number: int, block_ts: int,
    contract: str, symbol: str, token_name: str, decimals: int, amount: float,
) -> bool:
    try:
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO token_transfers
                   (wallet_address, tx_hash, block_number, block_ts, contract,
                    symbol, token_name, decimals, amount)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (wallet_address.lower(), tx_hash, block_number, block_ts,
                 contract.lower(), symbol, token_name, decimals, amount),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def insert_eth_transfer(
    wallet_address: str, tx_hash: str, block_number: int, block_ts: int, value_eth: float,
) -> bool:
    try:
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO eth_transfers
                   (wallet_address, tx_hash, block_number, block_ts, value_eth)
                   VALUES (?, ?, ?, ?, ?)""",
                (wallet_address.lower(), tx_hash, block_number, block_ts, value_eth),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def get_token_transfers_for_pnl(wallet_address: str, since_ts: int = 0) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM token_transfers
               WHERE wallet_address = ? AND block_ts >= ?
               ORDER BY block_ts ASC, id ASC""",
            (wallet_address.lower(), since_ts),
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_token_accumulations(hours: int = 1) -> list[dict]:
    """Tokens with IN transfers from 2 or more distinct wallets in the last N hours."""
    since_ts = int(time.time()) - hours * 3600
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT contract, symbol,
                   COUNT(DISTINCT wallet_address) AS wallet_count,
                   GROUP_CONCAT(DISTINCT wallet_address) AS wallets
            FROM token_transfers
            WHERE block_ts >= ? AND amount > 0
            GROUP BY contract
            HAVING wallet_count >= 2
            ORDER BY wallet_count DESC
            """,
            (since_ts,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── PnL lots ─────────────────────────────────────────────────────────────────

def get_pnl_lots(wallet_address: str, contract: str) -> list[dict]:
    # amount_remaining is TEXT (a Decimal string), so the "still open" filter is
    # applied in Python after rehydration — a numeric SQL comparison on a TEXT
    # column would compare lexically, not by value.
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM pnl_lots
               WHERE wallet_address = ? AND contract = ?
               ORDER BY acquired_ts ASC, id ASC""",
            (wallet_address.lower(), contract.lower()),
        ).fetchall()
    lots = [_money_keys(dict(r), "amount_remaining", "cost_usd_per_unit") for r in rows]
    return [lot for lot in lots if lot["amount_remaining"] > Decimal("1e-9")]


def insert_pnl_lot(
    wallet_address: str, contract: str, acquired_ts: int,
    amount, cost_usd_per_unit,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO pnl_lots
               (wallet_address, contract, acquired_ts, amount_remaining, cost_usd_per_unit)
               VALUES (?, ?, ?, ?, ?)""",
            (wallet_address.lower(), contract.lower(), acquired_ts,
             _money_str(amount), _money_str(cost_usd_per_unit)),
        )


def update_lot_remaining(lot_id: int, new_remaining) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE pnl_lots SET amount_remaining = ? WHERE id = ?",
            (_money_str(new_remaining), lot_id),
        )


# ── PnL history ───────────────────────────────────────────────────────────────

def insert_pnl_record(
    wallet_address: str, contract: str, symbol: str,
    close_ts: int, realized_pnl, cost_basis, proceeds,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO pnl_history
               (wallet_address, contract, symbol, close_ts, realized_pnl,
                cost_basis, proceeds, computed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (wallet_address.lower(), contract.lower(), symbol, close_ts,
             _money_str(realized_pnl), _money_str(cost_basis), _money_str(proceeds),
             datetime.now(timezone.utc).isoformat()),
        )


def get_pnl_history(wallet_address: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM pnl_history WHERE wallet_address = ? ORDER BY close_ts DESC",
            (wallet_address.lower(),),
        ).fetchall()
    return [_money_keys(dict(r), "realized_pnl", "cost_basis", "proceeds") for r in rows]


# ── Unmatched sells ───────────────────────────────────────────────────────────

def insert_unmatched_sell(
    wallet_address: str, contract: str, symbol: str,
    close_ts: int, amount, reason: str,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO unmatched_sells
               (wallet_address, contract, symbol, close_ts, amount, reason, computed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (wallet_address.lower(), contract.lower(), symbol, close_ts,
             _money_str(amount), reason, datetime.now(timezone.utc).isoformat()),
        )


def clear_pnl_data(wallet_address: str) -> None:
    addr = wallet_address.lower()
    with get_conn() as conn:
        conn.execute("DELETE FROM pnl_lots WHERE wallet_address = ?", (addr,))
        conn.execute("DELETE FROM pnl_history WHERE wallet_address = ?", (addr,))
        conn.execute("DELETE FROM unmatched_sells WHERE wallet_address = ?", (addr,))


def get_pnl_coverage(wallet_address: str) -> dict:
    # coverage = matched / (matched + unmatched)
    # low coverage = wallet had open positions before the 12-month window, so PnL is understated
    addr = wallet_address.lower()
    with get_conn() as conn:
        matched = conn.execute(
            "SELECT COUNT(*) FROM pnl_history WHERE wallet_address = ?", (addr,)
        ).fetchone()[0]
        unmatched = conn.execute(
            "SELECT COUNT(*) FROM unmatched_sells WHERE wallet_address = ?", (addr,)
        ).fetchone()[0]
    total = matched + unmatched
    coverage = matched / total if total else None
    return {"matched": matched, "unmatched": unmatched, "coverage": coverage}


# ── Scores ────────────────────────────────────────────────────────────────────

def upsert_score(
    wallet_address: str, win_rate: float | None, realized_pnl: float | None,
    early_entry: float | None, diversity: float | None,
    composite: float | None, trade_count: int,
) -> None:
    computed_at = datetime.now(timezone.utc).date().isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO scores
               (wallet_address, win_rate, realized_pnl, early_entry, diversity,
                composite, trade_count, computed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (wallet_address.lower(), win_rate, realized_pnl, early_entry,
             diversity, composite, trade_count, computed_at),
        )


def get_latest_scores() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT s.* FROM scores s
               INNER JOIN (
                   SELECT wallet_address, MAX(computed_at) AS max_at
                   FROM scores GROUP BY wallet_address
               ) latest ON s.wallet_address = latest.wallet_address
                       AND s.computed_at = latest.max_at
               ORDER BY composite DESC""",
        ).fetchall()
    return [dict(r) for r in rows]


# ── Alerts ────────────────────────────────────────────────────────────────────

def get_last_alert_ts(contract: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT MAX(sent_at) FROM alerts_log
               WHERE contract = ? AND alert_type = 'consensus_accumulation'""",
            (contract,),
        ).fetchone()
    val = row[0] if row and row[0] else None
    if not val:
        return 0
    try:
        dt = datetime.fromisoformat(val)
        return int(dt.timestamp())
    except Exception:
        return 0


def insert_alert(contract: str, symbol: str, wallets: list[str], telegram_ok: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO alerts_log
               (contract, symbol, wallets_json, alert_type, sent_at, telegram_ok)
               VALUES (?, ?, ?, 'consensus_accumulation', ?, ?)""",
            (contract, symbol, json.dumps(wallets),
             datetime.now(timezone.utc).isoformat(), int(telegram_ok)),
        )


def get_recent_alerts(limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM alerts_log ORDER BY sent_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
