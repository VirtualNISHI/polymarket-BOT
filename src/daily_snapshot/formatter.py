"""Format the snapshot for Discord (rich embed) and X (280-char tweet).

Display labels: each market is rendered with a short label. By default we
truncate ``question`` to ``label_max_chars`` characters; ``display_aliases``
({slug: short_label}) lets the operator override per-market labels (e.g. for
Japanese-language summaries).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .collector import SnapshotRow

JST = ZoneInfo("Asia/Tokyo")
DISCORD_COLOR_DEFAULT = 0x5865F2  # blurple


def _label(row: SnapshotRow, aliases: dict[str, str], max_chars: int = 40) -> str:
    if row.slug and row.slug in aliases:
        return aliases[row.slug]
    q = row.question or "(unknown market)"
    return q if len(q) <= max_chars else q[: max_chars - 1].rstrip() + "…"


def _fmt_pct(p: float | None) -> str:
    if p is None:
        return "—"
    return f"{p * 100:.0f}%"


def _fmt_delta_pp(d: float | None) -> str:
    """Format a YES-price delta as percentage points: 0.052 → '+5.2pt'."""
    if d is None:
        return "—"
    pp = d * 100
    sign = "+" if pp >= 0 else ""
    return f"{sign}{pp:.1f}pt"


def _market_url(row: SnapshotRow) -> str:
    if row.slug:
        return f"https://polymarket.com/event/{row.slug}"
    return "https://polymarket.com/"


# ---------- Discord ----------

def build_discord_embed(
    *,
    snapshot_date: datetime,
    movers: list[SnapshotRow],
    crypto: list[SnapshotRow],
    macro: list[SnapshotRow],
    politics: list[SnapshotRow] | None = None,
    aliases: dict[str, str],
    color: int = DISCORD_COLOR_DEFAULT,
    footer_text: str = "Auto-generated daily at 00:00 JST · Data via Polymarket public API",
) -> dict[str, Any]:
    date_str = snapshot_date.astimezone(JST).strftime("%Y-%m-%d")
    fields: list[dict[str, Any]] = []

    if movers:
        lines = [
            f"• {_label(r, aliases)}  **{_fmt_pct(r.yes_price)}**  {_fmt_delta_pp(r.one_day_change)}"
            for r in movers
        ]
        fields.append({"name": "🔥 Top movers (24h)", "value": "\n".join(lines), "inline": False})

    def _category_block(name: str, rows: list[SnapshotRow]) -> dict[str, Any] | None:
        if not rows:
            return None
        lines = [
            f"• {_label(r, aliases)}: {_fmt_pct(r.yes_price)} ({_fmt_delta_pp(r.one_day_change)})"
            for r in rows
        ]
        return {"name": name, "value": "\n".join(lines), "inline": False}

    for block in (
        _category_block("🪙 Crypto", crypto),
        _category_block("🏛 Macro", macro),
        _category_block("🗳 Politics", politics or []),
    ):
        if block is not None:
            fields.append(block)

    return {
        "title": f"📊 Polymarket Daily Snapshot",
        "description": f"**{date_str} (JST)**",
        "color": color,
        "fields": fields,
        "footer": {"text": footer_text},
        "timestamp": snapshot_date.astimezone(JST).isoformat(),
    }


# ---------- X (Twitter) ----------

X_MAX_CHARS = 280


def build_tweet(
    *,
    snapshot_date: datetime,
    movers: list[SnapshotRow],
    aliases: dict[str, str],
    hashtags: str = "#Polymarket #PredictionMarket",
    cta_url: str | None = None,
) -> str:
    """Compress the snapshot into a single 280-char tweet.

    Strategy: header + top 3 movers as one-liners + hashtags. If too long,
    progressively shorten labels then drop the bottom mover.
    """
    date_str = snapshot_date.astimezone(JST).strftime("%m/%d JST")
    header = f"📊 Polymarket Daily {date_str}\n🔥 Top movers (24h)"

    def render(rows: list[SnapshotRow], label_max: int) -> str:
        body_lines = [
            f"• {_label(r, aliases, max_chars=label_max)} {_fmt_pct(r.yes_price)} {_fmt_delta_pp(r.one_day_change)}"
            for r in rows
        ]
        parts = [header, *body_lines]
        if cta_url:
            parts.append(cta_url)
        if hashtags:
            parts.append(hashtags)
        return "\n".join(parts)

    # Try widest labels first, then shrink, then drop one.
    for label_max in (40, 32, 24, 20, 16):
        text = render(movers, label_max)
        if len(text) <= X_MAX_CHARS:
            return text

    # Last resort: drop to top 2 movers.
    for label_max in (32, 24, 20, 16):
        text = render(movers[: max(1, len(movers) - 1)], label_max)
        if len(text) <= X_MAX_CHARS:
            return text

    # Still too long → truncate. Should be unreachable given header+hashtag size.
    return render(movers[:1], 16)[:X_MAX_CHARS]
