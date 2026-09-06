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
    comps = watch.find_comparables(listing, SOLD)
    listing["comparables"] = comps["comps"] if comps else []
    listing["negotiation"] = watch.calculate_negotiation(listing, SOLD, comps)
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
    assert "Sold Comparables Used" in html
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


def test_filter_listings_excludes_ids():
    config = {**CONFIG, "excluded_ids": ["otm-111111"]}
    kept = watch.filter_listings([dict(LISTINGS[0]), dict(LISTINGS[1])], config)
    assert [l["id"] for l in kept] == ["barkers-222222"]


def test_filter_listings_without_excluded_ids_keeps_all():
    kept = watch.filter_listings([dict(LISTINGS[0]), dict(LISTINGS[1])], CONFIG)
    assert [l["id"] for l in kept] == ["otm-111111", "barkers-222222"]
def test_run_cycle_flags_degraded_when_sold_prices_unavailable(sandbox, monkeypatch):
    """Score evidence silently vanishing must surface as a degraded run."""
    monkeypatch.setattr(watch, "fetch_sold_prices", lambda area: [])
    monkeypatch.setattr(watch, "fetch_epc_bedrooms", lambda area, config: None)

    status, summary = watch._run_cycle()

    assert status == "degraded"
    state = json.loads((sandbox / "state_file.tmp").read_text())
    assert state["failed_runs"] == 1
def test_find_alerts_new_and_price_drop():
    state = {"seen": {"otm-111111": {"price": 180000}}}
    new_listings, price_drops = watch.find_alerts([dict(LISTINGS[0]), dict(LISTINGS[1])], state)
    assert [l["id"] for l in new_listings] == ["barkers-222222"]
    assert [l["id"] for l in price_drops] == ["otm-111111"]
    assert price_drops[0]["old_price"] == 180000


def test_find_alerts_no_alerts_when_nothing_changed():
    state = {"seen": {"otm-111111": {"price": LISTINGS[0]["price"]}}}
    new_listings, price_drops = watch.find_alerts([dict(LISTINGS[0])], state)
    assert new_listings == []
    assert price_drops == []


def test_find_alerts_lenient_when_legacy_seen_entry_has_no_price():
    """A seen entry written before the price field existed must not crash the
    whole run with a KeyError (alerts are the core goal; a stale state.json
    must not silence them)."""
    state = {"seen": {"otm-111111": {"address": "x"}}}
    new_listings, price_drops = watch.find_alerts([dict(LISTINGS[0])], state)
    assert new_listings == []
    assert price_drops == []


def test_removed_listing_returns_to_market_reenters_tracking(sandbox):
    """A listing archived as 'removed' (delisted URL, no sale happened) that
    comes back on the portal must re-enter the dashboard — a re-listing is
    exactly what the buyer must not miss. Sold/STC stays archived forever,
    but 'removed' must not permanently blind the tracker. The listing
    re-enters as a tracked card preserving its original first_seen (no
    NEW-alert spam for a URL that may still be dead this run)."""
    state = {
        "sold": {
            "rm-1": {
                "status": "removed", "price": 170000,
                "address": "Leyland Road, Batley, WF17",
                "source": "Rightmove", "first_seen": "2026-08-01T00:00:00",
                "sold_date": "2026-09-01T00:00:00", "days_on_market": 5,
            }
        },
        "seen": {}, "off_market": {}, "run_history": [], "failed_runs": 0,
    }
    (sandbox / "state_file.tmp").write_text(json.dumps(state))

    config = json.loads((sandbox / "config_file.tmp").read_text())
    config["manual_listings"] = [{
        "id": "rm-1", "source": "Rightmove",
        "address": "Leyland Road, Batley, WF17", "price": 170000,
        "bedrooms": 3, "type": "terraced", "url": "https://x/rm-1",
        "agent": "A", "image": "", "sqft": 800,
    }]
    (sandbox / "config_file.tmp").write_text(json.dumps(config))

    status, summary = watch._run_cycle()  # conftest stubs detect_listing_status -> None (alive)

    assert status == "ok"
    # otm + barkers are new; rm-1 re-enters silently as a tracked card.
    assert summary["new"] == 2
    state = json.loads((sandbox / "state_file.tmp").read_text())
    assert "rm-1" in state["seen"]                 # re-entered tracking
    assert "rm-1" not in state["sold"]             # stale 'removed' archive cleared
    assert state["seen"]["rm-1"]["first_seen"] == "2026-08-01T00:00:00"
    html = (sandbox / "html_file.tmp").read_text()
    assert "Leyland Road, Batley" in html          # back on the dashboard


def test_removed_listing_still_dead_does_not_flap_alerts(sandbox, monkeypatch):
    """A manual listing that stays delisted (config still lists it, URL still
    404s) must not alternate between NEW alerts and re-archival every cycle:
    it is re-admitted, the status poll re-archives it in the same run, and no
    alert fires."""
    state = {
        "sold": {
            "rm-1": {
                "status": "removed", "price": 170000,
                "address": "Leyland Road, Batley, WF17",
                "source": "Rightmove", "first_seen": "2026-08-01T00:00:00",
                "sold_date": "2026-09-01T00:00:00", "days_on_market": 5,
            }
        },
        "seen": {}, "off_market": {}, "run_history": [], "failed_runs": 0,
    }
    (sandbox / "state_file.tmp").write_text(json.dumps(state))

    config = json.loads((sandbox / "config_file.tmp").read_text())
    config["manual_listings"] = [{
        "id": "rm-1", "source": "Rightmove",
        "address": "Leyland Road, Batley, WF17", "price": 170000,
        "bedrooms": 3, "type": "terraced", "url": "https://x/rm-1",
        "agent": "A", "image": "", "sqft": 800,
    }]
    (sandbox / "config_file.tmp").write_text(json.dumps(config))
    monkeypatch.setattr(watch, "detect_listing_status", lambda l: "removed")

    status, summary = watch._run_cycle()

    assert status == "ok"
    assert summary["new"] == 2           # only otm + barkers; dead rm-1 not alerted
    state = json.loads((sandbox / "state_file.tmp").read_text())
    assert "rm-1" not in state["seen"]   # re-archived in the same run
    assert state["sold"]["rm-1"]["status"] == "removed"