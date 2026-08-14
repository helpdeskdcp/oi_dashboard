"""
test_virtual_trailing_route.py -- regression tests for
GET /api/papertrades/virtual-trailing (Milestone 21, Phase 1). Lives at
repo root, matching every other route-level test file.
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
        resp = client.get("/api/papertrades/virtual-trailing")
        assert resp.status_code == 302

    def test_non_admin_gets_403(self, client):
        import datetime as dt
        now = dt.datetime.now().isoformat()
        conn = sqlite3.connect(app.DB_PATH)
        cur = conn.execute(
            "INSERT INTO users (email, username, password_hash, role, is_verified, created_at, updated_at) "
            "VALUES (?,?,?,?,1,?,?)",
            ("sub_vt@example.com", "sub_vt", "x", "subscriber", now, now),
        )
        conn.commit()
        user_id = cur.lastrowid
        conn.close()
        with client.session_transaction() as sess:
            sess["user_id"] = user_id
            sess["csrf_token"] = CSRF_TOKEN
        resp = client.get("/api/papertrades/virtual-trailing")
        assert resp.status_code == 403


class TestBehavior:
    def test_returns_empty_list_with_no_trades(self, client):
        _login_admin(client)
        resp = client.get("/api/papertrades/virtual-trailing")
        assert resp.status_code == 200
        assert resp.get_json() == {"trades": []}

    def test_returns_current_state_and_respects_symbol_filter(self, client):
        _login_admin(client)
        state = virtual_trailing._init_state(
            {"id": 1, "symbol": "NIFTY", "direction": "CE", "entry_price": 100.0,
             "sl_price": 90.0, "target_price": 120.0},
            now="2026-08-14T09:15:00",
        )
        virtual_trailing.upsert_state(state)
        other = virtual_trailing._init_state(
            {"id": 2, "symbol": "BANKNIFTY", "direction": "PE", "entry_price": 50.0,
             "sl_price": 45.0, "target_price": 60.0},
            now="2026-08-14T09:15:00",
        )
        virtual_trailing.upsert_state(other)

        resp = client.get("/api/papertrades/virtual-trailing")
        assert len(resp.get_json()["trades"]) == 2

        resp = client.get("/api/papertrades/virtual-trailing?symbol=NIFTY")
        trades = resp.get_json()["trades"]
        assert len(trades) == 1
        assert trades[0]["symbol"] == "NIFTY"

    def test_active_only_excludes_exited_trades(self, client):
        _login_admin(client)
        active = virtual_trailing._init_state(
            {"id": 1, "symbol": "NIFTY", "direction": "CE", "entry_price": 100.0,
             "sl_price": 90.0, "target_price": 120.0},
            now="2026-08-14T09:15:00",
        )
        virtual_trailing.upsert_state(active)
        exited = virtual_trailing.evaluate_trade(
            virtual_trailing._init_state(
                {"id": 2, "symbol": "NIFTY", "direction": "CE", "entry_price": 100.0,
                 "sl_price": 90.0, "target_price": 120.0},
                now="2026-08-14T09:15:00",
            ),
            90.0,
        )
        virtual_trailing.upsert_state(exited)

        resp = client.get("/api/papertrades/virtual-trailing?active_only=1")
        trades = resp.get_json()["trades"]
        assert len(trades) == 1
        assert trades[0]["state"] == "ACTIVE"
