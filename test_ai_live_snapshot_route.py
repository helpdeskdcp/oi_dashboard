"""
test_ai_live_snapshot_route.py -- regression tests for the AI Live
Analysis Snapshot routes (Milestone 21, Phase 3):
GET /api/ai-live-snapshot, GET /api/ai-live-snapshot/json.
Lives at repo root, matching every other route-level test file.
"""
import os
import sqlite3

os.environ["SKIP_AUTOSTART"] = "1"

import pytest

import app
import auth
import billing
import intelligence_orchestrator
from agents import audit_log, event_bus
from agents.risk_manager import risk_store
from agents.runtime import runtime_store
from agents.sys_admin import sysadmin_store
from agents.trading_intelligence import candle_recorder
from agents.trading_intelligence import data_access as ti_data_access
from agents.trading_intelligence import multi_timeframe, structure_tuning, ti_store, virtual_trailing
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
    monkeypatch.setattr(multi_timeframe, "get_timeframe", lambda symbol, tf: {"available": False, "candles": None})
    monkeypatch.setattr(intelligence_orchestrator, "build_snapshot", lambda symbol, **kw: None)
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


def _insert_cycle_and_strike(db_path):
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO cycles (symbol, ts, date, time, underlying_ltp, atm, pcr) VALUES (?,?,?,?,?,?,?)",
        ("NIFTY", "2026-08-14T09:15:00", "2026-08-14", "09:15:00", 24505.0, 24500.0, 1.1),
    )
    cycle_id = cur.lastrowid
    conn.execute(
        "INSERT INTO strikes (cycle_id, strike, ce_ltp, pe_ltp) VALUES (?,?,?,?)",
        (cycle_id, 24500, 120.0, 95.0),
    )
    conn.commit()
    conn.close()


class TestFeatureFlagDefaultsDisabled:
    def test_both_routes_404_when_flag_is_off(self, client, monkeypatch):
        # Explicit, not relying on the getenv default -- app.py's
        # load_dotenv() walks up from cwd and can pick up the real
        # production .env (which sets this true) when tests run from
        # inside a nested worktree, so the "off" state must be forced
        # here rather than assumed.
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_AI_LIVE_SNAPSHOT_UI", False)
        _login_admin(client)
        assert client.get("/api/ai-live-snapshot?symbol=NIFTY").status_code == 404
        assert client.get("/api/ai-live-snapshot/json?symbol=NIFTY").status_code == 404


class TestAuthWhenEnabled:
    def test_unauthenticated_get_redirects_to_login(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_AI_LIVE_SNAPSHOT_UI", True)
        resp = client.get("/api/ai-live-snapshot?symbol=NIFTY")
        assert resp.status_code == 302

    def test_non_admin_gets_403(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_AI_LIVE_SNAPSHOT_UI", True)
        import datetime as dt
        now = dt.datetime.now().isoformat()
        conn = sqlite3.connect(app.DB_PATH)
        cur = conn.execute(
            "INSERT INTO users (email, username, password_hash, role, is_verified, created_at, updated_at) "
            "VALUES (?,?,?,?,1,?,?)",
            ("sub_ais@example.com", "sub_ais", "x", "subscriber", now, now),
        )
        conn.commit()
        user_id = cur.lastrowid
        conn.close()
        with client.session_transaction() as sess:
            sess["user_id"] = user_id
            sess["csrf_token"] = CSRF_TOKEN
        resp = client.get("/api/ai-live-snapshot?symbol=NIFTY")
        assert resp.status_code == 403


class TestBehaviorWhenEnabled:
    def test_requires_symbol(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_AI_LIVE_SNAPSHOT_UI", True)
        _login_admin(client)
        assert client.get("/api/ai-live-snapshot").status_code == 400
        assert client.get("/api/ai-live-snapshot/json").status_code == 400

    def test_no_cycle_data_returns_none_data_not_an_error(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_AI_LIVE_SNAPSHOT_UI", True)
        _login_admin(client)
        resp = client.get("/api/ai-live-snapshot?symbol=NIFTY")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["data"] is None
        assert data["telegram_text"] == "No live snapshot available."

    def test_main_endpoint_returns_data_and_telegram_text(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_AI_LIVE_SNAPSHOT_UI", True)
        _login_admin(client)
        _insert_cycle_and_strike(app.DB_PATH)

        resp = client.get("/api/ai-live-snapshot?symbol=NIFTY")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["data"]["symbol"] == "NIFTY"
        assert data["data"]["ce_ltp"] == 120.0
        assert "NIFTY" in data["telegram_text"]

    def test_json_endpoint_returns_pure_data_no_wrapper(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_AI_LIVE_SNAPSHOT_UI", True)
        _login_admin(client)
        _insert_cycle_and_strike(app.DB_PATH)

        resp = client.get("/api/ai-live-snapshot/json?symbol=NIFTY")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["symbol"] == "NIFTY"
        assert "telegram_text" not in data
