"""Discord webhook smoke-test.

Sends a representative single-trade embed and a convergence embed to the
configured webhook so you can verify formatting & permissions before letting
the hourly cron run.

Usage:
    python scripts/test_discord.py            # send both samples
    python scripts/test_discord.py --trade    # single-trade only
    python scripts/test_discord.py --convergence
    python scripts/test_discord.py --dry-run  # print payloads, don't POST
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_settings  # noqa: E402
from src.discord_client import DiscordClient  # noqa: E402
from src.formatter import (  # noqa: E402
    ConvergenceNotification,
    ConvergenceWalletEntry,
    TradeNotification,
    WalletStats,
    format_convergence_embed,
    format_trade_embed,
)


SAMPLE_TRADE = TradeNotification(
    market_id="0xdd22472e552920b8438158ea7238bfadfa4f736aa4cee91a6b86c39ead110917",
    market_question="Will Donald Trump win the 2024 US Presidential Election?",
    market_slug="will-donald-trump-win-the-2024-us-presidential-election",
    side="YES",
    amount_usd=125_000,
    entry_price=0.621,
    wallet=WalletStats(
        address="0x56687bf447db6ffa42ffe2204a05edaa20f55839",
        rank=5,
        cumulative_pnl_usd=22_053_934,
        win_rate=1.0,
        market_appearances=14,
    ),
)

SAMPLE_CONVERGENCE = ConvergenceNotification(
    market_id="0xa923afcb8297e3ade170f2f8c088f3c277557fadef2c67054d72cc59f8504b2b",
    market_question="Will a Republican win Pennsylvania Presidential Election?",
    market_slug=None,
    side="YES",
    total_amount_usd=143_827,
    wallets=[
        ConvergenceWalletEntry(address="0xed229c0d13f97ed258a4f1cb50c9b5a1d8bccdd0", amount_usd=66_466),
        ConvergenceWalletEntry(address="0x8119010a3a3f5f96f3e35e21a3a3aef5b7e08887", amount_usd=56_610),
        ConvergenceWalletEntry(address="0x863134d0f8c9f0e5ec6d8b0c69d1b5a52a0c7a53", amount_usd=18_258),
        ConvergenceWalletEntry(address="0x1f2dd6d473f3e824cd2f8a89d9c69fb96f6ad0cf", amount_usd=1_877),
        ConvergenceWalletEntry(address="0x56687bf447db6ffa42ffe2204a05edaa20f55839", amount_usd=617),
    ],
    current_probability=0.61,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Discord webhook smoke test")
    parser.add_argument("--trade", action="store_true", help="send single-trade only")
    parser.add_argument("--convergence", action="store_true", help="send convergence only")
    parser.add_argument("--dry-run", action="store_true", help="don't POST")
    args = parser.parse_args()

    if args.dry_run:
        os.environ["DRY_RUN"] = "true"

    settings = load_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("test_discord")

    if not settings.discord_webhook_url and not settings.dry_run:
        print(
            "ERROR: DISCORD_WEBHOOK_URL not set in .env. "
            "Use --dry-run to preview the payloads instead.",
            file=sys.stderr,
        )
        sys.exit(2)

    send_both = not args.trade and not args.convergence

    with DiscordClient(settings.discord_webhook_url, dry_run=settings.dry_run) as discord:
        if args.trade or send_both:
            log.info("sending single-trade sample")
            discord.send(embeds=[format_trade_embed(SAMPLE_TRADE)])

        if args.convergence or send_both:
            log.info("sending convergence sample")
            content = "@here" if settings.convergence.enable_here_mention else None
            allowed_mentions = (
                {"parse": ["everyone"]} if settings.convergence.enable_here_mention else None
            )
            discord.send(
                content=content,
                embeds=[format_convergence_embed(SAMPLE_CONVERGENCE)],
                allowed_mentions=allowed_mentions,
            )

    print("OK - check the #smart-money-poly channel.")


if __name__ == "__main__":
    main()
