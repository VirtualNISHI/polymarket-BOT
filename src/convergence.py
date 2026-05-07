"""Convergence detection (SPEC §3).

Aggregates new_trades over the lookback window; alerts when ``min_wallet_count``
distinct wallets have hit the same market+side with combined volume above
``min_total_amount_usd``. Re-alerts only when wallet count grows.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .db import connect, transaction
from .discord_client import DiscordClient
from .formatter import (
    ConvergenceNotification,
    ConvergenceWalletEntry,
    format_convergence_embed,
)
from .polymarket_client import PolymarketClient

log = logging.getLogger(__name__)


@dataclass
class ConvergenceCandidate:
    market_id: str
    market_question: str
    side: str
    wallet_count: int
    total_amount_usd: float
    wallet_amounts: list[tuple[str, float]]


def detect(conn: sqlite3.Connection, settings: Settings) -> list[ConvergenceCandidate]:
    cv = settings.convergence
    cutoff = f"-{cv.lookback_hours} hours"

    rows = conn.execute(
        """
        SELECT
            market_id,
            COALESCE(MAX(market_question), '') AS market_question,
            side,
            COUNT(DISTINCT wallet_address) AS wallet_count,
            SUM(amount_usd) AS total_amount
        FROM new_trades
        WHERE detected_at >= datetime('now', ?)
        GROUP BY market_id, side
        HAVING wallet_count >= ? AND total_amount >= ?
        """,
        (cutoff, cv.min_wallet_count, cv.min_total_amount_usd),
    ).fetchall()

    candidates: list[ConvergenceCandidate] = []
    for r in rows:
        wallet_rows = conn.execute(
            """
            SELECT wallet_address, SUM(amount_usd) AS amt
            FROM new_trades
            WHERE detected_at >= datetime('now', ?)
              AND market_id = ? AND side = ?
            GROUP BY wallet_address
            ORDER BY amt DESC
            """,
            (cutoff, r["market_id"], r["side"]),
        ).fetchall()
        candidates.append(
            ConvergenceCandidate(
                market_id=r["market_id"],
                market_question=r["market_question"],
                side=r["side"],
                wallet_count=r["wallet_count"],
                total_amount_usd=r["total_amount"] or 0.0,
                wallet_amounts=[(w["wallet_address"], w["amt"] or 0.0) for w in wallet_rows],
            )
        )
    return candidates


def previously_alerted_count(conn: sqlite3.Connection, market_id: str, side: str) -> int | None:
    row = conn.execute(
        "SELECT wallet_count FROM convergence_alerts WHERE market_id = ? AND side = ?",
        (market_id, side),
    ).fetchone()
    return row["wallet_count"] if row else None


def upsert_alert(
    conn: sqlite3.Connection, c: ConvergenceCandidate, wallets_json: str
) -> None:
    conn.execute(
        """
        INSERT INTO convergence_alerts (
            market_id, market_question, side, wallet_count, total_amount_usd, wallets, last_alerted_at
        ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(market_id, side) DO UPDATE SET
            market_question = excluded.market_question,
            wallet_count = excluded.wallet_count,
            total_amount_usd = excluded.total_amount_usd,
            wallets = excluded.wallets,
            last_alerted_at = CURRENT_TIMESTAMP
        """,
        (
            c.market_id,
            c.market_question,
            c.side,
            c.wallet_count,
            c.total_amount_usd,
            wallets_json,
        ),
    )


def _to_notification(
    c: ConvergenceCandidate, client: PolymarketClient | None = None
) -> ConvergenceNotification:
    slug: str | None = None
    probability: float | None = None
    if client is not None:
        try:
            meta = client.market_meta(c.market_id)
            slug = meta.get("slug")
            prices = meta.get("prices") or {}
            probability = prices.get(c.side.upper())
        except Exception as exc:
            log.debug("market_meta failed for %s: %s", c.market_id, exc)
    return ConvergenceNotification(
        market_id=c.market_id,
        market_question=c.market_question,
        market_slug=slug,
        side=c.side,
        total_amount_usd=c.total_amount_usd,
        wallets=[ConvergenceWalletEntry(address=a, amount_usd=v) for a, v in c.wallet_amounts],
        current_probability=probability,
    )


def run_convergence(settings: Settings) -> int:
    """Detect and alert. Returns the number of alerts sent."""
    import json

    sent = 0
    conn = connect(settings.db_path)
    try:
        candidates = detect(conn, settings)
        if not candidates:
            log.info("convergence: no candidates")
            return 0

        with DiscordClient(settings.discord_webhook_url, dry_run=settings.dry_run) as discord, \
             PolymarketClient(user_agent=settings.polymarket_user_agent) as poly:
            for c in candidates:
                prev = previously_alerted_count(conn, c.market_id, c.side)
                if prev is not None and c.wallet_count <= prev:
                    log.debug(
                        "skip re-alert market=%s side=%s (prev=%d cur=%d)",
                        c.market_id, c.side, prev, c.wallet_count,
                    )
                    continue

                if settings.convergence.enable_here_mention:
                    content = "@here Convergence detected"
                    allowed_mentions: dict[str, Any] | None = {"parse": ["everyone"]}
                else:
                    content = "Convergence detected"
                    allowed_mentions = {"parse": []}

                try:
                    discord.send(
                        content=content,
                        embeds=[format_convergence_embed(_to_notification(c, poly))],
                        allowed_mentions=allowed_mentions,
                    )
                except Exception as exc:
                    log.warning("convergence discord send failed: %s", exc)
                    continue

                wallets_json = json.dumps([a for a, _ in c.wallet_amounts])
                with transaction(conn):
                    upsert_alert(conn, c, wallets_json)
                sent += 1
    finally:
        conn.close()
    log.info("convergence run: %d alerts sent", sent)
    return sent
