"""SQLite schema and connection helpers.

Run `python -m src.db init` to create / migrate the schema.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    wallet_address TEXT PRIMARY KEY,
    score REAL NOT NULL,
    cumulative_pnl_usd REAL,
    win_rate REAL,
    market_appearances INTEGER,
    cumulative_volume_usd REAL,
    enabled BOOLEAN DEFAULT TRUE,
    note TEXT,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS watchlist_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    built_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    wallet_count INTEGER,
    scanned_markets INTEGER,
    note TEXT
);

CREATE TABLE IF NOT EXISTS new_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT UNIQUE,
    wallet_address TEXT NOT NULL,
    market_id TEXT NOT NULL,
    market_question TEXT,
    side TEXT NOT NULL,
    amount_usd REAL,
    entry_price REAL,
    probability_at_trade REAL,
    traded_at TIMESTAMP,
    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notified_individually BOOLEAN DEFAULT FALSE,
    raw_payload TEXT,
    FOREIGN KEY (wallet_address) REFERENCES watchlist(wallet_address)
);

CREATE INDEX IF NOT EXISTS idx_new_trades_market ON new_trades(market_id, side);
CREATE INDEX IF NOT EXISTS idx_new_trades_detected ON new_trades(detected_at);
CREATE INDEX IF NOT EXISTS idx_new_trades_wallet ON new_trades(wallet_address);

CREATE TABLE IF NOT EXISTS poll_state (
    wallet_address TEXT PRIMARY KEY,
    last_polled_at TIMESTAMP,
    last_seen_trade_at TIMESTAMP,
    last_seen_trade_id TEXT,
    FOREIGN KEY (wallet_address) REFERENCES watchlist(wallet_address)
);

CREATE TABLE IF NOT EXISTS convergence_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    market_question TEXT,
    side TEXT NOT NULL,
    wallet_count INTEGER,
    total_amount_usd REAL,
    wallets TEXT,
    last_alerted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(market_id, side)
);

-- Daily snapshot: one row per (date, market) for audit + future trend analysis.
CREATE TABLE IF NOT EXISTS daily_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date TEXT NOT NULL,        -- YYYY-MM-DD in JST
    market_id TEXT NOT NULL,
    slug TEXT,
    question TEXT,
    category TEXT,                       -- bucket label (Crypto / Macro / Politics / NULL)
    yes_price REAL,
    one_day_change REAL,
    volume_24h_usd REAL,
    section TEXT,                        -- 'movers' | 'crypto' | 'macro' | 'politics'
    rank_in_section INTEGER,
    captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(snapshot_date, section, market_id)
);

CREATE INDEX IF NOT EXISTS idx_daily_snapshot_date ON daily_snapshot(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_daily_snapshot_market ON daily_snapshot(market_id, snapshot_date);

-- Japanese label cache for Polymarket markets. Keyed by event slug (stable
-- per market). ``source`` distinguishes 'llm' (auto-translated) from 'manual'
-- (operator-curated via settings.yaml display_aliases) so manual entries
-- always win the merge in the formatter.
CREATE TABLE IF NOT EXISTS market_jp_label (
    slug TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'llm',
    question TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_schema(db_path: Path | str) -> None:
    conn = connect(db_path)
    try:
        with transaction(conn):
            conn.executescript(SCHEMA)
        log.info("Initialized schema at %s", db_path)
    finally:
        conn.close()


def _cli() -> None:
    from .config import load_settings

    parser = argparse.ArgumentParser(prog="src.db")
    parser.add_argument("command", choices=["init"], help="schema command")
    args = parser.parse_args()

    settings = load_settings()
    logging.basicConfig(level=settings.log_level)

    if args.command == "init":
        init_schema(settings.db_path)
        print(f"Initialized: {settings.db_path}")


if __name__ == "__main__":
    _cli()
