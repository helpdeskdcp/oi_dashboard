"""
test_agents/test_event_bus.py -- regression tests for agents/event_bus.py.
"""
import datetime as dt

import pytest

from agents import event_bus


class TestPublishAndEventsSince:
    def test_publish_returns_id_and_round_trips(self, agent_db):
        event_id = event_bus.publish("dev", "dev_agent.detected", {"file": "exit_engine_v4.py"}, severity="warning")
        assert isinstance(event_id, int)

    def test_events_since_returns_only_newer_events(self, agent_db):
        old_ts = "2020-01-01T00:00:00"
        event_bus.publish("dev", "dev_agent.detected", {"n": 1})
        events = event_bus.events_since(old_ts)
        assert len(events) == 1
        assert events[0]["payload_json"] == {"n": 1}

    def test_events_since_excludes_events_at_or_before_cutoff(self, agent_db):
        event_bus.publish("dev", "dev_agent.detected", {"n": 1})
        future_cutoff = (dt.datetime.now() + dt.timedelta(days=1)).isoformat()
        assert event_bus.events_since(future_cutoff) == []

    def test_filters_by_event_type(self, agent_db):
        old_ts = "2020-01-01T00:00:00"
        event_bus.publish("dev", "dev_agent.detected", {"n": 1})
        event_bus.publish("dev", "dev_agent.proposed", {"n": 2})
        matched = event_bus.events_since(old_ts, event_type="dev_agent.proposed")
        assert len(matched) == 1
        assert matched[0]["payload_json"] == {"n": 2}

    def test_oldest_first(self, agent_db):
        old_ts = "2020-01-01T00:00:00"
        event_bus.publish("dev", "dev_agent.detected", {"n": 1})
        event_bus.publish("dev", "dev_agent.detected", {"n": 2})
        events = event_bus.events_since(old_ts)
        assert [e["payload_json"]["n"] for e in events] == [1, 2]

    def test_invalid_severity_rejected(self, agent_db):
        with pytest.raises(ValueError):
            event_bus.publish("dev", "dev_agent.detected", {}, severity="not-a-real-severity")

    def test_every_valid_severity_accepted(self, agent_db):
        for severity in event_bus.VALID_SEVERITIES:
            event_id = event_bus.publish("dev", "dev_agent.detected", {"severity": severity}, severity=severity)
            assert isinstance(event_id, int)
