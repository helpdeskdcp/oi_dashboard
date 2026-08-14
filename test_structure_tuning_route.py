"""
test_structure_tuning_route.py -- regression tests for
GET /api/structure/tuning/history (Milestone 20, Phase 7). Lives at
repo root, matching every other route-level test file.
"""
import os
import sqlite3

os.environ["SKIP_AUTOSTART"] = "1"

import pytest

import app
import auth
import billing
import institutional_levels as il
from agents import audit_log, event_bus
from agents.risk_manager import risk_store
from agents.runtime import runtime_store
from agents.sys_admin import sysadmin_store
from agents.trading_intelligence import candle_recorder
from agents.trading_intelligence import data_access as ti_data_access
from agents.trading_intelligence import structure_tuning, ti_store
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
        resp = client.get("/api/structure/tuning/history")
        assert resp.status_code == 302

    def test_non_admin_gets_403(self, client):
        import datetime as dt
        now = dt.datetime.now().isoformat()
        conn = sqlite3.connect(app.DB_PATH)
        cur = conn.execute(
            "INSERT INTO users (email, username, password_hash, role, is_verified, created_at, updated_at) "
            "VALUES (?,?,?,?,1,?,?)",
            ("sub_tuning@example.com", "sub_tuning", "x", "subscriber", now, now),
        )
        conn.commit()
        user_id = cur.lastrowid
        conn.close()
        with client.session_transaction() as sess:
            sess["user_id"] = user_id
            sess["csrf_token"] = CSRF_TOKEN
        resp = client.get("/api/structure/tuning/history")
        assert resp.status_code == 403


class TestBehavior:
    def test_returns_current_values_and_empty_history(self, client):
        _login_admin(client)
        resp = client.get("/api/structure/tuning/history")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["current_values"] == {"max_retest_candles": il.MAX_RETEST_CANDLES,
                                           "min_volume_multiplier": il.MIN_VOLUME_MULTIPLIER}
        assert data["history"] == []

    def test_filters_by_parameter(self, client, monkeypatch):
        _login_admin(client)
        import datetime as dt
        structure_tuning._record(
            structure_tuning.TuningDecision(parameter="max_retest_candles", current_value=3, best_candidate=None,
                                             current_win_rate=None, best_win_rate=None, sample_size=0,
                                             applied=False, reason="test"),
            now=dt.datetime.now(),
        )
        resp = client.get("/api/structure/tuning/history?parameter=max_retest_candles")
        data = resp.get_json()
        assert len(data["history"]) == 1
        assert data["history"][0]["parameter"] == "max_retest_candles"
