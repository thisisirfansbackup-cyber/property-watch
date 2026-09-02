"""Parser tests against captured HTML fixtures (tests/fixtures/*.html).

Refresh fixtures with the live pages whenever a source changes its markup.
"""
import re
from pathlib import Path

import watch

FIXTURES = Path(__file__).parent / "fixtures"


def test_otm_fixture_parses_listings():
    html = (FIXTURES / "onthemarket_search.html").read_text()
    blocks = re.findall(r'<li id="result-(\d+)"[^>]*>(.*?)</li>', html, re.DOTALL)
    assert len(blocks) >= 3, "OTM fixture should contain several listing blocks"
    listings = []
    for pid, blk in blocks:
        listing = watch._parse_otm_listing(pid, blk)
        if listing:
            listings.append(listing)
    assert listings
    first = listings[0]
    assert first["id"].startswith("otm-")
    assert first["price"] > 0
    assert first["url"].startswith("https://www.onthemarket.com/details/")


def test_barkers_fixture_parses_listings():
    html = (FIXTURES / "barkers_search.html").read_text()
    listings = watch._parse_barkers_page(html)
    assert len(listings) >= 3
    first = listings[0]
    assert first["id"].startswith("barkers-")
    assert first["price"] > 0
    assert first["url"].startswith("https://www.barkersestateagents.co.uk")
    assert first["address"]


def test_otm_malformed_block_returns_none():
    assert watch._parse_otm_listing("999", "<div>no price here</div>") is None


def test_filter_listings_applies_bedroom_price_type_band():
    config = {
        "filters": {
            "bedrooms": 3,
            "min_price": 120000,
            "max_price": 220000,
            "property_types": ["Terraced", "Semi-detached"],
        }
    }
    listings = [
        {"id": "a", "bedrooms": 3, "price": 150000, "type": "terraced"},
        {"id": "b", "bedrooms": 2, "price": 150000, "type": "terraced"},
        {"id": "c", "bedrooms": 3, "price": 500000, "type": "terraced"},
        {"id": "d", "bedrooms": 3, "price": 150000, "type": "detached"},
    ]
    filtered = watch.filter_listings(listings, config)
    assert [l["id"] for l in filtered] == ["a"]


def test_extract_postcode_area_from_address():
    assert watch.extract_postcode_area("Union Road, Liversedge, WF15") == "WF15"
    assert watch.extract_postcode_area("Somewhere, LS1 4AP") == "LS1"
    assert watch.extract_postcode_area("No postcode here") == ""