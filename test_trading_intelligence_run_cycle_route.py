"""
test_trading_intelligence_run_cycle_route.py -- regression tests for
POST /api/trading-intelligence/run-cycle, the web-triggered equivalent
of `trading_intelligence_cli.py run-cycle` (Today Signal Audit
follow-up). Lives at repo root, matching every other route-level test
file (test_intelligence_alerts.py, test_intelligence_history.py) --
this is a NEW app.py route, not a trading_intelligence-package-internal
concern, so it follows that convention rather than
test_agents/trading_intelligence/'s own.
"""
import datetime as dt
import os
import sqlite3

os.environ["SKIP_AUTOSTART"] = "1"

import pytest

import app
import auth
import billing
from agents import audit_log, event_bus
from agents import config as agents_config
from agents.risk_manager import risk_store
from agents.runtime import runtime_store
from agents.runtime import scheduling_control as sc
from agents.sys_admin import sysadmin_store
from agents.trading_intelligence import data_access as ti_data_access
from agents.trading_intelligence import ti_store
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


def _post(client, path, reason=None, **kw):
    """auth.csrf_guard() (app.py's global before_request hook) requires a
    valid csrf_token for every POST -- included in the JSON body here,
    matching one of the three places csrf_guard() itself checks (form
    field, X-CSRFToken header, or JSON body)."""
    payload = {"csrf_token": CSRF_TOKEN}
    if reason is not None:
        payload["reason"] = reason
    payload.update(kw)
    return client.post(path, json=payload)


class TestDisabledByDefault:
    def test_returns_403_when_flag_off(self, client, monkeypatch):
        monkeypatch.setattr(agents_config, "TI_RUN_CYCLE_API_ENABLED", False)
        _login_admin(client)
        resp = _post(client, "/api/trading-intelligence/run-cycle", reason="test")
        assert resp.status_code == 403

    def test_flag_defaults_to_false(self):
        assert agents_config.TI_RUN_CYCLE_API_ENABLED is False


class TestEnabledBehavior:
    def test_missing_reason_returns_400(self, client, monkeypatch):
        monkeypatch.setattr(agents_config, "TI_RUN_CYCLE_API_ENABLED", True)
        _login_admin(client)
        resp = _post(client, "/api/trading-intelligence/run-cycle")
        assert resp.status_code == 400

    def test_blank_reason_returns_400(self, client, monkeypatch):
        monkeypatch.setattr(agents_config, "TI_RUN_CYCLE_API_ENABLED", True)
        _login_admin(client)
        resp = _post(client, "/api/trading-intelligence/run-cycle", reason="   ")
        assert resp.status_code == 400

    def test_valid_reason_calls_run_scheduled_cycle_and_returns_results(self, client, monkeypatch):
        monkeypatch.setattr(agents_config, "TI_RUN_CYCLE_API_ENABLED", True)
        monkeypatch.setattr(app.ti_api, "run_scheduled_cycle", lambda **kw: {
            "NIFTY": {"available": True, "action": "BUY CE", "trade_opened": True, "trade_id": 7},
            "BANKNIFTY": {"available": True, "action": "NO_TRADE", "trade_opened": False, "trade_id": None},
        })
        _login_admin(client)
        resp = _post(client, "/api/trading-intelligence/run-cycle", reason="manual check")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["results"]["NIFTY"]["trade_opened"] is True
        assert data["results"]["NIFTY"]["trade_id"] == 7
        assert data["results"]["BANKNIFTY"]["trade_opened"] is False


class TestMethodAndAuth:
    def test_get_returns_405(self, client, monkeypatch):
        monkeypatch.setattr(agents_config, "TI_RUN_CYCLE_API_ENABLED", True)
        _login_admin(client)
        resp = client.get("/api/trading-intelligence/run-cycle")
        assert resp.status_code == 405

    def test_unauthenticated_post_is_rejected(self, client, monkeypatch):
        """No session at all -- app.py's before_request hooks run
        _csrf_guard() before the route's own @auth.roles_required("admin")
        check ever executes, so an unauthenticated POST with no CSRF
        token in its (nonexistent) session fails at the CSRF layer (400),
        never reaching a redirect. Same pre-existing behavior every other
        state-changing admin route in this app already has -- not
        something specific to this new route."""
        monkeypatch.setattr(agents_config, "TI_RUN_CYCLE_API_ENABLED", True)
        resp = client.post("/api/trading-intelligence/run-cycle", json={"reason": "test"})
        assert resp.status_code == 400

    def test_non_admin_gets_403(self, client, monkeypatch):
        monkeypatch.setattr(agents_config, "TI_RUN_CYCLE_API_ENABLED", True)
        now = dt.datetime.now().isoformat()
        conn = sqlite3.connect(app.DB_PATH)
        cur = conn.execute(
            "INSERT INTO users (email, username, password_hash, role, is_verified, created_at, updated_at) "
            "VALUES (?,?,?,?,1,?,?)",
            ("sub_ti@example.com", "sub_ti", "x", "subscriber", now, now),
        )
        conn.commit()
        user_id = cur.lastrowid
        conn.close()
        with client.session_transaction() as sess:
            sess["user_id"] = user_id
            sess["csrf_token"] = CSRF_TOKEN
        resp = _post(client, "/api/trading-intelligence/run-cycle", reason="test")
        assert resp.status_code == 403


class TestSchedulerSafetyUntouched:
    def test_runtime_scheduler_enabled_still_false(self):
        assert agents_config.RUNTIME_SCHEDULER_ENABLED is False

    def test_trading_intelligence_now_schedulable(self):
        """Milestone 17: trading_intelligence was deliberately removed
        from NEVER_SCHEDULABLE_AGENTS -- quant_researcher and
        shadow_mode remain permanently blocked."""
        assert sc.is_schedulable("trading_intelligence") is True
        assert sc.is_schedulable("quant_researcher") is False
        assert sc.is_schedulable("shadow_mode") is False

    def test_route_source_never_touches_scheduler_locks(self):
        """AST-based (on the parsed body, not the docstring -- the route's
        own docstring explains what it deliberately avoids by naming
        these exact constants in prose, which a raw substring check on
        the full source would false-positive on): the route function
        never imports scheduling_control/agent_runtime or references
        RUNTIME_SCHEDULER_ENABLED/NEVER_SCHEDULABLE_AGENTS in executable
        code."""
        import ast
        import inspect
        source = inspect.getsource(app.api_trading_intelligence_run_cycle)
        tree = ast.parse(source)
        fn = tree.body[0]
        body = fn.body[1:] if (fn.body and isinstance(fn.body[0], ast.Expr)
                                and isinstance(fn.body[0].value, ast.Constant)
                                and isinstance(fn.body[0].value.value, str)) else fn.body
        names_used = {n.id for node in body for n in ast.walk(node) if isinstance(n, ast.Name)}
        attrs_used = {n.attr for node in body for n in ast.walk(node) if isinstance(n, ast.Attribute)}
        assert "RUNTIME_SCHEDULER_ENABLED" not in names_used | attrs_used
        assert "NEVER_SCHEDULABLE_AGENTS" not in names_used | attrs_used
        for node in [n for b in body for n in ast.walk(b)]:
            if isinstance(node, ast.Import):
                assert not any("scheduling_control" in a.name or "agent_runtime" in a.name for a in node.names)
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "scheduling_control" not in node.module
                assert "agent_runtime" not in node.module
