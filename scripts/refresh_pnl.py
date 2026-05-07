"""Weekly PnL refresh (SPEC §8.2).

Re-fetches the public profit leaderboard and updates each enabled watchlist
wallet's score / PnL / market count. Wallets whose score has dropped by more
than ``rebuild_threshold_score_drop`` are auto-disabled (the next monthly
``build_watchlist`` will reconsider them).

Wallets that have completely fallen off the leaderboard top window are kept
but their score is recalculated from the current /positions snapshot — they
may have just gone quiet, not gone bad.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_settings  # noqa: E402
from src.db import connect, transaction  # noqa: E402
from src.polymarket_client import PolymarketClient  # noqa: E402
from src.watchlist_builder import WalletAggregate, score_wallet  # noqa: E402


def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("refresh_pnl")

    cfg = settings.watchlist_builder
    weights = {
        "pnl": cfg.scoring.pnl_weight,
        "win_rate": cfg.scoring.win_rate_weight,
        "market_count": cfg.scoring.market_count_weight,
    }
    drop = settings.refresh_pnl.rebuild_threshold_score_drop

    with PolymarketClient(user_agent=settings.polymarket_user_agent) as client:
        leaderboard = client.top_profit_traders_union()
        lb_by_addr = {r["address"]: r for r in leaderboard}
        log.info("refreshed leaderboard: %d unique entries (4 windows)", len(lb_by_addr))

        conn = connect(settings.db_path)
        disabled = 0
        updated = 0
        try:
            with transaction(conn):
                rows = conn.execute(
                    "SELECT wallet_address, score FROM watchlist WHERE enabled = TRUE"
                ).fetchall()

                for r in rows:
                    addr = r["wallet_address"]
                    old_score = r["score"] or 0.0
                    lb_entry = lb_by_addr.get(addr)

                    if lb_entry:
                        new_pnl = lb_entry["pnl_usd"]
                    else:
                        # Wallet fell off the leaderboard window. Probe their
                        # current positions for a partial snapshot.
                        try:
                            positions = client._get(  # type: ignore[attr-defined]
                                "https://data-api.polymarket.com",
                                "/positions",
                                {"user": addr},
                            ) or []
                        except Exception:
                            positions = []
                        new_pnl = sum(
                            float(p.get("cashPnl") or 0)
                            for p in (positions if isinstance(positions, list) else [])
                        )

                    # Re-derive market_appearances from /trades.
                    try:
                        trades = client.prediction_market_address_trades(
                            wallet_address=addr, limit=500
                        )
                    except Exception:
                        trades = []
                    distinct_markets = {
                        t["market_id"] for t in trades if t.get("market_id")
                    }

                    agg = WalletAggregate(address=addr)
                    agg.cumulative_pnl_usd = new_pnl
                    agg.market_appearances = max(len(distinct_markets), 1)
                    agg.markets_seen = distinct_markets
                    agg.wins = agg.market_appearances  # placeholder, see SPEC §8.2
                    agg.cumulative_volume_usd = sum(
                        float(t.get("amount_usd") or 0) for t in trades
                    )
                    new_score = score_wallet(agg, weights)

                    if old_score > 0 and (old_score - new_score) / old_score >= drop:
                        conn.execute(
                            """
                            UPDATE watchlist
                            SET enabled = FALSE,
                                score = ?,
                                cumulative_pnl_usd = ?,
                                win_rate = ?,
                                market_appearances = ?,
                                cumulative_volume_usd = ?,
                                note = COALESCE(note || ' | ', '')
                                       || 'auto-disabled by refresh_pnl '
                                       || strftime('%Y-%m-%d', 'now'),
                                updated_at = CURRENT_TIMESTAMP
                            WHERE wallet_address = ?
                            """,
                            (
                                new_score,
                                agg.cumulative_pnl_usd,
                                agg.win_rate,
                                agg.market_appearances,
                                agg.cumulative_volume_usd,
                                addr,
                            ),
                        )
                        disabled += 1
                        log.info(
                            "disabled %s: score %.0f -> %.0f (-%.0f%%)",
                            addr, old_score, new_score,
                            (old_score - new_score) / old_score * 100,
                        )
                    else:
                        conn.execute(
                            """
                            UPDATE watchlist
                            SET score = ?,
                                cumulative_pnl_usd = ?,
                                win_rate = ?,
                                market_appearances = ?,
                                cumulative_volume_usd = ?,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE wallet_address = ?
                            """,
                            (
                                new_score,
                                agg.cumulative_pnl_usd,
                                agg.win_rate,
                                agg.market_appearances,
                                agg.cumulative_volume_usd,
                                addr,
                            ),
                        )
                        updated += 1
        finally:
            conn.close()

    log.info("refresh_pnl: updated=%d disabled=%d", updated, disabled)
    print(f"refresh_pnl: updated={updated} disabled={disabled}")


if __name__ == "__main__":
    main()
