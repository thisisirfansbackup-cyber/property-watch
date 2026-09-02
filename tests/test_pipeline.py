"""End-to-end pipeline tests with mocked fetchers and temp runtime files."""
import json
from datetime import datetime
from pathlib import Path

import pytest

import watch

CONFIG = {
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

LISTINGS = [
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

SOLD = [
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
    (tmp_path / "config_file.tmp").write_text(json.dumps(CONFIG))
    (tmp_path / "state_file.tmp").write_text(json.dumps({}))

    monkeypatch.setattr(watch, "fetch_ontemarket", lambda cfg: [dict(LISTINGS[0])])
    monkeypatch.setattr(watch, "fetch_barkers", lambda cfg: [dict(LISTINGS[1])])
    monkeypatch.setattr(watch, "fetch_sold_prices", lambda area: [dict(s) for s in SOLD])
    monkeypatch.setattr(watch, "enrich_with_sqft", lambda listings: [dict(l) for l in listings])
    monkeypatch.setattr(watch, "send_email", lambda *a, **k: "subject")
    monkeypatch.setattr(watch, "send_telegram_alert", lambda *a, **k: True)
    monkeypatch.setattr(watch, "push_to_github", lambda: None)
    return tmp_path


def test_run_cycle_updates_state_and_generates_html(sandbox):
    status, summary = watch._run_cycle()
    assert status == "ok"
    assert summary["new"] == 2
    assert summary["drops"] == 0

    state = json.loads((sandbox / "state_file.tmp").read_text())
    assert set(state["seen"].keys()) == {"otm-111111", "barkers-222222"}
    assert state["failed_runs"] == 0
    entry = state["seen"]["otm-111111"]
    assert entry["first_seen"] == entry["last_seen"]
    assert len(entry["price_history"]) == 1
    assert entry["source"] == "OnTheMarket"

    html = (sandbox / "html_file.tmp").read_text()
    assert "Firthcliffe Road" in html
    assert "Recently off market" not in html
    assert "Auto-refreshes every 30 minutes" in html


def test_run_cycle_detects_price_drop(sandbox):
    state = {
        "seen": {
            "otm-111111": {
                "price": 180000,
                "address": LISTINGS[0]["address"],
                "sqft": 800,
                "source": "OnTheMarket",
                "first_seen": "2026-08-01T00:00:00",
                "last_seen": "2026-09-01T00:00:00",
                "price_history": [{"date": "2026-08-01T00:00:00", "price": 180000}],
            }
        }
    }
    (sandbox / "state_file.tmp").write_text(json.dumps(state))

    status, summary = watch._run_cycle()
    assert status == "ok"
    assert summary["new"] == 1
    assert summary["drops"] == 1

    state = json.loads((sandbox / "state_file.tmp").read_text())
    entry = state["seen"]["otm-111111"]
    assert len(entry["price_history"]) == 2
    assert entry["price_history"][1]["price"] == 160000
    assert entry["first_seen"] == "2026-08-01T00:00:00"


def test_generate_html_smoke(sandbox, monkeypatch):
    monkeypatch.setattr(watch, "_seen_before", set())
    listing = dict(LISTINGS[0])
    listing["confidence"] = {
        "score": 85,
        "breakdown": {"area_median": {"score": 80, "detail": "x"}},
    }
    listing["mortgage"] = watch.estimate_mortgage(listing["price"])
    listing["negotiation"] = watch.calculate_negotiation(listing, SOLD)
    listing["comparables"] = watch.find_street_comparables(listing, SOLD)
    listing["price_history"] = [
        {"date": "2026-08-01T00:00:00", "price": 170000},
        {"date": "2026-09-01T00:00:00", "price": 160000},
    ]
    listing["first_seen"] = "2026-08-01T00:00:00"
    listing["rank"] = 1

    state = {
        "seen": {},
        "off_market": {},
        "run_history": [],
        "failed_runs": 0,
        "last_run": datetime.now().isoformat(),
    }
    watch.generate_html([listing], {"WF15": {"trend": "stable", "change_pct": 1}}, state)

    html = (sandbox / "html_file.tmp").read_text()
    assert "Firthcliffe Road" in html
    assert "class=\"spark\"" in html
    assert "Street Comparables" in html
    assert "Auto-refreshes every 30 minutes" in html
    assert "Recently off market" not in html


def test_generate_html_renders_off_market_section(sandbox):
    state = {
        "seen": {},
        "off_market": {
            "old-1": {
                "address": "Vicarage Road, Heckmondwike",
                "price": 155000,
                "sqft": None,
                "source": "OnTheMarket",
                "first_seen": "2026-07-01T00:00:00",
                "last_seen": "2026-08-20T00:00:00",
                "price_history": [],
            }
        },
        "run_history": [],
        "failed_runs": 0,
        "last_run": datetime.now().isoformat(),
    }
    watch.generate_html([], {}, state)
    html = (sandbox / "html_file.tmp").read_text()
    assert "Recently off market" in html
    assert "Vicarage Road" in html
    assert "50 days on market" in html


def test_health_tracking_in_state(sandbox):
    status, _ = watch._run_cycle()
    assert status == "ok"
    state = json.loads((sandbox / "state_file.tmp").read_text())
    assert state["last_successful_run"]
    assert len(state["run_history"]) == 1