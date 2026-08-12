"""
test_trading_mode_routes.py -- Milestone 14, Phase 3: regression tests
for the dashboard's manual PAPER/LIVE_ENABLED/LIVE_DISABLED switch
(/api/runtime/trading-mode, /api/runtime/enable-live,
/api/runtime/disable-live).

Same throwaway-DB-per-test technique as test_runtime_control_routes.py.
"""
import os
os.environ["SKIP_AUTOSTART"] = "1"

import sqlite3

import pytest

import app
import auth
import billing
from agents import audit_log, event_bus
from agents.intelligence_alerts import (
    dedup_store as ia_dedup_store, rate_limiter as ia_rate_limiter, retry_tracker as ia_retry_tracker,
    store as ia_store, threshold_store as ia_threshold_store,
)
from agents.ops import event_log as ops_event_log
from agents.risk_manager import risk_store
from agents.runtime import runtime_store, trading_mode
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

    for mod in (ia_store, ia_threshold_store, ia_dedup_store, ia_rate_limiter, ia_retry_tracker):
        monkeypatch.setattr(mod, "DB_PATH", db_path)
    monkeypatch.setattr(ops_event_log, "DB_PATH", db_path)

    app.init_db()
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        yield c


def _login_session(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id


def _csrf(client, token="test-csrf-token-value"):
    with client.session_transaction() as sess:
        sess["csrf_token"] = token
    return token


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


def _login_admin(client, db_path):
    admin_id = sqlite3.connect(db_path).execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]
    _login_session(client, admin_id)
    return _csrf(client)


class TestDefaultState:
    def test_boots_to_paper_mode(self, client):
        token = _login_admin(client, app.DB_PATH)
        resp = client.get("/api/runtime/trading-mode", headers={"X-CSRFToken": token})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["mode"] == "PAPER"
        assert body["live_execution_implemented"] is False

    def test_unauthenticated_is_rejected(self, client):
        resp = client.get("/api/runtime/trading-mode")
        assert resp.status_code in (302, 401, 403)


class TestEnableLive:
    def test_requires_a_reason(self, client):
        token = _login_admin(client, app.DB_PATH)
        resp = client.post(
            "/api/runtime/enable-live", json={"acknowledge_no_execution": True},
            headers={"X-CSRFToken": token},
        )
        assert resp.status_code == 400

    def test_requires_explicit_acknowledgement(self, client):
        token = _login_admin(client, app.DB_PATH)
        resp = client.post(
            "/api/runtime/enable-live", json={"reason": "testing"},
            headers={"X-CSRFToken": token},
        )
        assert resp.status_code == 400
        assert "acknowledge_no_execution" in resp.get_json()["error"]
        # Refused -- mode must still be PAPER, not silently flipped.
        assert trading_mode.get_current_mode() == trading_mode.PAPER

    def test_succeeds_with_reason_and_acknowledgement(self, client):
        token = _login_admin(client, app.DB_PATH)
        resp = client.post(
            "/api/runtime/enable-live",
            json={"reason": "demoing the badge", "acknowledge_no_execution": True},
            headers={"X-CSRFToken": token},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["mode"] == "LIVE_ENABLED"
        assert body["live_execution_implemented"] is False
        assert trading_mode.get_current_mode() == trading_mode.LIVE_ENABLED

    def test_non_admin_gets_403(self, client):
        user_id = _seed_user(app.DB_PATH, email="sub@example.com", role="subscriber")
        _login_session(client, user_id)
        token = _csrf(client)
        resp = client.post(
            "/api/runtime/enable-live", json={"reason": "x", "acknowledge_no_execution": True},
            headers={"X-CSRFToken": token},
        )
        assert resp.status_code == 403


class TestDisableLive:
    def test_disable_after_enable_is_audited_and_reflected(self, client):
        token = _login_admin(client, app.DB_PATH)
        client.post(
            "/api/runtime/enable-live",
            json={"reason": "turn on", "acknowledge_no_execution": True},
            headers={"X-CSRFToken": token},
        )
        resp = client.post(
            "/api/runtime/disable-live", json={"reason": "turn back off"},
            headers={"X-CSRFToken": token},
        )
        assert resp.status_code == 200
        assert resp.get_json()["mode"] == "LIVE_DISABLED"

        status = client.get("/api/runtime/trading-mode", headers={"X-CSRFToken": token}).get_json()
        assert status["mode"] == "LIVE_DISABLED"
        history = status["history"]
        assert len(history) >= 2
        assert history[0]["new_mode"] == "LIVE_DISABLED"
        assert history[0]["previous_mode"] == "LIVE_ENABLED"
        assert history[0]["changed_by"] == "testadmin"
        assert history[0]["reason"] == "turn back off"

    def test_requires_a_reason(self, client):
        token = _login_admin(client, app.DB_PATH)
        resp = client.post("/api/runtime/disable-live", json={}, headers={"X-CSRFToken": token})
        assert resp.status_code == 400


class TestBootResetInvariant:
    def test_reset_to_paper_on_boot_overrides_whatever_was_persisted(self, client):
        token = _login_admin(client, app.DB_PATH)
        client.post(
            "/api/runtime/enable-live",
            json={"reason": "was on before a restart", "acknowledge_no_execution": True},
            headers={"X-CSRFToken": token},
        )
        assert trading_mode.get_current_mode() == trading_mode.LIVE_ENABLED

        trading_mode.reset_to_paper_on_boot()
        assert trading_mode.get_current_mode() == trading_mode.PAPER
