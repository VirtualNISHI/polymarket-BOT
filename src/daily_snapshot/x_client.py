"""X (Twitter) posting via tweepy with OAuth1.0a user context.

Tweet text uses the v2 endpoint (``client.create_tweet``); media upload
requires the v1.1 endpoint (``api.media_upload``) since media v2 isn't
available on Free/Basic tiers. Both auth from the same 4 OAuth1.0a secrets.
"""
from __future__ import annotations

import io
import logging
from typing import Any

log = logging.getLogger(__name__)


class XClient:
    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        access_token: str,
        access_secret: str,
        dry_run: bool = False,
    ) -> None:
        self._dry_run = dry_run
        self._client: Any = None
        self._api: Any = None  # v1.1 — only used for media upload
        if dry_run:
            return
        if not all([api_key, api_secret, access_token, access_secret]):
            raise ValueError("X credentials incomplete; cannot post (use dry_run)")
        try:
            import tweepy  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "tweepy is required to post to X. Install with `pip install tweepy>=4.14`."
            ) from exc
        self._client = tweepy.Client(
            consumer_key=api_key,
            consumer_secret=api_secret,
            access_token=access_token,
            access_token_secret=access_secret,
        )
        # Lazily set up v1.1 API only if/when an image is uploaded.
        self._auth = tweepy.OAuth1UserHandler(
            api_key, api_secret, access_token, access_secret
        )
        self._tweepy = tweepy

    def _ensure_v1_api(self) -> Any:
        if self._api is None:
            self._api = self._tweepy.API(self._auth)
        return self._api

    def post(
        self,
        text: str,
        *,
        image_bytes: bytes | None = None,
        image_filename: str = "snapshot.png",
    ) -> dict[str, Any] | None:
        """Post a tweet, optionally with an image attached."""
        if self._dry_run or self._client is None:
            log.info(
                "[dry-run] x tweet (%d chars) image=%s:\n%s",
                len(text),
                len(image_bytes) if image_bytes else None,
                text,
            )
            return None

        media_ids: list[str] | None = None
        if image_bytes is not None:
            api = self._ensure_v1_api()
            try:
                media = api.media_upload(
                    filename=image_filename,
                    file=io.BytesIO(image_bytes),
                )
            except Exception as exc:
                log.error("x media upload failed: %s", exc)
                raise
            media_ids = [str(media.media_id)]
            log.info("x media uploaded: media_id=%s", media.media_id)

        try:
            resp = self._client.create_tweet(text=text, media_ids=media_ids)
        except Exception as exc:
            log.error("x post failed: %s", exc)
            raise
        data = getattr(resp, "data", None) or {}
        log.info("x tweet posted: id=%s", data.get("id"))
        return data
