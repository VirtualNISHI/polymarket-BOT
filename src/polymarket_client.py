"""Polymarket public-API client (Gamma + Data API).

No API key required. Public-method signatures match the previous Nansen
client so ``watchlist_builder`` / ``tracker`` / ``refresh_pnl`` import-sites
are unchanged.

Endpoints:
- Gamma  https://gamma-api.polymarket.com   — market metadata, screener
- Data   https://data-api.polymarket.com    — per-wallet trades, per-market holders + positions

Polymarket's public APIs do not expose a per-market PnL leaderboard directly,
so ``prediction_market_pnl_leaderboard`` joins ``/holders`` (top current
holders per market) with per-wallet ``/positions`` (which carries
``cashPnl``) — see the method docstring for the cost model.

All responses are normalized to the field names the rest of the codebase
expects (``market_id``, ``address``, ``pnl_usd``, ``trade_id``, ``traded_at``, …).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
DATA_BASE = "https://data-api.polymarket.com"
LB_BASE = "https://lb-api.polymarket.com"

# Map SPEC categories → Gamma tag_slug. Polymarket has no "Macro" category;
# "Geopolitics" is the closest match. Override per-deployment via settings.yaml
# if Polymarket renames or reorganizes tags.
CATEGORY_TAG_SLUG = {
    "crypto": "crypto",
    "politics": "politics",
    "macro": "geopolitics",
    "sports": "sports",
    "tech": "tech",
    "business": "business",
    "science": "science",
    "climate": "climate",
}


class PolymarketError(RuntimeError):
    pass


def _to_float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _unix_to_iso(ts: Any) -> str | None:
    """Convert int/float unix seconds to ISO-8601 UTC. Pass through ISO strings."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(ts, str):
        return ts
    return None


def _directional_side(outcome: str | None, action: str | None) -> str:
    """Normalize a Polymarket trade to the YES/NO direction the wallet is betting on.

    Polymarket trades carry both ``outcome`` ("Yes"/"No" — the token traded) and
    ``side`` ("BUY"/"SELL"). The directional bet:

    - BUY  Yes → "YES"
    - SELL No  → "YES"   (selling NO = bullish on YES)
    - BUY  No  → "NO"
    - SELL Yes → "NO"
    """
    o = (outcome or "").strip().upper()
    a = (action or "").strip().upper()
    if o == "YES":
        return "NO" if a == "SELL" else "YES"
    if o == "NO":
        return "YES" if a == "SELL" else "NO"
    return o or "?"


class PolymarketClient:
    def __init__(
        self,
        *,
        user_agent: str = "polymarket-smart-money/0.1",
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "PolymarketClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- low-level ----

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    def _get(self, base: str, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{base}{path}"
        log.debug("GET %s params=%s", url, params)
        resp = self._client.get(url, params=params)
        if resp.status_code >= 400:
            log.warning("Polymarket %s -> %s: %s", url, resp.status_code, resp.text[:300])
            resp.raise_for_status()
        return resp.json()

    # ---- normalized helpers ----

    @staticmethod
    def _normalize_market(m: dict[str, Any]) -> dict[str, Any]:
        volume_usd = _to_float(m.get("volumeNum") or m.get("volume"))
        liquidity_usd = _to_float(m.get("liquidityNum") or m.get("liquidity"))
        # Gamma returns outcomePrices as a JSON string like '["0.42","0.58"]'.
        prices_raw = m.get("outcomePrices")
        outcomes_raw = m.get("outcomes")
        if isinstance(prices_raw, str):
            try:
                import json as _json
                prices_raw = _json.loads(prices_raw)
            except Exception:
                prices_raw = []
        if isinstance(outcomes_raw, str):
            try:
                import json as _json
                outcomes_raw = _json.loads(outcomes_raw)
            except Exception:
                outcomes_raw = []
        prices: dict[str, float] = {}
        for o, p in zip(outcomes_raw or [], prices_raw or []):
            try:
                prices[str(o).upper()] = float(p)
            except (TypeError, ValueError):
                continue
        # tags appear as inline list of {id,label,slug,...} when ?include_tag=true
        tag_slugs = [
            (t.get("slug") or "").lower()
            for t in (m.get("tags") or [])
            if isinstance(t, dict) and t.get("slug")
        ]
        events = m.get("events") or []
        event_slug = None
        event_title = None
        if events and isinstance(events[0], dict):
            event_slug = events[0].get("slug")
            event_title = events[0].get("title")
        return {
            "market_id": m.get("conditionId") or m.get("id"),
            "question": m.get("question"),
            "category": m.get("category"),
            "slug": m.get("slug"),
            "volume_usd": volume_usd,
            "volume_24h_usd": _to_float(m.get("volume24hr")),
            "liquidity_usd": liquidity_usd,
            "yes_price": prices.get("YES"),
            "no_price": prices.get("NO"),
            "one_day_change": _to_float(m.get("oneDayPriceChange")) if m.get("oneDayPriceChange") is not None else None,
            "one_week_change": _to_float(m.get("oneWeekPriceChange")) if m.get("oneWeekPriceChange") is not None else None,
            "tag_slugs": tag_slugs,
            "event_slug": event_slug,
            "event_title": event_title,
            "closed": bool(m.get("closed")),
            "active": bool(m.get("active")),
            "end_date": m.get("endDate"),
            "raw": m,
        }

    @staticmethod
    def _normalize_trade(t: dict[str, Any]) -> dict[str, Any]:
        size = _to_float(t.get("size"))
        price = _to_float(t.get("price"))
        return {
            "trade_id": t.get("transactionHash"),
            "wallet_address": (t.get("proxyWallet") or "").lower(),
            "market_id": t.get("conditionId"),
            "market_question": t.get("title"),
            "slug": t.get("slug"),
            "side": _directional_side(t.get("outcome"), t.get("side")),
            # ``action`` is the raw BUY/SELL — preserved because it's needed to
            # estimate per-market realized cashflow (used by win_rate). ``side``
            # collapses outcome+action to YES/NO directional bet.
            "action": (t.get("side") or "").strip().upper(),
            "amount_usd": size * price,
            "price": price,
            "probability": price,  # for YES tokens, price = implied probability
            "traded_at": _unix_to_iso(t.get("timestamp")),
            "raw": t,
        }

    # ---- public API (drop-in replacement for the previous Nansen client) ----

    def prediction_market_screener(
        self,
        *,
        category: str | None = None,
        limit: int = 10,
        include_closed: bool = True,
        **extra: Any,
    ) -> list[dict[str, Any]]:
        """Top markets, optionally filtered by category. Sorted by total volume desc.

        SPEC §1.1 prefers resolved (closed) markets so PnL is finalized; we fetch
        a closed batch and an active batch and union them, both sorted by volume.
        """
        params: dict[str, Any] = {
            "limit": limit,
            "order": "volumeNum",
            "ascending": "false",
            **extra,
        }
        if category:
            slug = CATEGORY_TAG_SLUG.get(category.lower(), category.lower())
            params["tag_slug"] = slug

        out: list[dict[str, Any]] = []
        seen: set[str] = set()

        if include_closed:
            closed_params = {**params, "closed": "true"}
            try:
                closed = self._get(GAMMA_BASE, "/markets", closed_params) or []
            except Exception as exc:
                log.warning("gamma /markets closed failed: %s", exc)
                closed = []
            for m in (closed if isinstance(closed, list) else []):
                norm = self._normalize_market(m)
                if norm["market_id"] and norm["market_id"] not in seen:
                    seen.add(norm["market_id"])
                    out.append(norm)

        active_params = {**params, "active": "true", "closed": "false"}
        try:
            active = self._get(GAMMA_BASE, "/markets", active_params) or []
        except Exception as exc:
            log.warning("gamma /markets active failed: %s", exc)
            active = []
        for m in (active if isinstance(active, list) else []):
            norm = self._normalize_market(m)
            if norm["market_id"] and norm["market_id"] not in seen:
                seen.add(norm["market_id"])
                out.append(norm)

        out.sort(key=lambda r: r["volume_usd"], reverse=True)
        return out[: limit * 2]  # keep both batches' worth, dedup'd

    def active_markets_with_tags(
        self,
        *,
        limit: int = 200,
        order: str = "volume24hr",
        ascending: bool = False,
    ) -> list[dict[str, Any]]:
        """Active+open markets sorted by ``order`` (default ``volume24hr`` desc), tags inline.

        Used by the daily snapshot. ``include_tag=true`` makes Gamma return the
        ``tags`` array on each market so we can bucket client-side without a
        per-market follow-up call.
        """
        params: dict[str, Any] = {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "order": order,
            "ascending": "true" if ascending else "false",
            "include_tag": "true",
        }
        try:
            data = self._get(GAMMA_BASE, "/markets", params) or []
        except Exception as exc:
            log.warning("gamma /markets active+tags failed: %s", exc)
            return []
        rows = data if isinstance(data, list) else []
        return [self._normalize_market(m) for m in rows if (m.get("conditionId") or m.get("id"))]

    def prediction_market_pnl_leaderboard(
        self, *, market_id: str, top_n: int = 50, **extra: Any  # noqa: ARG002
    ) -> list[dict[str, Any]]:
        """Top wallets by PnL for a single market.

        Polymarket's Data API does not expose ``/positions`` indexed by market
        alone; it requires ``user``. So this is a two-step join:

        1. ``/holders?market={cid}&limit=top_n`` → top current holders per token
           (response is a list of 2 entries — one per outcome token YES/NO).
        2. For each unique holder, ``/positions?user={addr}&market={cid}``
           returns their actual ``cashPnl`` / ``realizedPnl``. Sum across the
           wallet's YES + NO positions (rare but possible to hold both).

        Then sort client-side by total ``cashPnl``. This is per-market work
        proportional to ``top_n``; the watchlist build aggregates many
        markets so the total request count is roughly
        ``markets × (1 + 2·top_n)``.
        """
        try:
            holders_data = self._get(
                DATA_BASE, "/holders", {"market": market_id, "limit": top_n}
            ) or []
        except Exception as exc:
            log.warning("data /holders market=%s failed: %s", market_id, exc)
            return []

        addresses: list[str] = []
        seen: set[str] = set()
        for token_entry in holders_data if isinstance(holders_data, list) else []:
            for h in token_entry.get("holders", []) or []:
                addr = (h.get("proxyWallet") or "").lower()
                if addr and addr not in seen:
                    seen.add(addr)
                    addresses.append(addr)

        rows: list[dict[str, Any]] = []
        for addr in addresses:
            try:
                positions = self._get(
                    DATA_BASE, "/positions", {"user": addr, "market": market_id}
                ) or []
            except Exception as exc:
                log.debug("data /positions user=%s market=%s failed: %s", addr, market_id, exc)
                continue
            if not isinstance(positions, list) or not positions:
                continue

            cash_pnl = sum(_to_float(p.get("cashPnl")) for p in positions)
            realized_pnl = sum(_to_float(p.get("realizedPnl")) for p in positions)
            # ``initialValue`` is the USD cost basis at acquisition — the most
            # reliable per-position USD volume marker. ``totalBought`` is in
            # share units and only multiplies cleanly to USD when there's an
            # open position with avgPrice > 0; for fully-redeemed positions
            # it falls to zero. Prefer initialValue and fall back accordingly.
            initial_value = sum(_to_float(p.get("initialValue")) for p in positions)
            if initial_value > 0:
                volume_usd = initial_value
            else:
                total_bought = sum(_to_float(p.get("totalBought")) for p in positions)
                avg_price_num = sum(
                    _to_float(p.get("avgPrice")) * _to_float(p.get("size")) for p in positions
                )
                size_total = sum(_to_float(p.get("size")) for p in positions)
                avg_price = (avg_price_num / size_total) if size_total else 0.0
                volume_usd = total_bought * avg_price

            rows.append({
                "address": addr,
                "pnl_usd": cash_pnl,
                "realized_pnl_usd": realized_pnl,
                "volume_usd": volume_usd,
                "market_id": market_id,
                "raw": positions,
            })

        rows.sort(key=lambda r: r["pnl_usd"], reverse=True)
        return rows[:top_n]

    def prediction_market_address_trades(
        self,
        *,
        wallet_address: str,
        since: str | None = None,  # noqa: ARG002 — kept for interface compat
        limit: int = 200,
        **extra: Any,
    ) -> list[dict[str, Any]]:
        """Recent trades for a wallet, newest first.

        Polymarket's Data API has no ``since`` parameter; we fetch the most-recent
        ``limit`` trades and the tracker dedupes against ``poll_state``.
        """
        params: dict[str, Any] = {
            "user": wallet_address,
            "limit": limit,
            **extra,
        }
        try:
            data = self._get(DATA_BASE, "/trades", params) or []
        except Exception as exc:
            log.warning("data /trades user=%s failed: %s", wallet_address, exc)
            return []
        rows = data if isinstance(data, list) else []
        return [self._normalize_trade(t) for t in rows if t.get("transactionHash")]

    # The lb-api caps each call at 50 entries and ignores ``offset``/``page``
    # (verified empirically). Use ``top_profit_traders_union`` to expand the
    # pool by unioning multiple time windows.
    LB_VALID_WINDOWS = ("All", "30d", "7d", "1d")

    def top_profit_traders(
        self, *, window: str = "All", limit: int = 50
    ) -> list[dict[str, Any]]:
        """Polymarket's public profit leaderboard for a single window.

        Returns top traders by realized profit. Polymarket's own definition of
        "who's actually winning". Surfaces whales who have already cashed out
        (Theo4, Fredi9999, …) — they wouldn't appear in ``/holders``.

        ``window`` accepts ``"All"``, ``"30d"``, ``"7d"``, ``"1d"`` (the only
        values accepted by lb-api at time of writing). The endpoint caps the
        response at 50 entries regardless of ``limit``.
        """
        try:
            data = self._get(LB_BASE, "/profit", {"window": window, "limit": limit}) or []
        except Exception as exc:
            log.warning("lb /profit window=%s failed: %s", window, exc)
            return []
        rows = data if isinstance(data, list) else []
        return [
            {
                "address": (r.get("proxyWallet") or "").lower(),
                "pnl_usd": _to_float(r.get("amount")),
                "name": r.get("name") or r.get("pseudonym") or "",
                "pseudonym": r.get("pseudonym") or "",
                "window": window,
                "raw": r,
            }
            for r in rows
            if r.get("proxyWallet")
        ]

    def top_profit_traders_union(
        self, *, windows: tuple[str, ...] = LB_VALID_WINDOWS
    ) -> list[dict[str, Any]]:
        """Union the profit leaderboard across multiple windows.

        Each window returns up to 50 unique wallets; combining ``All`` (lifetime
        whales), ``30d``/``7d``/``1d`` (currently-hot traders) yields ~120–140
        unique candidates — the best we can do given the lb-api's 50-entry cap.

        For wallets appearing in multiple windows, keeps the entry with the
        highest ``pnl_usd`` (typically the ``All`` window).
        """
        merged: dict[str, dict[str, Any]] = {}
        for w in windows:
            for entry in self.top_profit_traders(window=w, limit=50):
                addr = entry["address"]
                prev = merged.get(addr)
                if prev is None or entry["pnl_usd"] > prev["pnl_usd"]:
                    merged[addr] = entry
        return list(merged.values())

    # ---- bonus (not required by SPEC, useful for embed enrichment) ----

    def market_meta(self, market_id: str) -> dict[str, Any]:
        """One-shot market lookup by conditionId.

        Returns ``{"slug": str|None, "question": str|None, "prices": {YES: 0.42, NO: 0.58}}``.
        Empty dict on failure. Used by the convergence formatter to build a
        clickable embed title and show the current YES/NO probability.
        """
        try:
            data = self._get(GAMMA_BASE, "/markets", {"condition_ids": market_id}) or []
        except Exception as exc:
            log.debug("gamma market_meta market=%s failed: %s", market_id, exc)
            return {}
        rows = data if isinstance(data, list) else []
        if not rows:
            return {}
        m = rows[0]

        outcomes = m.get("outcomes") or []
        prices_raw = m.get("outcomePrices") or "[]"
        if isinstance(outcomes, str):
            try:
                import json
                outcomes = json.loads(outcomes)
            except Exception:
                outcomes = []
        if isinstance(prices_raw, str):
            try:
                import json
                prices_raw = json.loads(prices_raw)
            except Exception:
                prices_raw = []

        prices: dict[str, float] = {}
        for o, p in zip(outcomes, prices_raw):
            try:
                prices[str(o).upper()] = float(p)
            except (TypeError, ValueError):
                continue

        return {
            "slug": m.get("slug"),
            "question": m.get("question"),
            "prices": prices,
        }

    def market_current_prices(self, market_id: str) -> dict[str, float]:
        """Backward-compatible alias for the prices field of ``market_meta``."""
        return self.market_meta(market_id).get("prices", {})
