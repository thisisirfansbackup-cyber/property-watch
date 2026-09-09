"""Regression tests for the AI-facing pipeline seams, using RECORDED API
responses (no live network): the mockable ``watch.http_get`` layer, the
startup config validator, the sqft/detail-page cache, the rule-based
market-trend predictor, the evidence-bundle helper, outcome-driven weight
tuning, and the scored-vs-recorded-outcome replay check.
"""
import datetime
import json

import watch


LR_CSV = (
    "A,B,C,D,E,F,G,H,I,J,K,L\n"
    "0,150000,2026-06-01,WF15 8AN,T,F,,,12,FIRTHCLIFFE ROAD,,LIVERSEDGE,\n"
    "0,160000,2026-05-01,WF15 8AN,T,F,,,14,FIRTHCLIFFE ROAD,,LIVERSEDGE,\n"
    "0,155000,2026-04-01,WF15 8AN,T,F,,,16,FIRTHCLIFFE ROAD,,LIVERSEDGE,\n"
)

EPC_CSV = (
    "POSTCODE,ADDRESS1,ADDRESS2,TOTAL_FLOOR_AREA,BEDROOMS\n"
    "WF15 8AN,12,FIRTHCLIFFE ROAD,70.0,3\n"
    "WF15 8AN,14,FIRTHCLIFFE ROAD,80.0,3\n"
    "WF15 8AN,16,FIRTHCLIFFE ROAD,75.0,3\n"
)


class _Resp:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise OSError(f"HTTP {self.status_code}")


def _listing(**overrides):
    listing = {
        "id": "otm-test1",
        "source": "OnTheMarket",
        "address": "12 Firthcliffe Road, Liversedge, WF15 8AN",
        "price": 155000,
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


def _sales():
    now = datetime.datetime.now()
    fmt = lambda d: (now - datetime.timedelta(days=d)).strftime("%Y-%m-%d")
    return [
        {"price": 150000, "date": fmt(30), "type": "terraced",
         "street": "FIRTHCLIFFE ROAD", "town": "LIVERSEDGE",
         "postcode": "WF15 8AN", "paon": "12"},
        {"price": 160000, "date": fmt(60), "type": "terraced",
         "street": "FIRTHCLIFFE ROAD", "town": "LIVERSEDGE",
         "postcode": "WF15 8AN", "paon": "14"},
        {"price": 155000, "date": fmt(90), "type": "terraced",
         "street": "FIRTHCLIFFE ROAD", "town": "LIVERSEDGE",
         "postcode": "WF15 8AN", "paon": "16"},
    ]


def _epc_map():
    return {
        ("WF158AN", "firthcliffe road", "12"): {"beds": 3, "area_sqm": 70.0},
        ("WF158AN", "firthcliffe road", "14"): {"beds": 3, "area_sqm": 80.0},
        ("WF158AN", "firthcliffe road", "16"): {"beds": 3, "area_sqm": 75.0},
    }


def _base_weights():
    return {"price_vs_area": 0.3, "sqft_value": 0.25, "price_drop": 0.15,
            "listing_age": 0.1, "market_context": 0.2}


def _history(medians):
    return {"market_history": {
        "WF15|terraced": [
            {"date": "2026-01-01T00:00:00", "median": m} for m in medians]}}

# --- Mockable HTTP layer: recorded Land Registry + EPC responses --------

def test_http_get_seam_replays_recorded_land_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "SOLD_CACHE_FILE", tmp_path / "sold_cache.json")
    monkeypatch.setattr(watch, "http_get", lambda *a, **k: _Resp(LR_CSV))
    result = watch.fetch_sold_prices("WF15")
    assert len(result) == 3
    assert result[0]["postcode"] == "WF15 8AN"
    assert result[0]["paon"] == "12"


def test_http_get_seam_replays_recorded_epc(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "EPC_CACHE_FILE", tmp_path / "epc_cache.json")
    monkeypatch.setattr(watch, "http_get", lambda *a, **k: _Resp(EPC_CSV))
    config = {"epc": {"email": "e@example.com", "api_key": "k"}}
    epc_map = watch.fetch_epc_bedrooms("WF15", config)
    assert watch._epc_lookup_bedrooms(
        {"postcode": "WF15 8AN", "street": "Firthcliffe Road", "paon": "12"},
        epc_map) == 3


def test_full_scoring_replays_without_live_calls():
    listing = _listing()
    score, breakdown = watch.calculate_confidence(
        listing, _sales(), [listing], epc_map=_epc_map())
    assert 0 <= score <= 100
    assert breakdown["_based_on"] >= 2


# --- Config validation at startup ---------------------------------------

def test_validate_config_accepts_good_config():
    config = {
        "filters": {"bedrooms": 3, "min_price": 120000, "max_price": 220000,
                    "property_types": ["Terraced"]},
        "search": {"centre": "Heckmondwike, WF16"},
    }
    assert watch.validate_config(config) == []


def test_validate_config_flags_malformed_settings():
    problems = watch.validate_config({
        "filters": {"bedrooms": 0, "min_price": 300000, "max_price": 100000,
                    "property_types": []},
        "search": {},
    })
    assert any("bedrooms" in p for p in problems)
    assert any("min_price" in p for p in problems)
    assert any("property_types" in p for p in problems)
    assert any("centre" in p for p in problems)


def test_validate_config_missing_filters():
    assert watch.validate_config({}) != []


def test_run_cycle_aborts_on_bad_config(sandbox):
    config = json.loads((sandbox / "config_file.tmp").read_text())
    config["filters"]["min_price"] = 999999
    config["filters"]["max_price"] = 100000
    (sandbox / "config_file.tmp").write_text(json.dumps(config))
    status, summary = watch._run_cycle()
    assert status == "degraded"
    assert summary.get("config_errors")

# --- Detail-page (sqft) cache --------------------------------------------

def test_enrich_with_sqft_uses_cache_without_refetch(tmp_path, monkeypatch):
    cache_file = tmp_path / "detail_cache.json"
    monkeypatch.setattr(watch, "DETAIL_CACHE_FILE", cache_file)
    cache_file.write_text(json.dumps({
        "otm-1": {"fetched": datetime.datetime.now().isoformat(),
                  "sqft": 800, "sqm": 74},
    }))
    calls = []
    monkeypatch.setattr(
        watch, "http_get",
        lambda *a, **k: (calls.append(a), _Resp(""))[1])
    listings = [{"id": "otm-1", "source": "OnTheMarket"}]
    out = watch.enrich_with_sqft(listings)
    assert out[0]["sqft"] == 800
    assert out[0]["sqm"] == 74
    assert calls == []


def test_enrich_with_sqft_caches_fetched_detail_page(tmp_path, monkeypatch):
    cache_file = tmp_path / "detail_cache.json"
    monkeypatch.setattr(watch, "DETAIL_CACHE_FILE", cache_file)
    body = '{"minimumAreaSqFt":900,"minimumAreaSqM":83}'
    monkeypatch.setattr(watch, "http_get", lambda *a, **k: _Resp(body))
    listings = [{"id": "otm-9", "source": "OnTheMarket"}]
    out = watch.enrich_with_sqft(listings)
    assert out[0]["sqft"] == 900
    saved = json.loads(cache_file.read_text())
    assert saved["otm-9"]["sqft"] == 900


def test_parse_detail_page_extracts_sqft():
    parsed = watch._parse_detail_page(
        '{"minimumAreaSqFt":872,"minimumAreaSqM":81}')
    assert parsed == {"sqft": 872, "sqm": 81}
    assert watch._parse_detail_page("<html>no data</html>") == {}


# --- Market-trend predictor ----------------------------------------------

def test_predict_trend_heating():
    pred = watch.predict_market_trend(_history([100000, 110000, 120000, 130000]), "WF15|terraced")
    assert pred["trend"] == "heating"
    assert pred["runs"] == 4


def test_predict_trend_cooling():
    pred = watch.predict_market_trend(_history([130000, 120000, 110000, 100000]), "WF15|terraced")
    assert pred["trend"] == "cooling"


def test_predict_trend_stable_and_unknown():
    assert watch.predict_market_trend(
        _history([100000, 101000, 100500, 101000]), "WF15|terraced")["trend"] == "stable"
    assert watch.predict_market_trend({}, "WF15|terraced")["trend"] == "unknown"
    assert watch.predict_market_trend(
        _history([100000]), "WF15|terraced")["trend"] == "unknown"


def test_record_market_snapshot_appends_and_bounds():
    state = {}
    for i in range(watch.MARKET_HISTORY_MAX + 3):
        watch.record_market_snapshot(state, {"WF15|terraced": {"median": 150000 + i}})
    assert len(state["market_history"]["WF15|terraced"]) == watch.MARKET_HISTORY_MAX

# --- Evidence bundles -----------------------------------------------------

def test_get_evidence_bundle_returns_structured_bundle():
    bundle = watch.get_evidence_bundle(_listing(), _sales())
    assert bundle["tier"] == 1
    assert bundle["grade"] in ("HIGH", "MEDIUM", "LOW")
    assert bundle["median"] > 0
    assert bundle["count"] >= 3
    assert bundle["label"]


def test_get_evidence_bundle_unscored_when_no_evidence():
    bundle = watch.get_evidence_bundle({"address": "Nowhere, WF15", "type": "terraced"}, [])
    assert bundle == {"tier": None, "grade": "UNSCORED", "median": 0,
                      "count": 0, "label": "no comparables"}


# --- Outcome-driven weight tuning ------------------------------------------

def test_tuned_weights_no_outcomes_returns_base():
    assert watch.tuned_weights({}, _base_weights()) == _base_weights()


def test_tuned_weights_lost_bid_shifts_weight_to_evidence():
    state = {"outcomes": {"rm-1": [
        {"status": "lost-bid", "predicted": 29, "cleared_above_asking": True}]}}
    tuned = watch.tuned_weights(state, _base_weights())
    assert tuned["listing_age"] < 0.1
    assert tuned["price_vs_area"] > 0.3
    assert abs(sum(tuned.values()) - 1.0) < 1e-6
    for key, val in tuned.items():
        assert abs(val - _base_weights()[key]) <= watch.WEIGHT_TUNE_CAP + 1e-9


def test_tuned_weights_cap_bounds_repeated_misses():
    state = {"outcomes": {"rm-%d" % i: [{"status": "lost-bid", "predicted": 20}]} for i in range(20)}
    tuned = watch.tuned_weights(state, _base_weights())
    for key, val in tuned.items():
        assert abs(val - _base_weights()[key]) <= watch.WEIGHT_TUNE_CAP + 1e-9


def test_tuned_weights_flow_into_confidence():
    tuned = watch.tuned_weights({"outcomes": {"rm-1": [{"status": "lost-bid", "predicted": 20}]}}, _base_weights())
    assert abs(sum(tuned.values()) - 1.0) < 1e-6
    listing = _listing()
    score, _bd = watch.calculate_confidence(listing, _sales(), [listing], epc_map=_epc_map(), base_weights=tuned)
    assert 0 <= score <= 100


# --- Outcome replay: score must not contradict recorded reality ------------

def test_leyland_replay_no_overpriced_verdict_at_low_grade():
    listing = {
        "id": "rm-91657857", "source": "Rightmove",
        "address": "Leyland Road, Batley, WF17", "price": 170000,
        "bedrooms": 3, "type": "end of terrace", "url": "https://x",
        "agent": "Watsons", "image": "", "sqft": 872,
        "first_seen": datetime.datetime.now().isoformat(),
    }
    now = datetime.datetime.now()
    sales = [
        {"price": p, "date": (now - datetime.timedelta(days=20 + i * 9)).strftime("%Y-%m-%d"),
         "type": "terraced", "street": s, "town": "BATLEY",
         "postcode": "WF17 %dAA" % i, "paon": ""}
        for i, (p, s) in enumerate(zip(
            [80000, 100000, 120000, 136000, 138000, 150000, 160000, 205000],
            ["MILL STREET", "CHASTER STREET", "BACK CARLINGHOW LANE",
             "CLOUGH DRIVE", "BRIDLE STREET", "NEW STREET", "MARKET PLACE",
             "KIRKLEES WAY"]))
    ]
    comps = watch.find_comparables(listing, sales)
    neg = watch.calculate_negotiation(listing, sales, comps)
    assert neg["label"] != "overpriced"
    assert "overpriced" not in neg["range_text"].lower()
