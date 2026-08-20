"""
test_execution_state_route.py -- regression tests for the Execution State
observability routes (Post-launch upgrade, Phase C):
GET /api/execution-state, GET /api/execution-state/<execution_id>/transitions.
Lives at repo root, matching every other route-level test file (see
test_ai_live_snapshot_route.py, this file's own direct template).
"""
import datetime as dt
import os
import sqlite3

os.environ["SKIP_AUTOSTART"] = "1"

# Relative to the real current date, not a hardcoded literal -- see
# test_agents/trading_intelligence/test_execution_state.py's own
# FUTURE_EXPIRY/PAST_EXPIRY for why.
FUTURE_EXPIRY = (dt.date.today() + dt.timedelta(days=7)).isoformat()

import pytest

import app
import auth
import billing
import intelligence_orchestrator
from agents import audit_log, event_bus
from agents.risk_manager import risk_store
from agents.runtime import runtime_store
from agents.sys_admin import sysadmin_store
from agents.trading_intelligence import candle_recorder, execution_state
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
    monkeypatch.setattr(execution_state, "DB_PATH", db_path)
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


def _create_execution(execution_id, *, instrument="NIFTY", direction="CE", expiry_date=FUTURE_EXPIRY):
    execution_state.create_execution(
        execution_id, instrument=instrument, direction=direction, strike=24900,
        entry_price=118.0, quantity=50, sl=106.0, t1=132.0, confidence=82,
        decision_reason="test setup", signal_reference=f"ti_paper_trades:{execution_id}",
        expiry_date=expiry_date,
    )


class TestFeatureFlagDefaultsDisabled:
    def test_both_routes_404_when_flag_is_off(self, client, monkeypatch):
        # Explicit, not relying on the getenv default -- app.py's
        # load_dotenv() walks up from cwd and can pick up the real
        # production .env when tests run from inside a nested worktree,
        # so the "off" state must be forced here rather than assumed.
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_EXECUTION_STATE_UI", False)
        _login_admin(client)
        assert client.get("/api/execution-state").status_code == 404
        assert client.get("/api/execution-state/paper_trade_1/transitions").status_code == 404


class TestAuthWhenEnabled:
    def test_unauthenticated_get_redirects_to_login(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_EXECUTION_STATE_UI", True)
        resp = client.get("/api/execution-state")
        assert resp.status_code == 302

    def test_non_admin_gets_403(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_EXECUTION_STATE_UI", True)
        import datetime as dt
        now = dt.datetime.now().isoformat()
        conn = sqlite3.connect(app.DB_PATH)
        cur = conn.execute(
            "INSERT INTO users (email, username, password_hash, role, is_verified, created_at, updated_at) "
            "VALUES (?,?,?,?,1,?,?)",
            ("sub_es@example.com", "sub_es", "x", "subscriber", now, now),
        )
        conn.commit()
        user_id = cur.lastrowid
        conn.close()
        with client.session_transaction() as sess:
            sess["user_id"] = user_id
            sess["csrf_token"] = CSRF_TOKEN
        resp = client.get("/api/execution-state")
        assert resp.status_code == 403


class TestBehaviorWhenEnabled:
    def test_no_executions_returns_empty_not_an_error(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_EXECUTION_STATE_UI", True)
        _login_admin(client)
        resp = client.get("/api/execution-state")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["executions"] == []
        assert data["counts_by_state"] == {}

    def test_lists_real_executions_with_counts_by_state(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_EXECUTION_STATE_UI", True)
        _login_admin(client)
        _create_execution("paper_trade_1")
        _create_execution("paper_trade_2")
        execution_state.transition("paper_trade_2", "APPROVED")

        resp = client.get("/api/execution-state")
        assert resp.status_code == 200
        data = resp.get_json()
        ids = {e["execution_id"] for e in data["executions"]}
        assert ids == {"paper_trade_1", "paper_trade_2"}
        assert data["counts_by_state"] == {"SIGNAL": 1, "APPROVED": 1}

    def test_active_only_excludes_completed(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_EXECUTION_STATE_UI", True)
        _login_admin(client)
        _create_execution("paper_trade_1")
        _create_execution("paper_trade_2")
        for state in ("APPROVED", "READY", "ORDER_INTENT", "SUBMITTED", "FILLED", "MONITORING", "EXIT_INTENT", "EXIT", "COMPLETED"):
            execution_state.transition("paper_trade_2", state)

        resp = client.get("/api/execution-state?active_only=1")
        assert resp.status_code == 200
        data = resp.get_json()
        ids = {e["execution_id"] for e in data["executions"]}
        assert ids == {"paper_trade_1"}

    def test_transitions_endpoint_returns_the_real_audit_trail_including_rejections(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_EXECUTION_STATE_UI", True)
        _login_admin(client)
        _create_execution("paper_trade_1")
        execution_state.transition("paper_trade_1", "APPROVED")
        execution_state.transition("paper_trade_1", "SUBMITTED")  # invalid -- must be rejected, not skipped

        resp = client.get("/api/execution-state/paper_trade_1/transitions")
        assert resp.status_code == 200
        transitions = resp.get_json()["transitions"]
        to_states = [t["to_state"] for t in transitions]
        assert "SUBMITTED" in to_states
        rejected = next(t for t in transitions if t["to_state"] == "SUBMITTED")
        assert rejected["accepted"] == 0

    def test_transitions_endpoint_for_unknown_execution_id_returns_empty_not_an_error(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_EXECUTION_STATE_UI", True)
        _login_admin(client)
        resp = client.get("/api/execution-state/does-not-exist/transitions")
        assert resp.status_code == 200
        assert resp.get_json()["transitions"] == []


class TestLiveLtpEnrichment:
    """GET /api/execution-state now returns live_ltp/hit_status per
    execution (execution_state.list_executions_with_live_ltp()) --
    end-to-end confirmation through the actual route, not just the
    underlying function (see test_agents/trading_intelligence/
    test_execution_state.py::TestListExecutionsWithLiveLtp for the
    function-level cases)."""

    def _insert_strike(self, db_path, *, symbol, strike, ce_ltp):
        conn = sqlite3.connect(db_path)
        cur = conn.execute(
            "INSERT INTO cycles (symbol, ts, date, time, underlying_ltp, atm) VALUES (?,?,?,?,?,?)",
            (symbol, "2026-08-20T10:00:00", "2026-08-20", "10:00:00", strike, strike),
        )
        conn.execute("INSERT INTO strikes (cycle_id, strike, ce_ltp) VALUES (?,?,?)", (cur.lastrowid, strike, ce_ltp))
        conn.commit()
        conn.close()

    def test_response_carries_live_ltp_and_hit_status(self, client, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_EXECUTION_STATE_UI", True)
        _login_admin(client)
        _create_execution("paper_trade_1")
        for state in ("APPROVED", "READY", "ORDER_INTENT", "SUBMITTED", "FILLED", "MONITORING"):
            execution_state.transition("paper_trade_1", state)
        self._insert_strike(app.DB_PATH, symbol="NIFTY", strike=24900, ce_ltp=132.0)   # >= t1=132.0

        resp = client.get("/api/execution-state")
        assert resp.status_code == 200
        row = resp.get_json()["executions"][0]
        assert row["live_ltp"] == 132.0
        assert row["hit_status"] == "TARGET_HIT"
