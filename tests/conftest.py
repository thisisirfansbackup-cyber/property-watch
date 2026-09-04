"""Shared fixtures: repo-root importability + pipeline sandbox.

The ``sandbox`` fixture redirects runtime files to tmp_path and stubs all
network/side-effect functions so ``_run_cycle`` is fully hermetic. It is
shared by test_pipeline.py and test_rating_trust.py.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import watch

SANDBOX_CONFIG = {
    "filters": {
        "bedrooms": 3,
        "min_price": 120000,
        "max_price": 220000,
        "property_types": ["Terraced", "Semi-detached", "End of terrace"],
    },
    "search": {"centre": "Heckmondwike, WF16", "radius_miles": 2},
    "sources": ["ontemarket", "barkers"],
    "manual_listings": [],
    "sqft_overrides": {},
    "email": {"sender": "", "recipient": ""},
    "telegram": {},
}

SANDBOX_LISTINGS = [
    {
        "id": "otm-111111",
        "source": "OnTheMarket",
        "address": "Firthcliffe Road, Liversedge, WF15",
        "price": 160000,
        "bedrooms": 3,
        "type": "terraced house",
        "url": "https://example.com/1",
        "agent": "Agent A",
        "image": "",
        "sqft": 800,
    },
    {
        "id": "barkers-222222",
        "source": "Barkers",
        "address": "Union Road, Heckmondwike, WF16",
        "price": 170000,
        "bedrooms": 3,
        "type": "semi-detached",
        "url": "https://example.com/2",
        "agent": "Barkers Estate Agents",
        "image": "",
        "sqft": 850,
    },
]

SANDBOX_SOLD = [
    {"price": 150000, "date": "2026-06-01", "type": "terraced", "street": "FIRTHCLIFFE ROAD", "town": "LIVERSEDGE"},
    {"price": 160000, "date": "2026-05-01", "type": "terraced", "street": "FIRTHCLIFFE ROAD", "town": "LIVERSEDGE"},
    {"price": 170000, "date": "2026-07-01", "type": "semi-detached", "street": "UNION ROAD", "town": "HECKMONDWIKE"},
]


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect runtime files to temp and stub all network/side-effect functions."""
    for name in (
        "CONFIG_FILE",
        "STATE_FILE",
        "STATE_BAK",
        "LOG_FILE",
        "HTML_FILE",
        "SOLD_CACHE_FILE",
        "LOCAL_CONFIG_FILE",
    ):
        monkeypatch.setattr(watch, name, tmp_path / f"{name.lower()}.tmp")
    (tmp_path / "config_file.tmp").write_text(json.dumps(SANDBOX_CONFIG))
    (tmp_path / "state_file.tmp").write_text(json.dumps({}))

    monkeypatch.setattr(watch, "fetch_ontemarket", lambda cfg: [dict(SANDBOX_LISTINGS[0])])
    monkeypatch.setattr(watch, "fetch_barkers", lambda cfg: [dict(SANDBOX_LISTINGS[1])])
    monkeypatch.setattr(watch, "fetch_sold_prices", lambda area: [dict(s) for s in SANDBOX_SOLD])
    monkeypatch.setattr(watch, "enrich_with_sqft", lambda listings: [dict(l) for l in listings])
    monkeypatch.setattr(watch, "send_email", lambda *a, **k: "subject")
    monkeypatch.setattr(watch, "send_telegram_alert", lambda *a, **k: True)
    monkeypatch.setattr(watch, "push_to_github", lambda: None)
    monkeypatch.setattr(watch, "detect_listing_status", lambda l: None)
    return tmp_path