"""
test_intelligence_history.py -- Milestone 13, Phase 2: regression tests
for agents/intelligence_history/ (store/analytics/api),
intelligence_history_cli.py, and the three new GET /api/intelligence/
history/* routes.

NOTE on file location: no "backend/" or "tests/" directory exists
anywhere in this repository -- every one of its 20+ existing test files
lives at the repo root as test_*.py. This file follows that established
convention, same as test_intelligence_orchestrator.py before it.

Same SKIP_AUTOSTART=1 + throwaway-DB technique as every other route-level
test file in this project: no live thread, no real oi_history.db touched
by any test here.
"""
import ast
import datetime as dt
import os
import sqlite3
from pathlib import Path

os.environ["SKIP_AUTOSTART"] = "1"

import pytest

import app
import auth
import billing
from agents import audit_log, event_bus
from agents.intelligence_history import analytics, api as history_api, store as history_store
from agents.risk_manager import risk_store
from agents.runtime import scheduling_control as sc
from agents.runtime import runtime_store
from agents.sys_admin import sysadmin_store
from agents.trading_intelligence import data_access as ti_data_access
from agents.trading_supervisor import supervision_store
from agents import config as agents_config

import intelligence_history_cli
import intelligence_models

AGENT_MODULES = (audit_log, event_bus, risk_store, supervision_store, sysadmin_store, runtime_store)
INTELLIGENCE_HISTORY_FILES = [
    Path("agents/intelligence_history/__init__.py"),
    Path("agents/intelligence_history/store.py"),
    Path("agents/intelligence_history/analytics.py"),
    Path("agents/intelligence_history/api.py"),
]
FORBIDDEN_IMPORT_SUBSTRINGS = (
    "smartapi", "smartconnect", "angelone", "angel_one", "broker",
    "paper_trading", "ti_store",
)


@pytest.fixture()
def client(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(billing, "DB_PATH", db_path)
    for mod in AGENT_MODULES:
        monkeypatch.setattr(mod, "DB_PATH", db_path)
    monkeypatch.setattr(history_store, "DB_PATH", db_path)
    monkeypatch.setattr(ti_data_access, "DB_PATH", db_path)
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_USERNAME", "testadmin")
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_PASSWORD", "Testpass123")
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_EMAIL", None)
    app.init_db()
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        yield c


@pytest.fixture()
def history_db(monkeypatch, tmp_path):
    """Just the intelligence_history tables, for tests that don't need a
    full Flask client (store/analytics unit tests)."""
    db_path = str(tmp_path / "history.db")
    monkeypatch.setattr(history_store, "DB_PATH", db_path)
    history_store.init_db()
    return db_path


def _login_admin(client):
    conn = sqlite3.connect(app.DB_PATH)
    admin_id = conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]
    conn.close()
    with client.session_transaction() as sess:
        sess["user_id"] = admin_id


def _snapshot(**overrides):
    defaults = dict(
        symbol="NIFTY", bias="BULLISH", confidence=60, oi_strength=50,
        probability_score=55, volume_score=80, greeks_alignment="BULLISH LEAN", institutional_score=40,
    )
    defaults.update(overrides)
    return intelligence_models.MarketIntelligenceSnapshot(**defaults)


def _log(symbol="NIFTY", ts=None, **overrides):
    ts = ts or dt.datetime.now().isoformat()
    return history_store.record_snapshot(ts=ts, symbol=symbol, timeframe="3m", snapshot=_snapshot(symbol=symbol, **overrides))


# --- 1. snapshot records can be inserted and read ---------------------------

class TestInsertion:
    def test_record_and_count(self, history_db):
        _log()
        assert history_store.count_total() == 1

    def test_list_since_is_chronological(self, history_db):
        _log(ts="2026-08-09T09:00:00")
        _log(ts="2026-08-09T09:30:00")
        _log(ts="2026-08-09T09:15:00")
        rows = history_store.list_since(symbol="NIFTY")
        assert [r["ts"] for r in rows] == ["2026-08-09T09:00:00", "2026-08-09T09:15:00", "2026-08-09T09:30:00"]

    def test_list_recent_is_newest_first_and_limited(self, history_db):
        for i in range(5):
            _log(ts=f"2026-08-09T09:0{i}:00")
        recent = history_store.list_recent(limit=3)
        assert len(recent) == 3
        assert recent[0]["ts"] == "2026-08-09T09:04:00"

    def test_last_snapshot_ts_per_symbol(self, history_db):
        _log(symbol="NIFTY", ts="2026-08-09T09:00:00")
        _log(symbol="BANKNIFTY", ts="2026-08-09T09:30:00")
        assert history_store.last_snapshot_ts(symbol="NIFTY") == "2026-08-09T09:00:00"
        assert history_store.last_snapshot_ts(symbol="BANKNIFTY") == "2026-08-09T09:30:00"
        assert history_store.last_snapshot_ts() == "2026-08-09T09:30:00"

    def test_count_since_filters_by_ts(self, history_db):
        _log(ts="2026-08-09T08:00:00")
        _log(ts="2026-08-09T10:00:00")
        assert history_store.count_since("2026-08-09T09:00:00") == 1


# --- 2. analytics: bias stability --------------------------------------------

class TestBiasStability:
    def test_no_flips_reports_zero_rate(self, history_db):
        for i in range(3):
            _log(ts=f"2026-08-09T09:0{i}:00", bias="BULLISH")
        result = analytics.compute_bias_stability(symbol="NIFTY")
        assert result == {"snapshot_count": 3, "transitions": 2, "flips": 0, "flip_rate": 0.0}

    def test_every_transition_flipping_reports_rate_one(self, history_db):
        _log(ts="2026-08-09T09:00:00", bias="BULLISH")
        _log(ts="2026-08-09T09:05:00", bias="BEARISH")
        _log(ts="2026-08-09T09:10:00", bias="BULLISH")
        result = analytics.compute_bias_stability(symbol="NIFTY")
        assert result["flips"] == 2
        assert result["flip_rate"] == 1.0

    def test_fewer_than_two_snapshots_reports_none_rate(self, history_db):
        _log()
        result = analytics.compute_bias_stability(symbol="NIFTY")
        assert result["flip_rate"] is None

    def test_empty_history_reports_zero_counts(self, history_db):
        result = analytics.compute_bias_stability(symbol="NOTHING_LOGGED")
        assert result == {"snapshot_count": 0, "transitions": 0, "flips": 0, "flip_rate": None}


# --- 3. analytics: confidence stability --------------------------------------

class TestConfidenceStability:
    def test_constant_confidence_has_zero_stdev(self, history_db):
        for i in range(4):
            _log(ts=f"2026-08-09T09:0{i}:00", confidence=70)
        result = analytics.compute_confidence_stability(symbol="NIFTY")
        assert result["mean_confidence"] == 70
        assert result["stdev"] == 0.0
        assert result["max_single_step_delta"] == 0

    def test_varying_confidence_reports_real_stdev_and_max_delta(self, history_db):
        _log(ts="2026-08-09T09:00:00", confidence=40)
        _log(ts="2026-08-09T09:05:00", confidence=70)
        _log(ts="2026-08-09T09:10:00", confidence=50)
        result = analytics.compute_confidence_stability(symbol="NIFTY")
        assert result["max_single_step_delta"] == 30
        assert result["stdev"] > 0


# --- 4. analytics: greeks coherence ------------------------------------------

class TestGreeksCoherence:
    def test_agreeing_bullish_is_fully_coherent(self, history_db):
        _log(bias="BULLISH", greeks_alignment="BULLISH LEAN")
        _log(bias="BULLISH", greeks_alignment="BULLISH LEAN")
        result = analytics.compute_greeks_coherence(symbol="NIFTY")
        assert result == {"snapshot_count": 2, "resolved_count": 2, "coherent_count": 2, "coherence_rate": 1.0}

    def test_disagreeing_bullish_with_bearish_lean_is_incoherent(self, history_db):
        _log(bias="BULLISH", greeks_alignment="BEARISH LEAN")
        result = analytics.compute_greeks_coherence(symbol="NIFTY")
        assert result["coherence_rate"] == 0.0

    def test_neutral_and_unavailable_rows_are_excluded_not_incoherent(self, history_db):
        _log(bias="NEUTRAL", greeks_alignment="NEUTRAL")
        _log(bias="BULLISH", greeks_alignment="UNAVAILABLE")
        result = analytics.compute_greeks_coherence(symbol="NIFTY")
        assert result == {"snapshot_count": 2, "resolved_count": 0, "coherent_count": 0, "coherence_rate": None}


# --- 5. analytics: OI responsiveness -----------------------------------------

class TestOiResponsiveness:
    def test_constant_oi_strength_has_zero_change_rate(self, history_db):
        _log(ts="2026-08-09T09:00:00", oi_strength=50)
        _log(ts="2026-08-09T09:05:00", oi_strength=50)
        result = analytics.compute_oi_responsiveness(symbol="NIFTY")
        assert result["change_rate"] == 0.0

    def test_changing_oi_strength_has_nonzero_change_rate(self, history_db):
        _log(ts="2026-08-09T09:00:00", oi_strength=10)
        _log(ts="2026-08-09T09:05:00", oi_strength=90)
        result = analytics.compute_oi_responsiveness(symbol="NIFTY")
        assert result["change_rate"] == 1.0


# --- 6. analytics: bias/price correlation (honest pending, never fabricated) -

class TestBiasPriceCorrelation:
    def test_no_directional_snapshots_reports_zero(self, history_db):
        _log(bias="NEUTRAL")
        result = analytics.compute_bias_price_correlation(symbol="NIFTY")
        assert result["directional_count"] == 0
        assert result["agreement_rate"] is None

    def test_no_archived_candle_data_reports_pending_not_fabricated(self, history_db, monkeypatch):
        """No data/history/<symbol>/3m.parquet exists for a symbol that
        was never really tracked -- compute_bias_price_correlation()
        must degrade to 'pending', never invent an agreement rate."""
        _log(symbol="NOT_A_REAL_SYMBOL", bias="BULLISH")
        result = analytics.compute_bias_price_correlation(symbol="NOT_A_REAL_SYMBOL")
        assert result["directional_count"] == 1
        assert result["scored_count"] == 0
        assert result["pending_count"] == 1
        assert result["agreement_rate"] is None


# --- 7. compute_report bundles all five sections -----------------------------

class TestComputeReport:
    def test_report_has_all_five_sections(self, history_db):
        _log()
        report = analytics.compute_report(symbol="NIFTY")
        assert report["symbol"] == "NIFTY"
        for section in ("bias_stability", "confidence_stability", "greeks_coherence",
                        "oi_responsiveness", "bias_price_correlation"):
            assert section in report


# --- 8. api.py is a thin, read-only pass-through -----------------------------

class TestApi:
    def test_get_status_shape(self, history_db):
        _log()
        status = history_api.get_status()
        assert status["read_only"] is True
        assert status["no_orders_placed"] is True
        assert status["snapshot_count"] == 1

    def test_get_recent_delegates_to_store(self, history_db):
        _log(ts="2026-08-09T09:00:00")
        _log(ts="2026-08-09T09:05:00")
        recent = history_api.get_recent(symbol="NIFTY", limit=1)
        assert len(recent) == 1
        assert recent[0]["ts"] == "2026-08-09T09:05:00"

    def test_get_report_delegates_to_analytics(self, history_db):
        _log()
        assert history_api.get_report(symbol="NIFTY") == analytics.compute_report(symbol="NIFTY")

    def test_get_status_carries_runtime_visibility_fields(self, history_db):
        """Milestone 13, Phase 3: the dashboard's Runtime Status card
        fields. last_manual_snapshot_ts and last_history_write_ts are
        intentionally the same value -- there is no separate "queried but
        not logged" event to report honestly (see api.py's own
        docstring)."""
        _log(ts="2026-08-09T09:00:00")
        status = history_api.get_status()
        assert status["runtime_scheduler_enabled"] is agents_config.RUNTIME_SCHEDULER_ENABLED
        assert status["last_manual_snapshot_ts"] == "2026-08-09T09:00:00"
        assert status["last_history_write_ts"] == "2026-08-09T09:00:00"
        assert status["total_history_records"] == 1
        assert status["app_version"] == agents_config.APP_VERSION
        assert status["environment"] == agents_config.ENVIRONMENT

    def test_get_status_with_no_history_reports_none_not_fabricated(self, history_db):
        status = history_api.get_status()
        assert status["last_manual_snapshot_ts"] is None
        assert status["last_history_write_ts"] is None
        assert status["total_history_records"] == 0


# --- 8b. pagination: store.list_recent(offset=) + api.get_recent_page -------

class TestPagination:
    def test_offset_skips_newest_rows(self, history_db):
        for i in range(5):
            _log(ts=f"2026-08-09T09:0{i}:00")
        page1 = history_store.list_recent(limit=2, offset=0)
        page2 = history_store.list_recent(limit=2, offset=2)
        assert [r["ts"] for r in page1] == ["2026-08-09T09:04:00", "2026-08-09T09:03:00"]
        assert [r["ts"] for r in page2] == ["2026-08-09T09:02:00", "2026-08-09T09:01:00"]

    def test_count_total_respects_symbol_filter(self, history_db):
        _log(symbol="NIFTY")
        _log(symbol="NIFTY")
        _log(symbol="BANKNIFTY")
        assert history_store.count_total() == 3
        assert history_store.count_total(symbol="NIFTY") == 2
        assert history_store.count_total(symbol="BANKNIFTY") == 1

    def test_get_recent_page_shape_and_total(self, history_db):
        for i in range(3):
            _log(symbol="NIFTY", ts=f"2026-08-09T09:0{i}:00")
        page = history_api.get_recent_page(symbol="NIFTY", limit=2, offset=0)
        assert page["total"] == 3
        assert page["limit"] == 2
        assert page["offset"] == 0
        assert len(page["items"]) == 2

    def test_get_recent_unaffected_by_new_offset_param(self, history_db):
        """Existing get_recent() callers (unchanged signature default)
        still get a bare list, not a dict -- this must keep working
        exactly as it did in Phase 2."""
        _log()
        recent = history_api.get_recent(symbol="NIFTY", limit=1)
        assert isinstance(recent, list)


# --- 8c. snapshot detail: store.get_by_id + api.get_snapshot ----------------

class TestSnapshotDetail:
    def test_get_by_id_returns_the_logged_row(self, history_db):
        row_id = _log(symbol="NIFTY", bias="BULLISH")
        row = history_store.get_by_id(row_id)
        assert row["id"] == row_id
        assert row["symbol"] == "NIFTY"
        assert row["bias"] == "BULLISH"

    def test_get_by_id_missing_returns_none_not_fabricated(self, history_db):
        assert history_store.get_by_id(9999) is None

    def test_api_get_snapshot_delegates_to_store(self, history_db):
        row_id = _log()
        assert history_api.get_snapshot(row_id) == history_store.get_by_id(row_id)


# --- 9. CLI: dry-run performs zero writes ------------------------------------

class TestCliDryRun:
    def test_dry_run_log_performs_no_insert(self, history_db, monkeypatch, capsys):
        import argparse
        import intelligence_orchestrator as orch
        monkeypatch.setattr(orch, "build_snapshot", lambda symbol, timeframe="3m": _snapshot(symbol=symbol))
        before = history_store.count_total()
        intelligence_history_cli._cmd_log(argparse.Namespace(symbol="NIFTY", timeframe="3m", dry_run=True, export_json=None))
        after = history_store.count_total()
        assert before == after == 0
        out = capsys.readouterr().out
        assert intelligence_history_cli.DRY_RUN_BANNER in out

    def test_real_log_writes_exactly_one_row(self, history_db, monkeypatch, capsys):
        import argparse
        import intelligence_orchestrator as orch
        monkeypatch.setattr(orch, "build_snapshot", lambda symbol, timeframe="3m": _snapshot(symbol=symbol))
        intelligence_history_cli._cmd_log(argparse.Namespace(symbol="NIFTY", timeframe="3m", dry_run=False, export_json=None))
        assert history_store.count_total() == 1

    def test_log_with_no_available_snapshot_writes_nothing(self, history_db, monkeypatch, capsys):
        import argparse
        import intelligence_orchestrator as orch
        monkeypatch.setattr(orch, "build_snapshot", lambda symbol, timeframe="3m": None)
        intelligence_history_cli._cmd_log(argparse.Namespace(symbol="NIFTY", timeframe="3m", dry_run=False, export_json=None))
        assert history_store.count_total() == 0
        out = capsys.readouterr().out
        assert "no market snapshot available" in out


# --- 10. all HTTP endpoints GET-only, admin-gated ----------------------------

class TestEndpointsAreGetOnly:
    @pytest.mark.parametrize("path", [
        "/api/intelligence/history/status",
        "/api/intelligence/history/recent",
        "/api/intelligence/history/report?symbol=NIFTY",
        "/api/intelligence/history/page?symbol=NIFTY",
    ])
    def test_get_succeeds_for_an_admin(self, client, path):
        _login_admin(client)
        resp = client.get(path)
        assert resp.status_code == 200

    @pytest.mark.parametrize("path", [
        "/api/intelligence/history/status",
        "/api/intelligence/history/recent",
        "/api/intelligence/history/report?symbol=NIFTY",
        "/api/intelligence/history/page?symbol=NIFTY",
        "/api/intelligence/history/snapshot/1",
    ])
    def test_post_returns_405(self, client, path):
        _login_admin(client)
        resp = client.post(path)
        assert resp.status_code == 405

    def test_report_without_symbol_returns_400(self, client):
        _login_admin(client)
        resp = client.get("/api/intelligence/history/report")
        assert resp.status_code == 400

    def test_unauthenticated_is_redirected_to_login(self, client):
        resp = client.get("/api/intelligence/history/status")
        assert resp.status_code == 302

    def test_non_admin_gets_403(self, client):
        now = dt.datetime.now().isoformat()
        conn = sqlite3.connect(app.DB_PATH)
        cur = conn.execute(
            "INSERT INTO users (email, username, password_hash, role, is_verified, created_at, updated_at) "
            "VALUES (?,?,?,?,1,?,?)",
            ("sub@example.com", "sub", "x", "subscriber", now, now),
        )
        conn.commit()
        user_id = cur.lastrowid
        conn.close()
        with client.session_transaction() as sess:
            sess["user_id"] = user_id
        resp = client.get("/api/intelligence/history/status")
        assert resp.status_code == 403

    def test_status_endpoint_reports_read_only_and_no_orders_placed(self, client):
        _login_admin(client)
        data = client.get("/api/intelligence/history/status").get_json()
        assert data["read_only"] is True
        assert data["no_orders_placed"] is True


# --- 10b. Milestone 13, Phase 3: /page and /snapshot/<id> routes ------------

class TestHistoryPageRoute:
    def test_page_returns_paginated_shape(self, client):
        _login_admin(client)
        conn = sqlite3.connect(app.DB_PATH)
        for i in range(3):
            history_store.record_snapshot(
                ts=f"2026-08-09T09:0{i}:00", symbol="NIFTY", timeframe="3m", snapshot=_snapshot(),
            )
        conn.close()
        resp = client.get("/api/intelligence/history/page?symbol=NIFTY&limit=2&offset=0")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] == 3
        assert len(data["items"]) == 2

    def test_page_unauthenticated_is_redirected_to_login(self, client):
        resp = client.get("/api/intelligence/history/page")
        assert resp.status_code == 302

    def test_page_limit_is_capped_at_100(self, client):
        _login_admin(client)
        resp = client.get("/api/intelligence/history/page?limit=999")
        assert resp.status_code == 200
        assert resp.get_json()["limit"] == 100


class TestSnapshotDetailRoute:
    def test_existing_snapshot_returns_200_with_full_row(self, client):
        _login_admin(client)
        row_id = history_store.record_snapshot(
            ts="2026-08-09T09:00:00", symbol="NIFTY", timeframe="3m", snapshot=_snapshot(bias="BULLISH"),
        )
        resp = client.get(f"/api/intelligence/history/snapshot/{row_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["id"] == row_id
        assert data["bias"] == "BULLISH"

    def test_missing_snapshot_returns_404_with_honest_reason(self, client):
        _login_admin(client)
        resp = client.get("/api/intelligence/history/snapshot/9999")
        assert resp.status_code == 404
        assert "error" in resp.get_json()

    def test_snapshot_unauthenticated_is_redirected_to_login(self, client):
        resp = client.get("/api/intelligence/history/snapshot/1")
        assert resp.status_code == 302

    def test_snapshot_non_admin_gets_403(self, client):
        now = dt.datetime.now().isoformat()
        conn = sqlite3.connect(app.DB_PATH)
        cur = conn.execute(
            "INSERT INTO users (email, username, password_hash, role, is_verified, created_at, updated_at) "
            "VALUES (?,?,?,?,1,?,?)",
            ("sub2@example.com", "sub2", "x", "subscriber", now, now),
        )
        conn.commit()
        user_id = cur.lastrowid
        conn.close()
        with client.session_transaction() as sess:
            sess["user_id"] = user_id
        resp = client.get("/api/intelligence/history/snapshot/1")
        assert resp.status_code == 403


# --- 11. no broker/scheduler modules are imported ----------------------------

class TestNoBrokerImports:
    def test_intelligence_history_source_files_have_no_forbidden_imports(self):
        """Static AST check -- independent of what else this pytest
        process has already imported. Parses each intelligence_history
        source file's own import statements directly."""
        for path in INTELLIGENCE_HISTORY_FILES:
            source = path.read_text()
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    lowered = name.lower()
                    for forbidden in FORBIDDEN_IMPORT_SUBSTRINGS:
                        assert forbidden not in lowered, f"{path}: forbidden import {name!r} contains {forbidden!r}"

    def test_intelligence_history_never_imports_app_module(self):
        for path in INTELLIGENCE_HISTORY_FILES:
            source = path.read_text()
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    assert not any(alias.name == "app" for alias in node.names), path
                if isinstance(node, ast.ImportFrom):
                    assert node.module != "app", path


# --- 12. scheduler flags / locked agents untouched ---------------------------

class TestSchedulerSafetyUntouched:
    def test_runtime_scheduler_enabled_still_false(self):
        assert agents_config.RUNTIME_SCHEDULER_ENABLED is False

    def test_runtime_control_api_enabled_still_false(self):
        assert agents_config.RUNTIME_CONTROL_API_ENABLED is False

    def test_trading_intelligence_still_unschedulable(self):
        assert sc.is_schedulable("trading_intelligence") is False

    def test_quant_researcher_still_unschedulable(self):
        assert sc.is_schedulable("quant_researcher") is False

    def test_shadow_mode_still_unschedulable(self):
        assert sc.is_schedulable("shadow_mode") is False

    def test_intelligence_history_never_registered_as_a_runtime_agent(self):
        from agents.runtime import agent_runtime
        assert "intelligence_history" not in agent_runtime.RUNTIME_AGENT_NAMES
