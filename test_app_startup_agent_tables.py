"""
test_app_startup_agent_tables.py -- regression tests for wiring the
agents/sys_admin and agents/runtime modules' own init_db() functions
into app.py's startup init_db().

Root cause this fixes: the live production oi_history.db never had
agent_audit_log, agent_status, agent_events, sysadmin_log,
runtime_policy, or runtime_workflow created -- app.py's init_db() never
called any of these six modules' own init_db(). This left /api/sysadmin/
overview's "runtime" section and /api/runtime/status's "control" section
permanently degraded ("no such table: agent_audit_log" / "control-plane
state unavailable"), even though both were designed to degrade safely
rather than crash (Milestone 12, Phase 1.1 hotfix).

Same technique as test_auth.py: SKIP_AUTOSTART=1 before importing app,
throwaway DB per test -- but ALSO monkeypatches the six agents modules'
own independent DB_PATH constants (each defaults to "oi_history.db" on
its own, not derived from app.DB_PATH -- confirmed by grep: app.py never
assigned any agents module's DB_PATH before this fix), since that's
exactly what this fix wires together.
"""
import os
os.environ["SKIP_AUTOSTART"] = "1"

import sqlite3

import pytest

import app
import auth
import billing
from agents import audit_log, event_bus
from agents.risk_manager import risk_store
from agents.runtime import runtime_store
from agents.sys_admin import sysadmin_store
from agents.trading_supervisor import supervision_store

AGENT_MODULES = (audit_log, event_bus, risk_store, supervision_store, sysadmin_store, runtime_store)
EXPECTED_TABLES = {
    "agent_audit_log", "agent_events", "agent_status",
    "sysadmin_log", "runtime_policy", "runtime_workflow",
}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(billing, "DB_PATH", db_path)
    for mod in AGENT_MODULES:
        monkeypatch.setattr(mod, "DB_PATH", db_path)
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_USERNAME", "testadmin")
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_PASSWORD", "Testpass123")
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_EMAIL", None)
    app.init_db()
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        yield c


def _login_admin(client):
    conn = sqlite3.connect(app.DB_PATH)
    admin_id = conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]
    conn.close()
    with client.session_transaction() as sess:
        sess["user_id"] = admin_id


class TestAgentTablesCreatedOnStartup:
    def test_fresh_startup_creates_all_six_tables(self, client):
        conn = sqlite3.connect(app.DB_PATH)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        missing = EXPECTED_TABLES - tables
        assert not missing, f"still missing: {missing}"

    def test_repeated_startup_is_idempotent_and_preserves_data(self, client):
        # Seed a row via the newly-wired sysadmin_store, then call
        # app.init_db() again (as every restart does) and confirm
        # nothing was wiped or altered -- CREATE TABLE IF NOT EXISTS
        # only, matching every other migration in this function.
        sysadmin_store.upsert_agent_status("dev_agent", enabled=True)
        before = sysadmin_store.get_agent_status("dev_agent")

        app.init_db()

        after = sysadmin_store.get_agent_status("dev_agent")
        assert before == after

    def test_sysadmin_overview_runtime_section_has_no_missing_table_error(self, client):
        _login_admin(client)
        resp = client.get("/api/sysadmin/overview")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["runtime"].get("error") is None

    def test_runtime_status_control_section_is_populated(self, client):
        _login_admin(client)
        resp = client.get("/api/runtime/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["control"] is not None
        assert data["control"]["agents"]["trading_intelligence"]["schedulable"] is False
        assert data["control"]["agents"]["quant_researcher"]["schedulable"] is False

    def test_runtime_scheduler_enabled_still_false_by_default(self, client):
        from agents import config
        assert config.RUNTIME_SCHEDULER_ENABLED is False

    def test_runtime_control_api_enabled_still_false_by_default(self, client):
        from agents import config
        assert config.RUNTIME_CONTROL_API_ENABLED is False
