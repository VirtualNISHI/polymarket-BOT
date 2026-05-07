"""Daily snapshot entrypoint — run at 00:00 JST via cron."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_settings  # noqa: E402
from src.daily_snapshot.job import run as run_daily  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket daily snapshot poster")
    parser.add_argument("--dry-run", action="store_true", help="don't post to Discord/X")
    parser.add_argument(
        "--no-x", action="store_true", help="skip the X post regardless of settings"
    )
    parser.add_argument(
        "--no-discord", action="store_true", help="skip the Discord post regardless of settings"
    )
    args = parser.parse_args()

    if args.dry_run:
        os.environ["DRY_RUN"] = "true"

    settings = load_settings()
    if args.no_x:
        settings.daily_snapshot.enable_x = False
    if args.no_discord:
        settings.daily_snapshot.enable_discord = False

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_daily(settings, ensure_schema=True)


if __name__ == "__main__":
    main()
