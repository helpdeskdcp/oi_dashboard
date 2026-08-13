"""
test_structure_overlay_route.py -- regression tests for
GET /api/structure/<symbol>/overlay (Milestone 20, Phase 5: dashboard
Structure Overlay panel). Lives at repo root, matching every other
route-level test file (test_intelligence_alerts.py,
test_trading_intelligence_run_cycle_route.py) -- this is a NEW app.py
route, not a trading_intelligence-package-internal concern.
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
from agents.risk_manager import risk_store
from agents.runtime import runtime_store
from agents.sys_admin import sysadmin_store
from agents.trading_intelligence import data_access as ti_data_access
from agents.trading_intelligence import structure_overlay
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


class TestAuth:
    def test_unauthenticated_get_redirects_to_login(self, client):
        # Matches every other roles_required("admin") GET route in this
        # app -- login_required's own 302-to-login-page behavior, not
        # something specific to this new route.
        resp = client.get("/api/structure/NIFTY/overlay")
        assert resp.status_code == 302

    def test_non_admin_gets_403(self, client):
        now = dt.datetime.now().isoformat()
        conn = sqlite3.connect(app.DB_PATH)
        cur = conn.execute(
            "INSERT INTO users (email, username, password_hash, role, is_verified, created_at, updated_at) "
            "VALUES (?,?,?,?,1,?,?)",
            ("sub_overlay@example.com", "sub_overlay", "x", "subscriber", now, now),
        )
        conn.commit()
        user_id = cur.lastrowid
        conn.close()
        with client.session_transaction() as sess:
            sess["user_id"] = user_id
            sess["csrf_token"] = CSRF_TOKEN
        resp = client.get("/api/structure/NIFTY/overlay")
        assert resp.status_code == 403


class TestMethod:
    def test_post_returns_405(self, client):
        _login_admin(client)
        resp = client.post("/api/structure/NIFTY/overlay")
        assert resp.status_code == 405


class TestBehavior:
    def test_unknown_symbol_returns_404(self, client):
        _login_admin(client)
        resp = client.get("/api/structure/NOT_A_REAL_SYMBOL/overlay")
        assert resp.status_code == 404

    def test_known_symbol_calls_compute_overlay_and_returns_its_result(self, client, monkeypatch):
        monkeypatch.setattr(structure_overlay, "compute_overlay",
                             lambda sym: {"symbol": sym, "available": True, "state": "RANGE", "confidence": 40})
        _login_admin(client)
        resp = client.get("/api/structure/NIFTY/overlay")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["symbol"] == "NIFTY"
        assert data["state"] == "RANGE"

    def test_symbol_is_uppercased(self, client, monkeypatch):
        seen = []
        monkeypatch.setattr(structure_overlay, "compute_overlay",
                             lambda sym: seen.append(sym) or {"symbol": sym, "available": False, "reason": "x"})
        _login_admin(client)
        resp = client.get("/api/structure/nifty/overlay")
        assert resp.status_code == 200
        assert seen == ["NIFTY"]

    def test_never_sends_telegram_or_opens_a_trade(self, client, monkeypatch):
        # AST-based guarantee that the real compute_overlay() never
        # touches telegram_notifier/paper_trading already lives in
        # test_agents/trading_intelligence/test_structure_overlay.py --
        # this just confirms the route's own EXECUTABLE code (not its
        # docstring, which mentions "telegram" in prose) doesn't add a
        # second path to either.
        import ast
        import inspect
        source = inspect.getsource(app.api_structure_overlay)
        tree = ast.parse(source)
        fn = tree.body[0]
        body = fn.body[1:] if (fn.body and isinstance(fn.body[0], ast.Expr)
                                and isinstance(fn.body[0].value, ast.Constant)
                                and isinstance(fn.body[0].value.value, str)) else fn.body
        names_and_attrs = {n.id for node in body for n in ast.walk(node) if isinstance(n, ast.Name)} | \
                           {n.attr for node in body for n in ast.walk(node) if isinstance(n, ast.Attribute)}
        assert not any("telegram" in n.lower() for n in names_and_attrs)
        assert not any("paper_trading" in n.lower() for n in names_and_attrs)
