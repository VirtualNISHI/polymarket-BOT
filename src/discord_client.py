"""Discord webhook client."""
from __future__ import annotations

import logging
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)


class DiscordClient:
    def __init__(
        self,
        webhook_url: str,
        *,
        dry_run: bool = False,
        timeout: float = 15.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._webhook_url = webhook_url
        self._dry_run = dry_run
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "DiscordClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    def send(
        self,
        *,
        content: str | None = None,
        embeds: list[dict[str, Any]] | None = None,
        allowed_mentions: dict[str, Any] | None = None,
        image_bytes: bytes | None = None,
        image_filename: str = "snapshot.png",
    ) -> None:
        """POST to the webhook. With ``image_bytes`` set, sends multipart/form-data
        so the image attaches to the message; otherwise sends JSON.
        """
        payload: dict[str, Any] = {}
        if content:
            payload["content"] = content
        if embeds:
            payload["embeds"] = embeds
        if allowed_mentions is not None:
            payload["allowed_mentions"] = allowed_mentions

        if self._dry_run or not self._webhook_url:
            log.info(
                "[dry-run] discord payload=%s image_bytes=%s",
                payload,
                len(image_bytes) if image_bytes else None,
            )
            return

        if image_bytes is not None:
            import json as _json

            files = {
                "files[0]": (image_filename, image_bytes, "image/png"),
                "payload_json": (None, _json.dumps(payload), "application/json"),
            }
            resp = self._client.post(self._webhook_url, files=files)
        else:
            resp = self._client.post(self._webhook_url, json=payload)

        if resp.status_code >= 400:
            log.warning("Discord webhook %s: %s", resp.status_code, resp.text[:300])
            resp.raise_for_status()
