"""
test_candle_freshness_route.py -- regression tests for
GET /api/runtime/candle-freshness (Milestone 20, Phase 6: candle_recorder.py
health metric). Lives at repo root, matching every other route-level test
file (test_structure_overlay_route.py, test_trading_intelligence_run_cycle_route.py).
"""
import collections
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
    monkeypatch.setattr(candle_recorder, "_completed", collections.defaultdict(
        lambda: collections.deque(maxlen=candle_recorder.MAX_CANDLES_IN_MEMORY)))
    monkeypatch.setattr(candle_recorder, "_forming", {})
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
        resp = client.get("/api/runtime/candle-freshness")
        assert resp.status_code == 302

    def test_non_admin_gets_403(self, client):
        now = dt.datetime.now().isoformat()
        conn = sqlite3.connect(app.DB_PATH)
        cur = conn.execute(
            "INSERT INTO users (email, username, password_hash, role, is_verified, created_at, updated_at) "
            "VALUES (?,?,?,?,1,?,?)",
            ("sub_freshness@example.com", "sub_freshness", "x", "subscriber", now, now),
        )
        conn.commit()
        user_id = cur.lastrowid
        conn.close()
        with client.session_transaction() as sess:
            sess["user_id"] = user_id
            sess["csrf_token"] = CSRF_TOKEN
        resp = client.get("/api/runtime/candle-freshness")
        assert resp.status_code == 403


class TestBehavior:
    def test_returns_every_symbol_and_every_recorder_timeframe(self, client):
        _login_admin(client)
        resp = client.get("/api/runtime/candle-freshness")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "NIFTY" in data
        assert set(data["NIFTY"].keys()) == {"1m", "3m", "5m"}

    def test_no_recorded_candle_reports_unavailable_and_stale(self, client):
        _login_admin(client)
        resp = client.get("/api/runtime/candle-freshness")
        entry = resp.get_json()["NIFTY"]["1m"]
        assert entry["last_candle_timestamp"] is None
        assert entry["candle_lag_seconds"] is None
        assert entry["source"] == "unavailable"
        assert entry["stale"] is True

    def test_a_recorded_candle_is_reported_fresh(self, client):
        _login_admin(client)
        now = dt.datetime.now()
        candle_recorder.append_tick("NIFTY", now - dt.timedelta(seconds=65), 24500.0)
        candle_recorder.append_tick("NIFTY", now, 24510.0)   # closes the previous bucket

        resp = client.get("/api/runtime/candle-freshness")
        entry = resp.get_json()["NIFTY"]["1m"]
        assert entry["source"] == "live_recorder"
        assert entry["last_candle_timestamp"] is not None
        assert entry["candle_lag_seconds"] is not None
        assert entry["candle_lag_seconds"] < 600   # NSE threshold -- fresh, not stale
        assert entry["stale"] is False
