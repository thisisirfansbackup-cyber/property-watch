"""Tests for state persistence: price history, off-market, re-listing."""
import json

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
def test_save_state_is_atomic_and_backs_up_previous(tmp_path, monkeypatch):
    """save_state must never leave a torn state.json, and .bak keeps the prior state."""
    state_file = tmp_path / "state.json"
    bak_file = tmp_path / "state.json.bak"
    monkeypatch.setattr(watch, "STATE_FILE", state_file)
    monkeypatch.setattr(watch, "STATE_BAK", bak_file)
    state_file.write_text(json.dumps({"seen": {}, "failed_runs": 0}))

    watch.save_state({"seen": {"otm-1": {"price": 100000}}, "failed_runs": 3})

    state = json.loads(state_file.read_text())
    assert state["seen"]["otm-1"]["price"] == 100000
    assert state["failed_runs"] == 3
    # The backup holds the previous state, and no temp file is left behind.
    backup = json.loads(bak_file.read_text())
    assert backup["seen"] == {}
    assert not list(tmp_path.glob("state.json.tmp"))


def test_load_state_falls_back_to_backup_on_corrupt_json(tmp_path, monkeypatch):
    """A crash mid-write must not brick the pipeline: recover from state.json.bak."""
    state_file = tmp_path / "state.json"
    bak_file = tmp_path / "state.json.bak"
    monkeypatch.setattr(watch, "STATE_FILE", state_file)
    monkeypatch.setattr(watch, "STATE_BAK", bak_file)
    state_file.write_text("{this is truncated json, not valid")
    bak_file.write_text(json.dumps({"seen": {"otm-1": {"price": 100000}}}))

    state = watch.load_state()

    assert state["seen"]["otm-1"]["price"] == 100000


def test_load_state_starts_fresh_when_both_state_and_backup_corrupt(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    bak_file = tmp_path / "state.json.bak"
    monkeypatch.setattr(watch, "STATE_FILE", state_file)
    monkeypatch.setattr(watch, "STATE_BAK", bak_file)
    state_file.write_text("not json")
    bak_file.write_text("also not json")

    state = watch.load_state()

    assert state["seen"] == {}
    assert state["failed_runs"] == 0