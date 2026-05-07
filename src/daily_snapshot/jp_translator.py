"""Japanese label generation for Polymarket markets.

Calls Claude Haiku 4.5 to translate English ``question`` strings to short
Japanese labels (e.g. "FOMC 12月-25bp", "BTC ETF $100B by EOY"). Results are
cached to SQLite by market slug — the question text is stable per market,
so a slug we've translated before never gets re-translated.

Fallback chain on ``build_label_map``:
1. Manual `display_aliases` (operator-curated) — always wins
2. SQLite cache lookup (`market_jp_label` table)
3. Fresh API call (one batched call for all cache misses)
4. Empty dict on API failure → formatter falls back to English truncation
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Iterable

from pydantic import BaseModel, Field

from .collector import SnapshotRow

log = logging.getLogger(__name__)


# Embedded examples chosen to match the screenshot's house style: tickers in
# English, qualifiers in Japanese, dates compressed. The model imitates these
# more reliably than free-form rules.
SYSTEM_PROMPT = """You generate short Japanese labels for Polymarket prediction-market questions, used in a daily Discord/X snapshot.

Style:
- Mix English keywords (BTC, SOL, ETH, FOMC, ETF, Trump, Fed, CPI, dollar amounts) with Japanese qualifiers.
- 8-20 visible characters preferred (CJK width).
- Preserve numeric values, tickers, and named entities verbatim in English/Latin.
- Use Japanese for verbs/qualifiers: 承認 / 到達 / 削減 / リセッション / 確認 / 突破 / 達成.
- Drop "Will ... ?" framing — output a noun phrase, not a question.
- Drop dates if implied; keep year (2026年内) if essential to the bet.
- No punctuation at the end. No quotes around the output.

Examples:
- "Will SOL ETF be approved in 2026?" → "SOL ETF 2026年内承認"
- "Will the Fed cut 25bp in December?" → "FOMC 12月-25bp"
- "Will Bitcoin ETF reach $100B AUM by EOY?" → "BTC ETF $100B by EOY"
- "Will the US enter recession in 2026?" → "US 2026年内リセッション"
- "Will ETH staking ratio reach 38%?" → "ETH staking 比率38%"
- "Will Bitcoin hit $150k by June 30, 2026?" → "BTC $150k 6月末まで"
- "Will Donald Trump announce US blockade of Hormuz lifted by May 8?" → "Trump ホルムズ封鎖解除5/8"
- "Strait of Hormuz traffic returns to normal by May 15?" → "ホルムズ通航正常化5/15"
"""


class TranslatedLabel(BaseModel):
    slug: str = Field(description="Echo of the input slug; used to map back.")
    label: str = Field(description="Short Japanese label, 8-20 chars preferred.")


class TranslationBatch(BaseModel):
    translations: list[TranslatedLabel]


def _read_cache(conn: sqlite3.Connection, slugs: Iterable[str]) -> dict[str, str]:
    slugs = [s for s in slugs if s]
    if not slugs:
        return {}
    placeholders = ",".join("?" * len(slugs))
    rows = conn.execute(
        f"SELECT slug, label FROM market_jp_label WHERE slug IN ({placeholders})",
        list(slugs),
    ).fetchall()
    return {r["slug"]: r["label"] for r in rows}


def _write_cache(
    conn: sqlite3.Connection,
    items: list[tuple[str, str, str, str]],  # (slug, label, source, question)
) -> None:
    if not items:
        return
    conn.executemany(
        """
        INSERT OR REPLACE INTO market_jp_label (slug, label, source, question)
        VALUES (?, ?, ?, ?)
        """,
        items,
    )
    conn.commit()


def _build_user_prompt(items: list[tuple[str, str]]) -> str:
    payload = json.dumps(
        [{"slug": slug, "question": q} for slug, q in items],
        ensure_ascii=False,
    )
    return (
        "Translate each item to a short Japanese label. "
        "Echo each `slug` in your output so the caller can map back.\n\n"
        f"Items (JSON):\n{payload}"
    )


def _call_claude(
    api_key: str, model: str, items: list[tuple[str, str]]
) -> dict[str, str]:
    """Translate a batch of (slug, question) pairs via Anthropic Messages API."""
    if not items:
        return {}
    try:
        import anthropic  # type: ignore
    except ImportError:
        log.warning("anthropic SDK not installed; skipping JP translation")
        return {}

    client = anthropic.Anthropic(api_key=api_key)
    try:
        resp = client.messages.parse(
            model=model,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_user_prompt(items)}],
            output_format=TranslationBatch,
        )
    except Exception as exc:
        log.warning("claude translate batch failed: %s", exc)
        return {}

    parsed = getattr(resp, "parsed_output", None)
    if not isinstance(parsed, TranslationBatch):
        log.warning("claude translate returned unexpected shape: %r", parsed)
        return {}

    out = {t.slug: t.label.strip() for t in parsed.translations if t.slug and t.label}
    log.info("claude translated %d/%d items", len(out), len(items))
    return out


def _call_gemini(
    api_key: str, model: str, items: list[tuple[str, str]]
) -> dict[str, str]:
    """Translate via Google Gemini with JSON-schema response.

    Free tier on ``gemini-2.0-flash`` is 1,500 RPD as of 2026-Q2 — daily
    snapshot needs <10 calls per run, so this is effectively unmetered.
    """
    if not items:
        return {}
    try:
        from google import genai  # type: ignore
        from google.genai import types as genai_types  # type: ignore
    except ImportError:
        log.warning("google-genai not installed; skipping JP translation")
        return {}

    client = genai.Client(api_key=api_key)
    try:
        resp = client.models.generate_content(
            model=model,
            contents=_build_user_prompt(items),
            config=genai_types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=TranslationBatch,
                temperature=0.3,
                max_output_tokens=2048,
            ),
        )
    except Exception as exc:
        log.warning("gemini translate batch failed: %s", exc)
        return {}

    parsed = getattr(resp, "parsed", None)
    # SDK can return either a parsed Pydantic instance or a dict — handle both.
    if isinstance(parsed, TranslationBatch):
        translations = parsed.translations
    elif isinstance(parsed, dict) and "translations" in parsed:
        try:
            translations = TranslationBatch(**parsed).translations
        except Exception as exc:
            log.warning("gemini parsed dict invalid: %s", exc)
            return {}
    else:
        # Fall back to JSON parse on raw text if structured output didn't bind.
        text = getattr(resp, "text", None)
        if not text:
            log.warning("gemini response had no parsed/text content")
            return {}
        try:
            data = json.loads(text)
            translations = TranslationBatch(**data).translations
        except Exception as exc:
            log.warning("gemini text parse failed: %s; raw=%s", exc, text[:200])
            return {}

    out = {t.slug: t.label.strip() for t in translations if t.slug and t.label}
    log.info("gemini translated %d/%d items", len(out), len(items))
    return out


_PROVIDER_DISPATCH = {
    "anthropic": _call_claude,
    "gemini": _call_gemini,
}


def build_label_map(
    rows: list[SnapshotRow],
    *,
    conn: sqlite3.Connection,
    manual_aliases: dict[str, str],
    api_key: str,
    provider: str = "gemini",
    model: str = "gemini-2.0-flash",
    enable_translation: bool = True,
) -> dict[str, str]:
    """Build slug → JP-label dict for the formatter.

    Order of precedence:
    1. ``manual_aliases`` (operator-curated, always wins)
    2. SQLite cache lookup
    3. Fresh API call to ``provider`` ("gemini" or "anthropic")
    """
    label_map: dict[str, str] = dict(manual_aliases)

    candidate_slugs = [r.slug for r in rows if r.slug and r.slug not in label_map]
    if not candidate_slugs:
        return label_map

    cached = _read_cache(conn, candidate_slugs)
    label_map.update(cached)

    miss_rows = [r for r in rows if r.slug and r.slug not in label_map]
    if not miss_rows:
        log.info("all %d markets resolved from cache", len(candidate_slugs))
        return label_map

    if not enable_translation:
        return label_map
    if not api_key:
        log.info("%s api key not set; skipping JP translation for %d misses",
                 provider, len(miss_rows))
        return label_map

    call = _PROVIDER_DISPATCH.get(provider)
    if call is None:
        log.warning("unknown jp_translation_provider=%r; valid: %s",
                    provider, list(_PROVIDER_DISPATCH))
        return label_map

    items = [(r.slug, r.question) for r in miss_rows if r.slug and r.question]
    fresh = call(api_key, model, items)
    if fresh:
        question_by_slug = {r.slug: r.question for r in miss_rows if r.slug}
        _write_cache(
            conn,
            [
                (slug, label, f"llm:{provider}", question_by_slug.get(slug, ""))
                for slug, label in fresh.items()
            ],
        )
        label_map.update(fresh)

    return label_map
