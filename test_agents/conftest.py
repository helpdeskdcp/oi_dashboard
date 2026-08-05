"""
test_agents/conftest.py -- shared fixtures for the agent framework tests.
Same throwaway-DB technique as test_backtest_profiles.py (monkeypatch
DB_PATH to a tmp_path file) -- no real oi_history.db touched, no live
data threads.
"""
import pytest

from agents import audit_log, event_bus


@pytest.fixture()
def agent_db(monkeypatch, tmp_path):
    """Points BOTH agents.audit_log and agents.event_bus at the same
    throwaway SQLite file (matching production, where both tables live in
    oi_history.db) and creates their tables."""
    db_path = str(tmp_path / "test_agents.db")
    monkeypatch.setattr(audit_log, "DB_PATH", db_path)
    monkeypatch.setattr(event_bus, "DB_PATH", db_path)
    audit_log.init_db()
    event_bus.init_db()
    return db_path
