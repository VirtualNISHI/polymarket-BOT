"""Hourly cron entrypoint: poll tracker + convergence."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_settings  # noqa: E402
from src.job import run as run_job  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Smart Money tracker hourly job")
    parser.add_argument("--dry-run", action="store_true", help="don't post to Discord")
    args = parser.parse_args()

    if args.dry_run:
        os.environ["DRY_RUN"] = "true"

    settings = load_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_job(settings, ensure_schema=True)


if __name__ == "__main__":
    main()
