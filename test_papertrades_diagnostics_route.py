"""
test_papertrades_diagnostics_route.py -- regression tests for
GET /api/papertrades/diagnostics (Milestone 20, Phase 6). Lives at repo
root, matching every other route-level test file.
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
from agents.trading_intelligence import candle_recorder
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
    monkeypatch.setattr(candle_recorder, "DB_PATH", db_path)
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


def _closed_trade(entry_time="2026-08-13T09:00:00", points=100.0):
    tid = ti_store.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                               target_price=130.0, sl_price=85.0, qty=50)
    conn = sqlite3.connect(ti_store.DB_PATH)
    conn.execute("UPDATE ti_paper_trades SET entry_time=? WHERE id=?", (entry_time, tid))
    conn.commit()
    conn.close()
    ti_store.close_trade(tid, exit_price=100.0 + points / 50.0, exit_reason="TARGET HIT")


class TestAuth:
    def test_unauthenticated_get_redirects_to_login(self, client):
        resp = client.get("/api/papertrades/diagnostics")
        assert resp.status_code == 302

    def test_non_admin_gets_403(self, client):
        now = dt.datetime.now().isoformat()
        conn = sqlite3.connect(app.DB_PATH)
        cur = conn.execute(
            "INSERT INTO users (email, username, password_hash, role, is_verified, created_at, updated_at) "
            "VALUES (?,?,?,?,1,?,?)",
            ("sub_diag@example.com", "sub_diag", "x", "subscriber", now, now),
        )
        conn.commit()
        user_id = cur.lastrowid
        conn.close()
        with client.session_transaction() as sess:
            sess["user_id"] = user_id
            sess["csrf_token"] = CSRF_TOKEN
        resp = client.get("/api/papertrades/diagnostics")
        assert resp.status_code == 403


class TestBehavior:
    def test_defaults_to_today_when_no_date_given(self, client, monkeypatch):
        _login_admin(client)
        today = dt.datetime.now().date().isoformat()
        _closed_trade(entry_time=f"{today}T09:00:00", points=50.0)

        resp = client.get("/api/papertrades/diagnostics")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["date"] == today
        assert data["available"] is True

    def test_explicit_date_is_honored(self, client):
        _login_admin(client)
        _closed_trade(entry_time="2026-08-10T09:00:00", points=100.0)

        resp = client.get("/api/papertrades/diagnostics?date=2026-08-10")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["date"] == "2026-08-10"
        assert data["trade_count"] == 1

    def test_invalid_date_returns_400(self, client):
        _login_admin(client)
        resp = client.get("/api/papertrades/diagnostics?date=not-a-date")
        assert resp.status_code == 400

    def test_no_trades_for_the_date_reports_unavailable(self, client):
        _login_admin(client)
        resp = client.get("/api/papertrades/diagnostics?date=2020-01-01")
        assert resp.status_code == 200
        assert resp.get_json()["available"] is False
