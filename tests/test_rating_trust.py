"""Rating-trust tests: evidence grades, clearing estimate, deal quality,
negotiation caps, wobble guard, EPC coverage, sold detection, outcomes.

Spec: .scratch/rating-trust/spec.md (issues 01-07).
"""
import datetime
import json

import pytest

import watch


def make_listing(**overrides):
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


def make_sale(price, date, type="terraced", street="FIRTHCLIFFE ROAD",
              town="LIVERSEDGE", postcode="WF15 8AN", paon=""):
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


def recent_date(days_ago=30):
    return (datetime.datetime.now() - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%d")


def leyland_listing(**overrides):
    """The real-world complaint: Leyland Road, Batley WF17, manual listing."""
    listing = {
        "id": "rm-91657857",
        "source": "Rightmove",
        "address": "Leyland Road, Batley, WF17",
        "price": 170000,
        "bedrooms": 3,
        "type": "end of terrace",
        "url": "https://www.rightmove.co.uk/properties/91657857",
        "agent": "Watsons",
        "image": "",
        "sqft": 872,
        "first_seen": datetime.datetime.now().isoformat(),
    }
    listing.update(overrides)
    return listing


def district_pool():
    """WF17-style district pool: no Leyland Road sales, cheap central streets.

    Prices chosen so the time-weighted median (all sales within 6 months,
    each expanded x2) is exactly 137000: 8 values -> index 7 = 136000 and
    index 8 = 138000.
    """
    prices = [80000, 100000, 120000, 136000, 138000, 150000, 160000, 205000]
    streets = ["MILL STREET", "CHASTER STREET", "BACK CARLINGHOW LANE",
               "CLOUGH DRIVE", "BRIDLE STREET", "NEW STREET", "MARKET PLACE",
               "KIRKLEES WAY"]
    return [
        make_sale(p, recent_date(20 + i * 9), "terraced", street=s,
                  town="BATLEY", postcode=f"WF17 {i}AA")
        for i, (p, s) in enumerate(zip(prices, streets))
    ]


# ---------------------------------------------------------------------------
# Issue 02: evidence grade matrix
# ---------------------------------------------------------------------------

def test_grade_street_level_fresh_five_is_high():
    assert watch._comparables_grade(1, 5, recent_date(30)) == "HIGH"


def test_grade_street_level_stale_sales_not_high():
    assert watch._comparables_grade(1, 6, "2024-01-01") == "MEDIUM"


def test_grade_street_level_three_is_medium():
    assert watch._comparables_grade(1, 3, recent_date(30)) == "MEDIUM"


def test_grade_street_level_two_is_low():
    assert watch._comparables_grade(1, 2, recent_date(30)) == "LOW"


def test_grade_sector_eight_is_medium_seven_is_low():
    assert watch._comparables_grade(2, 8, recent_date(30)) == "MEDIUM"
    assert watch._comparables_grade(2, 7, recent_date(30)) == "LOW"


def test_grade_district_always_low():
    assert watch._comparables_grade(3, 225, recent_date(5)) == "LOW"
    assert watch._comparables_grade(4, 500, recent_date(5)) == "LOW"


def test_find_comparables_attaches_grade():
    listing = leyland_listing()
    comps = watch.find_comparables(listing, district_pool())
    assert comps["tier"] == 3
    assert comps["grade"] == "LOW"


def test_find_comparables_street_tier_is_high():
    sales = [
        make_sale(150000, recent_date(10)),
        make_sale(155000, recent_date(20)),
        make_sale(160000, recent_date(30)),
        make_sale(158000, recent_date(40)),
        make_sale(162000, recent_date(50)),
    ]
    listing = {"address": "Firthcliffe Road, Liversedge, WF15", "type": "terraced"}
    comps = watch.find_comparables(listing, sales)
    assert comps["tier"] == 1
    assert comps["grade"] == "HIGH"


# ---------------------------------------------------------------------------
# Issue 03: clearing estimate
# ---------------------------------------------------------------------------

def test_estimate_low_grade_anchors_to_asking():
    est = watch.calculate_estimate(leyland_listing(), {"grade": "LOW", "median": 137500})
    assert est["mid"] == 170000
    assert est["low"] == 144500  # ±15%
    assert est["high"] == 195500
    assert est["grade"] == "LOW"


def test_estimate_unscored_is_point_at_asking():
    est = watch.calculate_estimate(leyland_listing(), None)
    assert est["low"] == est["high"] == est["mid"] == 170000
    assert est["grade"] == "UNSCORED"


def test_estimate_medium_grade_pulls_down_bounded_by_cap():
    comps = {"grade": "MEDIUM", "median": 137500, "count": 10,
             "comps": [], "label": "same type, sector WF17 9"}
    est = watch.calculate_estimate(leyland_listing(), comps)
    # asking 23.6% over median > 10% cap -> pulled to max(median, asking*0.90)
    assert est["mid"] == 153000
    assert est["low"] == round(153000 * 0.92)


def test_estimate_high_grade_pulls_to_15pc_bound():
    comps = {"grade": "HIGH", "median": 137500, "count": 6, "comps": [], "label": "x"}
    est = watch.calculate_estimate(leyland_listing(), comps)
    assert est["mid"] == 144500  # max(median, asking * 0.85)


def test_estimate_no_pull_when_asking_near_median():
    comps = {"grade": "MEDIUM", "median": 165000, "count": 10, "comps": [], "label": "x"}
    est = watch.calculate_estimate(leyland_listing(), comps)
    assert est["mid"] == 170000  # 3% over median <= 10% cap -> no pull


def test_estimate_temperature_nudge_capped_at_3pc():
    comps = {"grade": "LOW", "median": 137500}
    rising = {"trend": "rising", "change_pct": 6.0, "recent_count": 5, "older_count": 5}
    cooling = {"trend": "cooling", "change_pct": -6.0, "recent_count": 5, "older_count": 5}
    assert watch.calculate_estimate(leyland_listing(), comps, rising)["mid"] == round(170000 * 1.03)
    assert watch.calculate_estimate(leyland_listing(), comps, cooling)["mid"] == round(170000 * 0.97)


def test_estimate_temperature_ignored_without_two_cohorts():
    comps = {"grade": "LOW", "median": 137500}
    temp = {"trend": "stable", "change_pct": 0, "recent_count": 5, "older_count": 0}
    assert watch.calculate_estimate(leyland_listing(), comps, temp)["mid"] == 170000


# ---------------------------------------------------------------------------
# Issue 02/03: weight multipliers, badge gating, negotiation caps
# ---------------------------------------------------------------------------

def comps_with_grade(grade, median=137500):
    return {"tier": 3, "label": "same type, WF17 district", "count": 225,
            "median": median, "comps": [], "grade": grade}


def test_weight_multiplier_by_grade():
    listing = leyland_listing()
    _, low_bd = watch.calculate_confidence(listing, [], [listing], comps_with_grade("LOW"))
    _, high_bd = watch.calculate_confidence(listing, [], [listing], comps_with_grade("HIGH"))
    assert low_bd["_weights"]["area_median"] == pytest.approx(0.30 * 0.5)
    assert high_bd["_weights"]["area_median"] == pytest.approx(0.30)


def test_confidence_excludes_price_factor_when_unscored():
    listing = leyland_listing(first_seen=None, sqft=None)
    score, bd = watch.calculate_confidence(listing, [], [listing], None)
    assert bd["area_median"]["score"] is None
    assert "area_median" not in bd["_weights"]


def test_badge_low_grade_never_says_overpriced():
    for score in (29, 45, 70, 85):
        css, text, label = watch._confidence_badge(score, "LOW")
        assert label == "Low Evidence"
        assert text == str(score)


def test_badge_medium_high_keeps_bands():
    assert watch._confidence_badge(85, "HIGH")[2] == "Great Deal"
    assert watch._confidence_badge(65, "MEDIUM")[2] == "Fair Price"
    assert watch._confidence_badge(45, "MEDIUM")[2] == "Overpriced"
    assert watch._confidence_badge(29, "HIGH")[2] == "Way Over"


def test_negotiation_capped_at_low_grade():
    listing = leyland_listing()
    comps = comps_with_grade("LOW")
    neg = watch.calculate_negotiation(listing, [], comps)
    assert neg["label"] != "overpriced"
    assert neg["low"] >= 161500  # asking * (1 - 0.05)


def test_negotiation_cap_uses_config_override():
    listing = leyland_listing()
    comps = comps_with_grade("LOW")
    neg = watch.calculate_negotiation(listing, [], comps, caps={"low": 0.02})
    assert neg["low"] >= 166600  # asking * (1 - 0.02)


def test_negotiation_high_grade_still_allows_overpriced():
    listing = leyland_listing()
    comps = comps_with_grade("HIGH")
    neg = watch.calculate_negotiation(listing, [], comps)
    assert neg["label"] == "overpriced"



# ---------------------------------------------------------------------------
# Issue 07 acceptance: the Leyland Road replay
# ---------------------------------------------------------------------------

def test_leyland_road_replay():
    """The exact complaint: tier-3 district comps must not produce an
    overpriced verdict, and the estimate must not undercut the asking price."""
    listing = leyland_listing()
    sold = district_pool()

    comps = watch.find_comparables(listing, sold)
    assert comps["grade"] == "LOW"
    assert comps["median"] == 137000

    est = watch.calculate_estimate(listing, comps)
    assert est["mid"] >= listing["price"]
    assert est["low"] == 144500 and est["high"] == 195500

    score, bd = watch.calculate_confidence(listing, sold, [listing], comps)
    assert bd["area_median"]["score"] is not None
    css, text, label = watch._confidence_badge(score, comps["grade"])
    assert label not in ("Overpriced", "Way Over")

    neg = watch.calculate_negotiation(listing, sold, comps)
    assert neg["label"] != "overpriced"
    assert neg["low"] >= 161500
    assert "130,625" not in neg["range_text"].replace("&pound;", "\xa3")


# ---------------------------------------------------------------------------
# Issue 04: wobble guard
# ---------------------------------------------------------------------------

def _attach(state, listing):
    sold = {"WF17": district_pool()}
    watch._attach_derived(
        listing, sold, {"WF17"}, state, [listing],
        market_temps={}, caps=None, sold_meta={},
    )
    return listing


def test_wobble_first_run_commits_category():
    state = {"seen": {}}
    listing = _attach(state, leyland_listing())
    assert listing["verdict"]["category"] in ("great", "fair", "over", "bad")
    assert listing["verdict"]["pending"] is None


def test_wobble_category_flip_held_for_two_runs():
    state = {"seen": {}}
    listing = _attach(state, leyland_listing())
    watch._update_state(state, [listing], {})

    # Force a committed category that differs from what the next run computes
    other = {"great", "fair", "over", "bad"} - {listing["verdict"]["category"]}
    forced = sorted(other)[0]
    state["seen"][listing["id"]]["verdict_category"] = forced
    state["seen"][listing["id"]]["pending_verdict"] = None
    state["seen"][listing["id"]]["evidence_basis"] = listing["evidence_basis"]

    listing2 = _attach(state, leyland_listing())
    if listing2["verdict"]["category"] == forced:
        pytest.skip("computed category equal to forced category")
    assert listing2["verdict"]["category"] == forced  # held for first run
    assert listing2["verdict"]["pending"]["category"] != forced

    # Second consecutive run on the same basis -> commit the flip
    watch._update_state(state, [listing2], {})
    listing3 = _attach(state, leyland_listing())
    assert listing3["verdict"]["category"] == listing2["verdict"]["pending"]["category"]
    assert listing3["verdict"]["pending"] is None


def test_wobble_price_change_commits_immediately():
    state = {"seen": {}}
    listing = _attach(state, leyland_listing())
    watch._update_state(state, [listing], {})

    # Seed a committed category that differs from what the next run computes
    other = {"great", "fair", "over", "bad"} - {listing["verdict"]["category"]}
    state["seen"][listing["id"]]["verdict_category"] = sorted(other)[0]

    dropped = _attach(state, leyland_listing(price=150000))
    assert dropped["verdict"]["pending"] is None  # price change -> immediate


def test_wobble_basis_change_sets_revised_note():
    state = {"seen": {}}
    listing = _attach(state, leyland_listing())
    watch._update_state(state, [listing], {})
    # Change the basis behind the scorer's back
    state["seen"][listing["id"]]["evidence_basis"] = {
        "tier": 1, "label": "same type, your street", "count": 5,
        "area": "WF17", "cache_fetched": None,
    }
    listing2 = _attach(state, leyland_listing())
    assert listing2["verdict"]["revised"] is True


def test_update_state_persists_wobble_fields():
    state = {"seen": {}}
    listing = _attach(state, leyland_listing())
    watch._update_state(state, [listing], {})
    entry = state["seen"][listing["id"]]
    assert entry["verdict_category"] == listing["verdict"]["category"]
    assert entry["evidence_basis"]["area"] == "WF17"
    assert entry["evidence_basis"]["tier"] == 3



# ---------------------------------------------------------------------------
# Issue 05: EPC floor areas + coverage fallback
# ---------------------------------------------------------------------------

def epc_map_with_areas(sales, areas, missing_idx=()):
    epc = {}
    for i, s in enumerate(sales):
        key = (
            (s.get("postcode") or "").upper().replace(" ", ""),
            (s.get("street") or "").strip().lower(),
            (s.get("paon") or "").strip().lower(),
        )
        if i in missing_idx:
            continue
        epc[key] = {"beds": 3, "area_sqm": areas[i]}
    return epc


def test_epc_lookup_bedrooms_handles_legacy_and_new_values():
    sale = make_sale(150000, "2026-06-01", paon="12")
    assert watch._epc_lookup_bedrooms(sale, {("WF158AN", "firthcliffe road", "12"): 3}) == 3
    assert watch._epc_lookup_bedrooms(
        sale, {("WF158AN", "firthcliffe road", "12"): {"beds": 4, "area_sqm": 80.0}}
    ) == 4
    assert watch._epc_lookup_bedrooms(sale, {}) is None


def test_epc_lookup_floor_area():
    sale = make_sale(150000, "2026-06-01", paon="12")
    assert watch._epc_lookup_floor_area(
        sale, {("WF158AN", "firthcliffe road", "12"): {"beds": 3, "area_sqm": 80.0}}
    ) == 80.0
    assert watch._epc_lookup_floor_area(sale, {("WF158AN", "firthcliffe road", "12"): 3}) is None


def test_factor2_uses_real_epc_sizes_when_coverage_sufficient():
    sales = [
        make_sale(150000, recent_date(10), paon="12"),
        make_sale(155000, recent_date(20), paon="14"),
        make_sale(160000, recent_date(30), paon="16"),
        make_sale(158000, recent_date(40), paon="18"),
        make_sale(162000, recent_date(50), paon="20"),
    ]
    areas = [70.0, 74.0, 72.0, 76.0, 73.0]  # 5/5 matched = 100% coverage
    epc = epc_map_with_areas(sales, areas)
    listing = make_listing(price=160000, sqft=800)  # £200/sqft
    score, bd = watch.calculate_confidence(listing, sales, [listing], epc_map=epc)
    assert bd["sqft_value"]["score"] is not None
    assert "EPC-matched" in bd["sqft_value"]["detail"]


def test_factor2_excluded_when_coverage_below_60pc():
    sales = [
        make_sale(150000, recent_date(10), paon="12"),
        make_sale(155000, recent_date(20), paon="14"),
        make_sale(160000, recent_date(30), paon="16"),
        make_sale(158000, recent_date(40), paon="18"),
        make_sale(162000, recent_date(50), paon="20"),
    ]
    areas = [70.0, 74.0, 72.0, 76.0, 73.0]
    epc = epc_map_with_areas(sales, areas, missing_idx={0, 1, 2})  # 2/5 = 40%
    listing = make_listing(price=160000, sqft=800)
    score, bd = watch.calculate_confidence(listing, sales, [listing], epc_map=epc)
    assert bd["sqft_value"]["score"] is None


def test_factor2_excluded_without_epc_map():
    listing = make_listing(price=160000, sqft=800)
    sales = [
        make_sale(150000, recent_date(10), paon="12"),
        make_sale(155000, recent_date(20), paon="14"),
        make_sale(160000, recent_date(30), paon="16"),
    ]
    score, bd = watch.calculate_confidence(listing, sales, [listing])
    assert bd["sqft_value"]["score"] is None

# ---------------------------------------------------------------------------
# Issue 06: sold / STC detection
# ---------------------------------------------------------------------------

def test_match_sold_marker_variants():
    assert watch._match_sold_marker("<title>12 Foo Road - Sold STC</title>") == "stc"
    assert watch._match_sold_marker("This property is now Under Offer") == "under_offer"
    assert watch._match_sold_marker("The house has been sold") == "sold"
    assert watch._match_sold_marker("See sold prices near this home") is None
    assert watch._match_sold_marker("Sold Price History of the street") is None
    assert watch._match_sold_marker("For sale, offers over") is None


def test_match_sold_marker_nav_widget_not_a_sale():
    """The 'recently sold & under offer' nav widget appears on every Rightmove
    listing page — it must never be treated as a status marker for THIS
    property (the defect that wrongly archived Hare Park Drive and Hadfield Road).
    """
    page = (
        '<html><body>'
        '<a title="recently sold & under offer - see similar nearby properties">'
        'See nearby</a>'
        '<h1>12 Some Road</h1>'
        '</body></html>'
    )
    assert watch._match_sold_marker(page) is None
    # The "and" variant (Rightmove sometimes uses & or and)
    page2 = page.replace("&amp;", "and")
    assert watch._match_sold_marker(page2) is None
    # A real under-offer banner elsewhere on the page must still be detected
    page3 = page + "<span>Under Offer</span>"
    assert watch._match_sold_marker(page3) == "under_offer"


def test_detect_listing_status_200_with_marker(monkeypatch):
    class FakeResp:
        status_code = 200
        text = "<html>Sold STC</html>"

    class FakeSession:
        def get(self, url, timeout=20):
            return FakeResp()

    monkeypatch.setattr(watch, "SESSION", FakeSession())
    assert watch.detect_listing_status({"id": "rm-1", "url": "https://x"}) == "stc"


def test_detect_listing_status_404_is_removed(monkeypatch):
    class FakeResp:
        status_code = 404
        text = ""

    class FakeSession:
        def get(self, url, timeout=20):
            return FakeResp()

    monkeypatch.setattr(watch, "SESSION", FakeSession())
    assert watch.detect_listing_status({"id": "rm-1", "url": "https://x"}) == "removed"


def test_detect_listing_status_network_error_never_marks_sold(monkeypatch):
    class FakeSession:
        def get(self, url, timeout=20):
            raise OSError("boom")

    monkeypatch.setattr(watch, "SESSION", FakeSession())
    assert watch.detect_listing_status({"id": "rm-1", "url": "https://x"}) == "error"


def test_check_sold_statuses_removed_requires_two_consecutive():
    state = {"seen": {"rm-1": {"price": 1, "address": "x", "removed_misses": 0}}}
    listing = {"id": "rm-1", "url": "u", "source": "Rightmove"}

    def fake_status(l):
        return "removed"

    orig = watch.detect_listing_status
    watch.detect_listing_status = fake_status
    try:
        assert watch._check_sold_statuses([listing], state) == []
        assert state["seen"]["rm-1"]["removed_misses"] == 1
        events = watch._check_sold_statuses([listing], state)
        assert len(events) == 1 and events[0]["status"] == "removed"
    finally:
        watch.detect_listing_status = orig


def test_record_sold_moves_seen_to_sold_and_outcomes():
    now_iso = datetime.datetime.now().isoformat()
    state = {
        "seen": {
            "rm-9": {
                "price": 170000, "address": "Leyland Road, Batley, WF17",
                "sqft": 872, "source": "Rightmove", "first_seen": now_iso,
                "evidence_basis": {"tier": 3},
            }
        }
    }
    events = [{"id": "rm-9", "status": "stc", "url": "u", "source": "Rightmove",
               "address": "Leyland Road, Batley, WF17", "price": 170000}]
    rows = watch._record_sold(state, events)
    assert "rm-9" not in state["seen"]
    assert state["sold"]["rm-9"]["status"] == "stc"
    assert state["sold"]["rm-9"]["days_on_market"] == 0
    assert state["outcomes"]["rm-9"][0]["source"] == "auto"
    assert rows[0]["address"] == "Leyland Road, Batley, WF17"


def test_sold_listings_not_readded_by_pipeline(sandbox, monkeypatch):
    """A listing already in state['sold'] must not re-enter the pipeline."""
    state = {
        "sold": {"otm-111111": {"status": "sold"}},
        "seen": {},
        "off_market": {},
        "run_history": [],
        "failed_runs": 0,
    }
    (sandbox / "state_file.tmp").write_text(json.dumps(state))
    status, summary = watch._run_cycle()
    state = json.loads((sandbox / "state_file.tmp").read_text())
    assert "otm-111111" not in state["seen"]
    assert "otm-111111" in state["sold"]


def test_manual_listing_marked_sold_in_pipeline(sandbox, monkeypatch):
    config = json.loads((sandbox / "config_file.tmp").read_text())
    config["manual_listings"] = [{
        "id": "rm-999", "source": "Rightmove",
        "address": "Test Road, Heckmondwike, WF16", "price": 170000,
        "bedrooms": 3, "type": "terraced", "url": "https://x/rm-999",
        "agent": "A", "image": "", "sqft": 800,
    }]
    (sandbox / "config_file.tmp").write_text(json.dumps(config))
    monkeypatch.setattr(watch, "detect_listing_status", lambda l: "stc")

    status, summary = watch._run_cycle()
    assert summary["sold"] == 1
    state = json.loads((sandbox / "state_file.tmp").read_text())
    assert "rm-999" in state["sold"]
    assert state["sold"]["rm-999"]["status"] == "stc"
    assert "rm-999" not in state["seen"]
# ---------------------------------------------------------------------------
# Issue 07: --outcome CLI
# ---------------------------------------------------------------------------

def test_parse_args_outcome():
    args = watch.parse_args(["--outcome", "rm-1", "--status", "sold", "--days", "7", "--note", "x"])
    assert args.outcome == "rm-1" and args.status == "sold" and args.days == 7


def test_parse_args_server_mode_preserved():
    args = watch.parse_args(["--server", "--port=9090"])
    assert args.server is True and args.port == 9090


def test_parse_args_defaults_run_pipeline():
    args = watch.parse_args([])
    assert args.server is False and args.outcome is None


def test_record_outcome_user_sold_transitions_seen(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    now_iso = datetime.datetime.now().isoformat()
    state_file.write_text(json.dumps({
        "seen": {"rm-1": {"price": 170000, "address": "X", "first_seen": now_iso,
                          "source": "Rightmove", "sqft": 800}},
    }))
    monkeypatch.setattr(watch, "STATE_FILE", state_file)
    monkeypatch.setattr(watch, "STATE_BAK", tmp_path / "state.bak")

    watch.record_outcome("rm-1", "sold", days=7, note="cleared above asking")
    state = json.loads(state_file.read_text())
    assert "rm-1" not in state["seen"]
    assert state["sold"]["rm-1"]["status"] == "sold"
    assert state["sold"]["rm-1"]["days_on_market"] == 7
    assert state["outcomes"]["rm-1"][0]["source"] == "user"
    assert state["outcomes"]["rm-1"][0]["note"] == "cleared above asking"


def test_record_outcome_lost_bid_stays_active(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"seen": {"rm-1": {"price": 1, "address": "X"}}}))
    monkeypatch.setattr(watch, "STATE_FILE", state_file)
    monkeypatch.setattr(watch, "STATE_BAK", tmp_path / "state.bak")

    watch.record_outcome("rm-1", "lost-bid", days=5, note="lost to higher bid")
    state = json.loads(state_file.read_text())
    assert "rm-1" in state["seen"]
    assert state["outcomes"]["rm-1"][0]["status"] == "lost-bid"


def test_record_outcome_unknown_listing_is_error(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"seen": {}}))
    monkeypatch.setattr(watch, "STATE_FILE", state_file)
    monkeypatch.setattr(watch, "STATE_BAK", tmp_path / "state.bak")

    with pytest.raises(ValueError):
        watch.record_outcome("rm-unknown", "sold")


def test_main_outcome_mode_rejects_missing_status(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"seen": {"rm-1": {"price": 1}}}))
    monkeypatch.setattr(watch, "STATE_FILE", state_file)
    monkeypatch.setattr(watch, "STATE_BAK", tmp_path / "state.bak")

    assert watch.main(["--outcome", "rm-1"]) == 2


def test_main_outcome_mode_happy_path(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"seen": {"rm-1": {"price": 1, "address": "X"}}}))
    monkeypatch.setattr(watch, "STATE_FILE", state_file)
    monkeypatch.setattr(watch, "STATE_BAK", tmp_path / "state.bak")

    assert watch.main(["--outcome", "rm-1", "--status", "lost-bid", "--days", "3"]) == 0
    state = json.loads(state_file.read_text())
    assert state["outcomes"]["rm-1"][0]["days"] == 3
