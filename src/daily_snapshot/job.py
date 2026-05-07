"""Daily snapshot orchestrator: collect → format → post (Discord + X) → persist."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from typing import Iterable
from zoneinfo import ZoneInfo

from ..config import Settings, load_settings
from ..db import connect, init_schema, transaction
from ..discord_client import DiscordClient
from ..polymarket_client import PolymarketClient
from .collector import SnapshotRow, by_category, collect_snapshot, top_movers
from .formatter import build_discord_embed, build_tweet
from .image_renderer import render_snapshot_png
from .jp_translator import build_label_map
from .x_client import XClient

JST = ZoneInfo("Asia/Tokyo")
log = logging.getLogger(__name__)


def _persist(
    conn: sqlite3.Connection,
    *,
    snapshot_date: str,
    section: str,
    rows: Iterable[SnapshotRow],
) -> None:
    """Replace the (date, section) slice with the new ranking.

    Wiping first means the audit table always reflects what was actually
    posted on a given date — re-runs supersede earlier ones cleanly.
    """
    with transaction(conn):
        conn.execute(
            "DELETE FROM daily_snapshot WHERE snapshot_date = ? AND section = ?",
            (snapshot_date, section),
        )
        for rank, r in enumerate(rows, start=1):
            conn.execute(
                """
                INSERT OR REPLACE INTO daily_snapshot
                  (snapshot_date, market_id, slug, question, category,
                   yes_price, one_day_change, volume_24h_usd, section, rank_in_section)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_date,
                    r.market_id,
                    r.slug,
                    r.question,
                    r.category,
                    r.yes_price,
                    r.one_day_change,
                    r.volume_24h_usd,
                    section,
                    rank,
                ),
            )


def run(settings: Settings | None = None, *, ensure_schema: bool = True) -> None:
    settings = settings or load_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = settings.daily_snapshot

    if ensure_schema:
        init_schema(settings.db_path)

    now = datetime.now(tz=JST)
    snapshot_date_str = now.strftime("%Y-%m-%d")
    log.info("daily snapshot for %s (dry_run=%s)", snapshot_date_str, settings.dry_run)

    with PolymarketClient(user_agent=settings.polymarket_user_agent) as poly:
        rows = collect_snapshot(
            poly,
            fetch_limit=cfg.fetch_limit,
            min_volume_24h_usd=cfg.min_volume_24h_usd,
            category_map=cfg.category_map,
            excluded_tag_slugs=cfg.excluded_tag_slugs,
        )

    if not rows:
        log.warning("no markets after filtering — skipping post")
        return

    movers = top_movers(rows, n=cfg.movers_count)
    crypto = by_category(rows, category="Crypto", n=cfg.crypto_count)
    macro = by_category(rows, category="Macro", n=cfg.macro_count)
    politics = (
        by_category(rows, category="Politics", n=cfg.politics_count)
        if cfg.politics_count > 0
        else []
    )

    # Build the slug → label map: manual aliases + cache + LLM translation.
    # Done before persistence so a translation failure doesn't block the audit row.
    selected = list({r.market_id: r for r in (movers + crypto + macro + politics)}.values())
    provider_key = {
        "gemini": settings.gemini_api_key,
        "anthropic": settings.anthropic_api_key,
    }.get(cfg.jp_translation_provider, "")
    conn_for_labels = connect(settings.db_path)
    try:
        aliases = build_label_map(
            selected,
            conn=conn_for_labels,
            manual_aliases=cfg.display_aliases,
            api_key=provider_key,
            provider=cfg.jp_translation_provider,
            model=cfg.jp_translation_model,
            enable_translation=cfg.enable_jp_translation,
        )
    finally:
        conn_for_labels.close()

    # Image vs. text-mode: in image mode the PNG carries the full snapshot,
    # so the message body shrinks to a one-line caption. Embed/tweet text
    # mode is the fallback path.
    image_bytes: bytes | None = None
    if cfg.image_mode:
        try:
            image_bytes = render_snapshot_png(
                snapshot_date=now,
                movers=movers,
                crypto=crypto,
                macro=macro,
                politics=politics,
                aliases=aliases,
            )
            log.info("rendered snapshot image: %d bytes", len(image_bytes))
        except Exception as exc:
            log.warning("image render failed (%s) — falling back to text", exc)
            image_bytes = None

    if image_bytes is None:
        embed = build_discord_embed(
            snapshot_date=now,
            movers=movers,
            crypto=crypto,
            macro=macro,
            politics=politics,
            aliases=aliases,
            color=cfg.discord_color,
        )
        tweet_text = build_tweet(
            snapshot_date=now,
            movers=movers,
            aliases=aliases,
        )
    else:
        embed = None
        # %-m / %#m differ across platforms; use manual format to stay portable.
        date_short = f"{now.month}/{now.day:02d}"
        tweet_text = (
            f"📊 Polymarket Daily Snapshot {date_short} JST\n"
            "#Polymarket #PredictionMarket"
        )

    log.info("composed: %d movers / %d crypto / %d macro / %d politics",
             len(movers), len(crypto), len(macro), len(politics))

    # Persist before posting so a posting failure still leaves an audit trail.
    conn = connect(settings.db_path)
    try:
        _persist(conn, snapshot_date=snapshot_date_str, section="movers", rows=movers)
        _persist(conn, snapshot_date=snapshot_date_str, section="crypto", rows=crypto)
        _persist(conn, snapshot_date=snapshot_date_str, section="macro", rows=macro)
        if politics:
            _persist(conn, snapshot_date=snapshot_date_str, section="politics", rows=politics)
    finally:
        conn.close()

    # Discord
    if cfg.enable_discord:
        webhook = settings.daily_snapshot_discord_webhook_url
        if not webhook and not settings.dry_run:
            log.warning("daily snapshot discord webhook not configured — skipping discord post")
        else:
            with DiscordClient(webhook, dry_run=settings.dry_run) as dc:
                if image_bytes is not None:
                    dc.send(image_bytes=image_bytes, image_filename="snapshot.png")
                else:
                    dc.send(embeds=[embed] if embed else None)
            log.info("discord posted (image=%s)", image_bytes is not None)
    else:
        log.info("discord disabled in settings")

    # X
    if cfg.enable_x:
        try:
            xc = XClient(
                api_key=settings.x_api_key,
                api_secret=settings.x_api_secret,
                access_token=settings.x_access_token,
                access_secret=settings.x_access_secret,
                dry_run=settings.dry_run,
            )
        except (ValueError, ImportError) as exc:
            log.warning("x client unavailable: %s — skipping x post", exc)
        else:
            xc.post(tweet_text, image_bytes=image_bytes)
            log.info("x posted (image=%s)", image_bytes is not None)
    else:
        log.info("x disabled in settings")


if __name__ == "__main__":
    run()
