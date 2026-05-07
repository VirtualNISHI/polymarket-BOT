"""Initial / monthly watchlist build (SPEC §1)."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_settings  # noqa: E402
from src.db import init_schema  # noqa: E402
from src.watchlist_builder import build  # noqa: E402


def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    init_schema(settings.db_path)
    build(settings)


if __name__ == "__main__":
    main()
