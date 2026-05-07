"""Configuration loading: settings.yaml + .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


@dataclass
class ScoringWeights:
    pnl_weight: float = 0.4
    win_rate_weight: float = 0.3
    market_count_weight: float = 0.3


@dataclass
class WatchlistBuilderConfig:
    scan_categories: list[str] = field(default_factory=lambda: ["Crypto", "Politics", "Macro"])
    markets_per_category: int = 10
    leaderboard_top_n: int = 50
    min_market_appearances: int = 2
    min_cumulative_pnl_usd: float = 100_000
    watchlist_size: int = 50
    include_closed_markets: bool = True
    scoring: ScoringWeights = field(default_factory=ScoringWeights)


@dataclass
class TrackerConfig:
    poll_interval_minutes: int = 60
    min_notify_usd: float = 10_000
    always_notify_top_rank: int = 10
    consolidation_window_minutes: int = 60


@dataclass
class ConvergenceConfig:
    lookback_hours: int = 24
    min_wallet_count: int = 3
    min_total_amount_usd: float = 50_000
    enable_here_mention: bool = True


@dataclass
class RefreshPnlConfig:
    schedule: str = "weekly"
    rebuild_threshold_score_drop: float = 0.3


@dataclass
class DailySnapshotConfig:
    fetch_limit: int = 200
    min_volume_24h_usd: float = 50_000
    movers_count: int = 3
    crypto_count: int = 3
    macro_count: int = 3
    politics_count: int = 0  # 0 → don't render the politics block
    excluded_tag_slugs: list[str] = field(
        default_factory=lambda: ["sports", "nba", "nfl", "mlb", "nhl", "ufc", "soccer"]
    )
    category_map: dict[str, list[str]] = field(
        default_factory=lambda: {
            "Crypto": ["crypto", "bitcoin", "ethereum", "solana", "crypto-prices"],
            "Macro": [
                "macro-graph",
                "macro-single",
                "macro-indicators",
                "interest-rates",
                "fed",
                "economy",
                "recession",
                "inflation",
            ],
            "Politics": ["politics", "geopolitics", "elections", "trump"],
        }
    )
    display_aliases: dict[str, str] = field(default_factory=dict)
    label_max_chars: int = 40
    discord_color: int = 0x5865F2
    enable_discord: bool = True
    enable_x: bool = True
    enable_jp_translation: bool = True
    jp_translation_provider: str = "gemini"  # "gemini" | "anthropic"
    jp_translation_model: str = "gemini-2.5-flash-lite"
    # When true, renders a PNG card and attaches it to the Discord/X post.
    # The text body is replaced with a minimal caption since the image
    # already carries the data.
    image_mode: bool = True


@dataclass
class Settings:
    watchlist_builder: WatchlistBuilderConfig
    tracker: TrackerConfig
    convergence: ConvergenceConfig
    refresh_pnl: RefreshPnlConfig
    daily_snapshot: DailySnapshotConfig

    # env-derived
    polymarket_user_agent: str
    discord_webhook_url: str
    daily_snapshot_discord_webhook_url: str  # falls back to discord_webhook_url
    x_api_key: str
    x_api_secret: str
    x_access_token: str
    x_access_secret: str
    anthropic_api_key: str
    gemini_api_key: str
    log_level: str
    db_path: Path
    dry_run: bool


def _yaml_to_dataclasses(raw: dict[str, Any]) -> tuple[
    WatchlistBuilderConfig,
    TrackerConfig,
    ConvergenceConfig,
    RefreshPnlConfig,
    DailySnapshotConfig,
]:
    wb_raw = raw.get("watchlist_builder", {}) or {}
    scoring_raw = wb_raw.get("scoring", {}) or {}
    wb = WatchlistBuilderConfig(
        scan_categories=wb_raw.get("scan_categories", ["Crypto", "Politics", "Macro"]),
        markets_per_category=wb_raw.get("markets_per_category", 10),
        leaderboard_top_n=wb_raw.get("leaderboard_top_n", 50),
        min_market_appearances=wb_raw.get("min_market_appearances", 2),
        min_cumulative_pnl_usd=wb_raw.get("min_cumulative_pnl_usd", 100_000),
        watchlist_size=wb_raw.get("watchlist_size", 50),
        include_closed_markets=wb_raw.get("include_closed_markets", True),
        scoring=ScoringWeights(
            pnl_weight=scoring_raw.get("pnl_weight", 0.4),
            win_rate_weight=scoring_raw.get("win_rate_weight", 0.3),
            market_count_weight=scoring_raw.get("market_count_weight", 0.3),
        ),
    )

    tr_raw = raw.get("tracker", {}) or {}
    tr = TrackerConfig(
        poll_interval_minutes=tr_raw.get("poll_interval_minutes", 60),
        min_notify_usd=tr_raw.get("min_notify_usd", 10_000),
        always_notify_top_rank=tr_raw.get("always_notify_top_rank", 10),
        consolidation_window_minutes=tr_raw.get("consolidation_window_minutes", 60),
    )

    cv_raw = raw.get("convergence", {}) or {}
    cv = ConvergenceConfig(
        lookback_hours=cv_raw.get("lookback_hours", 24),
        min_wallet_count=cv_raw.get("min_wallet_count", 3),
        min_total_amount_usd=cv_raw.get("min_total_amount_usd", 50_000),
        enable_here_mention=cv_raw.get("enable_here_mention", True),
    )

    rp_raw = raw.get("refresh_pnl", {}) or {}
    rp = RefreshPnlConfig(
        schedule=rp_raw.get("schedule", "weekly"),
        rebuild_threshold_score_drop=rp_raw.get("rebuild_threshold_score_drop", 0.3),
    )

    ds_default = DailySnapshotConfig()
    ds_raw = raw.get("daily_snapshot", {}) or {}
    ds = DailySnapshotConfig(
        fetch_limit=ds_raw.get("fetch_limit", ds_default.fetch_limit),
        min_volume_24h_usd=ds_raw.get("min_volume_24h_usd", ds_default.min_volume_24h_usd),
        movers_count=ds_raw.get("movers_count", ds_default.movers_count),
        crypto_count=ds_raw.get("crypto_count", ds_default.crypto_count),
        macro_count=ds_raw.get("macro_count", ds_default.macro_count),
        politics_count=ds_raw.get("politics_count", ds_default.politics_count),
        excluded_tag_slugs=ds_raw.get("excluded_tag_slugs", ds_default.excluded_tag_slugs),
        category_map=ds_raw.get("category_map", ds_default.category_map),
        display_aliases=ds_raw.get("display_aliases", {}) or {},
        label_max_chars=ds_raw.get("label_max_chars", ds_default.label_max_chars),
        discord_color=int(ds_raw.get("discord_color", ds_default.discord_color)),
        enable_discord=bool(ds_raw.get("enable_discord", ds_default.enable_discord)),
        enable_x=bool(ds_raw.get("enable_x", ds_default.enable_x)),
        enable_jp_translation=bool(
            ds_raw.get("enable_jp_translation", ds_default.enable_jp_translation)
        ),
        jp_translation_provider=ds_raw.get(
            "jp_translation_provider", ds_default.jp_translation_provider
        ),
        jp_translation_model=ds_raw.get(
            "jp_translation_model", ds_default.jp_translation_model
        ),
        image_mode=bool(ds_raw.get("image_mode", ds_default.image_mode)),
    )

    return wb, tr, cv, rp, ds


def load_settings(
    settings_path: Path | str | None = None, env_path: Path | str | None = None
) -> Settings:
    settings_path = Path(settings_path or DEFAULT_SETTINGS_PATH)
    env_path = Path(env_path or DEFAULT_ENV_PATH)

    if env_path.exists():
        load_dotenv(env_path, override=False)

    raw: dict[str, Any] = {}
    if settings_path.exists():
        with settings_path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    wb, tr, cv, rp, ds = _yaml_to_dataclasses(raw)

    db_path_str = os.getenv("DB_PATH", "./data/smart_money.db")
    db_path = Path(db_path_str)
    if not db_path.is_absolute():
        db_path = (PROJECT_ROOT / db_path).resolve()

    discord_url = os.getenv("DISCORD_WEBHOOK_URL", "")
    daily_url = os.getenv("DAILY_SNAPSHOT_DISCORD_WEBHOOK_URL", "") or discord_url

    return Settings(
        watchlist_builder=wb,
        tracker=tr,
        convergence=cv,
        refresh_pnl=rp,
        daily_snapshot=ds,
        polymarket_user_agent=os.getenv(
            "POLYMARKET_USER_AGENT", "polymarket-smart-money/0.1"
        ),
        discord_webhook_url=discord_url,
        daily_snapshot_discord_webhook_url=daily_url,
        x_api_key=os.getenv("X_API_KEY", ""),
        x_api_secret=os.getenv("X_API_SECRET", ""),
        x_access_token=os.getenv("X_ACCESS_TOKEN", ""),
        x_access_secret=os.getenv("X_ACCESS_SECRET", ""),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        db_path=db_path,
        dry_run=os.getenv("DRY_RUN", "false").lower() in {"1", "true", "yes"},
    )
