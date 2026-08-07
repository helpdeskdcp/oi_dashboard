import pytest

from agents.runtime import runtime_events as re


class TestEmit:
    def test_emit_rejects_unknown_event_type(self, agent_db):
        with pytest.raises(ValueError):
            re.emit("x", "not_a_real_event_type", {})

    def test_emit_publishes_to_the_shared_event_bus(self, agent_db):
        re.emit("test_agent", re.MARKET_OPEN, {"note": "x"})
        events = re.poll("2020-01-01T00:00:00")
        assert any(e["event_type"] == re.MARKET_OPEN for e in events)

    def test_default_severity_applied_for_known_critical_types(self, agent_db):
        re.emit("risk_manager", re.RISK_ALERT, {})
        events = re.poll("2020-01-01T00:00:00", event_types=(re.RISK_ALERT,))
        assert events[0]["severity"] == "critical"

    def test_explicit_severity_overrides_default(self, agent_db):
        re.emit("risk_manager", re.RISK_ALERT, {}, severity="warning")
        events = re.poll("2020-01-01T00:00:00", event_types=(re.RISK_ALERT,))
        assert events[0]["severity"] == "warning"


class TestPoll:
    def test_poll_without_filter_returns_everything_since(self, agent_db):
        re.emit("a", re.MARKET_OPEN, {})
        re.emit("b", re.BROKER_CONNECTED, {})
        events = re.poll("2020-01-01T00:00:00")
        assert {e["event_type"] for e in events} == {re.MARKET_OPEN, re.BROKER_CONNECTED}

    def test_poll_with_filter_returns_only_matching_types_in_ts_order(self, agent_db):
        re.emit("a", re.MARKET_OPEN, {})
        re.emit("b", re.BROKER_CONNECTED, {})
        re.emit("c", re.MARKET_CLOSE, {})
        events = re.poll("2020-01-01T00:00:00", event_types=(re.MARKET_OPEN, re.MARKET_CLOSE))
        assert [e["event_type"] for e in events] == [re.MARKET_OPEN, re.MARKET_CLOSE]

    def test_all_event_types_are_unique(self):
        assert len(re.ALL_EVENT_TYPES) == len(set(re.ALL_EVENT_TYPES))
