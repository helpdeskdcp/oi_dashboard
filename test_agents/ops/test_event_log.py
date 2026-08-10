"""
test_agents/ops/test_event_log.py -- Milestone 16, Phase 1: Persistent
Runtime Event Log. Own throwaway-DB fixture (event_log.py has no
dependency on the shared agent_db fixture's other tables), matching
test_intelligence_alerts.py's own alerts_db pattern.
"""
import datetime as dt

import pytest

from agents.ops import event_log, models


@pytest.fixture()
def ops_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "ops.db")
    monkeypatch.setattr(event_log, "DB_PATH", db_path)
    event_log.init_db()
    return db_path


class TestRecordAndGetEvents:
    def test_insert_and_count(self, ops_db):
        event_log.record_event(models.SCHEDULER_STARTED, {"resumed_workflow_count": 0})
        assert event_log.count_events() == 1

    def test_rejects_unknown_event_type(self, ops_db):
        with pytest.raises(ValueError):
            event_log.record_event("not_a_real_event_type", {})

    def test_payload_round_trips_as_a_dict(self, ops_db):
        event_log.record_event(models.ALERT_SENT, {"symbol": "NIFTY", "attempt": 1})
        row = event_log.get_events(limit=1)[0]
        assert row["payload"] == {"symbol": "NIFTY", "attempt": 1}
        assert row["event_type"] == models.ALERT_SENT
        assert row["ts"]

    def test_events_are_newest_first(self, ops_db):
        for i in range(5):
            event_log.record_event(
                models.HEARTBEAT_UPDATED, {"i": i}, now=dt.datetime(2026, 8, 10, 9, i, 0),
            )
        events = event_log.get_events(limit=5)
        assert [e["payload"]["i"] for e in events] == [4, 3, 2, 1, 0]


class TestPagination:
    def test_limit_restricts_page_size(self, ops_db):
        for i in range(10):
            event_log.record_event(models.HEARTBEAT_UPDATED, {"i": i}, now=dt.datetime(2026, 8, 10, 9, i, 0))
        assert len(event_log.get_events(limit=3)) == 3

    def test_offset_skips_newest_rows(self, ops_db):
        for i in range(5):
            event_log.record_event(models.HEARTBEAT_UPDATED, {"i": i}, now=dt.datetime(2026, 8, 10, 9, i, 0))
        page2 = event_log.get_events(limit=2, offset=2)
        assert [e["payload"]["i"] for e in page2] == [2, 1]


class TestFiltering:
    def test_filters_by_event_type(self, ops_db):
        event_log.record_event(models.ALERT_SENT, {})
        event_log.record_event(models.ALERT_SUPPRESSED, {})
        event_log.record_event(models.ALERT_SENT, {})
        assert len(event_log.get_events(event_type=models.ALERT_SENT)) == 2
        assert event_log.count_events(event_type=models.ALERT_SUPPRESSED) == 1

    def test_unfiltered_returns_all_types(self, ops_db):
        event_log.record_event(models.ALERT_SENT, {})
        event_log.record_event(models.RATE_LIMIT_HIT, {})
        assert event_log.count_events() == 2


class TestRetentionPurge:
    def test_purge_removes_rows_older_than_retention(self, ops_db):
        old = dt.datetime.now() - dt.timedelta(days=31)
        recent = dt.datetime.now() - dt.timedelta(days=1)
        event_log.record_event(models.HEARTBEAT_UPDATED, {"which": "old"}, now=old)
        event_log.record_event(models.HEARTBEAT_UPDATED, {"which": "recent"}, now=recent)
        deleted = event_log.purge_old_events(30)
        assert deleted == 1
        remaining = event_log.get_events(limit=10)
        assert len(remaining) == 1
        assert remaining[0]["payload"]["which"] == "recent"

    def test_purge_returns_zero_when_nothing_is_old_enough(self, ops_db):
        event_log.record_event(models.HEARTBEAT_UPDATED, {})
        assert event_log.purge_old_events(30) == 0
        assert event_log.count_events() == 1

    def test_default_retention_days(self):
        from agents import config
        assert config.OPS_EVENT_RETENTION_DAYS == 30

    def test_purge_never_touches_agent_events_table(self, ops_db):
        """Explicit safety guarantee -- see event_log.py's own module
        docstring: agent_events holds audit-significant governance
        events that must never be time-purged. Static check that
        purge_old_events()'s own CODE (not its docstring, which
        explains this exact guarantee in prose) never references that
        table name."""
        import ast
        import inspect
        source = inspect.getsource(event_log.purge_old_events)
        tree = ast.parse(source)
        func_body = tree.body[0].body
        if func_body and isinstance(func_body[0], ast.Expr) and isinstance(func_body[0].value, ast.Constant):
            func_body = func_body[1:]  # drop the docstring
        code_only = ast.unparse(ast.Module(body=func_body, type_ignores=[]))
        assert "agent_events" not in code_only
        assert "ops_event_log" in code_only


class TestPersistenceAcrossReloads:
    def test_persistence_survives_store_reload(self, ops_db):
        event_log.record_event(models.SCHEDULER_STARTED, {"resumed_workflow_count": 2})

        import importlib
        reloaded = importlib.reload(event_log)
        reloaded.DB_PATH = ops_db

        events = reloaded.get_events(limit=1)
        assert len(events) == 1
        assert events[0]["payload"]["resumed_workflow_count"] == 2


class TestEventTaxonomy:
    def test_all_required_event_types_exist(self):
        required = (
            "SCHEDULER_STARTED", "SCHEDULER_STOPPED", "HEARTBEAT_UPDATED", "ALERT_SENT", "ALERT_SUPPRESSED",
            "RATE_LIMIT_HIT", "RETRY_SCHEDULED", "RETRY_EXHAUSTED", "CIRCUIT_OPENED", "CIRCUIT_HALF_OPEN",
            "CIRCUIT_CLOSED", "WATCHDOG_STALE_CYCLE",
        )
        for name in required:
            assert hasattr(models, name)
            assert getattr(models, name) in models.ALL_EVENT_TYPES

    def test_no_duplicate_event_type_strings(self):
        assert len(models.ALL_EVENT_TYPES) == len(set(models.ALL_EVENT_TYPES))
