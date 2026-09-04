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
  `first_seen`, `last_seen`, `price_history`), `sold` listings and their
  `outcomes`, `off_market` entries, recent `run_history` for health checks,
  and `failed_runs`. Each `seen` entry also persists its `evidence_basis`
  (tier/label/count/area/cache fetch date) and the wobble-guard
  `verdict_category` / `pending_verdict` fields.
- **Price history**: every observed price change is appended to a listing's
  `price_history` (bounded). Drives the sparkline and the repeat-drops signal.
- **Off-market / re-listed**: a listing absent for N consecutive runs moves to
  `off_market`; if it reappears it is tagged `RE-LISTED` and its original
  `first_seen` is preserved.
- **Sold / STC**: `sold` listings leave the active dashboard. Sold/STC/Under
  Offer is detected by polling each Rightmove listing's own page every run;
  a `sold`/`stc` outcome moves the listing from `seen` immediately.
- **Evidence grade** (HIGH / MEDIUM / LOW / UNSCORED): quality of the
  comparable-price basis. HIGH = street-level with ≥5 fresh sales, MEDIUM =
  sector-level ≥8 (or thin street-level), LOW = district-tier / thin pools,
  UNSCORED = no comps. Low-grade evidence is *down-weighted* (LOW ×0.5,
  MEDIUM ×0.75) — missing evidence is excluded, weak evidence is never
  counted as full-strength.
- **Deal-quality score** (0-100): how far below asking the buyer can
  realistically land the house, weighted from sold-comparable evidence
  (grade-adjusted), real £/sqft vs EPC-matched comps, price-drop history,
  listing age, and market context. It is *not* "is this overpriced".
- **Clearing estimate**: the card's headline — what the house will actually
  clear at. Asking-anchored; sold comps may pull it down *only* at MEDIUM+
  grade, and never past the grade's negotiation cap. Range width by grade.
- **Negotiation guide**: capped by evidence grade — LOW/UNSCORED evidence
  never advises more than 5% under asking and never says "overpriced".
- **Outcome**: a recorded real-world event for a listing (`sold`, `stc`,
  `lost-bid`, `withdrawn`) with days + optional note, sourced from the user
  (`--outcome` CLI) or auto-detected from listing pages.
- **Comparables**: recent same-type Land Registry sales matched at street level
  for each listing (informational, shown in the breakdown).
- **Market temperature**: rising / stable / cooling per postcode area, from
  time-weighted sold-price averages. Act as the interim demand proxy until
  enough STC history accumulates.
- **Run health**: a run is *degraded* when all sources fail, a source returns 0
  when it normally returns listings, or the filtered count collapses by ≥50%
  vs the rolling median. After `WATCHDOG_FAIL_THRESHOLD` consecutive degraded
  runs, a Telegram watchdog alert fires.
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