"""
test_agents/conftest.py -- shared fixtures for the agent framework tests.
Same throwaway-DB technique as test_backtest_profiles.py (monkeypatch
DB_PATH to a tmp_path file) -- no real oi_history.db touched, no live
data threads.
"""
import pytest

from agents import audit_log, config, event_bus
from agents.ops import event_log as ops_event_log
from agents.risk_manager import risk_store
from agents.runtime import runtime_store
from agents.shadow_mode import store as shadow_store
from agents.sys_admin import sysadmin_store
from agents.trading_supervisor import supervision_store


@pytest.fixture()
def agent_db(monkeypatch, tmp_path):
    """Points agents.audit_log, agents.event_bus, agents.risk_manager.
    risk_store, agents.trading_supervisor.supervision_store,
    agents.sys_admin.sysadmin_store, agents.runtime.runtime_store
    (Milestone 9), AND agents.shadow_mode.store (Milestone 12, Phase 3
    -- agent_runtime.run_agent_cycle("shadow_mode", ...) reads it via
    shadow_api.get_status()) at the same throwaway SQLite file (matching
    production, where all their tables live in oi_history.db) and
    creates their tables."""
    db_path = str(tmp_path / "test_agents.db")
    monkeypatch.setattr(audit_log, "DB_PATH", db_path)
    monkeypatch.setattr(event_bus, "DB_PATH", db_path)
    monkeypatch.setattr(risk_store, "DB_PATH", db_path)
    monkeypatch.setattr(supervision_store, "DB_PATH", db_path)
    monkeypatch.setattr(sysadmin_store, "DB_PATH", db_path)
    monkeypatch.setattr(runtime_store, "DB_PATH", db_path)
    monkeypatch.setattr(shadow_store, "DB_PATH", db_path)
    monkeypatch.setattr(ops_event_log, "DB_PATH", db_path)
    audit_log.init_db()
    event_bus.init_db()
    risk_store.init_db()
    supervision_store.init_db()
    sysadmin_store.init_db()
    runtime_store.init_db()
    shadow_store.init_db()
    ops_event_log.init_db()
    return db_path


@pytest.fixture(autouse=True)
def _isolated_memory_db(monkeypatch, tmp_path):
    """Autouse, unlike agent_db above -- detector.detect()/
    patcher.generate_patch()/pipeline.run_proposal() all default to
    agents.memory.get_memory_store() when a test doesn't explicitly
    inject a memory_store, and that default reads
    agents.config.MEMORY_DB_PATH. Without this, any test in this suite
    that exercises the Milestone 3 LLM path without passing memory_store
    would silently write into this repo's real oi_history.db."""
    monkeypatch.setattr(config, "MEMORY_DB_PATH", str(tmp_path / "test_memory.db"))
