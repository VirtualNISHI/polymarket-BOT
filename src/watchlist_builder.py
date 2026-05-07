"""Build the smart-money watchlist (SPEC §1).

Strategy: pull Polymarket's public profit leaderboard (lb-api), enrich each
candidate with their /trades history to derive ``market_appearances`` and a
``win_rate`` estimate (per-market cashflow), score with the SPEC formula, and
persist top N to ``watchlist``.

The earlier per-market ``/holders`` aggregation utilities (``collect_markets``,
``aggregate_leaderboards``) are retained here as building blocks but no longer
on the default ``build()`` path — see SPEC §1.1 for why.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .db import connect, transaction
from .polymarket_client import PolymarketClient

log = logging.getLogger(__name__)


def _market_id(m: dict[str, Any]) -> str | None:
    for key in ("market_id", "id", "marketId", "slug"):
        v = m.get(key)
        if v:
            return str(v)
    return None


def _wallet_address(row: dict[str, Any]) -> str | None:
    for key in ("address", "wallet_address", "wallet", "trader"):
        v = row.get(key)
        if v:
            return str(v).lower()
    return None


def _float(row: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        v = row.get(key)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return default


def _int(row: dict[str, Any], *keys: str, default: int = 0) -> int:
    for key in keys:
        v = row.get(key)
        if v is None:
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return default


@dataclass
class WalletAggregate:
    address: str
    market_appearances: int = 0
    cumulative_pnl_usd: float = 0.0
    cumulative_volume_usd: float = 0.0
    wins: int = 0
    markets_seen: set[str] = field(default_factory=set)

    @property
    def win_rate(self) -> float:
        if self.market_appearances == 0:
            return 0.0
        return self.wins / self.market_appearances

    def add(self, market_id: str, row: dict[str, Any]) -> None:
        if market_id in self.markets_seen:
            return
        self.markets_seen.add(market_id)
        self.market_appearances += 1
        self.cumulative_pnl_usd += _float(row, "pnl_usd", "pnl", "realized_pnl_usd")
        self.cumulative_volume_usd += _float(row, "volume_usd", "volume", "traded_volume_usd")
        pnl = _float(row, "pnl_usd", "pnl", "realized_pnl_usd")
        if pnl > 0:
            self.wins += 1


def score_wallet(agg: WalletAggregate, weights: dict[str, float]) -> float:
    return (
        agg.cumulative_pnl_usd * weights["pnl"]
        + agg.win_rate * 1_000_000 * weights["win_rate"]
        + agg.market_appearances * 100_000 * weights["market_count"]
    )


def collect_markets(client: PolymarketClient, settings: Settings) -> list[dict[str, Any]]:
    """Fetch top markets per category, dedupe by market_id."""
    cfg = settings.watchlist_builder
    seen: set[str] = set()
    markets: list[dict[str, Any]] = []
    for category in cfg.scan_categories:
        try:
            rows = client.prediction_market_screener(
                category=category,
                limit=cfg.markets_per_category,
                include_closed=cfg.include_closed_markets,
            )
        except Exception as exc:
            log.warning("screener category=%s failed: %s", category, exc)
            continue
        for m in rows:
            mid = _market_id(m)
            if not mid or mid in seen:
                continue
            seen.add(mid)
            markets.append(m)
    log.info("Collected %d unique markets across %d categories", len(markets), len(cfg.scan_categories))
    return markets


def aggregate_leaderboards(
    client: PolymarketClient, markets: list[dict[str, Any]], top_n: int
) -> dict[str, WalletAggregate]:
    aggregates: dict[str, WalletAggregate] = {}
    for m in markets:
        mid = _market_id(m)
        if not mid:
            continue
        try:
            leaderboard = client.prediction_market_pnl_leaderboard(market_id=mid, top_n=top_n)
        except Exception as exc:
            log.warning("leaderboard market_id=%s failed: %s", mid, exc)
            continue
        for row in leaderboard:
            addr = _wallet_address(row)
            if not addr:
                continue
            agg = aggregates.setdefault(addr, WalletAggregate(address=addr))
            agg.add(mid, row)
    log.info("Aggregated %d unique wallets across %d markets", len(aggregates), len(markets))
    return aggregates


def select_top(
    aggregates: dict[str, WalletAggregate], settings: Settings
) -> list[tuple[WalletAggregate, float]]:
    cfg = settings.watchlist_builder
    weights = {
        "pnl": cfg.scoring.pnl_weight,
        "win_rate": cfg.scoring.win_rate_weight,
        "market_count": cfg.scoring.market_count_weight,
    }
    candidates: list[tuple[WalletAggregate, float]] = []
    for agg in aggregates.values():
        if agg.market_appearances < cfg.min_market_appearances:
            continue
        if agg.cumulative_pnl_usd < cfg.min_cumulative_pnl_usd:
            continue
        candidates.append((agg, score_wallet(agg, weights)))
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[: cfg.watchlist_size]


def persist_watchlist(
    settings: Settings,
    selected: list[tuple[WalletAggregate, float]],
    scanned_markets: int,
) -> None:
    conn = connect(settings.db_path)
    try:
        with transaction(conn):
            for agg, score in selected:
                conn.execute(
                    """
                    INSERT INTO watchlist (
                        wallet_address, score, cumulative_pnl_usd, win_rate,
                        market_appearances, cumulative_volume_usd, enabled, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, TRUE, CURRENT_TIMESTAMP)
                    ON CONFLICT(wallet_address) DO UPDATE SET
                        score = excluded.score,
                        cumulative_pnl_usd = excluded.cumulative_pnl_usd,
                        win_rate = excluded.win_rate,
                        market_appearances = excluded.market_appearances,
                        cumulative_volume_usd = excluded.cumulative_volume_usd,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        agg.address,
                        score,
                        agg.cumulative_pnl_usd,
                        agg.win_rate,
                        agg.market_appearances,
                        agg.cumulative_volume_usd,
                    ),
                )
            conn.execute(
                """
                INSERT INTO watchlist_history (wallet_count, scanned_markets, note)
                VALUES (?, ?, ?)
                """,
                (len(selected), scanned_markets, "build_watchlist"),
            )
    finally:
        conn.close()


def print_summary(selected: list[tuple[WalletAggregate, float]]) -> None:
    print(f"Top {len(selected)} candidates:")
    for i, (agg, score) in enumerate(selected, start=1):
        print(
            f"{i:>3}. {agg.address[:10]}... | "
            f"PnL: ${agg.cumulative_pnl_usd:>12,.0f} | "
            f"WinRate: {agg.win_rate:>5.0%} | "
            f"Markets: {agg.market_appearances:>3} | "
            f"Score: {score:>12,.0f}"
        )


def estimate_wins_from_trades(trades: list[dict[str, Any]]) -> tuple[int, int]:
    """Estimate (wins, total_markets) from a wallet's trade history.

    For each market, sum cashflow = SELL_USD − BUY_USD across trades. A market
    is a "win" if cashflow > 0 — the wallet sold for more than they bought.

    Caveats:
    - Markets where the wallet held to resolution (won the share's $1 settlement)
      look like losses here because settlement isn't a SELL trade. We can't tell
      from ``/trades`` alone; would need Gamma resolution lookup per market.
    - For wallets on the lb-api profit leaderboard the bias is consistent and
      the win_rate is still a useful relative signal between candidates.
    """
    cashflow: dict[str, float] = {}
    for t in trades:
        mid = t.get("market_id")
        if not mid:
            continue
        amount = _float(t, "amount_usd")
        action = (t.get("action") or "").upper()
        if action == "SELL":
            cashflow[mid] = cashflow.get(mid, 0.0) + amount
        elif action == "BUY":
            cashflow[mid] = cashflow.get(mid, 0.0) - amount
        else:
            cashflow.setdefault(mid, 0.0)
    wins = sum(1 for v in cashflow.values() if v > 0)
    return wins, len(cashflow)


def aggregate_from_leaderboard(
    client: PolymarketClient, settings: Settings
) -> dict[str, WalletAggregate]:
    """Pull Polymarket's public profit leaderboard, enrich with trade history.

    SPEC §1 originally described a per-market ``/holders``-aggregation, but
    Polymarket's ``/holders`` ranks by *current share count*, so the smart
    money — wallets that have already cashed out (Theo4, Fredi9999, ...) —
    don't appear, and most candidates are small unredeemed tails.

    The official ``lb-api.polymarket.com/profit`` leaderboard ranks by
    realized lifetime profit, which is exactly what we want. We then call
    ``/trades?user={addr}`` to count distinct markets traded, giving us
    ``market_appearances`` for the SPEC scoring formula, and estimate
    ``win_rate`` from per-market cashflow (see SPEC §8.2 for caveats).
    """
    cfg = settings.watchlist_builder
    # lb-api caps each window at 50 entries — union All + 30d + 7d + 1d to
    # reach a pool of ~120-140 unique candidates (verified empirically).
    leaderboard = client.top_profit_traders_union()
    log.info("Pulled %d unique candidates from lb-api /profit (4 windows)", len(leaderboard))

    aggregates: dict[str, WalletAggregate] = {}
    for entry in leaderboard:
        addr = entry["address"]
        try:
            trades = client.prediction_market_address_trades(
                wallet_address=addr, limit=500
            )
        except Exception as exc:
            log.warning("trades fetch failed for %s: %s", addr, exc)
            trades = []

        wins, n_markets = estimate_wins_from_trades(trades)
        distinct_markets = {t["market_id"] for t in trades if t.get("market_id")}

        agg = WalletAggregate(address=addr)
        agg.cumulative_pnl_usd = entry["pnl_usd"]
        agg.market_appearances = max(len(distinct_markets), 1)
        agg.markets_seen = distinct_markets
        # If we could enumerate per-market resolved PnL we'd set wins precisely.
        # Cashflow heuristic underestimates wins for held-to-resolution positions
        # (settlement isn't a SELL trade); fall back to "all wins" if cashflow
        # gave us nothing — leaderboard membership implies overall profitability.
        agg.wins = wins if n_markets > 0 else agg.market_appearances
        agg.cumulative_volume_usd = sum(_float(t, "amount_usd") for t in trades)
        aggregates[addr] = agg

    return aggregates


def build(settings: Settings) -> list[tuple[WalletAggregate, float]]:
    """Build watchlist from Polymarket's public profit leaderboard."""
    with PolymarketClient(user_agent=settings.polymarket_user_agent) as client:
        aggregates = aggregate_from_leaderboard(client, settings)
    selected = select_top(aggregates, settings)
    persist_watchlist(settings, selected, scanned_markets=0)
    print_summary(selected)
    return selected
