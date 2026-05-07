# Reusing this Stack for Other Bots

This repo's "daily snapshot" pieces are intentionally domain-agnostic. To
build another **Discord + X bot that posts a generated image card on a
schedule**, copy this repo and replace just the data-collection layer.

## What's reusable as-is

These modules don't care what the data is — only the shape (a list of
``SnapshotRow``-like rows with ``slug`` / ``question`` / a numeric value
/ a delta). Keep them, configure them, ship.

| File | Reuse |
|---|---|
| [src/discord_client.py](src/discord_client.py) | Discord webhook poster with image attachment |
| [src/daily_snapshot/x_client.py](src/daily_snapshot/x_client.py) | X (Twitter) v2 tweet + v1.1 media upload via tweepy |
| [src/daily_snapshot/image_renderer.py](src/daily_snapshot/image_renderer.py) | 1200×720 dark card with sections, bullets, Δ coloring |
| [src/daily_snapshot/jp_translator.py](src/daily_snapshot/jp_translator.py) | Gemini / Claude translator with SQLite cache |
| [src/daily_snapshot/formatter.py](src/daily_snapshot/formatter.py) | Discord embed + X tweet text fallback |
| [scripts/_ci_state.sh](scripts/_ci_state.sh) | SQLite persistence across GH Actions runs (`bot-state` branch) |
| [.github/workflows/daily-snapshot.yml](.github/workflows/daily-snapshot.yml) | Cron 3×/day + workflow_dispatch + Noto CJK install |

## What you swap per project

| File | Swap to |
|---|---|
| `src/polymarket_client.py` | Your data-source client (Nansen, CoinGecko, custom REST API…) |
| `src/daily_snapshot/collector.py` | The screener / categorization for **your** domain |
| `config/settings.yaml` → `daily_snapshot:` block | Categories, excluded tags, label aliases |
| The 3-tuple of section names in `image_renderer.py` | Your section headers + emojis |

## Minimum new-project recipe

1. **Click "Use this template"** on the GitHub repo (Settings → General →
   Template repository ✓ to enable, then the green button appears at the
   top of the repo). Or fork. Or `git clone` and re-init.

2. **Replace the data layer.** Implement two things:
   - A client class that returns rows of `(market_id, slug, question,
     value, delta_24h, category, …)`. Mimic ``PolymarketClient.active_markets_with_tags``.
   - A collector that filters / dedupes / categorizes those rows. Mimic
     ``collect_snapshot``, ``top_movers``, ``by_category``.

3. **Tweak the card.** Edit ``image_renderer.py``:
   - Change `W, H = 1200, 720` if you want a different aspect ratio.
   - Change the `sections` tuple (emoji + title) to match your domain
     (e.g. `("📈", "Macro indicators", …)`, `("⚡", "Hot tokens", …)`).
   - Header title string ("Polymarket Daily Snapshot" → your bot name).

4. **Provision GitHub.**
   - Create a new GitHub repo (Public OK — secrets stay encrypted).
   - Push the code.
   - In repo Settings → Secrets and variables → Actions, add **7 secrets**:
     - `DISCORD_WEBHOOK_URL`
     - `DAILY_SNAPSHOT_DISCORD_WEBHOOK_URL` (can equal the above)
     - `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET`
     - `GEMINI_API_KEY` *(optional — drops to English-truncation labels if missing)*

5. **Test.** GitHub repo → Actions → `daily-snapshot` → Run workflow with
   `dry_run: true` (no real posting). Inspect the rendered image in the
   logs / Discord channel. Then run with `dry_run: false`.

6. **Schedule.** The workflow cron is `5 15,23,7 * * *` UTC = 00:05 /
   08:05 / 16:05 JST. Edit `.github/workflows/daily-snapshot.yml` if you
   want a different cadence.

## Architecture cheatsheet

```
[GitHub Actions cron]
        ↓
   [Restore SQLite from `bot-state` branch]
        ↓
   [collect_snapshot]  ←  domain-specific data client
        ↓
   [top_movers / by_category]  ←  ranking + dedup
        ↓
   [Gemini translator]  ←  English question → Japanese label (cached)
        ↓
   [render_snapshot_png]  ←  Pillow + pilmoji + Noto CJK
        ↓
   [Discord webhook (image)]   [X media_upload + create_tweet (image)]
        ↓
   [Persist SQLite to `bot-state` branch]
```

Per-run cost: Polymarket Gamma 1 GET, Gemini ~9 short translations (free
tier), Discord 1 POST, X 1 media upload + 1 tweet POST. Total wall time
~30 sec on `ubuntu-latest`.

## Common adaptations

**Different chains / networks (e.g. Solana on-chain).** Keep everything
except `polymarket_client.py`. Write a Helius / Jupiter client returning
the same row shape. Drop `gamma-api.polymarket.com` from User-Agent.

**Different post cadence.** Edit the three `cron:` lines in
`.github/workflows/daily-snapshot.yml`. Cron is **UTC** — subtract 9 hours
to get the JST equivalent. Use `:05` minute to dodge the GH Actions
top-of-hour rush.

**Different platforms (Slack / Telegram / Mastodon instead of X).**
Replace `x_client.py` with a Slack/Telegram client. The image bytes
returned by `render_snapshot_png` post directly to Slack `files.upload`
or Telegram `sendPhoto`. Workflow secrets change accordingly.

**More than 3 sections.** Card is 720px high with 3 sections × 3 rows.
For 4 sections, bump `H = 800` and add a row. For 5+, switch to
`H = 1080` (square) and consider 2-column layout.

**Different theme.** All colors are constants at the top of
`image_renderer.py` (`BG`, `CARD`, `TEXT`, `GREEN`, `RED`, `ACCENT`).
Swap to taste — light theme: `BG=(245,247,250) TEXT=(20,24,33) DIM=(100,110,124)`.

## Known gotchas

- **Emoji rendering.** `pilmoji` falls back to network fetch (Twemoji CDN).
  GH Actions runners have outbound HTTPS, so this works. Some emoji codepoints
  (e.g. 🏛 CLASSICAL BUILDING) render as tofu via Twemoji — substitute
  with a more reliable equivalent (we use 🏦 for Macro).
- **Japanese fonts.** GH `ubuntu-latest` doesn't ship CJK fonts; the workflow
  installs `fonts-noto-cjk` at the start. Locally, Windows ships Yu Gothic.
- **GH Actions cron drift.** Up to 15 min late under load. Acceptable for
  daily snapshots; not for tight-deadline triggers.
- **X Free tier limits.** 17 tweets/24h, 1500/month. Daily snapshot at
  3×/day uses 90/month — well under.
- **Gemini free tier limits.** 1500 RPD on `gemini-2.5-flash-lite`. Daily
  snapshot uses ~9/run cold, much less with cache hits.
- **`bot-state` branch.** Force-pushed every run. Don't put unrelated
  files on it. Rotate or delete to wipe the translation cache.
