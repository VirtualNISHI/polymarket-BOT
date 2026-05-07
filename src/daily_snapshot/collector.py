"""Daily snapshot data collection.

One Gamma ``/markets`` call (with ``include_tag=true``) yields everything we
need: ``oneDayPriceChange`` for the 24h delta, ``volume24hr`` for ranking,
and inline ``tags`` for client-side bucketing. No CLOB price-history calls
are required.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

from ..polymarket_client import PolymarketClient

log = logging.getLogger(__name__)


@dataclass
class SnapshotRow:
    market_id: str
    slug: str | None
    question: str
    yes_price: float | None
    one_day_change: float | None  # absolute price delta on YES, e.g. 0.052 = +5.2pp
    volume_24h_usd: float
    tag_slugs: list[str]
    category: str | None  # bucket label assigned by ``categorize``
    event_slug: str | None = None
    event_title: str | None = None


def categorize(tag_slugs: Iterable[str], category_map: dict[str, list[str]]) -> str | None:
    """Map a market's tags to a single bucket label.

    ``category_map`` is ``{bucket_label: [tag_slugs...]}``. First match wins,
    in the order ``category_map`` is iterated (Python 3.7+ preserves insertion
    order). Returns None if no bucket matches.
    """
    tag_set = {s.lower() for s in tag_slugs}
    for label, slugs in category_map.items():
        for s in slugs:
            if s.lower() in tag_set:
                return label
    return None


def collect_snapshot(
    client: PolymarketClient,
    *,
    fetch_limit: int = 200,
    min_volume_24h_usd: float = 50_000,
    category_map: dict[str, list[str]],
    excluded_tag_slugs: Iterable[str] = (),
) -> list[SnapshotRow]:
    """Fetch the universe of active markets and return categorized rows.

    Filtering:
    - Drops markets with ``volume_24h_usd < min_volume_24h_usd`` (kills the
      illiquid long tail where ``oneDayPriceChange`` is noisy or None).
    - Drops markets whose tag set intersects ``excluded_tag_slugs`` (default
      use: drop sports / live games which dominate volume but aren't the
      macro/crypto/politics signal we want).
    - Drops markets where ``yes_price`` is None (malformed entries).

    Rows that don't match any bucket get ``category=None`` so the formatter
    can still use them for the cross-category "Top movers" section.
    """
    raw = client.active_markets_with_tags(
        limit=fetch_limit, order="volume24hr", ascending=False
    )
    excluded = {s.lower() for s in excluded_tag_slugs}

    rows: list[SnapshotRow] = []
    for m in raw:
        if m["yes_price"] is None:
            continue
        if m["volume_24h_usd"] < min_volume_24h_usd:
            continue
        if any(s in excluded for s in m["tag_slugs"]):
            continue
        rows.append(
            SnapshotRow(
                market_id=str(m["market_id"]),
                slug=m["slug"],
                question=m["question"] or "",
                yes_price=m["yes_price"],
                one_day_change=m["one_day_change"],
                volume_24h_usd=float(m["volume_24h_usd"] or 0.0),
                tag_slugs=m["tag_slugs"],
                category=categorize(m["tag_slugs"], category_map),
                event_slug=m.get("event_slug"),
                event_title=m.get("event_title"),
            )
        )

    log.info(
        "snapshot universe: %d markets after filtering (from %d raw)",
        len(rows),
        len(raw),
    )
    return rows


def _question_prefix_key(q: str, n: int = 25) -> str:
    """Lower-case, whitespace-collapsed first n chars — used to collapse
    near-duplicate question variants that share an event theme but differ only
    by a deadline / price tier (e.g. ``Strait of Hormuz traffic returns to
    normal by May 15`` vs. ``…by end of May``)."""
    return " ".join((q or "").lower().split())[:n]


def _dedup_by_event_and_prefix(
    rows: list[SnapshotRow], *, prefix_len: int = 25
) -> list[SnapshotRow]:
    """Keep at most one row per event_slug AND per question prefix.

    The caller is expected to pre-sort ``rows`` by the ranking metric they want
    to win the dedup tiebreak (the first occurrence wins).
    """
    seen_events: set[str] = set()
    seen_prefixes: set[str] = set()
    out: list[SnapshotRow] = []
    for r in rows:
        prefix = _question_prefix_key(r.question, prefix_len)
        if r.event_slug and r.event_slug in seen_events:
            continue
        if prefix and prefix in seen_prefixes:
            continue
        if r.event_slug:
            seen_events.add(r.event_slug)
        if prefix:
            seen_prefixes.add(prefix)
        out.append(r)
    return out


def top_movers(rows: list[SnapshotRow], *, n: int = 3) -> list[SnapshotRow]:
    """Return the n markets with the largest absolute 24h price change.

    Deduped by event_slug so multi-market events (e.g. price-tier ladders)
    contribute one row each.
    """
    eligible = [r for r in rows if r.one_day_change is not None]
    eligible.sort(key=lambda r: abs(r.one_day_change or 0.0), reverse=True)
    return _dedup_by_event_and_prefix(eligible)[:n]


def by_category(
    rows: list[SnapshotRow],
    *,
    category: str,
    n: int,
    sort_by: str = "volume_24h_usd",
) -> list[SnapshotRow]:
    """Return top n rows for a given bucket label.

    ``sort_by``: ``"volume_24h_usd"`` (default) or ``"abs_change"``.
    Deduped by event_slug after sorting.
    """
    cat_rows = [r for r in rows if r.category == category]
    if sort_by == "abs_change":
        cat_rows = [r for r in cat_rows if r.one_day_change is not None]
        cat_rows.sort(key=lambda r: abs(r.one_day_change or 0.0), reverse=True)
    else:
        cat_rows.sort(key=lambda r: r.volume_24h_usd, reverse=True)
    return _dedup_by_event_and_prefix(cat_rows)[:n]
