# Property Watch — Domain Context

Single-context repository. One deployable: `watch.py`.

## Purpose

Monitor the Heckmondwike housing market for 3-bed terraced / semi-detached
houses within the configured price band, so the buyer doesn't miss a listing
or a price drop.

## Core concepts

- **Listing**: a property currently for sale from a source (OnTheMarket,
  Barkers, or manual). Has an id (`otm-*`, `barkers-*`, `manual-*`), address,
  price, bedrooms, type, agent, url, image, sqft.
- **Filtering**: listings are matched to the configured number of bedrooms,
  price band, and property types before anything else happens.
- **State** (`state.json`, committed): tracks `seen` listings (with
  `first_seen`, `last_seen`, `price_history`), `off_market` entries, recent
  `run_history` for health checks, and `failed_runs`.
- **Price history**: every observed price change is appended to a listing's
  `price_history` (bounded). Drives the sparkline and the repeat-drops signal.
- **Off-market / re-listed**: a listing absent for N consecutive runs moves to
  `off_market`; if it reappears it is tagged `RE-LISTED` and its original
  `first_seen` is preserved.
- **Buying-confidence score** (0-100): weighted blend of area median vs asking,
  £/sqft value, price-drop history, listing age, and market context.
- **Comparables**: recent same-type Land Registry sales matched at street level
  for each listing (informational, shown in the breakdown).
- **Market temperature**: rising / stable / cooling per postcode area, from
  time-weighted sold-price averages.
- **Run health**: a run is *degraded* when all sources fail, a source returns 0
  when it normally returns listings, or the filtered count collapses by ≥50%
  vs the rolling median. After `WATCHDOG_FAIL_THRESHOLD` consecutive degraded
  runs, a Telegram watchdog alert fires.

## Secrets & configuration

- `config.json` is committed **without secrets**.
- Secrets (Gmail app password, Telegram bot token) resolve from environment
  variables (`GMAIL_APP_PASSWORD`, `TELEGRAM_BOT_TOKEN`) first, then from
  `config.json`, then from the git-ignored `config.local.json` overlay.
- CI (GitHub Actions) supplies secrets via repository secrets; local runs keep
  using `config.local.json`.

## Architecture rules

- No secrets in committed files, ever.
- `watch.py` is the single pipeline: fetch → filter → enrich → score → alert →
  state → dashboard + optional `--server` (read-only, binds 127.0.0.1).
- `state.json` and `index.html` are committed so CI can persist state and Pages
  can serve the dashboard from `master`.
- ADRs live in `docs/adr/` when a decision needs recording.