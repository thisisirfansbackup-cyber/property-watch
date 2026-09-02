"""Tests for state persistence: price history, off-market, re-listing."""
import watch


def make_listing(lid="test-1", price=150000):
    return {
        "id": lid,
        "source": "Manual",
        "address": "Test Road, Heckmondwike, WF15",
        "price": price,
        "bedrooms": 3,
        "type": "terraced",
        "url": "",
        "agent": "Test",
        "image": "",
        "sqft": 800,
    }


def test_new_listing_gets_price_history():
    state = {}
    watch._update_state(state, [make_listing()], {})
    seen = state["seen"]["test-1"]
    assert seen["price"] == 150000
    assert len(seen["price_history"]) == 1
    assert seen["price_history"][0]["price"] == 150000
    assert seen["first_seen"] == seen["last_seen"]
    assert seen["misses"] == 0


def test_price_change_appends_history_and_keeps_first_seen():
    state = {}
    watch._update_state(state, [make_listing(price=150000)], {})
    watch._update_state(state, [make_listing(price=145000)], {})
    seen = state["seen"]["test-1"]
    assert len(seen["price_history"]) == 2
    assert seen["price_history"][-1]["price"] == 145000
    assert seen["first_seen"] == seen["price_history"][0]["date"]


def test_unchanged_price_does_not_duplicate_history():
    state = {}
    watch._update_state(state, [make_listing()], {})
    watch._update_state(state, [make_listing()], {})
    assert len(state["seen"]["test-1"]["price_history"]) == 1


def test_off_market_after_two_consecutive_misses():
    state = {}
    watch._update_state(state, [make_listing()], {})
    watch._update_state(state, [], {})  # first miss
    assert "test-1" in state["seen"]
    assert "test-1" not in state["off_market"]
    watch._update_state(state, [], {})  # second consecutive miss
    assert "test-1" not in state["seen"]
    assert "test-1" in state["off_market"]
    assert state["off_market"]["test-1"]["price"] == 150000
    assert state["off_market"]["test-1"]["address"] == make_listing()["address"]


def test_relisting_restores_first_seen_and_clears_off_market():
    state = {}
    watch._update_state(state, [make_listing()], {})
    first_seen = state["seen"]["test-1"]["first_seen"]
    watch._update_state(state, [], {})
    watch._update_state(state, [], {})
    assert "test-1" in state["off_market"]

    # reappears -> treated as re-listed with preserved first_seen
    watch._update_state(state, [make_listing()], {})
    assert "test-1" in state["seen"]
    assert "test-1" not in state["off_market"]
    assert state["seen"]["test-1"]["first_seen"] == first_seen
    assert state["seen"]["test-1"]["misses"] == 0


def test_single_miss_then_return_resets_miss_counter():
    state = {}
    watch._update_state(state, [make_listing()], {})
    watch._update_state(state, [], {})  # 1 miss
    watch._update_state(state, [make_listing()], {})  # back
    assert state["seen"]["test-1"]["misses"] == 0
    watch._update_state(state, [], {})  # 1 miss again
    assert "test-1" in state["seen"]  # still not off-market


def test_off_market_list_bounded():
    state = {"seen": {}}
    # Push 40 distinct listings off-market
    for i in range(40):
        state["seen"][f"lid-{i}"] = {
            "price": 150000,
            "address": f"Street {i}",
            "sqft": None,
            "source": "Manual",
            "first_seen": "2026-01-01T00:00:00",
            "last_seen": f"2026-08-{min(i + 1, 30):02d}T00:00:00",
            "price_history": [{"date": "2026-01-01T00:00:00", "price": 150000}],
        }
    watch._update_state(state, [], {})
    watch._update_state(state, [], {})
    assert len(state["off_market"]) <= 30