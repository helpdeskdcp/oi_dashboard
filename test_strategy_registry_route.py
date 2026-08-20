"""
test_strategy_registry_route.py -- regression tests for
GET /api/strategy-registry and the Operations Dashboard's Strategy Registry
panel. Same technique as test_candle_freshness_route.py.
"""
import datetime as dt
import os
import sqlite3

os.environ["SKIP_AUTOSTART"] = "1"

import pytest

import app
import auth
import billing


@pytest.fixture()
def client(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(billing, "DB_PATH", db_path)
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
        resp = client.get("/api/strategy-registry")
        assert resp.status_code == 302

    def test_non_admin_gets_403(self, client):
        now = dt.datetime.now().isoformat()
        conn = sqlite3.connect(app.DB_PATH)
        cur = conn.execute(
            "INSERT INTO users (email, username, password_hash, role, is_verified, created_at, updated_at) "
            "VALUES (?,?,?,?,1,?,?)",
            ("sub_registry@example.com", "sub_registry", "x", "subscriber", now, now),
        )
        conn.commit()
        user_id = cur.lastrowid
        conn.close()
        with client.session_transaction() as sess:
            sess["user_id"] = user_id
            sess["csrf_token"] = CSRF_TOKEN
        resp = client.get("/api/strategy-registry")
        assert resp.status_code == 403


class TestContract:
    def test_returns_a_list_of_flag_entries(self, client):
        _login_admin(client)
        resp = client.get("/api/strategy-registry")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)
        assert len(data) > 0
        for entry in data:
            assert {"flag", "module", "description", "enabled"} <= set(entry.keys())


class TestSysadminPagePanel:
    def test_admin_page_renders_strategy_registry_panel(self, client):
        _login_admin(client)
        resp = client.get("/admin/sysadmin")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "Strategy Registry" in body
        assert "strategy-registry-table" in body
        assert "/api/strategy-registry" in body
