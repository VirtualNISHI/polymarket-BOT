"""Hourly cron entrypoint: tracker + convergence."""
from __future__ import annotations

import logging

from .config import Settings, load_settings
from .convergence import run_convergence
from .db import init_schema
from .tracker import run_tracker

log = logging.getLogger(__name__)


def run(settings: Settings | None = None, *, ensure_schema: bool = True) -> None:
    settings = settings or load_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if ensure_schema:
        init_schema(settings.db_path)

    log.info("starting tracker (dry_run=%s)", settings.dry_run)
    run_tracker(settings)

    log.info("starting convergence")
    run_convergence(settings)
