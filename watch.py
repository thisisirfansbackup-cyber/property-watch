#!/usr/bin/env python3
"""Property watch - monitors Heckmondwike area property listings.

Checks OnTheMarket and Barkers for 3-bed terraced/semi-detached houses within
a 5-mile ring of WF16 (12 postcode districts, enforced at filter time) and the
configured price band. Alerts on new listings and price
drops via email and Telegram. Tracks per-property price history, off-market /
re-listed status, and generates an HTML dashboard with a tiered
buying-confidence score, mortgage estimates and market temperature.

Secrets (Gmail app password, Telegram bot token) are resolved from environment
variables first (GMAIL_APP_PASSWORD, TELEGRAM_BOT_TOKEN), then from config.json,
then from the git-ignored config.local.json overlay. Set PROPERTY_WATCH_SKIP_PUSH=1
to skip the automatic git push (CI commits and pushes itself).
"""

import base64
import csv
import io
import json
import os
import re
import shutil
import smtplib
import statistics
import subprocess
import sys
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from curl_cffi import requests as cffi_requests

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
LOCAL_CONFIG_FILE = SCRIPT_DIR / "config.local.json"
STATE_FILE = SCRIPT_DIR / "state.json"
STATE_BAK = SCRIPT_DIR / "state.json.bak"
LOG_FILE = SCRIPT_DIR / "alerts.log"
HTML_FILE = SCRIPT_DIR / "index.html"
SOLD_CACHE_FILE = SCRIPT_DIR / "sold_cache.json"

# Tracking / watchdog tunables
OFF_MARKET_MAX = 30                # most recent "off market" entries kept in state
MISSING_RUNS_BEFORE_OFF_MARKET = 2 # consecutive misses before a listing is marked off-market
PRICE_HISTORY_MAX = 12             # per-listing price points retained
WATCHDOG_FAIL_THRESHOLD = 3        # consecutive degraded runs before a watchdog alert fires

# Sold-price comparables tunables
COMP_MIN_COUNT = 3                 # minimum sales before a comparison tier counts as evidence
COMP_LOOKBACK_DAYS = 730           # sold prices older than this are ignored
COMP_LIMIT = 5                     # comparables shown per property
EPC_CACHE_DAYS = 30                # EPC bedroom data refetched monthly (when a key is configured)
EPC_CACHE_FILE = SCRIPT_DIR / "epc_cache.json"

# Evidence-grade tunables (rating-trust redesign, .scratch/rating-trust)
GRADE_WEIGHTS = {"HIGH": 1.0, "MEDIUM": 0.75, "LOW": 0.5}
# Maximum % under asking the negotiation guide may suggest, by grade.
NEGOTIATION_CAPS = {"LOW": 0.05, "MEDIUM": 0.10, "HIGH": 0.15}
# Clearing-estimate range width (half-width fraction of the midpoint) by grade.
ESTIMATE_RANGE = {"HIGH": 0.04, "MEDIUM": 0.08, "LOW": 0.15, "UNSCORED": 0.0}
# Listing source whose detail pages we poll every run for sold/STC markers.
SOLD_CHECK_SOURCES = ("Rightmove",)

# Search area: everything within ~5 miles of WF16. Verified against
# postcodes.io centroid distances (WF16 centroid 53.7102,-1.6696):
#   WF15 1.3mi, WF16 0, WF17 1.3, WF13 1.5, WF14 2.4, WF12 2.8, BD19 2.2,
#   BD11 2.9, LS27 3.8, WF5 4.3, HD6 4.7, BD4 5.0
# Excluded (beyond 5mi): HD5 5.4mi, WF10 13.2mi, BD20 17.3mi.
# OnTheMarket/Barkers have no reliable server-side radius parameter, so the
# ring is enforced here at filter time.
SEARCH_RADIUS_MILES = 5.0
AREA_ALLOWLIST = {
    "WF16", "WF15", "WF17", "WF13", "WF14", "WF12",
    "BD19", "BD11", "LS27", "WF5", "HD6", "BD4",
}

SESSION = cffi_requests.Session(impersonate="chrome")

# Mortgage defaults (first-time buyer, £45k deposit, 4.5% rate, 25 years)
DEPOSIT = 45000
MORTGAGE_RATE = 0.045
MORTGAGE_YEARS = 25
STAMP_DUTY_THRESHOLD = 300000  # First-time buyer pays nothing under £300k


def _deep_merge(base, overlay):
    """Recursively merge overlay dict into base (overlay wins)."""
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config():
    """Load config.json, overlaying the git-ignored config.local.json if present."""
    with open(CONFIG_FILE) as f:
        config = json.load(f)
    if LOCAL_CONFIG_FILE.exists():
        try:
            with open(LOCAL_CONFIG_FILE) as f:
                local = json.load(f)
            config = _deep_merge(config, local)
            log("Loaded local config overlay (config.local.json)")
        except Exception as e:
            log(f"WARNING: could not load config.local.json: {e}")
    return config


def get_secret(config, env_name, cfg_path):
    """Resolve a secret: environment variable first, then config path like 'email.password_app'."""
    value = os.environ.get(env_name)
    if value:
        return value
    node = config
    for key in cfg_path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(key)
        if node is None:
            return None
    return node if isinstance(node, str) and node else None


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "seen": {},
        "off_market": {},
        "run_history": [],
        "failed_runs": 0,
        "last_run": None,
        "last_successful_run": None,
        "market_history": {},
    }


def save_state(state):
    if STATE_FILE.exists():
        shutil.copy2(STATE_FILE, STATE_BAK)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------------------
# Sold price fetching from Land Registry
# ---------------------------------------------------------------------------

def _load_sold_cache():
    if SOLD_CACHE_FILE.exists():
        try:
            with open(SOLD_CACHE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_sold_cache(cache):
    with open(SOLD_CACHE_FILE, "w") as f:
        json.dump(cache, f)


def fetch_sold_prices(postcode_area):
    """Fetch recent sold prices from Land Registry for a postcode area."""
    cache = _load_sold_cache()
    cache_key = postcode_area.upper()
    if cache_key in cache:
        cached = cache[cache_key]
        cached_date = datetime.fromisoformat(cached["fetched"])
        if (datetime.now() - cached_date).days < 7:
            log(f"Sold prices cache hit for {postcode_area}")
            return cached["data"]

    url = "https://landregistry.data.gov.uk/app/ppd/ppd_data.csv"
    start_date = (datetime.now() - timedelta(days=COMP_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    end_date = datetime.now().strftime("%Y-%m-%d")
    params = {
        "register": "England",
        "start_date": start_date,
        "end_date": end_date,
        "postcode": postcode_area.upper(),
        # The endpoint otherwise returns ~100 arbitrary rows and ignores the
        # date range; requesting a huge limit gets the full district history
        # which we filter by date below.
        "limit": "10000",
    }

    log(f"Fetching Land Registry sold prices for {postcode_area}...")
    try:
        r = SESSION.get(url, params=params, timeout=45)
        r.raise_for_status()
    except Exception as e:
        log(f"Land Registry fetch failed: {e}")
        return []

    rows = list(csv.reader(io.StringIO(r.text)))
    # Some PPD exports have a header row, some do not; detect by checking
    # whether the price column of the first row is numeric.
    if rows and (len(rows[0]) < 2 or not rows[0][1].strip().isdigit()):
        rows = rows[1:]

    type_map = {"T": "terraced", "S": "semi-detached", "D": "detached", "F": "flat", "O": "other"}
    tenure_map = {"F": "freehold", "L": "leasehold"}

    results = []
    for row in rows:
        if len(row) < 12:
            continue
        try:
            price = int(row[1])
        except (ValueError, IndexError):
            continue
        date = row[2] if len(row) > 2 else ""
        prop_type = type_map.get(row[4], row[4])
        tenure = tenure_map.get(row[6], row[6])
        paon = (row[8] if len(row) > 8 else "").strip()
        street = row[9] if len(row) > 9 else ""
        town = row[11] if len(row) > 11 else ""

        results.append({
            "price": price,
            "date": date,
            "type": prop_type,
            "tenure": tenure,
            "paon": paon,
            "street": street,
            "town": town,
            "postcode": (row[3] if len(row) > 3 else "").strip(),
        })

    results = [r for r in results if r["date"] >= start_date]
    log(f"Land Registry: {len(results)} recent sold prices ({start_date[:4]}+) for {postcode_area}")

    if not results:
        # Never overwrite a usable cache with an empty fetch (e.g. endpoint
        # silently returning no rows).
        log(f"Land Registry: no rows for {postcode_area} — keeping previous cache")
        return cache.get(cache_key, {}).get("data", [])

    cache[cache_key] = {
        "fetched": datetime.now().isoformat(),
        "data": results,
    }
    _save_sold_cache(cache)

    return results


def extract_postcode_area(address):
    """Extract postcode district (e.g. 'WF15', 'BD19') from an address string."""
    addr = (address or "").strip()
    # Full postcode first (e.g. 'WF16 0AB') -> district part
    match = re.search(r"\b([A-Z]{1,2}\d{1,2}[A-Z]?)\s*\d[A-Z]{2}\b", addr, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    # Bare district token at a word boundary (e.g. 'Cleckheaton, BD19').
    # Two leading letters required so road names like 'A6'/'M62' don't match.
    match = re.search(r"\b([A-Z]{2}\d{1,2}[A-Z]?)\b(?!\s*\d)", addr, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    return ""


# ---------------------------------------------------------------------------
# Time-weighted sold price helpers
# ---------------------------------------------------------------------------

def _time_weight(date_str):
    """Return weight for a sold price based on recency.

    Last 6 months: 1.0, 6-12 months: 0.5, 12+ months: 0.25
    """
    try:
        sale_date = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return 0.25
    age_months = (datetime.now() - sale_date).days / 30
    if age_months <= 6:
        return 1.0
    elif age_months <= 12:
        return 0.5
    else:
        return 0.25


def _weighted_median(sold_prices, prop_type=None):
    """Calculate time-weighted median price from sold prices."""
    filtered = sold_prices
    if prop_type:
        filtered = [s for s in sold_prices if s["type"] == prop_type]
    if not filtered:
        return 0

    # Expand each price by its weight (round to nearest 0.5)
    expanded = []
    for s in filtered:
        w = _time_weight(s["date"])
        count = max(1, round(w * 2) / 2)  # At least 1 entry
        expanded.extend([s["price"]] * int(count * 2))

    return statistics.median(expanded) if expanded else 0


def _weighted_mean(sold_prices, prop_type=None):
    """Calculate time-weighted mean price from sold prices."""
    filtered = sold_prices
    if prop_type:
        filtered = [s for s in sold_prices if s["type"] == prop_type]
    if not filtered:
        return 0

    total_weight = 0
    total_value = 0
    for s in filtered:
        w = _time_weight(s["date"])
        total_value += s["price"] * w
        total_weight += w

    return total_value / total_weight if total_weight > 0 else 0


# ---------------------------------------------------------------------------
# Market temperature
# ---------------------------------------------------------------------------

def calculate_market_temperature(sold_prices, prop_type=None):
    """Determine if the local market is rising, stable, or cooling.

    Compares weighted average of recent sales (last 6 months) vs older sales
    (6-12 months). When ``prop_type`` is given, only sales of that type are
    used (terraced and semi-detached move differently). When either cohort is
    empty the returned dict says so explicitly in ``detail`` instead of
    pretending a stable market.
    """
    pool = sold_prices
    label = "all house types"
    if prop_type:
        pool = [s for s in sold_prices if s.get("type") == prop_type]
        label = prop_type

    recent = [s for s in pool if _time_weight(s["date"]) == 1.0]
    older = [s for s in pool if _time_weight(s["date"]) == 0.5]

    if not recent or not older:
        counts = ", ".join(
            f"{t}: {sum(1 for s in pool if s.get('type') == t)}"
            for t in sorted({s.get("type", "?") for s in pool})
        ) or "no sales"
        return {
            "trend": "stable",
            "change_pct": 0,
            "detail": f"Not enough sales for a {label} trend (last 2 years: {counts})",
            "sales_count": len(pool),
            "prop_type": prop_type,
        }

    recent_avg = statistics.mean(s["price"] for s in recent)
    older_avg = statistics.mean(s["price"] for s in older)

    change_pct = ((recent_avg - older_avg) / older_avg) * 100 if older_avg > 0 else 0

    if change_pct > 2:
        trend = "rising"
    elif change_pct < -2:
        trend = "cooling"
    else:
        trend = "stable"

    return {
        "trend": trend,
        "change_pct": round(change_pct, 1),
        "detail": (
            f"{label.title()} prices {trend} "
            f"({change_pct:+.1f}% over 6 months, {len(recent)}+{len(older)} sales)"
        ),
        "recent_avg": round(recent_avg),
        "older_avg": round(older_avg),
        "recent_count": len(recent),
        "older_count": len(older),
        "sales_count": len(pool),
        "prop_type": prop_type,
    }


# ---------------------------------------------------------------------------
# Negotiation guide
# ---------------------------------------------------------------------------

def calculate_negotiation(listing, sold_prices, comps=None, caps=None):
    """Calculate a fair offer range for a listing.

    Uses the best available comparable tier (same find_comparables ladder as
    the confidence score) so the advice rests on same-type, same-street sales
    when they exist — and says which basis it used.

    ``caps`` bound how far below asking the guide may go (config
    ``negotiation_caps``). At LOW/UNSCORED evidence the guide never says
    "overpriced" and never suggests more than 5% under asking (rating-trust
    issue 02); at MEDIUM it never suggests more than 10% under asking.
    """
    if caps is None:
        caps = NEGOTIATION_CAPS
    else:
        caps = {str(k).upper(): v for k, v in caps.items()}
    if comps is None:
        comps = find_comparables(listing, sold_prices)
    if not comps or not comps.get("median"):
        return {
            "range_text": "Insufficient data",
            "low": 0,
            "high": 0,
            "vs_median": 0,
            "median": 0,
            "label": "insufficient",
            "basis": "",
            "count": 0,
            "grade": "UNSCORED",
        }

    median = comps["median"]
    basis = comps["label"]
    grade = comps.get("grade") or "UNSCORED"

    asking = listing["price"]
    vs_median_pct = ((asking - median) / median) * 100 if median > 0 else 0

    low = 0
    high = 0
    if asking <= median:
        low = int(asking * 0.97)
        high = int(asking)
        text = f"Below average — strong offer &pound;{low:,}&ndash;&pound;{high:,}"
        label = "strong"
    elif vs_median_pct <= 5:
        low = int(median * 0.95)
        high = int(median)
        text = f"Fair offer &pound;{low:,}&ndash;&pound;{high:,}"
        label = "fair"
    elif vs_median_pct <= 15:
        low = int(median)
        high = int(asking * 0.97)
        text = f"Negotiate to &pound;{low:,}&ndash;&pound;{high:,}"
        label = "negotiate"
    else:
        low = int(median * 0.95)
        high = int(median * 0.95)
        text = f"Consider offering &pound;{low:,}"
        label = "overpriced"

    # Grade-aware caps: weak evidence must not justify aggressive lowballs.
    if grade in ("LOW", "UNSCORED"):
        cap = caps.get("LOW", 0.05)
        if label == "overpriced":
            low = high = round(asking * (1 - cap))
            text = f"Consider offering &pound;{low:,}&ndash;&pound;{high:,}"
            label = "negotiate"
        low = max(low, round(asking * (1 - cap)))
    elif grade == "MEDIUM":
        low = max(low, round(asking * (1 - caps.get("MEDIUM", 0.10))))

    return {
        "range_text": text,
        "low": low,
        "high": high,
        "vs_median": round(vs_median_pct, 1),
        "median": median,
        "label": label,
        "basis": basis,
        "count": comps.get("count", 0),
        "grade": grade,
    }


# ---------------------------------------------------------------------------
# Clearing estimate (rating-trust)
# ---------------------------------------------------------------------------

def calculate_estimate(listing, comps, market_temp=None, caps=None, ranges=None):
    """Estimate what a listing will *actually clear at* (rating-trust, issue 03).

    Asking-anchored: starts at the asking price (sellers/agents price to the
    market), then:

      * market temperature nudges it (bounded at +/-3% and only when both a
        recent and an older cohort exist);
      * sold comparables may pull it *down* — only at MEDIUM/HIGH evidence
        grade, and never past the grade's negotiation cap. LOW evidence
        leaves the midpoint at asking; UNSCORED collapses to a point at
        asking.

    Returns {mid, low, high, grade, vs_asking, text}.
    """
    asking = listing["price"]
    if caps is None:
        caps = NEGOTIATION_CAPS
    else:
        caps = {str(k).upper(): v for k, v in caps.items()}
    if ranges is None:
        ranges = ESTIMATE_RANGE

    grade = (comps or {}).get("grade") or "UNSCORED"

    mid = float(asking)
    if comps and comps.get("median") and grade != "LOW":
        median = comps["median"]
        ratio = asking / median if median > 0 else 1
        cap = caps.get(grade, 0.10)
        if ratio > 1 + cap:
            mid = max(float(median), asking * (1 - cap))

    if market_temp and market_temp.get("recent_count") and market_temp.get("older_count"):
        change = market_temp.get("change_pct") or 0
        change = max(-3.0, min(3.0, change))
        mid = mid * (1 + change / 100)

    mid = round(mid)
    half = ranges.get(grade, 0.0)
    low = round(mid * (1 - half))
    high = round(mid * (1 + half))

    return {
        "mid": mid,
        "low": low,
        "high": high,
        "grade": grade,
        "vs_asking": round((mid - asking) / asking * 100, 1) if asking else 0,
        "text": (
            f"Estimate &pound;{low:,}&ndash;&pound;{high:,} "
            f"({grade} evidence) &middot; asking &pound;{asking:,}"
        ),
    }


# ---------------------------------------------------------------------------
# Confidence scoring (dynamic)
# ---------------------------------------------------------------------------

def _smooth_score(ratio, breakpoints):
    """Map a ratio to 0-100 using linear interpolation between breakpoints.

    breakpoints: list of (ratio, score) pairs sorted by ratio ascending.
    Score decreases as ratio increases (cheaper = better).
    """
    if ratio <= breakpoints[0][0]:
        return breakpoints[0][1]
    for i in range(len(breakpoints) - 1):
        r0, s0 = breakpoints[i]
        r1, s1 = breakpoints[i + 1]
        if ratio <= r1:
            t = (ratio - r0) / (r1 - r0) if r1 != r0 else 0
            return s0 + t * (s1 - s0)
    return breakpoints[-1][1]


def calculate_confidence(listing, sold_prices, all_listings, comps=None, epc_map=None, caps=None):
    """Calculate a deal-quality score (0-100) from real evidence only.

    Factors:
      1. Asking price vs sold-price comparables, best evidence tier (30%)
      2. Price per sqft vs EPC-matched comparable floor areas (25%)
      3. Price drop history (15%)
      4. Smart listing age — age × price drop combo (10%)
      5. Market context — vs current listings (20%)

    Uses smooth linear interpolation (no hard cliff edges between scores).
    A factor without evidence is EXCLUDED and the remaining weights are
    rescaled to 100%. Weak (LOW/MEDIUM-grade) comparable evidence is
    down-weighted via GRADE_WEIGHTS rather than counted as full-strength
    (rating-trust issues 02/05).
    ``comps`` is the best comparable tier from find_comparables(); when
    omitted it is derived here.
    """
    if comps is None:
        comps = find_comparables(listing, sold_prices)

    grade = (comps or {}).get("grade") or "UNSCORED"
    grade_mult = GRADE_WEIGHTS.get(grade, 1.0)

    sqft = listing.get("sqft")
    has_sqft = sqft and sqft > 0

    # Base weights
    w1 = 0.30   # sold comps
    w2 = 0.25   # sqft value
    w3 = 0.15   # price drop
    w4 = 0.10   # listing age (smart)
    w5 = 0.20   # market context

    if not has_sqft:
        w2 = 0.0

    active = {}   # factor name -> weight; only factors with real evidence
    breakdown = {}

    # --- Factor 1: Asking price vs sold-price comparables (best tier) ---
    if comps and comps.get("median"):
        median_price = comps["median"]
        avg_price = _weighted_mean(comps["comps"]) if comps.get("comps") else median_price
        ratio = listing["price"] / median_price if median_price > 0 else 1
        factor1 = _smooth_score(ratio, [
            (0.80, 100), (0.90, 85), (1.00, 65),
            (1.10, 40),  (1.20, 15), (1.30, 0),
        ])

        breakdown["area_median"] = {
            "score": factor1,
            "detail": (
                f"Asking is {ratio:.0%} of {comps['count']} sold comps "
                f"({comps['label']}, median &pound;{median_price:,.0f}, "
                f"{grade} evidence)"
            ),
            "median": median_price,
            "avg": avg_price,
            "tier_label": comps["label"],
            "grade": grade,
        }
        # Weak evidence is half-trusted, not full-trusted (rating-trust D3).
        active["area_median"] = w1 * grade_mult
    else:
        breakdown["area_median"] = {
            "score": None,
            "detail": "No comparable sold prices &mdash; price fairness not scored",
        }

    # --- Factor 2: Price per sqft vs EPC-matched comparable floor areas ---
    if has_sqft and "area_median" in active and comps:
        matched = []
        for s in comps.get("comps") or []:
            area = _epc_lookup_floor_area(s, epc_map) if epc_map else None
            if area:
                matched.append((s["price"], float(area)))
        comp_count = len(comps.get("comps") or [])
        coverage = len(matched) / comp_count if comp_count else 0.0
        if matched and coverage >= 0.60:
            per_sqft = [p / (a * 10.7639) for p, a in matched]
            area_sqft_price = statistics.median(per_sqft)
            listing_sqft_price = listing["price"] / sqft
            sqft_ratio = listing_sqft_price / area_sqft_price if area_sqft_price > 0 else 1
            factor2 = _smooth_score(sqft_ratio, [
                (0.80, 100), (0.90, 85), (1.00, 65),
                (1.10, 40),  (1.20, 15), (1.30, 0),
            ])

            breakdown["sqft_value"] = {
                "score": factor2,
                "detail": (
                    f"&pound;{listing_sqft_price:,.0f}/sqft vs "
                    f"&pound;{area_sqft_price:,.0f}/sqft EPC-matched median "
                    f"({len(matched)} comps)"
                ),
            }
            active["sqft_value"] = w2
        else:
            breakdown["sqft_value"] = {
                "score": None,
                "detail": (
                    "No EPC floor-area data for comparables &mdash; "
                    "size value not scored"
                ),
            }
    elif has_sqft:
        breakdown["sqft_value"] = {
            "score": None,
            "detail": "No local sold benchmark &mdash; &pound;/sqft shown for reference only",
        }
    else:
        breakdown["sqft_value"] = {
            "score": None,
            "detail": "Sq ft unknown &mdash; size value not scored",
        }

    # --- Factor 3: Price drop history (repeat drops signal a motivated seller) ---
    old_price = listing.get("old_price")
    history = listing.get("price_history") or []
    drop_count = 0
    if len(history) >= 2:
        for prev, curr in zip(history[:-1], history[1:]):
            if (
                isinstance(prev.get("price"), int)
                and isinstance(curr.get("price"), int)
                and curr["price"] < prev["price"]
            ):
                drop_count += 1
    series_high = None
    if history:
        series_high = max(
            (p.get("price") for p in history if isinstance(p.get("price"), int)),
            default=None,
        )

    ref_price = old_price or series_high
    has_drop = ref_price and ref_price > listing["price"]
    if has_drop:
        drop_pct = (ref_price - listing["price"]) / ref_price
        factor3 = _smooth_score(drop_pct, [
            (0.00, 50), (0.01, 60), (0.03, 70),
            (0.05, 80), (0.08, 90), (0.10, 100),
        ])
        if drop_count >= 2:
            factor3 = min(100, factor3 + 8 * min(drop_count - 1, 3))

        detail = f"Dropped from &pound;{ref_price:,} (&minus;{drop_pct:.0%})"
        if drop_count >= 2:
            detail += f" &middot; {drop_count} drops in tracked history"
        breakdown["price_drop"] = {"score": factor3, "detail": detail}
        active["price_drop"] = w3
    elif history:
        breakdown["price_drop"] = {"score": 50, "detail": "No price drops in tracked history"}
        active["price_drop"] = w3
    else:
        breakdown["price_drop"] = {"score": None, "detail": "No tracked price history yet"}

    # --- Factor 4: Smart listing age (age × price drop combo) ---
    first_seen = listing.get("first_seen")
    has_age = first_seen is not None
    if has_age:
        days_listed = (datetime.now() - datetime.fromisoformat(first_seen)).days

        if has_drop:
            # With price drop: longer = more motivated
            factor4 = _smooth_score(days_listed, [
                (0, 30), (7, 40), (14, 55),
                (30, 70), (45, 85), (60, 100),
            ])
        else:
            # Without price drop: long time = stubborn seller
            factor4 = _smooth_score(days_listed, [
                (0, 30), (7, 40), (14, 55),
                (30, 65), (45, 55), (60, 50),
            ])

        drop_note = " + price drop" if has_drop else ""
        breakdown["listing_age"] = {
            "score": factor4,
            "detail": f"On market {days_listed} days{drop_note}",
            "days": days_listed,
        }
        active["listing_age"] = w4
    else:
        breakdown["listing_age"] = {"score": None, "detail": "Listing age unknown"}

    # --- Factor 5: Market context (vs current listings) ---
    if all_listings and len(all_listings) >= 2:
        all_prices = [l["price"] for l in all_listings]
        market_avg = statistics.mean(all_prices)
        market_min = min(all_prices)
        market_max = max(all_prices)

        ctx_ratio = listing["price"] / market_avg if market_avg > 0 else 1
        factor5 = _smooth_score(ctx_ratio, [
            (0.80, 100), (0.90, 85), (1.00, 65),
            (1.10, 40),  (1.20, 15), (1.30, 0),
        ])

        breakdown["market_context"] = {
            "score": factor5,
            "detail": f"{ctx_ratio:.0%} of current avg (&pound;{market_avg:,.0f})",
        }
        active["market_context"] = w5
    else:
        breakdown["market_context"] = {
            "score": None,
            "detail": "Not enough other listings to compare against",
        }

    # --- Rescale over the factors that actually had evidence ---
    # Weak comparable evidence counts at reduced weight (GRADE_WEIGHTS); the
    # weighted mean stays on the 0-100 scale.
    breakdown["_weights"] = dict(active)
    total_weight = sum(active.values())
    if total_weight > 0:
        score = (
            sum(breakdown[name]["score"] * weight for name, weight in active.items())
            / total_weight
        )
    else:
        score = 0.0
    breakdown["_based_on"] = len(active)
    breakdown["_grade"] = grade

    return round(score), breakdown


def estimate_mortgage(price):
    """Estimate monthly mortgage payment and stamp duty."""
    deposit = min(DEPOSIT, price)
    loan = price - deposit

    monthly_rate = MORTGAGE_RATE / 12
    n_payments = MORTGAGE_YEARS * 12
    if monthly_rate > 0 and loan > 0:
        monthly = loan * (monthly_rate * (1 + monthly_rate) ** n_payments) / (
            (1 + monthly_rate) ** n_payments - 1
        )
    else:
        monthly = 0

    if price <= STAMP_DUTY_THRESHOLD:
        stamp_duty = 0
    else:
        stamp_duty = (price - STAMP_DUTY_THRESHOLD) * 0.05

    legal_survey = 2500
    total_upfront = deposit + stamp_duty + legal_survey

    return {
        "deposit": deposit,
        "loan": loan,
        "monthly": round(monthly),
        "stamp_duty": round(stamp_duty),
        "legal_survey": legal_survey,
        "total_upfront": round(total_upfront),
    }


def _comparables_grade(tier, count, newest_date_str):
    """Rate the *quality* of a comparable-price basis (rating-trust, issue 02).

    HIGH   = street-level (tier 0/1), >= 5 sales, newest within 12 months
    MEDIUM = street-level with 3-4 sales, OR same-sector (tier 2) >= 8 sales
    LOW    = district-tier (tier 3/4) or thin pools
    UNSCORED is *not* produced here — no-comps callers handle it directly.
    """
    if tier in (0, 1):
        fresh = False
        try:
            if newest_date_str:
                fresh = (datetime.now() - datetime.strptime(newest_date_str, "%Y-%m-%d")).days <= 365
        except (ValueError, TypeError):
            fresh = False
        if count >= 5 and fresh:
            return "HIGH"
        if count >= 3:
            return "MEDIUM"
        return "LOW"
    if tier == 2:
        return "MEDIUM" if count >= 8 else "LOW"
    return "LOW"


def find_comparables(listing, sold_prices, epc_map=None):
    """Find honest sold-price comparables using a tiered evidence ladder.

    Tiers (the first with >= COMP_MIN_COUNT sales wins):
      0. 3-bed + same type + same street   (only when EPC bedroom data available)
      1. same type + same street
      2. same type + same postcode sector
      3. same type + whole postcode district
      4. any house type + whole district   (last resort, labelled as such)

    Returns {"tier", "label", "median", "count", "comps"} or None when even
    the last-resort tier lacks COMP_MIN_COUNT sales — callers must treat the
    price factor as missing rather than inventing a neutral value. ``comps``
    holds the most recent COMP_LIMIT sales in the winning tier.
    """
    street = _street_of(listing.get("address"))
    district = extract_postcode_area(listing.get("address") or "")
    ptype = _type_key(listing)

    def _take(pool):
        pool = sorted(pool, key=lambda s: s["date"], reverse=True)
        return pool, _weighted_median(pool)

    tiers = []

    # --- Street-level pools -------------------------------------------------
    same_street = [
        s for s in sold_prices
        if _streets_match(street, (s.get("street") or "").strip().lower())
    ]
    same_street_type = (
        [s for s in same_street if s.get("type") == ptype] if ptype else same_street
    )

    # Tier 0: EPC-verified bedroom match on the street
    if epc_map and listing.get("bedrooms"):
        beds_pool = [
            {**s, "beds": listing["bedrooms"]}
            for s in same_street_type
            if _epc_lookup_bedrooms(s, epc_map) == listing["bedrooms"]
        ]
        if len(beds_pool) >= COMP_MIN_COUNT:
            pool, median = _take(beds_pool)
            tiers.append({
                "tier": 0,
                "label": f"{listing['bedrooms']}-bed, same type, your street",
                "count": len(pool),
                "comps": pool[:COMP_LIMIT],
                "median": median,
            })

    if len(same_street_type) >= COMP_MIN_COUNT:
        pool, median = _take(same_street_type)
        tiers.append({
            "tier": 1,
            "label": "same type, your street" if ptype else "your street",
            "count": len(pool),
            "comps": pool[:COMP_LIMIT],
            "median": median,
        })

    # --- Sector-level pool --------------------------------------------------
    listing_sector = _listing_sector(listing.get("address"))
    if listing_sector:
        sector_pool = [
            s for s in sold_prices
            if (not ptype or s.get("type") == ptype)
            and _postcode_sector(s.get("postcode")) == listing_sector
        ]
        if len(sector_pool) >= COMP_MIN_COUNT:
            pool, median = _take(sector_pool)
            tiers.append({
                "tier": 2,
                "label": (
                    f"same type, sector {listing_sector}"
                    if ptype else f"sector {listing_sector}"
                ),
                "count": len(pool),
                "comps": pool[:COMP_LIMIT],
                "median": median,
            })

    # --- District-level pools ----------------------------------------------
    district_type = (
        [s for s in sold_prices if s.get("type") == ptype] if ptype else list(sold_prices)
    )
    if len(district_type) >= COMP_MIN_COUNT:
        pool, median = _take(district_type)
        area_label = f"{district} district" if district else "local area"
        tiers.append({
            "tier": 3,
            "label": f"same type, {area_label}" if ptype else area_label,
            "count": len(pool),
            "comps": pool[:COMP_LIMIT],
            "median": median,
        })
    elif len(sold_prices) >= COMP_MIN_COUNT:
        pool, median = _take(list(sold_prices))
        area_label = (
            f"all house types, {district} district" if district else "all house types, local area"
        )
        tiers.append({
            "tier": 4,
            "label": area_label,
            "count": len(pool),
            "comps": pool[:COMP_LIMIT],
            "median": median,
        })

    winner = tiers[0] if tiers else None
    if winner is not None:
        newest = (winner.get("comps") or [{}])[0].get("date") or ""
        winner["grade"] = _comparables_grade(winner["tier"], winner["count"], newest)
    return winner


def _type_key(listing):
    """Map a free-text listing type onto a Land Registry type category.

    'End of Terrace' and 'Mid Terrace' both count as terraced; unrecognised
    types return '' so comparables match any type (and say so on the label).
    """
    t = (listing.get("type") or "").lower()
    if "terraced" in t or "terrace" in t:
        return "terraced"
    if "semi" in t:
        return "semi-detached"
    if "detached" in t:
        return "detached"
    if "flat" in t or "apartment" in t:
        return "flat"
    return ""


def _street_of(address):
    """Extract the street name from a listing address.

    Handles 'Cornmill Drive, Liversedge, WF15' as well as numbered forms like
    '12 Powell Street, Heckmondwike WF16 0AB' (leading numbers are stripped).
    """
    first = (address or "").split(",")[0].strip().lower()
    first = re.sub(r"^(flat\s+\S+|apartment\s+\S+)\s*,\s*", "", first)
    first = re.sub(r"^\d+\w{0,2}\s+", "", first)
    first = re.sub(r"\s+", " ", first).strip()
    return first


def _streets_match(a, b):
    """Case-insensitive street comparison with a guarded containment fallback."""
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    return len(shorter) >= 6 and shorter in longer


def _postcode_sector(postcode):
    """'WF16 0AB' -> 'WF16 0'; '' when unparseable."""
    pc = (postcode or "").upper().strip()
    m = re.match(r"([A-Z]{1,2}\d{1,2}[A-Z]?)\s*(\d)", pc)
    return f"{m.group(1)} {m.group(2)}" if m else ""


def _listing_sector(address):
    """Postcode sector of a listing address ('' when it has no full postcode)."""
    m = re.search(r"\b[A-Z]{1,2}\d{1,2}[A-Z]?\s*\d[A-Z]{2}\b", (address or "").upper())
    return _postcode_sector(m.group(0)) if m else ""


def _epc_lookup_bedrooms(sale, epc_map):
    """Bedroom count for a sold record via the EPC map, or None.

    Backward-compatible with caches written before floor areas were stored
    (value is then a plain int; new caches store {"beds": ..., "area_sqm": ...}).
    """
    if not epc_map:
        return None
    key = (
        (sale.get("postcode") or "").upper().replace(" ", ""),
        (sale.get("street") or "").strip().lower(),
        (sale.get("paon") or "").strip().lower(),
    )
    value = epc_map.get(key)
    if value is None:
        return None
    return value["beds"] if isinstance(value, dict) else value


def _epc_lookup_floor_area(sale, epc_map):
    """Floor area (square metres) for a sold record via the EPC map, or None.

    Only available once the cache stores per-record dicts (issue 05). Legacy
    int caches carry no floor area and return None.
    """
    if not epc_map:
        return None
    key = (
        (sale.get("postcode") or "").upper().replace(" ", ""),
        (sale.get("street") or "").strip().lower(),
        (sale.get("paon") or "").strip().lower(),
    )
    value = epc_map.get(key)
    if isinstance(value, dict):
        return value.get("area_sqm")
    return None


def _load_epc_cache():
    if EPC_CACHE_FILE.exists():
        try:
            with open(EPC_CACHE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_epc_cache(cache):
    with open(EPC_CACHE_FILE, "w") as f:
        json.dump(cache, f)


def _epc_settings(config):
    """Return (email, api_key) for the EPC open-data API, or None."""
    api_key = get_secret(config, "EPC_API_KEY", "epc.api_key")
    email = get_secret(config, "EPC_EMAIL", "epc.email")
    if api_key and email:
        return email, api_key
    return None


def fetch_epc_bedrooms(district, config):
    """Build {(postcode, street, paon) -> bedrooms} for a postcode district.

    Uses the free MHCLG EPC open-data API, which needs a free account
    (EPC_EMAIL / EPC_API_KEY env vars, or 'epc.email' / 'epc.api_key' in the
    local config overlay). The sold-price register itself carries no bedroom
    field — this join is the only honest way to filter sold comps by beds.
    Returns None when no key is configured or the service fails; callers then
    fall back to the keyless comparison tiers.
    """
    if not district:
        return None
    key = district.upper()
    cache = _load_epc_cache()
    if key in cache:
        try:
            fetched = datetime.fromisoformat(cache[key]["fetched"])
            if (datetime.now() - fetched).days < EPC_CACHE_DAYS:
                return cache[key].get("map")
        except (ValueError, KeyError, TypeError):
            pass
    auth = _epc_settings(config)
    if not auth:
        return None
    log(f"Fetching EPC bedroom data for {district}...")
    try:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        r = SESSION.get(
            "https://epc.opendatacommunities.org/api/v1/domestic/search",
            params={"outcode": key},
            headers={"Accept": "text/csv", "Authorization": f"Basic {token}"},
            timeout=45,
        )
        r.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(r.text)))
    except Exception as e:
        log(f"EPC fetch failed for {district}: {e}")
        return None

    def _col(row, *needles):
        for col in row:
            up = col.upper()
            if any(n in up for n in needles):
                return row[col]
        return ""

    result = {}
    for row in rows:
        bedrooms = _col(row, "BEDROOM")
        if not bedrooms or not str(bedrooms).strip().isdigit():
            continue
        pc = _col(row, "POSTCODE").upper().replace(" ", "")
        street = _col(row, "ADDRESS2", "ADDRESS3", "STREET").strip().lower()
        paon = _col(row, "ADDRESS1").strip().lower()
        if not (pc and street):
            continue
        # Store floor area when the record carries one (issue 05); legacy
        # format remains a plain int for older caches / records without one.
        floor = _col(row, "TOTAL_FLOOR_AREA", "TOTAL FLOOR AREA")
        if floor and str(floor).strip().replace(".", "", 1).isdigit():
            result[(pc, street, paon)] = {
                "beds": int(bedrooms),
                "area_sqm": float(floor),
            }
        else:
            result[(pc, street, paon)] = int(bedrooms)
    if not result:
        log(f"EPC: no bedroom data parsed for {district} — falling back to keyless tiers")
        return None
    cache[key] = {"fetched": datetime.now().isoformat(), "map": result}
    _save_epc_cache(cache)
    return result


# ---------------------------------------------------------------------------
# Property listing fetching
# ---------------------------------------------------------------------------

def fetch_ontemarket(config):
    """Fetch listings from OnTheMarket search results."""
    filters = config["filters"]
    search = config["search"]

    centre = search["centre"].split(",")[0].strip().lower().replace(" ", "-")
    url = f"https://www.onthemarket.com/for-sale/property/{centre}/"

    params = {
        "max-price": str(filters["max_price"]),
        "min-price": str(filters["min_price"]),
        "bedrooms-min": str(filters["bedrooms"]),
    }

    log(f"Fetching OTM: {url} {params}")
    r = SESSION.get(url, params=params, timeout=20)
    r.raise_for_status()

    listings = []
    for pid, html in re.findall(
        r'<li id="result-(\d+)"[^>]*>(.*?)</li>', r.text, re.DOTALL
    ):
        listing = _parse_otm_listing(pid, html)
        if listing:
            listings.append(listing)

    log(f"OTM: {len(listings)} raw listings parsed")
    return listings


def _parse_otm_listing(pid, html):
    """Parse a single OTM listing from its HTML snippet."""
    price_match = re.search(r'>([\u00a3\xa3][\d,]+)</a>', html)
    if not price_match:
        return None
    price_str = price_match.group(1).replace("\xa3", "").replace("\u00a3", "").replace(",", "")
    try:
        price = int(price_str)
    except ValueError:
        return None

    addr_match = re.search(r'<address[^>]*><span>([^<]+)</span>', html)
    address = addr_match.group(1).strip() if addr_match else ""

    alt_match = re.search(r'alt="(\d+)\s+bedroom\s+([^"]+?)\s+for sale', html, re.IGNORECASE)
    if alt_match:
        beds = int(alt_match.group(1))
        prop_type = alt_match.group(2).strip()
    else:
        beds_match = re.search(r'numberOfBedrooms[^>]*>.*?</svg>\s*(\d+)', html, re.DOTALL)
        beds = int(beds_match.group(1)) if beds_match else 0
        prop_type = "unknown"

    agent_match = re.search(r'font-bold text-white font-normal">\s*([^<]+)', html)
    agent = agent_match.group(1).strip() if agent_match else "Unknown"

    img_match = re.search(
        r'srcSet="(https://media\.onthemarket\.com/properties/[^"]+\.webp)', html
    )
    if not img_match:
        img_match = re.search(
            r'src="(https://media\.onthemarket\.com/properties/[^"]+\.jpg)', html
        )
    image = img_match.group(1) if img_match else ""

    return {
        "id": f"otm-{pid}",
        "source": "OnTheMarket",
        "address": address,
        "price": price,
        "bedrooms": beds,
        "type": prop_type,
        "url": f"https://www.onthemarket.com/details/{pid}/",
        "agent": agent,
        "image": image,
        "sqft": None,
    }


def fetch_barkers(config):
    """Fetch listings from Barkers Estate Agents."""
    listings = []
    start = 0
    max_pages = 10

    while start < max_pages * 12:
        url = "https://www.barkersestateagents.co.uk/properties-for-sale"
        params = {"filter_cat": "1"}
        if start > 0:
            params["start"] = str(start)

        log(f"Fetching Barkers page (start={start})")
        try:
            r = SESSION.get(url, params=params, timeout=20)
            r.raise_for_status()
        except Exception as e:
            log(f"Barkers fetch error: {e}")
            break

        page_listings = _parse_barkers_page(r.text)
        listings.extend(page_listings)

        if f"start={start + 12}" not in r.text:
            break
        start += 12

    log(f"Barkers: {len(listings)} raw listings parsed")
    return listings


def _parse_barkers_page(html):
    """Parse property listings from a Barkers search results page."""
    listings = []

    listing_blocks = re.split(r'<div[^>]*id="eapow-listing-(\d+)"', html)

    for i in range(1, len(listing_blocks), 2):
        lid = listing_blocks[i]
        block = listing_blocks[i + 1] if i + 1 < len(listing_blocks) else ""

        next_listing = block.find('id="eapow-listing-')
        if next_listing > 0:
            block = block[:next_listing]

        link_match = re.search(
            r'href="(/properties-for-sale/property/[^"]+)"', block
        )
        if not link_match:
            continue
        link = link_match.group(1)

        addr_match = re.search(
            r'<h3>\s*<a[^>]*>\s*(.*?)\s*</a>', block, re.DOTALL
        )
        if addr_match:
            address = re.sub(r'<[^>]+>', ' ', addr_match.group(1)).strip()
            address = re.sub(r'\s+', ' ', address)
        else:
            address = "Unknown"

        price_match = re.search(
            r'eapow-overview-price[^>]*>[^£]*£([\d,]+)', block
        )
        if not price_match:
            continue
        try:
            price = int(price_match.group(1).replace(",", ""))
        except ValueError:
            continue

        beds_match = re.search(
            r'flaticon-bed[^>]*>.*?<span class="IconNum">\s*(\d+)', block, re.DOTALL
        )
        beds = int(beds_match.group(1)) if beds_match else 0

        type_match = re.search(
            r'alt="(\d+)\s+bed\s+(\w[^"]*?)\s+in\s+', block, re.IGNORECASE
        )
        prop_type = type_match.group(2).strip() if type_match else "unknown"

        listings.append({
            "id": f"barkers-{lid}",
            "source": "Barkers",
            "address": address,
            "price": price,
            "bedrooms": beds,
            "type": prop_type,
            "url": f"https://www.barkersestateagents.co.uk{link}",
            "agent": "Barkers Estate Agents",
            "image": "",
            "sqft": None,
        })

    return listings


def filter_listings(listings, config):
    """Apply client-side filters: excluded ids, 5-mile ring, property type, beds, price."""
    filters = config["filters"]
    excluded = set(config.get("excluded_ids", []))
    target_types = [t.lower() for t in filters["property_types"]]
    allowed_beds = filters["bedrooms"]
    min_price = filters["min_price"]
    max_price = filters["max_price"]

    filtered = []
    outside = []
    excluded_count = 0
    for listing in listings:
        if listing["id"] in excluded:
            excluded_count += 1
            continue
        if listing["bedrooms"] != allowed_beds:
            continue
        if listing["price"] < min_price or listing["price"] > max_price:
            continue
        ptype = listing["type"].lower()
        if not any(t in ptype for t in target_types):
            continue
        area = extract_postcode_area(listing["address"])
        if area and area not in AREA_ALLOWLIST:
            outside.append(listing)
            continue
        filtered.append(listing)

    if excluded_count:
        log(f"Excluded {excluded_count} listing(s) matching excluded_ids")
    if outside:
        log(
            f"Excluded {len(outside)} listing(s) outside the "
            f"{SEARCH_RADIUS_MILES:g}-mile WF16 ring "
            f"(allowed: {', '.join(sorted(AREA_ALLOWLIST))})"
        )
    return filtered


def enrich_with_sqft(listings):
    """Fetch sq ft from OTM detail pages for each listing."""
    for listing in listings:
        if listing["source"] != "OnTheMarket":
            continue
        pid = listing["id"].replace("otm-", "")
        try:
            r = SESSION.get(
                f"https://www.onthemarket.com/details/{pid}/", timeout=15
            )
            if r.status_code == 200:
                sqft_match = re.search(r'"minimumAreaSqFt":(\d+)', r.text)
                if sqft_match:
                    listing["sqft"] = int(sqft_match.group(1))
                sqm_match = re.search(r'"minimumAreaSqM":(\d+)', r.text)
                if sqm_match:
                    listing["sqm"] = int(sqm_match.group(1))
        except Exception as e:
            log(f"  sqft fetch failed for {pid}: {e}")

    log(f"Enriched {sum(1 for l in listings if l.get('sqft'))} listings with sq ft")
    return listings


def find_alerts(current_listings, state):
    """Compare current listings against state to find new and price-dropped."""
    new_listings = []
    price_drops = []
    seen = state.get("seen", {})

    for listing in current_listings:
        lid = listing["id"]
        if lid not in seen:
            new_listings.append(listing)
        elif listing["price"] < seen[lid]["price"]:
            price_drops.append({**listing, "old_price": seen[lid]["price"]})

    return new_listings, price_drops


# ---------------------------------------------------------------------------
# Sold / STC detection (rating-trust, issue 06)
# ---------------------------------------------------------------------------

def _match_sold_marker(text):
    """Detect a sold/STC/under-offer marker in listing HTML text.

    Returns one of "stc", "under_offer", "sold", or None. Comparative
    phrases ("sold prices", "sold price history", "recently sold") and the
    "recently sold & under offer" navigation widget (which appears on every
    Rightmove listing page) must not trigger a sale — the page is still for
    sale.
    """
    if not text:
        return None
    lower = text.lower()
    # Strip the "recently sold & under offer" navigation widget that appears
    # on every listing page — it is not a status marker for this property.
    lower = lower.replace("recently sold & under offer", "")
    lower = lower.replace("recently sold and under offer", "")
    if "sold stc" in lower:
        return "stc"
    if "under offer" in lower:
        return "under_offer"
    if re.search(r"\bsold\b", lower) and not any(
        skip in lower
        for skip in ("sold price", "sold prices", "sold history", "recently sold")
    ):
        return "sold"
    return None


def detect_listing_status(listing):
    """Poll a listing's own detail page for a sold/STC/removed status.

    Returns "stc" / "under_offer" / "sold" / "removed" / None (still listed)
    / "error" (transient failure). Errors never imply a sale — the run's
    existing miss-counter handles transient disappearances.
    """
    url = listing.get("url") or ""
    if not url:
        return None
    try:
        r = SESSION.get(url, timeout=20)
    except Exception as e:
        log(f"  status check failed for {listing.get('id')}: {e}")
        return "error"
    if r.status_code in (404, 410):
        return "removed"
    if r.status_code != 200:
        return "error"
    return _match_sold_marker(r.text)


def _check_sold_statuses(listings, state):
    """Check tracked listings for sold/STC/removed. Returns observed events.

    Removal requires two consecutive "removed" polls (transient 404s happen);
    sold/STC/under-offer fire immediately. Fetch errors leave state untouched.
    """
    seen = state.setdefault("seen", {})
    events = []
    for listing in listings:
        lid = listing["id"]
        if listing.get("source") not in SOLD_CHECK_SOURCES:
            continue
        status = detect_listing_status(listing)
        if status == "error":
            continue
        entry = seen.get(lid)
        if status == "removed":
            if entry is None:
                events.append({**listing, "status": "removed"})
            else:
                entry["removed_misses"] = entry.get("removed_misses", 0) + 1
                if entry["removed_misses"] >= 2:
                    events.append({**listing, "status": "removed"})
            continue
        if entry is not None:
            entry["removed_misses"] = 0
        if status in ("stc", "under_offer", "sold"):
            events.append({**listing, "status": status})
    return events


def _record_sold(state, events):
    """Move detected-sold listings from ``seen`` into ``sold`` + ``outcomes``.

    Returns alert rows [{id, address, price, status, days_on_market}] for the
    caller to log/notify.
    """
    seen = state.setdefault("seen", {})
    sold = state.setdefault("sold", {})
    outcomes = state.setdefault("outcomes", {})
    rows = []
    now_iso = datetime.now().isoformat()
    for ev in events:
        lid = ev["id"]
        entry = seen.pop(lid, {})
        first_seen = entry.get("first_seen") or now_iso
        try:
            days_on_market = max((datetime.now() - datetime.fromisoformat(first_seen)).days, 0)
        except ValueError:
            days_on_market = 0
        status = ev["status"]
        sold[lid] = {
            "address": entry.get("address") or ev.get("address"),
            "price": entry.get("price") or ev.get("price"),
            "sqft": entry.get("sqft"),
            "source": entry.get("source") or ev.get("source"),
            "first_seen": first_seen,
            "sold_date": now_iso,
            "status": status,
            "days_on_market": days_on_market,
            "evidence_basis": entry.get("evidence_basis"),
        }
        outcomes.setdefault(lid, []).append({
            "date": now_iso,
            "status": status,
            "days": days_on_market,
            "note": None,
            "source": "auto",
        })
        rows.append({
            "id": lid,
            "address": sold[lid]["address"],
            "price": sold[lid]["price"],
            "status": status,
            "days_on_market": days_on_market,
        })
    return rows


def send_email(config, new_listings, price_drops):
    """Send a single email containing all alerts."""
    email_cfg = config["email"]
    password = get_secret(config, "GMAIL_APP_PASSWORD", "email.password_app")

    if not email_cfg.get("sender") or email_cfg["sender"] == "YOUR_GMAIL@gmail.com" or not password:
        log("Email not configured - skipping send (set GMAIL_APP_PASSWORD or config.local.json)")
        return None

    lines = []
    total = len(new_listings) + len(price_drops)
    lines.append(f"PROPERTY ALERT - {total} item(s)")
    lines.append(f"WF16 + 5 mile ring | 3-bed terraced/semi-detached")
    lines.append("")

    if new_listings:
        lines.append(f"--- {len(new_listings)} NEW LISTING(S) ---")
        for i, l in enumerate(new_listings, 1):
            lines.append("")
            size = f" | {l['sqft']} sq ft" if l.get("sqft") else ""
            conf = f" | Confidence: {l.get('confidence', {}).get('score', '?')}/100" if l.get("confidence") else ""
            lines.append(f"{i}. \xa3{l['price']:,} | {l['bedrooms']}-bed {l['type'].title()}{size}{conf}")
            lines.append(f"   {l['address']}")
            lines.append(f"   Agent: {l['agent']}")
            lines.append(f"   {l['url']}")

    if price_drops:
        if new_listings:
            lines.append("")
        lines.append(f"--- {len(price_drops)} PRICE DROP(S) ---")
        for i, l in enumerate(price_drops, 1):
            lines.append("")
            size = f" | {l['sqft']} sq ft" if l.get("sqft") else ""
            lines.append(
                f"{i}. \xa3{l['old_price']:,} -> \xa3{l['price']:,} "
                f"| {l['bedrooms']}-bed {l['type'].title()}{size}"
            )
            lines.append(f"   {l['address']}")
            lines.append(f"   Agent: {l['agent']}")
            lines.append(f"   {l['url']}")

    body = "\n".join(lines)
    subject = f"Property Alert: {len(new_listings)} new, {len(price_drops)} price drops"

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = email_cfg["sender"]
    msg["To"] = email_cfg["recipient"]

    with smtplib.SMTP(email_cfg["smtp_server"], email_cfg["smtp_port"]) as server:
        server.starttls()
        server.login(email_cfg["sender"], password)
        server.send_message(msg)

    return subject


def send_telegram_alert(config, new_listings, price_drops):
    """Send alerts via Telegram Bot API."""
    import urllib.request
    import urllib.parse

    tg_cfg = config.get("telegram", {})
    token = get_secret(config, "TELEGRAM_BOT_TOKEN", "telegram.bot_token")
    chat_id = tg_cfg.get("chat_id")

    if not token or not chat_id:
        log("Telegram not configured - skipping (add telegram section to config.json)")
        return False

    lines = []
    total = len(new_listings) + len(price_drops)
    lines.append(f"🏠 PROPERTY ALERT — {total} item(s)")
    lines.append("WF16 + 5 mile ring | 3-bed terraced/semi-detached")
    lines.append("")

    if new_listings:
        lines.append(f"📢 {len(new_listings)} NEW LISTING(S)")
        for i, l in enumerate(new_listings, 1):
            lines.append("")
            size = f" | {l['sqft']} sqft" if l.get("sqft") else ""
            conf = f" | Conf: {l.get('confidence', {}).get('score', '?')}/100" if l.get("confidence") else ""
            lines.append(f"{i}. £{l['price']:,} | {l['bedrooms']}-bed {l['type'].title()}{size}{conf}")
            lines.append(f"   {l['address']}")
            lines.append(f"   {l['agent']}")
            lines.append(f"   {l['url']}")

    if price_drops:
        if new_listings:
            lines.append("")
        lines.append(f"📉 {len(price_drops)} PRICE DROP(S)")
        for i, l in enumerate(price_drops, 1):
            lines.append("")
            size = f" | {l['sqft']} sqft" if l.get("sqft") else ""
            lines.append(
                f"{i}. £{l['old_price']:,} → £{l['price']:,} "
                f"| {l['bedrooms']}-bed {l['type'].title()}{size}"
            )
            lines.append(f"   {l['address']}")
            lines.append(f"   {l['agent']}")
            lines.append(f"   {l['url']}")

    text = "\n".join(lines)

    # Telegram has 4096 byte limit per message
    if len(text.encode("utf-8")) > 4096:
        # Split into multiple messages
        parts = []
        current = ""
        for line in lines:
            test = f"{current}\n{line}" if current else line
            if len(test.encode("utf-8")) > 4000:
                if current:
                    parts.append(current)
                current = line
            else:
                current = test
        if current:
            parts.append(current)
    else:
        parts = [text]

    for part in parts:
        _telegram_send_message(token, chat_id, part)

    log(f"Telegram alert sent ({len(parts)} message(s))")
    return True


def _telegram_send_message(token, chat_id, text):
    """Send a single Telegram message via the Bot API. Returns True on success."""
    import urllib.parse
    import urllib.request

    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            if not result.get("ok"):
                log(f"Telegram API error: {result}")
                return False
    except Exception as e:
        log(f"Telegram send error: {e}")
        return False
    return True


def send_watchdog_alert(config, failures, failed_runs):
    """Notify via Telegram when the pipeline keeps degrading."""
    token = get_secret(config, "TELEGRAM_BOT_TOKEN", "telegram.bot_token")
    chat_id = (config.get("telegram") or {}).get("chat_id")
    if not token or not chat_id:
        log("WATCHDOG: telegram not configured - skipping watchdog alert")
        return False
    text = (
        "⚠️ <b>PROPERTY WATCH PROBLEM</b>\n"
        f"{failed_runs} consecutive degraded run(s).\n"
        "Issues:\n" + "\n".join(f"• {f}" for f in failures)
    )
    ok = _telegram_send_message(token, chat_id, text)
    log(f"WATCHDOG alert {'sent' if ok else 'failed'} ({failed_runs} consecutive failures)")
    return ok


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def _sparkline(price_history):
    """Build a small inline SVG price-history sparkline for a listing card."""
    if not price_history:
        return ""
    entries = [
        {"price": p["price"], "date": p["date"]}
        for p in price_history
        if isinstance(p.get("price"), int)
    ]
    if len(entries) < 2:
        return ""
    w, h = 110, 26
    prices = [e["price"] for e in entries]
    low, high = min(prices), max(prices)
    rng = max(high - low, 1)
    n = len(prices)
    pts = []
    for i, price in enumerate(prices):
        x = 1 + round((i * (w - 4)) / (n - 1))
        y = round((h - 4) - ((price - low) / rng) * (h - 8))
        pts.append(f"{x},{y}")
    color = "#059669" if prices[-1] <= prices[0] else "#d97706"
    title = " &middot; ".join(
        f"{e['date'][:10]}: &pound;{e['price']:,}" for e in entries
    )[:200]
    return (
        f'<svg class="spark" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'role="img" aria-label="Price history: {title}">'
        f'<polyline fill="none" stroke="{color}" stroke-width="2" '
        f'stroke-linejoin="round" points="{" ".join(pts)}"/>'
        f"</svg>"
    )


def _sold_html(state):
    """Render a 'sold while watching' section from state['sold']."""
    sold = (state or {}).get("sold") or {}
    if not sold:
        return ""
    items = sorted(sold.items(), key=lambda kv: kv[1].get("sold_date") or "", reverse=True)[:12]
    cards = []
    for _lid, e in items:
        status = (e.get("status") or "sold").replace("_", " ").title()
        price_txt = f"&pound;{e['price']:,}" if e.get("price") else "price unknown"
        days = ""
        if e.get("days_on_market") is not None:
            days = f"&middot; {e['days_on_market']} days on market"
        cards.append(
            '<div class="off-card">'
            f'<div class="off-addr">{e.get("address", "Unknown")}</div>'
            f'<div class="off-meta">{status} &middot; {price_txt}</div>'
            f'<div class="off-days">{days}</div>'
            "</div>"
        )
    return (
        '<div class="off-market"><h2>&#10003; Sold while watching</h2>'
        f'<div class="off-market-grid">{"".join(cards)}</div></div>'
    )


def _off_market_html(state):
    """Render a 'recently off market' section from state (most recent first)."""
    if not state:
        return ""
    entries = state.get("off_market") or {}
    if not entries:
        return ""
    items = sorted(
        entries.items(), key=lambda kv: kv[1].get("last_seen") or "", reverse=True
    )[:10]
    cards = []
    for _lid, e in items:
        days = ""
        if e.get("first_seen") and e.get("last_seen"):
            try:
                d1 = datetime.fromisoformat(e["first_seen"])
                d2 = datetime.fromisoformat(e["last_seen"])
                days = f"{max((d2 - d1).days, 0)} days on market"
            except ValueError:
                days = ""
        last_seen = ""
        if e.get("last_seen"):
            try:
                last_seen = datetime.fromisoformat(e["last_seen"]).strftime("%d %b")
            except ValueError:
                last_seen = ""
        price_txt = f"&pound;{e['price']:,}" if e.get("price") else "price unknown"
        meta = price_txt
        if e.get("source"):
            meta += f" &middot; {e['source']}"
        cards.append(
            '<div class="off-card">'
            f'<div class="off-addr">{e.get("address", "Unknown")}</div>'
            f'<div class="off-meta">{meta}</div>'
            f'<div class="off-days">{days}{" &middot; last seen " + last_seen if last_seen else ""}</div>'
            "</div>"
        )
    return (
        '<div class="off-market"><h2>&#9203; Recently off market</h2>'
        f'<div class="off-market-grid">{"".join(cards)}</div></div>'
    )


def _run_summary_html(state):
    """Render last-run / source / health summary for the dashboard footer."""
    if not state:
        return ""
    parts = []
    last_run = state.get("last_run") or state.get("last_successful_run")
    if last_run:
        try:
            dt = datetime.fromisoformat(last_run)
            age_min = (datetime.now() - dt).total_seconds() / 60
            flag = "" if age_min < 180 else ' <span style="color:#dc2626">(STALE)</span>'
            parts.append(f"Last run {dt.strftime('%d %b %Y, %H:%M')}{flag}")
        except ValueError:
            pass
    history = state.get("run_history") or []
    if history and history[-1].get("sources"):
        sources = history[-1]["sources"]
        label_map = {"ontemarket": "OnTheMarket", "barkers": "Barkers"}
        parts.append(
            "Sources: "
            + " &middot; ".join(f"{label_map.get(k, k.title())}: {v}" for k, v in sources.items())
        )
    failed = state.get("failed_runs") or 0
    if failed:
        parts.append(f'<span style="color:#dc2626">&#9888;&#65039; {failed} consecutive degraded run(s)</span>')
    return " &middot; ".join(parts)


def _confidence_badge(score, grade=None):
    """Return CSS class and label for a deal-quality score.

    At LOW/UNSCORED evidence the price verdict itself is not trustworthy, so
    the badge can never call the house "Overpriced"/"Way Over" (rating-trust
    issue 02) — it reports the score plus a "Low Evidence" label instead.
    """
    if grade in ("LOW", "UNSCORED"):
        return "low", f"{score}", "Low Evidence"
    if score >= 80:
        return "great", f"{score}", "Great Deal"
    elif score >= 60:
        return "fair", f"{score}", "Fair Price"
    elif score >= 40:
        return "over", f"{score}", "Overpriced"
    else:
        return "bad", f"{score}", "Way Over"


_CSS = """
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&display=swap');
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'DM Sans', -apple-system, BlinkMacSystemFont, sans-serif; background: #fafaf9; color: #1c1917; min-height: 100vh; }
.header { padding: 28px 24px 16px; border-bottom: 1px solid #e7e5e4; }
.header h1 { font-size: 22px; font-weight: 700; color: #1c1917; letter-spacing: -0.3px; }
.header .sub { font-size: 13px; color: #78716c; margin-top: 3px; }
.header .count { font-size: 13px; color: #059669; margin-top: 4px; font-weight: 500; }
.market-temp { display: flex; justify-content: center; gap: 10px; padding: 10px 24px; flex-wrap: wrap; border-bottom: 1px solid #e7e5e4; }
.temp-item { font-size: 12px; padding: 4px 12px; border-radius: 20px; font-weight: 500; }
.temp-item.rising { background: #ecfdf5; color: #059669; }
.temp-item.stable { background: #f0fdfa; color: #0d9488; }
.temp-item.cooling { background: #fffbeb; color: #d97706; }
.summary { display: flex; justify-content: center; gap: 20px; padding: 12px 24px; border-bottom: 1px solid #e7e5e4; flex-wrap: wrap; font-size: 13px; color: #78716c; }
.summary .num { font-weight: 700; }
.summary .great .num { color: #059669; }
.summary .fair .num { color: #0d9488; }
.summary .over .num { color: #d97706; }
.grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; padding: 20px 24px; max-width: 1200px; margin: 0 auto; }
.card { background: #fff; border-radius: 10px; overflow: hidden; border: 1px solid #e7e5e4; transition: box-shadow 0.2s; position: relative; }
.card:hover { box-shadow: 0 4px 20px rgba(0,0,0,0.06); }
.card.featured { border-color: #059669; border-width: 1.5px; }
.card-link { display: block; text-decoration: none; color: inherit; }
.img-wrap { width: 100%; height: 180px; overflow: hidden; background: #d6d3d1; }
.img-wrap img { width: 100%; height: 100%; object-fit: cover; }
.info { padding: 16px 18px 14px; }
.price-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.price { font-size: 26px; font-weight: 700; color: #1c1917; letter-spacing: -0.5px; }
.old-price { font-size: 14px; color: #a8a29e; text-decoration: line-through; }
.drop-arrow { color: #059669; font-size: 14px; margin-left: 4px; }
.rating-row { display: flex; align-items: center; gap: 6px; }
.confidence { padding: 3px 8px; border-radius: 4px; font-size: 12px; font-weight: 700; }
.confidence.great { background: #ecfdf5; color: #059669; }
.confidence.fair { background: #f0fdfa; color: #0d9488; }
.confidence.over { background: #fffbeb; color: #d97706; }
.confidence.bad { background: #fef2f2; color: #dc2626; }
.confidence.low { background: #f5f5f4; color: #78716c; }
.confidence-label.low { color: #78716c; }
.estimate { font-size: 13px; font-weight: 600; color: #0d9488; margin-top: 8px; }
.estimate.est-low { color: #d97706; }
.revised { font-size: 11px; font-weight: 500; color: #d97706; margin-top: 4px; }
.confidence-label { font-size: 11px; font-weight: 500; }
.confidence-label.great { color: #059669; }
.confidence-label.fair { color: #0d9488; }
.confidence-label.over { color: #d97706; }
.confidence-label.bad { color: #dc2626; }
.rank-best { background: #ecfdf5; color: #059669; padding: 4px 10px; border-radius: 20px; font-size: 12px; font-weight: 600; border: 1px solid #a7f3d0; }
.rank { background: #f5f5f4; color: #78716c; padding: 4px 10px; border-radius: 20px; font-size: 12px; font-weight: 600; border: 1px solid #e7e5e4; }
.rank-gap { font-size: 11px; font-weight: 400; color: #a8a29e; }
.meta { font-size: 13px; color: #78716c; margin-top: 10px; }
.address { font-size: 15px; color: #1c1917; margin-top: 4px; font-weight: 500; line-height: 1.4; }
.details { margin-top: 10px; display: flex; gap: 8px; flex-wrap: wrap; }
.size { display: inline-block; background: #f5f5f4; color: #57534e; padding: 4px 10px; border-radius: 6px; font-size: 12px; font-weight: 500; border: 1px solid #e7e5e4; }
.size.unknown { color: #a8a29e; }
.tag { position: absolute; top: 12px; right: 12px; padding: 4px 10px; border-radius: 20px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; z-index: 2; }
.tag.new { background: #059669; color: #fff; }
.tag.drop { background: #d97706; color: #fff; }
.tag.relisted { background: #6366f1; color: #fff; }
.spark { display: block; margin-top: 4px; }
.off-market { max-width: 1200px; margin: 20px auto 0; padding: 0 24px; }
.off-market h2 { font-size: 15px; font-weight: 600; color: #57534e; margin-bottom: 10px; }
.off-market-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }
.off-card { background: #fff; border: 1px solid #e7e5e4; border-radius: 8px; padding: 10px 14px; }
.off-addr { font-size: 13px; font-weight: 500; color: #1c1917; }
.off-meta { font-size: 12px; color: #a8a29e; margin-top: 2px; }
.off-days { font-size: 11px; color: #78716c; margin-top: 2px; }
.mortgage { padding: 12px 18px; border-top: 1px solid #f5f5f4; background: #fafaf9; }
.mortgage-row { display: flex; justify-content: space-between; font-size: 13px; color: #78716c; padding: 2px 0; }
.mortgage-val { color: #1c1917; font-weight: 600; }
.details-toggle { padding: 12px 18px; border-top: 1px solid #f5f5f4; }
.details-toggle summary { font-size: 12px; color: #78716c; cursor: pointer; font-weight: 500; }
.details-toggle summary:hover { color: #1c1917; }
.negotiation { padding: 12px 18px; border-top: 1px solid #f5f5f4; background: #fafaf9; }
.neg-title { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: #a8a29e; margin-bottom: 4px; }
.neg-text { font-size: 14px; font-weight: 600; }
.neg-text.strong { color: #059669; }
.neg-text.fair { color: #0d9488; }
.neg-text.negotiate { color: #d97706; }
.neg-text.overpriced { color: #dc2626; }
.neg-detail { font-size: 11px; color: #a8a29e; margin-top: 2px; }
.breakdown { padding: 12px 18px; border-top: 1px solid #f5f5f4; background: #fafaf9; }
.breakdown-title { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: #a8a29e; margin-bottom: 8px; }
.factor { margin-bottom: 8px; }
.factor-label { font-size: 12px; color: #57534e; font-weight: 500; }
.factor-detail { font-size: 11px; color: #a8a29e; margin-top: 1px; }
.factor-bar { height: 4px; background: #e7e5e4; border-radius: 2px; margin-top: 4px; overflow: hidden; }
.factor-fill { height: 100%; background: #059669; border-radius: 2px; }
.empty { text-align: center; padding: 60px 24px; color: #a8a29e; font-size: 15px; }
.footer { text-align: center; padding: 20px 24px; color: #a8a29e; font-size: 12px; border-top: 1px solid #e7e5e4; }
@media (max-width: 768px) {
    .grid { grid-template-columns: 1fr; padding: 16px; gap: 14px; }
    .img-wrap { height: 160px; }
    .price { font-size: 22px; }
}
"""


def generate_html(listings, market_temps, state=None):
    """Generate a self-contained HTML dashboard with dynamic ratings."""
    sorted_listings = sorted(
        listings,
        key=lambda l: (
            l.get("confidence", {}).get("score", 0),
            l.get("sqft") or 0,
        ),
        reverse=True,
    )

    now = datetime.now().strftime("%d %b %Y, %H:%M")
    count = len(sorted_listings)

    # Assign rank badges
    for rank, l in enumerate(sorted_listings, 1):
        l["rank"] = rank

    # Market temperature HTML (per property type where data allows)
    temp_html = ""
    if market_temps:
        temp_items = []
        for key, temp in market_temps.items():
            trend_icon = {"rising": "&#9650;", "cooling": "&#9660;", "stable": "&#9679;"}.get(temp["trend"], "&#9679;")
            trend_class = temp["trend"]
            if "|" in key:
                area_name, ttype = key.split("|", 1)
                label = f"{area_name} {ttype.replace('-', ' ')}"
            else:
                label = f"{key} (all house types)"
            if temp.get("recent_count") and temp.get("older_count"):
                value = f'{trend_icon} {temp["trend"].title()} ({temp["change_pct"]:+.1f}%)'
            else:
                value = f"{trend_icon} not enough sales for a trend"
            detail = temp.get("detail", "")
            temp_items.append(
                f'<span class="temp-item {trend_class}" title="{detail}">{label}: {value}</span>'
            )
        temp_html = f'<div class="market-temp">{"".join(temp_items)}</div>'

    cards_html = ""
    for i, l in enumerate(sorted_listings):
        sqft = l.get("sqft")
        sqm = l.get("sqm")
        conf = l.get("confidence", {})
        mortgage = l.get("mortgage", {})
        negotiation = l.get("negotiation", {})
        conf_score = conf.get("score", 0)
        grade = l.get("evidence_grade") or (conf.get("breakdown") or {}).get("_grade")
        badge_class, badge_text, badge_label = _confidence_badge(conf_score, grade)
        rank = l.get("rank", i + 1)

        # Rank badge with gap
        top_score = sorted_listings[0].get("confidence", {}).get("score", 0) if sorted_listings else 0
        gap = top_score - conf_score if rank > 1 else 0
        rank_class = "rank-best" if rank == 1 else "rank"
        rank_text = f"#{rank}" if rank > 1 else "&#9733; #1"
        if rank > 1:
            rank_text = f"#{rank} <span class='rank-gap'>(+{gap} pts behind)</span>"
        rank_label = "Best Value" if rank == 1 else f"#{rank} of {count}"
        spark = _sparkline(l.get("price_history"))

        # Size badge
        if sqft:
            size_badge = f'<span class="size">{sqft} sq ft ({sqm} m&sup2;)</span>'
        else:
            size_badge = '<span class="size unknown">Size unknown</span>'

        # Image
        img_html = ""
        if l.get("image"):
            img_html = f'<img src="{l["image"]}" alt="{l["address"]}" loading="lazy" />'

        # Price drop
        price_drop_html = ""
        if "old_price" in l:
            price_drop_html = (
                f'<span class="old-price">&pound;{l["old_price"]:,}</span> '
                f'<span class="drop-arrow">&darr;</span> '
            )

        # Tag
        tag = ""
        if "old_price" in l:
            tag = '<span class="tag drop">PRICE DROP</span>'
        elif l.get("relisted"):
            tag = '<span class="tag relisted">RE-LISTED</span>'
        elif l["id"] not in _seen_before:
            tag = '<span class="tag new">NEW</span>'

        # Mortgage info (simplified)
        mortgage_html = ""
        if mortgage:
            mortgage_html = f"""
                <div class="mortgage">
                    <div class="mortgage-row"><span>Monthly payment</span><span class="mortgage-val">&pound;{mortgage['monthly']:,}/mo</span></div>
                    <div class="mortgage-row"><span>Deposit</span><span>&pound;{mortgage['deposit']:,}</span></div>
                </div>"""

        # Negotiation guide
        neg_html = ""
        if negotiation and negotiation.get("range_text") and negotiation["range_text"] != "Insufficient data":
            neg_class = negotiation.get("label", "fair")
            neg_html = f"""
                <div class="negotiation">
                    <div class="neg-title">Negotiation Guide</div>
                    <div class="neg-text {neg_class}">{negotiation['range_text']}</div>
                    <div class="neg-detail">Comp median: &pound;{negotiation.get('median', 0):,} &middot; asking is {negotiation.get('vs_median', 0):+.1f}% vs comps &middot; basis: {negotiation.get('basis', 'n/a')} ({negotiation.get('count', 0)} sale{'s' if negotiation.get('count', 0) != 1 else ''})</div>
                </div>"""

        # Sold comparables used by the score (informational)
        comps = l.get("comparables") or []
        if comps:
            comp_prices = " &middot; ".join(f"&pound;{c['price']:,}" for c in comps[:5])
            tier_label = (
                (conf.get("breakdown", {}).get("area_median") or {}).get("tier_label")
                or "comparables"
            )
            conf.setdefault("breakdown", {})["sold_comparables"] = {
                "score": None,
                "detail": f"{len(comps)} sale{'' if len(comps) == 1 else 's'} used ({tier_label}): {comp_prices}",
            }

        # Confidence breakdown
        breakdown_html = ""
        if conf.get("breakdown"):
            items = []
            based_on = conf["breakdown"].get("_based_on")
            if based_on is not None:
                items.append(
                    '<div class="factor"><span class="factor-label">Evidence</span>'
                    f'<span class="factor-detail">Based on {based_on} of 5 signals &mdash; '
                    'missing signals are excluded, not counted as neutral</span></div>'
                )
            for key, val in conf["breakdown"].items():
                if key.startswith("_"):
                    continue
                label = {
                    "area_median": "vs Sold Comparables",
                    "sqft_value": "Price per sqft",
                    "price_drop": "Price Drop",
                    "listing_age": "Listing Age",
                    "market_context": "Market Context",
                    "sold_comparables": "Sold Comparables Used",
                }.get(key, key)
                s = val["score"]
                if s is None:
                    items.append(f'<div class="factor"><span class="factor-label">{label}</span><span class="factor-detail">{val["detail"]}</span></div>')
                else:
                    bar_width = min(s, 100)
                    items.append(f'<div class="factor"><span class="factor-label">{label}</span><span class="factor-detail">{val["detail"]}</span><div class="factor-bar"><div class="factor-fill" style="width:{bar_width}%"></div></div></div>')
            breakdown_html = f"""
                <details class="details-toggle">
                    <summary>Show full breakdown</summary>
                    <div class="breakdown">
                        {neg_html}
                        {''.join(items)}
                    </div>
                </details>"""
        elif neg_html:
            breakdown_html = neg_html

        # Estimate + verdict note (rating-trust headline)
        estimate_html = ""
        estimate = l.get("estimate")
        if estimate and estimate.get("text"):
            est_class = "est-low" if estimate.get("grade") in ("LOW", "UNSCORED") else "est-ok"
            estimate_html = f'<div class="estimate {est_class}">{estimate["text"]}</div>'
        verdict_html = ""
        verdict = l.get("verdict")
        if verdict and verdict.get("revised"):
            verdict_html = '<div class="revised">Score revised &mdash; evidence changed</div>'

        featured = ' featured' if rank == 1 else ''
        cards_html += f"""
        <div class="card{featured}">
            {tag}
            <a href="{l['url']}" target="_blank" rel="noopener" class="card-link">
                <div class="img-wrap">
                    {img_html}
                </div>
                <div class="info">
                    <div class="price-row">
                        <div>{price_drop_html}<span class="price">&pound;{l['price']:,}</span>{spark}</div>
                        <div class="rating-row">
                            <span class="confidence {badge_class}">{badge_text}</span>
                            <span class="confidence-label {badge_class}">{badge_label}</span>
                            <span class="{rank_class}">{rank_text}</span>
                        </div>
                    </div>
                    <div class="meta">
                        {l['bedrooms']} bed &middot; {l['type'].title()} &middot; {l['agent']}
                    </div>
                    <div class="address">{l['address']}</div>
                    {estimate_html}
                    {verdict_html}
                    <div class="details">
                        {size_badge}
                    </div>
                </div>
            </a>
            {breakdown_html}
        </div>
"""

    if not cards_html:
        cards_html = '<div class="empty">No matching properties found right now. Next check in 30 minutes.</div>'

    # Summary stats
    scores = [l.get("confidence", {}).get("score", 0) for l in sorted_listings]
    avg_score = statistics.mean(scores) if scores else 0
    great_count = 0
    fair_count = 0
    over_count = 0
    low_count = 0
    for l in sorted_listings:
        s = l.get("confidence", {}).get("score", 0)
        g = l.get("evidence_grade") or (l.get("confidence", {}).get("breakdown") or {}).get("_grade")
        _label = _confidence_badge(s, g)[2]
        if _label == "Great Deal":
            great_count += 1
        elif _label == "Fair Price":
            fair_count += 1
        elif _label == "Low Evidence":
            low_count += 1
        else:
            over_count += 1

    off_html = _off_market_html(state)
    sold_html = _sold_html(state)
    run_summary_html = _run_summary_html(state)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta http-equiv="refresh" content="1800" />
<title>Property Watch - Heckmondwike</title>
<style>
{_CSS}
</style>
</head>
<body>
<div class="header">
    <h1>Property Watch</h1>
    <div class="sub">3-bed terraced/semi-detached &middot; &pound;120k&ndash;&pound;220k &middot; within 5 miles of WF16</div>
    <div class="count">{count} matching {properties(count)} &middot; Avg score: {avg_score:.0f}/100</div>
</div>
{temp_html}
<div class="summary">
    <span class="great"><span class="num">{great_count}</span> Great Deals</span>
    <span class="fair"><span class="num">{fair_count}</span> Fair Price</span>
    <span class="over"><span class="num">{over_count}</span> Overpriced</span>
    <span><span class="num">{low_count}</span> Low Evidence</span>
</div>
<div class="grid">
{cards_html}
</div>
{sold_html}
{off_html}
<div class="footer">
    Updated {now} &middot; Auto-refreshes every 30 minutes &middot; Evidence-based scoring: only real data counts &middot; {run_summary_html}<br/>
    Sold prices from HM Land Registry (comparables ladder: same type + street &rarr; postcode sector &rarr; district) &middot; Mortgage: &pound;{DEPOSIT:,} deposit at {MORTGAGE_RATE:.1%} over {MORTGAGE_YEARS} years
</div>
</body>
</html>"""

    HTML_FILE.write_text(html)
    log(f"HTML dashboard written to {HTML_FILE}")


def properties(n):
    return "property" if n == 1 else "properties"


_seen_before = set()


def push_to_github():
    """Push updated index.html to GitHub Pages."""
    try:
        subprocess.run(
            ["git", "add", "index.html"],
            cwd=str(SCRIPT_DIR),
            capture_output=True,
            timeout=10,
        )
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet", "index.html"],
            cwd=str(SCRIPT_DIR),
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            log("No HTML changes to push")
            return

        subprocess.run(
            ["git", "commit", "-m", f"Update dashboard {datetime.now().strftime('%Y-%m-%d %H:%M')}"],
            cwd=str(SCRIPT_DIR),
            capture_output=True,
            timeout=10,
        )
        result = subprocess.run(
            ["git", "push", "origin", "master"],
            cwd=str(SCRIPT_DIR),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            log("Pushed to GitHub Pages")
        else:
            log(f"Git push failed: {result.stderr[:200]}")
    except Exception as e:
        log(f"GitHub push error: {e}")


def _collect_raw_listings(config):
    """Fetch from all configured sources and merge manual listings.

    Returns (all_listings, source_counts) where source_counts maps each
    configured source to the number of raw listings parsed (0 on error).
    """
    source_counts = {}
    all_listings = []
    for source in config.get("sources", []):
        try:
            if source == "ontemarket":
                fetched = fetch_ontemarket(config)
            elif source == "barkers":
                fetched = fetch_barkers(config)
            else:
                log(f"Unknown source skipped: {source}")
                continue
            source_counts[source] = len(fetched)
            all_listings.extend(fetched)
        except Exception as e:
            source_counts[source] = 0
            log(f"ERROR fetching {source}: {e}")

    for ml in config.get("manual_listings", []):
        all_listings.append({
            "id": ml["id"],
            "source": ml.get("source", "Manual"),
            "address": ml["address"],
            "price": ml["price"],
            "bedrooms": ml.get("bedrooms", 0),
            "type": ml.get("type", "unknown"),
            "url": ml.get("url", ""),
            "agent": ml.get("agent", "Unknown"),
            "image": ml.get("image", ""),
            "sqft": ml.get("sqft"),
        })

    return all_listings, source_counts


def _verdict_category(score):
    """Map a deal-quality score to a headline category band."""
    if score >= 80:
        return "great"
    if score >= 60:
        return "fair"
    if score >= 40:
        return "over"
    return "bad"


def _attach_derived(listing, sold_prices, postcode_areas, state, all_listings,
                    epc_map=None, market_temps=None, caps=None, sold_meta=None):
    """Attach scoring inputs/results: first_seen, comparables, score, estimate.

    ``sold_meta`` maps postcode area -> {"fetched": iso} when available, so
    the persisted evidence basis records which cache the score relied on.

    Also applies the wobble guard (issue 04): the headline category may only
    flip commitment after it has been computed the same way in two
    consecutive runs on an unchanged evidence basis; basis changes raise a
    "score revised — evidence changed" note instead of silently whiplashing.
    """
    area = extract_postcode_area(listing["address"])
    sold = sold_prices.get(area, []) if area else []
    area_used = area
    if not sold:
        for a in postcode_areas:
            if sold_prices.get(a):
                sold = sold_prices[a]
                area_used = a
                break

    entry = state.get("seen", {}).get(listing["id"], {})
    listing["first_seen"] = entry.get("first_seen") or entry.get("last_seen")
    history = entry.get("price_history")
    listing["price_history"] = history if isinstance(history, list) else None
    listing["relisted"] = listing["id"] in state.get("off_market", {})

    if caps is None:
        caps = NEGOTIATION_CAPS

    comps = find_comparables(listing, sold, epc_map)
    grade = (comps or {}).get("grade") or "UNSCORED"
    listing["evidence_grade"] = grade
    listing["evidence_basis"] = {
        "tier": comps["tier"] if comps else None,
        "label": comps["label"] if comps else "no comparables",
        "count": comps["count"] if comps else 0,
        "grade": grade,
        "area": area_used,
        "cache_fetched": (sold_meta or {}).get(area_used, {}).get("fetched")
        if area_used and area_used in (sold_meta or {}) else None,
    }

    conf_score, conf_breakdown = calculate_confidence(listing, sold, all_listings, comps, epc_map, caps)
    listing["comparables"] = comps["comps"] if comps else []
    listing["confidence"] = {"score": conf_score, "breakdown": conf_breakdown}

    market_temp = None
    if market_temps:
        key = f"{area_used}|{_type_key(listing)}"
        market_temp = market_temps.get(key) or market_temps.get(area_used)
    listing["estimate"] = calculate_estimate(listing, comps, market_temp, caps=caps)
    listing["mortgage"] = estimate_mortgage(listing["price"])
    listing["negotiation"] = calculate_negotiation(listing, sold, comps, caps)

    # --- Wobble guard: category commitment + basis-change note ---
    prev = entry
    prev_cat = prev.get("verdict_category")
    prev_pending = prev.get("pending_verdict")
    prev_basis = prev.get("evidence_basis")
    basis_same = prev_basis == listing["evidence_basis"]
    price_changed = "price" in prev and prev["price"] != listing["price"]

    computed_cat = _verdict_category(conf_score)
    if not prev_cat or price_changed:
        category, pending = computed_cat, None
    elif computed_cat == prev_cat:
        category, pending = computed_cat, None
    elif prev_pending and prev_pending.get("category") == computed_cat and basis_same:
        # Second consecutive run computing the same new category -> commit.
        category, pending = computed_cat, None
    else:
        # First run seeing a change -> hold, remember the pending flip.
        category, pending = prev_cat, {"category": computed_cat, "since": datetime.now().isoformat()}

    revised = (not basis_same) or (pending is not None)
    listing["verdict"] = {
        "category": category,
        "pending": pending,
        "revised": revised,
        "computed": computed_cat,
    }


def _update_state(state, filtered, source_counts):
    """Persist seen listings with price history, off-market tracking and run history."""
    now = datetime.now()
    now_iso = now.isoformat()
    seen = state.setdefault("seen", {})
    off_market = state.setdefault("off_market", {})
    current_ids = {l["id"] for l in filtered}

    # Persist current listings (preserving first_seen and price history)
    for listing in filtered:
        lid = listing["id"]
        prior = seen.get(lid, {})
        history = []
        first_seen = now_iso
        if lid in off_market:
            prior_off = off_market.pop(lid)
            first_seen = prior_off.get("first_seen") or first_seen
            history = prior_off.get("price_history") or []
        elif lid in seen:
            first_seen = seen[lid].get("first_seen") or seen[lid].get("last_seen") or first_seen
            history = seen[lid].get("price_history") or []

        prev_price = seen[lid]["price"] if lid in seen else None
        if not history or history[-1]["price"] != listing["price"]:
            history.append({"date": now_iso, "price": listing["price"]})
        history = history[-PRICE_HISTORY_MAX:]

        seen[lid] = {
            "price": listing["price"],
            "address": listing["address"],
            "sqft": listing.get("sqft"),
            "source": listing.get("source"),
            "first_seen": first_seen,
            "last_seen": now_iso,
            "price_history": history,
            "misses": 0,
            "removed_misses": prior.get("removed_misses", 0),
        }
        # Rating-trust: persist the evidence basis + wobble-guard verdict
        # so the next run can detect basis changes and hold category flips.
        if "evidence_basis" in listing:
            seen[lid]["evidence_basis"] = listing["evidence_basis"]
        verdict = listing.get("verdict")
        if isinstance(verdict, dict):
            seen[lid]["verdict_category"] = verdict.get("category")
            seen[lid]["pending_verdict"] = verdict.get("pending")
            seen[lid]["verdict_revised"] = verdict.get("revised")

    # Promote listings absent for N consecutive runs to off-market
    for lid in list(seen.keys()):
        if lid in current_ids:
            continue
        entry = seen[lid]
        entry["misses"] = entry.get("misses", 0) + 1
        if entry["misses"] >= MISSING_RUNS_BEFORE_OFF_MARKET:
            off_market[lid] = {
                "address": entry.get("address", ""),
                "price": entry.get("price"),
                "sqft": entry.get("sqft"),
                "source": entry.get("source"),
                "first_seen": entry.get("first_seen"),
                "last_seen": entry.get("last_seen"),
                "price_history": entry.get("price_history") or [],
            }
            del seen[lid]

    # Keep the off-market list bounded
    if len(off_market) > OFF_MARKET_MAX:
        items = sorted(
            off_market.items(), key=lambda kv: kv[1].get("last_seen") or "", reverse=True
        )[:OFF_MARKET_MAX]
        state["off_market"] = dict(items)

def _source_medians(history):
    """Median raw-listing count per source across recent runs."""
    medians = {}
    srcs = set()
    for r in history:
        srcs.update((r.get("sources") or {}).keys())
    for src in srcs:
        vals = [r["sources"][src] for r in history if (r.get("sources") or {}).get(src) is not None]
        if len(vals) >= 2:
            medians[src] = statistics.median(vals)
    return medians


def _assess_health(config, state, source_counts, filtered_count):
    """Track run health; alert via Telegram on repeated degraded runs.

    A run is degraded if: every source returned 0, a source returned 0 when it
    normally returns >=1, or the filtered count collapses by >=50% vs the
    rolling median. Returns True when the run is healthy.
    """
    now_iso = datetime.now().isoformat()
    history = state.setdefault("run_history", [])
    history.append({"ts": now_iso, "sources": dict(source_counts), "filtered": filtered_count})
    state["run_history"] = history[-14:]

    failures = []
    total_raw = sum(source_counts.values()) if source_counts else 0
    if total_raw == 0:
        failures.append("all configured sources returned 0 listings")

    medians = _source_medians(history)
    for src, count in source_counts.items():
        norm = medians.get(src)
        if count == 0 and norm is not None and norm >= 1:
            failures.append(f"{src} returned 0 listings (normally ~{norm:g})")

    counts = [r["filtered"] for r in history if isinstance(r.get("filtered"), int)]
    if len(counts) >= 4:
        rolling = statistics.median(counts[-8:-1])
        if rolling > 0 and filtered_count < rolling * 0.5:
            failures.append(
                f"filtered count collapsed ({filtered_count} vs rolling median {rolling:.0f})"
            )

    if failures:
        state["failed_runs"] = state.get("failed_runs", 0) + 1
    else:
        state["failed_runs"] = 0
        state["last_successful_run"] = now_iso

    if failures:
        log(f"HEALTH: degraded run - {'; '.join(failures)} (consecutive: {state['failed_runs']})")
        fr = state["failed_runs"]
        if fr == WATCHDOG_FAIL_THRESHOLD or (fr > WATCHDOG_FAIL_THRESHOLD and fr % WATCHDOG_FAIL_THRESHOLD == 0):
            send_watchdog_alert(config, failures, fr)
        return False
    return True


def _run_cycle():
    """Run the full scrape -> rate -> alert -> state -> dashboard cycle.

    Returns (status, summary). Status is 'ok' or 'degraded'.
    """
    global _seen_before
    log("=== Run started ===")
    config = load_config()
    state = load_state()

    _seen_before = set(state.get("seen", {}).keys())

    # --- Fetch & filter ---
    all_listings, source_counts = _collect_raw_listings(config)
    log(f"Total raw listings: {len(all_listings)} ({source_counts or 'no sources configured'})")

    filtered = filter_listings(all_listings, config)
    log(f"After filtering: {len(filtered)} listings")

    # Never resurrect previously-sold listings (rating-trust, issue 01/06).
    sold_ids = set(state.get("sold", {}).keys())
    if sold_ids:
        kept = [l for l in filtered if l["id"] not in sold_ids]
        if len(kept) != len(filtered):
            log(f"Excluded {len(filtered) - len(kept)} previously-sold listing(s)")
            filtered = kept

    filtered = enrich_with_sqft(filtered)

    overrides = config.get("sqft_overrides", {})
    for l in filtered:
        if l["id"] in overrides and not l.get("sqft"):
            l["sqft"] = overrides[l["id"]]

    for l in filtered:
        if l.get("sqft") and not l.get("sqm"):
            l["sqm"] = round(l["sqft"] * 0.0929)

    # --- Sold prices & market context ---
    postcode_areas = set()
    for l in filtered:
        area = extract_postcode_area(l["address"])
        if area:
            postcode_areas.add(area)
    if not postcode_areas:
        postcode_areas = {"WF16"}

    all_sold = {}
    for area in postcode_areas:
        all_sold[area] = fetch_sold_prices(area)

    # Optional EPC bedroom data (only when a free EPC API key is configured);
    # enables true "3-bed, same type, same street" comparables.
    epc_maps = {}
    for area in postcode_areas:
        m = fetch_epc_bedrooms(area, config)
        if m:
            epc_maps[area] = m

    # Sold-cache meta (fetch dates) so the persisted evidence basis records
    # which cache each score relied on (rating-trust, issue 04).
    sold_meta = {}
    sold_cache = _load_sold_cache()
    for area in postcode_areas:
        cached = sold_cache.get(area)
        if isinstance(cached, dict):
            sold_meta[area] = {"fetched": cached.get("fetched")}

    # Negotiation/estimate cap overrides (config negotiation_caps).
    caps = {**NEGOTIATION_CAPS, **(config.get("negotiation_caps") or {})}

    # Market temperature per property type (terraced/semi move differently)
    market_temps = {}
    for area, sold in all_sold.items():
        if not sold:
            continue
        listed_types = sorted({_type_key(l) for l in filtered if _type_key(l)})
        for t in listed_types:
            temp = calculate_market_temperature(sold, t)
            market_temps[f"{area}|{t}"] = temp
            log(f"  {area} {t}: {temp['detail']}")

    # --- Scoring ---
    for listing in filtered:
        area = extract_postcode_area(listing["address"]) or next(iter(postcode_areas))
        _attach_derived(
            listing, all_sold, postcode_areas, state, filtered,
            epc_maps.get(area), market_temps, caps, sold_meta,
        )

    filtered.sort(key=lambda l: l["confidence"]["score"], reverse=True)

    # --- Sold / STC detection (rating-trust, issue 06) ---
    sold_rows = []
    try:
        sold_events = _check_sold_statuses(filtered, state)
        sold_rows = _record_sold(state, sold_events)
    except Exception as e:
        log(f"SOLD-TRACKING ERROR: {e}")
    if sold_rows:
        sold_now = {r["id"] for r in sold_rows}
        filtered = [l for l in filtered if l["id"] not in sold_now]
        for r in sold_rows:
            log(
                f"  SOLD: {r['status'].upper()} - {r['address']} "
                f"(&pound;{r['price']:,}) in {r['days_on_market']} day(s) on market"
            )

    # --- Alerts ---
    new_listings, price_drops = find_alerts(filtered, state)

    if new_listings or price_drops:
        log(f"ALERTS: {len(new_listings)} new, {len(price_drops)} price drops")
        for l in new_listings:
            log(f"  NEW: \xa3{l['price']:,} {l['bedrooms']}-bed {l['type']} - {l['address']} [{l['source']}] conf={l['confidence']['score']}")
        for l in price_drops:
            log(
                f"  DROP: \xa3{l['old_price']:,}->\xa3{l['price']:,} "
                f"{l['bedrooms']}-bed {l['type']} - {l['address']} [{l['source']}]"
            )
        try:
            subject = send_email(config, new_listings, price_drops)
            if subject:
                log(f"Email sent: {subject}")
        except Exception as e:
            log(f"EMAIL ERROR: {e}")
        try:
            send_telegram_alert(config, new_listings, price_drops)
        except Exception as e:
            log(f"TELEGRAM ERROR: {e}")
    else:
        log(f"No alerts ({len(filtered)} listings in range)")

    # --- State persistence ---
    _update_state(state, filtered, source_counts)
    healthy = _assess_health(config, state, source_counts, len(filtered))
    state["last_run"] = datetime.now().isoformat()
    save_state(state)

    # --- Dashboard ---
    generate_html(filtered, market_temps, state)

    status = "ok" if healthy else "degraded"
    log(f"=== Run complete (status={status}) ===")
    return status, {
        "new": len(new_listings),
        "drops": len(price_drops),
        "sold": len(sold_rows),
        "filtered": len(filtered),
    }


def parse_args(argv=None):
    """Parse CLI arguments (rating-trust issue 07 adds --outcome/--status)."""
    import argparse

    parser = argparse.ArgumentParser(description="Property watch monitor")
    parser.add_argument(
        "--server", action="store_true",
        help="run the read-only dashboard server (binds 127.0.0.1)",
    )
    parser.add_argument("--port", type=int, default=8080, help="port for --server")
    parser.add_argument(
        "--outcome", metavar="LISTING_ID",
        help="record a real-world outcome for a tracked listing",
    )
    parser.add_argument(
        "--status", choices=["sold", "stc", "lost-bid", "withdrawn"],
        help="outcome status (required with --outcome)",
    )
    parser.add_argument("--days", type=int, help="listing age in days at outcome")
    parser.add_argument("--note", help="free-text note for the outcome")
    return parser.parse_args(argv)


def record_outcome(listing_id, status, days=None, note=None):
    """Record a user-reported outcome in state.json (rating-trust, issue 07).

    ``sold``/``stc`` transition the listing from ``seen`` into ``sold``; every
    status appends to the top-level ``outcomes`` map. Unknown listing ids
    raise ValueError.
    """
    state = load_state()
    known = (
        listing_id in state.get("seen", {})
        or listing_id in state.get("sold", {})
        or listing_id in state.get("off_market", {})
    )
    if not known:
        raise ValueError(f"Unknown listing: {listing_id}")
    seen = state.setdefault("seen", {})
    outcomes = state.setdefault("outcomes", {})
    now_iso = datetime.now().isoformat()
    outcomes.setdefault(listing_id, []).append({
        "date": now_iso,
        "status": status,
        "days": days,
        "note": note,
        "source": "user",
    })
    if status in ("sold", "stc") and listing_id in seen:
        entry = seen.pop(listing_id)
        sold = state.setdefault("sold", {})
        sold[listing_id] = {
            "address": entry.get("address"),
            "price": entry.get("price"),
            "sqft": entry.get("sqft"),
            "source": entry.get("source"),
            "first_seen": entry.get("first_seen") or now_iso,
            "sold_date": now_iso,
            "status": status,
            "days_on_market": days if days is not None else 0,
            "evidence_basis": entry.get("evidence_basis"),
        }
    save_state(state)
    log(f"Outcome recorded: {listing_id} = {status}" + (f" ({note})" if note else ""))


def main(argv=None):
    args = parse_args(argv)
    if args.outcome:
        if not args.status:
            log("ERROR: --outcome requires --status (sold|stc|lost-bid|withdrawn)")
            return 2
        try:
            record_outcome(args.outcome, args.status, days=args.days, note=args.note)
        except ValueError as e:
            log(f"ERROR: {e}")
            return 2
        return 0
    if args.server:
        run_server(args.port)
        return 0
    status, summary = _run_cycle()
    if os.environ.get("PROPERTY_WATCH_SKIP_PUSH"):
        log("Skipping git push (PROPERTY_WATCH_SKIP_PUSH is set)")
    else:
        push_to_github()
    return 0
class PropertyHandler(BaseHTTPRequestHandler):
    """HTTP handler serving the generated dashboard (read-only)."""

    def log_message(self, format, *args):
        log(f"SERVER: {args[0]}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_FILE.read_bytes())
        else:
            self.send_response(404)
            self.end_headers()


def run_server(port=8080):
    """Run the dashboard as a local read-only web server (binds 127.0.0.1)."""
    log("Server mode - running initial cycle...")
    try:
        _run_cycle()
    except Exception as e:
        log(f"Initial cycle failed, serving last generated dashboard: {e}")

    server = HTTPServer(("127.0.0.1", port), PropertyHandler)
    log(f"Dashboard running at http://127.0.0.1:{port}")
    log("Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Server stopped")
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
