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


def make_sale(price, date, type="terraced", street="FIRTHCLIFFE ROAD", town="LIVERSEDGE", postcode="WF15 8AN", paon=""):
    return {
        "price": price,
        "date": date,
        "type": type,
        "tenure": "freehold",
        "street": street,
        "town": town,
        "postcode": postcode,
        "paon": paon,
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
    sales = [
        make_sale(150000, "2026-01-01"),
        make_sale(160000, "2026-07-01"),
        make_sale(155000, "2026-06-01"),
    ]
    neg = watch.calculate_negotiation(listing, sales, watch.find_comparables(listing, sales))
    assert neg["label"] in ("overpriced", "negotiate")


def test_negotiation_needs_three_sales():
    """Two sales are a coincidence, not a market — negotiation must refuse."""
    listing = make_listing(price=250000)
    sales = [make_sale(150000, "2026-01-01"), make_sale(160000, "2026-07-01")]
    neg = watch.calculate_negotiation(listing, sales, watch.find_comparables(listing, sales))
    assert neg["range_text"] == "Insufficient data"
    assert neg["count"] == 0


def test_negotiation_carries_tier_basis():
    listing = make_listing(price=140000)
    sales = [
        make_sale(150000, "2026-06-01"),
        make_sale(155000, "2026-05-01"),
        make_sale(160000, "2026-04-01"),
    ]
    comps = watch.find_comparables(listing, sales)
    neg = watch.calculate_negotiation(listing, sales, comps)
    assert neg["basis"] == comps["label"]
    assert neg["count"] == 3
    assert neg["median"] == comps["median"]


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


def test_find_comparables_prefers_same_type_street_tier():
    listing = {"address": "Firthcliffe Road, Liversedge, WF15", "type": "terraced"}
    sold = [
        make_sale(150000, "2026-06-01", "terraced"),
        make_sale(155000, "2026-05-01", "terraced"),
        make_sale(160000, "2026-04-01", "terraced"),
        make_sale(200000, "2026-05-01", "semi-detached"),
    ]
    result = watch.find_comparables(listing, sold)
    assert result["tier"] == 1
    assert result["label"] == "same type, your street"
    assert all(s["type"] == "terraced" for s in result["comps"])
    assert len(result["comps"]) == 3


def test_find_comparables_sector_fallback_when_street_thin():
    listing = {"address": "Firthcliffe Road, Liversedge, WF15 8AN", "type": "terraced"}
    sold = [
        make_sale(150000, "2026-06-01", "terraced", street="FIRTHCLIFFE ROAD"),
        make_sale(140000, "2026-03-01", "terraced", street="MILL LANE", postcode="WF15 8BP"),
        make_sale(145000, "2026-02-01", "terraced", street="HIGH STREET", postcode="WF15 8CC"),
    ]
    result = watch.find_comparables(listing, sold)
    assert result["tier"] == 2
    assert result["label"] == "same type, sector WF15 8"
    assert result["count"] == 3


def test_find_comparables_epc_bedroom_tier_wins():
    listing = {
        "address": "Firthcliffe Road, Liversedge, WF15",
        "type": "terraced",
        "bedrooms": 3,
    }
    sold = [
        make_sale(150000, "2026-06-01", "terraced", paon="12"),
        make_sale(155000, "2026-05-01", "terraced", paon="14"),
        make_sale(160000, "2026-04-01", "terraced", paon="16"),
    ]
    epc = {
        ("WF158AN", "firthcliffe road", "12"): 3,
        ("WF158AN", "firthcliffe road", "14"): 3,
        ("WF158AN", "firthcliffe road", "16"): 3,
    }
    result = watch.find_comparables(listing, sold, epc)
    assert result["tier"] == 0
    assert result["label"] == "3-bed, same type, your street"


def test_find_comparables_none_when_no_evidence():
    listing = {"address": "Nowhere Street, WF15", "type": "terraced"}
    sold = [make_sale(100000, "2026-01-01", "flat", street="OTHER STREET")]
    assert watch.find_comparables(listing, sold) is None


def test_type_key_maps_listing_type_variants():
    assert watch._type_key({"type": "End Terrace"}) == "terraced"
    assert watch._type_key({"type": "Mid-Terraced House"}) == "terraced"
    assert watch._type_key({"type": "Semi-Detached"}) == "semi-detached"
    assert watch._type_key({"type": "Detached"}) == "detached"
    assert watch._type_key({"type": "Bungalow"}) == ""


def test_confidence_excludes_missing_signals_and_labels_it():
    """Missing signals must be dropped and rescaled, never counted as neutral 50."""
    listing = make_listing(price=160000)
    score, breakdown = watch.calculate_confidence(listing, [], [listing])
    # listing age is the only factor with evidence here: no sold benchmark
    # (so no price or £/sqft fairness), no context, no history
    assert breakdown["_based_on"] == 1
    assert breakdown["area_median"]["score"] is None
    assert breakdown["sqft_value"]["score"] is None
    assert breakdown["market_context"]["score"] is None
    assert breakdown["price_drop"]["score"] is None
    assert breakdown["listing_age"]["score"] is not None
    assert 0 <= score <= 100


def test_confidence_zero_evidence_is_zero():
    listing = make_listing(price=160000, sqft=None, first_seen=None)
    score, breakdown = watch.calculate_confidence(listing, [], [listing])
    assert breakdown["_based_on"] == 0
    assert score == 0


def test_confidence_full_evidence_beats_partial():
    """With identical asking-vs-market prices, more evidence means more confidence."""
    sales = [
        make_sale(155000, "2026-06-01"),
        make_sale(150000, "2026-05-01"),
        make_sale(160000, "2026-04-01"),
    ]
    thin = make_listing(price=155000, sqft=None)
    full = make_listing(price=155000, sqft=800)
    thin_score, _ = watch.calculate_confidence(thin, sales, [thin])
    full_score, _ = watch.calculate_confidence(full, sales, [full])
    assert full_score > thin_score