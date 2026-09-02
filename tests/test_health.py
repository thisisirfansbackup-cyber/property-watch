"""Tests for the run-health watchdog assessment."""
import watch


def test_healthy_run_then_recovery():
    config = {"telegram": {}}
    state = {}

    ok = watch._assess_health(config, state, {"ontemarket": 5}, 5)
    assert ok
    assert state["failed_runs"] == 0
    assert state["last_successful_run"]

    ok = watch._assess_health(config, state, {"ontemarket": 0, "barkers": 0}, 0)
    assert not ok
    assert state["failed_runs"] == 1

    ok = watch._assess_health(config, state, {"ontemarket": 5}, 5)
    assert ok
    assert state["failed_runs"] == 0


def test_source_zero_flagged_when_normally_present():
    config = {"telegram": {}}
    state = {}

    watch._assess_health(config, state, {"ontemarket": 5}, 3)
    ok = watch._assess_health(config, state, {"ontemarket": 0}, 0)
    assert not ok


def test_filtered_collapse_detected():
    config = {"telegram": {}}
    state = {}
    for _ in range(3):
        watch._assess_health(config, state, {"ontemarket": 10}, 10)
    ok = watch._assess_health(config, state, {"ontemarket": 2}, 2)
    assert not ok
    assert state["failed_runs"] == 1


def test_watchdog_threshold_crossed_sends_alert(monkeypatch):
    config = {"telegram": {"chat_id": "123"}}
    sent = []
    monkeypatch.setattr(
        watch, "get_secret", lambda cfg, env, path: "fake-token"
    )
    monkeypatch.setattr(
        watch, "_telegram_send_message",
        lambda token, chat_id, text: sent.append((token, chat_id, text)) or True,
    )
    state = {}
    for _ in range(watch.WATCHDOG_FAIL_THRESHOLD):
        watch._assess_health(config, state, {"ontemarket": 0, "barkers": 0}, 0)
    assert len(sent) == 1
    assert "PROPERTY WATCH PROBLEM" in sent[0][2]