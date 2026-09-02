"""Unit tests for the pure scoring / negotiation / mortgage / comparables math."""
import datetime

import pytest

import watch


def make_listing(**overrides):
    """A listing that passes the default filters."""
    listing = {
        "id": "otm-test1",
        "source": "OnTheMarket",
        "address": "Firthcliffe Road, Liversedge, WF15",
        "price": 160000,
        "bedrooms": 3,
        "type": "terraced",
        "url": "https://example.com/1",
        "agent": "Test Agent",
        "image": "",
        "sqft": 800,
        "first_seen": datetime.datetime.now().isoformat(),
    }
    listing.update(overrides)
    return listing


def make_sale(price, date, type="terraced", street="FIRTHCLIFFE ROAD", town="LIVERSEDGE"):
    return {
        "price": price,
        "date": date,
        "type": type,
        "tenure": "freehold",
        "street": street,
        "town": town,
    }


def test_time_weight_recency():
    now = datetime.datetime.now()
    assert watch._time_weight((now - datetime.timedelta(days=30)).strftime("%Y-%m-%d")) == 1.0
    assert watch._time_weight((now - datetime.timedelta(days=200)).strftime("%Y-%m-%d")) == 0.5
    assert watch._time_weight((now - datetime.timedelta(days=400)).strftime("%Y-%m-%d")) == 0.25
    assert watch._time_weight("not-a-date") == 0.25


def test_weighted_median_filters_by_type():
    sales = [
        make_sale(100000, "2026-01-01", "terraced"),
        make_sale(200000, "2026-01-01", "semi-detached"),
    ]
    assert watch._weighted_median(sales, "terraced") == 100000
    assert watch._weighted_median(sales, "semi-detached") == 200000
    assert watch._weighted_median(sales, "detached") == 0


def test_weighted_mean_basic():
    sales = [make_sale(100000, "2026-01-01"), make_sale(200000, "2026-01-01")]
    assert watch._weighted_mean(sales) == 150000


def test_smooth_score_interpolation():
    breakpoints = [(0.8, 100), (0.9, 85), (1.0, 65), (1.1, 40), (1.2, 15), (1.3, 0)]
    assert watch._smooth_score(0.8, breakpoints) == 100
    assert watch._smooth_score(0.9, breakpoints) == 85
    assert watch._smooth_score(0.85, breakpoints) == pytest.approx(92.5)
    assert watch._smooth_score(0.5, breakpoints) == 100
    assert watch._smooth_score(2.0, breakpoints) == 0


def test_market_temperature_rising():
    now = datetime.datetime.now()
    recent = [(now - datetime.timedelta(days=30)).strftime("%Y-%m-%d")] * 5
    older = [(now - datetime.timedelta(days=240)).strftime("%Y-%m-%d")] * 5
    sales = [make_sale(200000, d) for d in recent] + [make_sale(150000, d) for d in older]
    temp = watch.calculate_market_temperature(sales)
    assert temp["trend"] == "rising"
    assert temp["change_pct"] > 2


def test_market_temperature_insufficient_data():
    temp = watch.calculate_market_temperature([make_sale(150000, "2026-01-01")])
    assert temp["trend"] == "stable"


def test_estimate_mortgage_below_threshold():
    m = watch.estimate_mortgage(150000)
    assert m["deposit"] == 45000
    assert m["loan"] == 105000
    assert m["monthly"] > 0
    assert m["stamp_duty"] == 0


def test_estimate_mortgage_stamp_duty_applies():
    m = watch.estimate_mortgage(350000)
    assert m["stamp_duty"] > 0
    assert m["loan"] == 305000


def test_negotiation_insufficient_data():
    listing = make_listing(price=150000)
    assert watch.calculate_negotiation(listing, [])["range_text"] == "Insufficient data"


def test_negotiation_overpriced_label():
    listing = make_listing(price=250000)
    sales = [make_sale(150000, "2026-01-01"), make_sale(160000, "2026-07-01")]
    neg = watch.calculate_negotiation(listing, sales)
    assert neg["label"] in ("overpriced", "negotiate")


def test_confidence_cheaper_scores_higher():
    sales = [
        make_sale(150000, "2026-06-01", "terraced"),
        make_sale(160000, "2026-05-01", "terraced"),
    ]
    listings = [
        make_listing(price=140000),
        make_listing(price=220000),
    ]
    low_conf, _ = watch.calculate_confidence(listings[0], sales, listings)
    high_conf, _ = watch.calculate_confidence(listings[1], sales, listings)
    assert low_conf > high_conf


def test_confidence_repeat_drops_boost_score():
    history = [
        {"date": "2026-07-01T00:00:00", "price": 200000},
        {"date": "2026-08-01T00:00:00", "price": 180000},
        {"date": "2026-09-01T00:00:00", "price": 160000},
    ]
    sales = [make_sale(150000, "2026-06-01", "terraced")]
    single = make_listing(price=160000)
    multi = make_listing(price=160000, price_history=history, old_price=200000)
    conf_single, _ = watch.calculate_confidence(single, sales, [single])
    conf_multi, _ = watch.calculate_confidence(multi, sales, [multi])
    assert conf_multi > conf_single


def test_street_comparables_matches_only_same_type_on_street():
    listing = {"address": "Firthcliffe Road, Liversedge, WF15", "type": "terraced"}
    sold = [
        {"price": 150000, "date": "2026-06-01", "type": "terraced", "street": "FIRTHCLIFFE ROAD"},
        {"price": 200000, "date": "2026-05-01", "type": "semi-detached", "street": "FIRTHCLIFFE ROAD"},
        {"price": 160000, "date": "2024-01-01", "type": "terraced", "street": "OTHER ROAD"},
    ]
    comps = watch.find_street_comparables(listing, sold)
    assert len(comps) == 1
    assert comps[0]["price"] == 150000


def test_street_comparables_empty_when_short_street():
    listing = {"address": "WF15 something", "type": "terraced"}
    assert watch.find_street_comparables(listing, []) == []