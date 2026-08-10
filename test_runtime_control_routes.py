"""
test_runtime_control_routes.py -- Milestone 12, Phase 2A: regression
tests for the new /api/runtime/control/* HTTP write routes (app.py).

Same technique as test_auth.py: SKIP_AUTOSTART=1 before importing app,
throwaway DB per test. In addition to app/auth/billing's own DB_PATH,
every agents.* module these routes touch (audit_log, event_bus,
risk_store, supervision_store, sysadmin_store, runtime_store) is
pointed at the SAME throwaway file and initialized -- these are
independent module-level DB_PATH constants (all default to the real
"oi_history.db"), not derived from app.DB_PATH, so skipping this step
would make policy_engine.set_policy()/scheduling_control.set_mode()
write to the real database instead of the test's own.
"""
import os
os.environ["SKIP_AUTOSTART"] = "1"

import sqlite3

import pytest

import app
import auth
import billing
from agents import audit_log, config as agents_config, event_bus
from agents.intelligence_alerts import (
    dedup_store as ia_dedup_store, rate_limiter as ia_rate_limiter, retry_tracker as ia_retry_tracker,
    store as ia_store, threshold_store as ia_threshold_store,
)
from agents.ops import event_log as ops_event_log
from agents.risk_manager import risk_store
from agents.runtime import policy_engine, runtime_store, scheduling_control
from agents.sys_admin import sysadmin_store
from agents.trading_supervisor import supervision_store


@pytest.fixture()
def client(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(billing, "DB_PATH", db_path)
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_USERNAME", "testadmin")
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_PASSWORD", "Testpass123")
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_EMAIL", None)

    for mod in (audit_log, event_bus, risk_store, supervision_store, sysadmin_store, runtime_store):
        monkeypatch.setattr(mod, "DB_PATH", db_path)
        mod.init_db()

    # app.init_db() also initializes agents.intelligence_alerts's own
    # tables (store/threshold_store/dedup_store/rate_limiter/
    # retry_tracker) -- each has its own module-level DB_PATH constant,
    # independent of app.DB_PATH, so without this they'd silently write
    # their CREATE TABLE IF NOT EXISTS calls into this worktree's real
    # local oi_history.db instead of the throwaway test file.
    for mod in (ia_store, ia_threshold_store, ia_dedup_store, ia_rate_limiter, ia_retry_tracker):
        monkeypatch.setattr(mod, "DB_PATH", db_path)
    monkeypatch.setattr(ops_event_log, "DB_PATH", db_path)

    app.init_db()
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        yield c


def _seed_user(db_path, email=None, username=None, password="Testpass123", role="subscriber"):
    conn = sqlite3.connect(db_path)
    now_str = auth.now_ist().isoformat()
    conn.execute(
        """INSERT INTO users (email, username, password_hash, role, is_verified, is_suspended,
                               trial_started_at, trial_ends_at, subscription_expires_at,
                               wallet_balance, created_at, updated_at)
           VALUES (?,?,?,?,1,0,?,?,?,?,?,?)""",
        (email, username, auth.hash_password(password), role, now_str, None, None, 50000, now_str, now_str),
    )
    conn.commit()
    user_id = conn.execute("SELECT id FROM users WHERE email=? OR username=?", (email, username)).fetchone()[0]
    conn.close()
    return user_id


def _login_session(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id


def _csrf(client, token="test-csrf-token-value"):
    with client.session_transaction() as sess:
        sess["csrf_token"] = token
    return token


def _login_admin(client, db_path):
    admin_id = sqlite3.connect(db_path).execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]
    _login_session(client, admin_id)
    return _csrf(client)


class TestDisabledByDefault:
    """RUNTIME_CONTROL_API_ENABLED defaults to False -- every write route
    must refuse with 403 regardless of role or CSRF validity, until a
    test explicitly opts in via monkeypatch."""

    def test_pause_refused_when_disabled(self, client):
        assert agents_config.RUNTIME_CONTROL_API_ENABLED is False
        token = _login_admin(client, app.DB_PATH)
        resp = client.post(
            "/api/runtime/control/pause", json={"reason": "test"},
            headers={"X-CSRFToken": token},
        )
        assert resp.status_code == 403
        assert "disabled" in resp.get_json()["error"]

    def test_resume_refused_when_disabled(self, client):
        token = _login_admin(client, app.DB_PATH)
        resp = client.post(
            "/api/runtime/control/resume", json={"reason": "test"},
            headers={"X-CSRFToken": token},
        )
        assert resp.status_code == 403

    def test_agent_mode_refused_when_disabled(self, client):
        token = _login_admin(client, app.DB_PATH)
        resp = client.post(
            "/api/runtime/control/agent/dev_agent/mode", json={"mode": "disabled", "reason": "test"},
            headers={"X-CSRFToken": token},
        )
        assert resp.status_code == 403


class TestCsrfEnforced:
    def test_pause_without_csrf_token_is_rejected(self, client, monkeypatch):
        monkeypatch.setattr(agents_config, "RUNTIME_CONTROL_API_ENABLED", True)
        _login_admin(client, app.DB_PATH)  # seeds a csrf token in session, but we don't send it
        resp = client.post("/api/runtime/control/pause", json={"reason": "test"})
        assert resp.status_code == 400


class TestPauseAndResume:
    def test_pause_engages_emergency_stop_and_resume_clears_it(self, client, monkeypatch):
        monkeypatch.setattr(agents_config, "RUNTIME_CONTROL_API_ENABLED", True)
        token = _login_admin(client, app.DB_PATH)

        resp = client.post(
            "/api/runtime/control/pause", json={"reason": "operator review"},
            headers={"X-CSRFToken": token},
        )
        assert resp.status_code == 200
        assert resp.get_json()["active_policy"] == "emergency_stop"
        assert policy_engine.is_emergency_stop() is True

        status = client.get("/api/runtime/status").get_json()
        assert status["control"]["emergency_stop"] is True

        resp = client.post(
            "/api/runtime/control/resume", json={"reason": "review complete"},
            headers={"X-CSRFToken": token},
        )
        assert resp.status_code == 200
        assert policy_engine.is_emergency_stop() is False

        status = client.get("/api/runtime/status").get_json()
        assert status["control"]["emergency_stop"] is False

    def test_pause_requires_a_non_empty_reason(self, client, monkeypatch):
        monkeypatch.setattr(agents_config, "RUNTIME_CONTROL_API_ENABLED", True)
        token = _login_admin(client, app.DB_PATH)
        resp = client.post("/api/runtime/control/pause", json={"reason": ""}, headers={"X-CSRFToken": token})
        assert resp.status_code == 400

    def test_resume_defaults_to_the_configured_default_policy(self, client, monkeypatch):
        monkeypatch.setattr(agents_config, "RUNTIME_CONTROL_API_ENABLED", True)
        token = _login_admin(client, app.DB_PATH)
        client.post("/api/runtime/control/pause", json={"reason": "x"}, headers={"X-CSRFToken": token})
        resp = client.post("/api/runtime/control/resume", json={"reason": "x"}, headers={"X-CSRFToken": token})
        assert resp.get_json()["active_policy"] == agents_config.RUNTIME_DEFAULT_POLICY


class TestAgentMode:
    def test_disabling_a_schedulable_agent_is_reflected_in_status(self, client, monkeypatch):
        monkeypatch.setattr(agents_config, "RUNTIME_CONTROL_API_ENABLED", True)
        token = _login_admin(client, app.DB_PATH)
        resp = client.post(
            "/api/runtime/control/agent/dev_agent/mode", json={"mode": "disabled", "reason": "flaky overnight"},
            headers={"X-CSRFToken": token},
        )
        assert resp.status_code == 200
        assert resp.get_json()["mode"] == "disabled"

        status = client.get("/api/runtime/status").get_json()
        assert status["control"]["agents"]["dev_agent"]["mode"] == "disabled"

    def test_invalid_mode_string_is_rejected(self, client, monkeypatch):
        monkeypatch.setattr(agents_config, "RUNTIME_CONTROL_API_ENABLED", True)
        token = _login_admin(client, app.DB_PATH)
        resp = client.post(
            "/api/runtime/control/agent/dev_agent/mode", json={"mode": "bogus", "reason": "x"},
            headers={"X-CSRFToken": token},
        )
        assert resp.status_code == 400

    @pytest.mark.parametrize("agent", ["quant_researcher", "shadow_mode"])
    @pytest.mark.parametrize("mode", ["enabled", "disabled", "dry_run"])
    def test_never_schedulable_agents_are_refused_under_any_mode(self, client, monkeypatch, agent, mode):
        monkeypatch.setattr(agents_config, "RUNTIME_CONTROL_API_ENABLED", True)
        token = _login_admin(client, app.DB_PATH)
        resp = client.post(
            f"/api/runtime/control/agent/{agent}/mode", json={"mode": mode, "reason": "attempted override"},
            headers={"X-CSRFToken": token},
        )
        assert resp.status_code == 400
        assert agent in resp.get_json()["error"]
        assert scheduling_control.is_schedulable(agent) is False

    @pytest.mark.parametrize("mode", ["enabled", "disabled", "dry_run"])
    def test_trading_intelligence_mode_change_now_succeeds(self, client, monkeypatch, mode):
        """Milestone 17: trading_intelligence was removed from
        NEVER_SCHEDULABLE_AGENTS -- this route now accepts any valid
        mode for it, same as any other schedulable agent."""
        monkeypatch.setattr(agents_config, "RUNTIME_CONTROL_API_ENABLED", True)
        token = _login_admin(client, app.DB_PATH)
        resp = client.post(
            "/api/runtime/control/agent/trading_intelligence/mode",
            json={"mode": mode, "reason": "M17 activation test"},
            headers={"X-CSRFToken": token},
        )
        assert resp.status_code == 200
        assert scheduling_control.get_mode("trading_intelligence") == mode
        assert scheduling_control.is_schedulable("trading_intelligence") is True


class TestAccessControl:
    def test_non_admin_gets_403(self, client, monkeypatch):
        monkeypatch.setattr(agents_config, "RUNTIME_CONTROL_API_ENABLED", True)
        user_id = _seed_user(app.DB_PATH, email="sub@example.com", role="subscriber")
        _login_session(client, user_id)
        token = _csrf(client)
        resp = client.post("/api/runtime/control/pause", json={"reason": "x"}, headers={"X-CSRFToken": token})
        assert resp.status_code == 403

    def test_unauthenticated_is_rejected(self, client, monkeypatch):
        # No session at all means no CSRF token either -- the global
        # CSRF guard (app.py's before_request hook) runs before the
        # @auth.roles_required decorator and rejects with 400 first.
        # Confirmed separately (TestAccessControl.test_non_admin_gets_403)
        # that a logged-in, CSRF-valid, wrong-role request is refused
        # with 403 by the auth layer itself.
        monkeypatch.setattr(agents_config, "RUNTIME_CONTROL_API_ENABLED", True)
        resp = client.post("/api/runtime/control/pause", json={"reason": "x"})
        assert resp.status_code in (302, 400, 401, 403)
