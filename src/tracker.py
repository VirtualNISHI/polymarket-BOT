"""New-trade detection (SPEC §2).

Polls each enabled watchlist wallet, diffs against ``poll_state``, and persists
new trades. Per-trade Discord notification follows §2.2 thresholds with a
1-hour consolidation window.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .config import Settings
from .db import connect, transaction
from .discord_client import DiscordClient
from .formatter import TradeNotification, WalletStats, format_trade_embed
from .polymarket_client import PolymarketClient

log = logging.getLogger(__name__)


def _trade_id(t: dict[str, Any]) -> str | None:
    for key in ("trade_id", "id", "tx_hash", "transaction_hash"):
        v = t.get(key)
        if v:
            return str(v)
    return None


def _trade_wallet(t: dict[str, Any], fallback: str) -> str:
    for key in ("wallet_address", "address", "wallet", "trader"):
        v = t.get(key)
        if v:
            return str(v).lower()
    return fallback.lower()


def _trade_market_id(t: dict[str, Any]) -> str | None:
    for key in ("market_id", "marketId", "market", "slug"):
        v = t.get(key)
        if v:
            return str(v)
    return None


def _trade_question(t: dict[str, Any]) -> str:
    for key in ("market_question", "question", "title", "market_name"):
        v = t.get(key)
        if v:
            return str(v)
    return ""


def _trade_side(t: dict[str, Any]) -> str:
    for key in ("side", "outcome", "direction"):
        v = t.get(key)
        if v:
            return str(v).upper()
    return "?"


def _trade_amount_usd(t: dict[str, Any]) -> float:
    for key in ("amount_usd", "size_usd", "usd_amount", "notional_usd"):
        v = t.get(key)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return 0.0


def _trade_price(t: dict[str, Any]) -> float | None:
    for key in ("price", "entry_price", "fill_price"):
        v = t.get(key)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _trade_probability(t: dict[str, Any]) -> float | None:
    for key in ("probability", "implied_probability", "prob"):
        v = t.get(key)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _trade_timestamp(t: dict[str, Any]) -> str | None:
    for key in ("traded_at", "timestamp", "time", "executed_at", "created_at"):
        v = t.get(key)
        if v:
            return str(v)
    return None


@dataclass
class WatchlistRow:
    wallet_address: str
    score: float
    cumulative_pnl_usd: float
    win_rate: float
    market_appearances: int
    rank: int


def load_enabled_watchlist(conn: sqlite3.Connection) -> list[WatchlistRow]:
    rows = conn.execute(
        """
        SELECT wallet_address, score, cumulative_pnl_usd, win_rate, market_appearances
        FROM watchlist
        WHERE enabled = TRUE
        ORDER BY score DESC
        """
    ).fetchall()
    return [
        WatchlistRow(
            wallet_address=r["wallet_address"],
            score=r["score"] or 0.0,
            cumulative_pnl_usd=r["cumulative_pnl_usd"] or 0.0,
            win_rate=r["win_rate"] or 0.0,
            market_appearances=r["market_appearances"] or 0,
            rank=i + 1,
        )
        for i, r in enumerate(rows)
    ]


def get_last_seen(conn: sqlite3.Connection, wallet: str) -> tuple[str | None, str | None]:
    row = conn.execute(
        "SELECT last_seen_trade_at, last_seen_trade_id FROM poll_state WHERE wallet_address = ?",
        (wallet,),
    ).fetchone()
    if not row:
        return None, None
    return row["last_seen_trade_at"], row["last_seen_trade_id"]


def update_poll_state(
    conn: sqlite3.Connection,
    wallet: str,
    last_trade_at: str | None,
    last_trade_id: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO poll_state (wallet_address, last_polled_at, last_seen_trade_at, last_seen_trade_id)
        VALUES (?, CURRENT_TIMESTAMP, ?, ?)
        ON CONFLICT(wallet_address) DO UPDATE SET
            last_polled_at = CURRENT_TIMESTAMP,
            last_seen_trade_at = COALESCE(excluded.last_seen_trade_at, poll_state.last_seen_trade_at),
            last_seen_trade_id = COALESCE(excluded.last_seen_trade_id, poll_state.last_seen_trade_id)
        """,
        (wallet, last_trade_at, last_trade_id),
    )


def filter_new_trades(
    trades: list[dict[str, Any]],
    last_seen_at: str | None,
    last_seen_id: str | None,
) -> list[dict[str, Any]]:
    """Return trades newer than the last-seen marker.

    ``trades`` may be in any order; we sort by timestamp ascending and slice.
    """
    def ts_key(t: dict[str, Any]) -> str:
        return _trade_timestamp(t) or ""

    sorted_trades = sorted(trades, key=ts_key)

    if last_seen_id:
        for i, t in enumerate(sorted_trades):
            if _trade_id(t) == last_seen_id:
                return sorted_trades[i + 1 :]

    if last_seen_at:
        return [t for t in sorted_trades if (_trade_timestamp(t) or "") > last_seen_at]

    return sorted_trades


def insert_new_trade(conn: sqlite3.Connection, wallet: str, t: dict[str, Any]) -> int | None:
    """INSERT OR IGNORE on trade_id. Returns row id if inserted, else None."""
    tid = _trade_id(t)
    market_id = _trade_market_id(t)
    if not tid or not market_id:
        return None
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO new_trades (
            trade_id, wallet_address, market_id, market_question, side,
            amount_usd, entry_price, probability_at_trade, traded_at, raw_payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tid,
            _trade_wallet(t, wallet),
            market_id,
            _trade_question(t),
            _trade_side(t),
            _trade_amount_usd(t),
            _trade_price(t),
            _trade_probability(t),
            _trade_timestamp(t),
            json.dumps(t, default=str),
        ),
    )
    return cursor.lastrowid if cursor.rowcount > 0 else None


def should_notify(
    trade: dict[str, Any], wl: WatchlistRow, settings: Settings
) -> bool:
    if _trade_amount_usd(trade) >= settings.tracker.min_notify_usd:
        return True
    if wl.rank <= settings.tracker.always_notify_top_rank:
        return True
    return False


def is_consolidated(
    conn: sqlite3.Connection,
    wallet: str,
    market_id: str,
    side: str,
    window_minutes: int,
) -> bool:
    """True if a notified trade for the same wallet/market/side exists within the window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).isoformat()
    row = conn.execute(
        """
        SELECT 1 FROM new_trades
        WHERE wallet_address = ? AND market_id = ? AND side = ?
          AND notified_individually = TRUE
          AND detected_at >= ?
        LIMIT 1
        """,
        (wallet, market_id, side, cutoff),
    ).fetchone()
    return row is not None


def mark_notified(conn: sqlite3.Connection, trade_id: str) -> None:
    conn.execute(
        "UPDATE new_trades SET notified_individually = TRUE WHERE trade_id = ?",
        (trade_id,),
    )


def _build_notification(
    trade: dict[str, Any], wl: WatchlistRow
) -> TradeNotification:
    return TradeNotification(
        market_id=_trade_market_id(trade) or "",
        market_question=_trade_question(trade),
        market_slug=trade.get("slug") or trade.get("market_slug"),
        side=_trade_side(trade),
        amount_usd=_trade_amount_usd(trade),
        entry_price=_trade_price(trade),
        wallet=WalletStats(
            address=wl.wallet_address,
            rank=wl.rank,
            cumulative_pnl_usd=wl.cumulative_pnl_usd,
            win_rate=wl.win_rate,
            market_appearances=wl.market_appearances,
        ),
    )


def poll_wallet(
    conn: sqlite3.Connection,
    nansen: PolymarketClient,
    discord: DiscordClient,
    wl: WatchlistRow,
    settings: Settings,
) -> int:
    """Poll one wallet. Returns the number of new trades detected."""
    last_at, last_id = get_last_seen(conn, wl.wallet_address)
    try:
        all_trades = nansen.prediction_market_address_trades(
            wallet_address=wl.wallet_address, since=last_at
        )
    except Exception as exc:
        log.warning("trades fetch failed for %s: %s", wl.wallet_address, exc)
        return 0

    new_trades = filter_new_trades(all_trades, last_at, last_id)
    if not new_trades:
        with transaction(conn):
            update_poll_state(conn, wl.wallet_address, last_at, last_id)
        return 0

    notified = 0
    inserted = 0
    with transaction(conn):
        for t in new_trades:
            if insert_new_trade(conn, wl.wallet_address, t) is None:
                continue
            inserted += 1

            if not should_notify(t, wl, settings):
                continue

            mid = _trade_market_id(t) or ""
            side = _trade_side(t)
            if is_consolidated(
                conn, wl.wallet_address, mid, side,
                settings.tracker.consolidation_window_minutes,
            ):
                continue

            try:
                discord.send(embeds=[format_trade_embed(_build_notification(t, wl))])
                tid = _trade_id(t)
                if tid:
                    mark_notified(conn, tid)
                notified += 1
            except Exception as exc:
                log.warning("discord send failed for trade %s: %s", _trade_id(t), exc)

        latest = new_trades[-1]
        update_poll_state(
            conn,
            wl.wallet_address,
            _trade_timestamp(latest) or last_at,
            _trade_id(latest) or last_id,
        )

    log.info(
        "wallet=%s rank=%d inserted=%d notified=%d",
        wl.wallet_address, wl.rank, inserted, notified,
    )
    return inserted


def run_tracker(settings: Settings) -> int:
    """Poll all enabled wallets. Returns total new trades inserted."""
    total = 0
    conn = connect(settings.db_path)
    try:
        watchlist = load_enabled_watchlist(conn)
        if not watchlist:
            log.warning("watchlist is empty — run scripts/build_watchlist.py first")
            return 0

        with PolymarketClient(user_agent=settings.polymarket_user_agent) as nansen, \
             DiscordClient(settings.discord_webhook_url, dry_run=settings.dry_run) as discord:
            for wl in watchlist:
                total += poll_wallet(conn, nansen, discord, wl, settings)
    finally:
        conn.close()
    log.info("tracker run: %d new trades across %d wallets", total, len(watchlist) if watchlist else 0)
    return total
