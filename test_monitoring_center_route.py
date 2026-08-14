"""
test_monitoring_center_route.py -- regression tests for the Autonomous
Trade Control Center routes (Milestone 21, Phase 2):
GET /api/monitoring/control-center, GET /api/monitoring/health,
POST /api/monitoring/control-center/{pause,resume,reset-virtual-state}.
Lives at repo root, matching every other route-level test file.
"""
import os
import sqlite3

os.environ["SKIP_AUTOSTART"] = "1"

import pytest

import app
import auth
import billing
from agents import audit_log, event_bus
from agents.risk_manager import risk_store
from agents.runtime import runtime_store
from agents.sys_admin import sysadmin_store
from agents.trading_intelligence import candle_recorder
from agents.trading_intelligence import data_access as ti_data_access
from agents.trading_intelligence import production_watchdog
from agents.trading_intelligence import structure_tuning, ti_store, virtual_trailing
from agents.trading_supervisor import supervision_store

AGENT_MODULES = (audit_log, event_bus, risk_store, supervision_store, sysadmin_store, runtime_store)


@pytest.fixture()
def client(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(billing, "DB_PATH", db_path)
    for mod in AGENT_MODULES:
        monkeypatch.setattr(mod, "DB_PATH", db_path)
    monkeypatch.setattr(ti_data_access, "DB_PATH", db_path)
    monkeypatch.setattr(ti_store, "DB_PATH", db_path)
    monkeypatch.setattr(candle_recorder, "DB_PATH", db_path)
    monkeypatch.setattr(structure_tuning, "DB_PATH", db_path)
    monkeypatch.setattr(virtual_trailing, "DB_PATH", db_path)
    monkeypatch.setattr(production_watchdog, "DB_PATH", db_path)
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_USERNAME", "testadmin")
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_PASSWORD", "Testpass123")
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_EMAIL", None)
    app.init_db()
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        yield c


CSRF_TOKEN = "test-csrf-token"


def _login_admin(client):
    conn = sqlite3.connect(app.DB_PATH)
    admin_id = conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]
    conn.close()
    with client.session_transaction() as sess:
        sess["user_id"] = admin_id
        sess["csrf_token"] = CSRF_TOKEN


ROUTES = ("/api/monitoring/control-center", "/api/monitoring/health")
POST_ROUTES = ("/api/monitoring/control-center/pause", "/api/monitoring/control-center/resume")


class TestFeatureFlagDefaultsDisabled:
    def test_get_routes_404_when_flag_is_off(self, client, monkeypatch):
        # Explicit, not relying on the getenv default -- app.py's
        # load_dotenv() walks up from cwd and can pick up the real
        # production .env (which sets this true) when tests run from
        # inside a nested worktree, so the "off" state must be forced
        # here rather than assumed.
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_CONTROL_CENTER_UI", False)
        _login_admin(client)
        for route in ROUTES:
            resp = client.get(route)
            assert resp.status_code == 404, route

    def test_post_routes_404_when_flag_is_off(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_CONTROL_CENTER_UI", False)
        _login_admin(client)
        for route in POST_ROUTES:
            resp = client.post(route, headers={"X-CSRFToken": CSRF_TOKEN})
            assert resp.status_code == 404, route


class TestAuthWhenEnabled:
    def test_unauthenticated_get_redirects_to_login(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_CONTROL_CENTER_UI", True)
        resp = client.get("/api/monitoring/control-center")
        assert resp.status_code == 302

    def test_non_admin_gets_403(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_CONTROL_CENTER_UI", True)
        import datetime as dt
        now = dt.datetime.now().isoformat()
        conn = sqlite3.connect(app.DB_PATH)
        cur = conn.execute(
            "INSERT INTO users (email, username, password_hash, role, is_verified, created_at, updated_at) "
            "VALUES (?,?,?,?,1,?,?)",
            ("sub_mc@example.com", "sub_mc", "x", "subscriber", now, now),
        )
        conn.commit()
        user_id = cur.lastrowid
        conn.close()
        with client.session_transaction() as sess:
            sess["user_id"] = user_id
            sess["csrf_token"] = CSRF_TOKEN
        resp = client.get("/api/monitoring/control-center")
        assert resp.status_code == 403


class TestBehaviorWhenEnabled:
    def test_control_center_returns_empty_state(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_CONTROL_CENTER_UI", True)
        _login_admin(client)
        resp = client.get("/api/monitoring/control-center")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["active_auto_paper_trades"] == 0
        assert data["trades"] == []

    def test_health_reports_unavailable_with_no_agent_status_row(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_CONTROL_CENTER_UI", True)
        _login_admin(client)
        resp = client.get("/api/monitoring/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["available"] is False
        assert data["status"] is None
        # Milestone 22: purely additive -- the watchdog key exists
        # alongside the untouched available/status keys above.
        assert "widget" in data["watchdog"]
        assert "metrics" in data["watchdog"]
        assert "checks" in data["watchdog"]

    def test_pause_then_resume_round_trips(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_CONTROL_CENTER_UI", True)
        _login_admin(client)
        virtual_trailing.init_db()

        resp = client.post("/api/monitoring/control-center/pause", headers={"X-CSRFToken": CSRF_TOKEN})
        assert resp.status_code == 200
        assert resp.get_json() == {"paused": True}
        assert virtual_trailing.is_paused() is True

        resp = client.post("/api/monitoring/control-center/resume", headers={"X-CSRFToken": CSRF_TOKEN})
        assert resp.get_json() == {"paused": False}
        assert virtual_trailing.is_paused() is False

    def test_reset_virtual_state_requires_trade_id(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_CONTROL_CENTER_UI", True)
        _login_admin(client)
        resp = client.post("/api/monitoring/control-center/reset-virtual-state", headers={"X-CSRFToken": CSRF_TOKEN})
        assert resp.status_code == 400

    def test_reset_virtual_state_removes_the_row(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_CONTROL_CENTER_UI", True)
        _login_admin(client)
        virtual_trailing.init_db()
        state = virtual_trailing._init_state(
            {"id": 5, "symbol": "NIFTY", "direction": "CE", "entry_price": 100.0,
             "sl_price": 90.0, "target_price": 120.0, "strike": 24500},
            now="2026-08-14T09:15:00",
        )
        virtual_trailing.upsert_state(state)

        resp = client.post("/api/monitoring/control-center/reset-virtual-state?trade_id=5",
                            headers={"X-CSRFToken": CSRF_TOKEN})
        assert resp.status_code == 200
        assert resp.get_json() == {"reset": True}
        assert virtual_trailing.get_state(5) is None
