"""
test_intelligence_alerts.py -- Milestone 14, Phase 1: regression tests
for agents/intelligence_alerts/ (store/rules/api), intelligence_alerts_cli.py,
and the three new GET /api/intelligence/alerts/* routes.

NOTE on file location: no "backend/" or "tests/" directory exists
anywhere in this repository -- every one of its test files lives at the
repo root as test_*.py. This file follows that established convention,
same as test_intelligence_history.py before it.

Same SKIP_AUTOSTART=1 + throwaway-DB technique as every other route-level
test file in this project: no live thread, no real oi_history.db touched
by any test here. Every Telegram send in this file is monkeypatched --
no test ever performs a real network call.
"""
import argparse
import ast
import datetime as dt
import sqlite3
from pathlib import Path

import os
os.environ["SKIP_AUTOSTART"] = "1"

import pytest

import app
import auth
import billing
from agents import audit_log, event_bus
from agents import config as agents_config
from agents.intelligence_alerts import api as alerts_api, rules, store as alerts_store
from agents.intelligence_history import store as history_store
from agents.risk_manager import risk_store
from agents.runtime import scheduling_control as sc
from agents.runtime import runtime_store
from agents.sys_admin import sysadmin_store
from agents.trading_intelligence import data_access as ti_data_access
from agents.trading_supervisor import supervision_store

import intelligence_alerts_cli
import intelligence_models

AGENT_MODULES = (audit_log, event_bus, risk_store, supervision_store, sysadmin_store, runtime_store)
INTELLIGENCE_ALERTS_FILES = [
    Path("agents/intelligence_alerts/__init__.py"),
    Path("agents/intelligence_alerts/store.py"),
    Path("agents/intelligence_alerts/rules.py"),
    Path("agents/intelligence_alerts/api.py"),
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
    monkeypatch.setattr(alerts_store, "DB_PATH", db_path)
    monkeypatch.setattr(ti_data_access, "DB_PATH", db_path)
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_USERNAME", "testadmin")
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_PASSWORD", "Testpass123")
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_EMAIL", None)
    app.init_db()
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        yield c


@pytest.fixture()
def alerts_db(monkeypatch, tmp_path):
    """Both intelligence_history's and intelligence_alerts' own tables,
    for tests that don't need a full Flask client -- rules.py reads
    history_store, so both must share the same throwaway DB file."""
    db_path = str(tmp_path / "alerts.db")
    monkeypatch.setattr(history_store, "DB_PATH", db_path)
    monkeypatch.setattr(alerts_store, "DB_PATH", db_path)
    history_store.init_db()
    alerts_store.init_db()
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


def _log_snapshot(symbol="NIFTY", ts=None, **overrides):
    ts = ts or dt.datetime.now().isoformat()
    return history_store.record_snapshot(ts=ts, symbol=symbol, timeframe="3m", snapshot=_snapshot(symbol=symbol, **overrides))


def _log_alert(symbol="NIFTY", ts=None, rule="bias_flip", **overrides):
    ts = ts or dt.datetime.now().isoformat()
    defaults = dict(ts=ts, symbol=symbol, rule=rule, detail=f"{symbol} {rule}")
    defaults.update(overrides)
    return alerts_store.record_alert(**defaults)


# --- 1. store: insert/read/pagination ----------------------------------------

class TestStore:
    def test_record_and_count(self, alerts_db):
        _log_alert()
        assert alerts_store.count_total() == 1

    def test_list_recent_is_newest_first_and_limited(self, alerts_db):
        for i in range(5):
            _log_alert(ts=f"2026-08-09T09:0{i}:00")
        recent = alerts_store.list_recent(limit=3)
        assert len(recent) == 3
        assert recent[0]["ts"] == "2026-08-09T09:04:00"

    def test_offset_skips_newest_rows(self, alerts_db):
        for i in range(5):
            _log_alert(ts=f"2026-08-09T09:0{i}:00")
        page2 = alerts_store.list_recent(limit=2, offset=2)
        assert [r["ts"] for r in page2] == ["2026-08-09T09:02:00", "2026-08-09T09:01:00"]

    def test_count_total_respects_symbol_filter(self, alerts_db):
        _log_alert(symbol="NIFTY")
        _log_alert(symbol="NIFTY")
        _log_alert(symbol="BANKNIFTY")
        assert alerts_store.count_total() == 3
        assert alerts_store.count_total(symbol="NIFTY") == 2

    def test_last_alert_ts_per_symbol(self, alerts_db):
        _log_alert(symbol="NIFTY", ts="2026-08-09T09:00:00")
        _log_alert(symbol="BANKNIFTY", ts="2026-08-09T09:30:00")
        assert alerts_store.last_alert_ts(symbol="NIFTY") == "2026-08-09T09:00:00"
        assert alerts_store.last_alert_ts() == "2026-08-09T09:30:00"

    def test_delivered_flags_round_trip(self, alerts_db):
        row_id = _log_alert(delivered_telegram=True, delivered_email=False)
        row = alerts_store.list_recent(limit=1)[0]
        assert row["id"] == row_id
        assert row["delivered_telegram"] == 1
        assert row["delivered_email"] == 0


# --- 2. rules: bias flip ------------------------------------------------------

class TestBiasFlip:
    def test_flip_triggers(self, alerts_db):
        _log_snapshot(ts="2026-08-09T09:00:00", bias="BULLISH")
        _log_snapshot(ts="2026-08-09T09:05:00", bias="BEARISH")
        result = rules.check_bias_flip(symbol="NIFTY")
        assert result is not None
        assert result["rule"] == "bias_flip"
        assert "BULLISH -> BEARISH" in result["detail"]

    def test_same_bias_does_not_trigger(self, alerts_db):
        _log_snapshot(ts="2026-08-09T09:00:00", bias="BULLISH")
        _log_snapshot(ts="2026-08-09T09:05:00", bias="BULLISH")
        assert rules.check_bias_flip(symbol="NIFTY") is None

    def test_fewer_than_two_snapshots_does_not_trigger(self, alerts_db):
        _log_snapshot(bias="BULLISH")
        assert rules.check_bias_flip(symbol="NIFTY") is None


# --- 3. rules: confidence instability ------------------------------------------

class TestConfidenceUnstable:
    def test_high_stdev_triggers(self, alerts_db, monkeypatch):
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_CONFIDENCE_STDEV_THRESHOLD", 5.0)
        for i, conf in enumerate([10, 90, 20, 80, 15]):
            _log_snapshot(ts=f"2026-08-09T09:0{i}:00", confidence=conf)
        result = rules.check_confidence_unstable(symbol="NIFTY")
        assert result is not None
        assert result["rule"] == "confidence_unstable"

    def test_low_stdev_does_not_trigger(self, alerts_db, monkeypatch):
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_CONFIDENCE_STDEV_THRESHOLD", 50.0)
        for i, conf in enumerate([60, 61, 59, 60, 61]):
            _log_snapshot(ts=f"2026-08-09T09:0{i}:00", confidence=conf)
        assert rules.check_confidence_unstable(symbol="NIFTY") is None

    def test_fewer_than_two_values_does_not_trigger(self, alerts_db):
        _log_snapshot(confidence=60)
        assert rules.check_confidence_unstable(symbol="NIFTY") is None


# --- 4. rules: Greeks incoherence ----------------------------------------------

class TestGreeksIncoherent:
    def test_disagreeing_bullish_triggers(self, alerts_db):
        _log_snapshot(bias="BULLISH", greeks_alignment="BEARISH LEAN")
        result = rules.check_greeks_incoherent(symbol="NIFTY")
        assert result is not None
        assert result["rule"] == "greeks_incoherent"

    def test_agreeing_bullish_does_not_trigger(self, alerts_db):
        _log_snapshot(bias="BULLISH", greeks_alignment="BULLISH LEAN")
        assert rules.check_greeks_incoherent(symbol="NIFTY") is None

    def test_neutral_bias_never_triggers(self, alerts_db):
        _log_snapshot(bias="NEUTRAL", greeks_alignment="BEARISH LEAN")
        assert rules.check_greeks_incoherent(symbol="NIFTY") is None

    def test_unavailable_greeks_never_triggers(self, alerts_db):
        _log_snapshot(bias="BULLISH", greeks_alignment="UNAVAILABLE")
        assert rules.check_greeks_incoherent(symbol="NIFTY") is None

    def test_no_history_does_not_trigger(self, alerts_db):
        assert rules.check_greeks_incoherent(symbol="NIFTY") is None


# --- 5. rules: OI non-responsiveness --------------------------------------------

class TestOiNonResponsive:
    def test_constant_oi_over_full_window_triggers(self, alerts_db, monkeypatch):
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_OI_WINDOW", 3)
        for i in range(3):
            _log_snapshot(ts=f"2026-08-09T09:0{i}:00", oi_strength=50)
        result = rules.check_oi_non_responsive(symbol="NIFTY")
        assert result is not None
        assert result["rule"] == "oi_non_responsive"

    def test_varying_oi_does_not_trigger(self, alerts_db, monkeypatch):
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_OI_WINDOW", 3)
        for i, oi in enumerate([50, 55, 60]):
            _log_snapshot(ts=f"2026-08-09T09:0{i}:00", oi_strength=oi)
        assert rules.check_oi_non_responsive(symbol="NIFTY") is None

    def test_window_not_yet_full_does_not_trigger(self, alerts_db, monkeypatch):
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_OI_WINDOW", 5)
        _log_snapshot(oi_strength=50)
        _log_snapshot(oi_strength=50)
        assert rules.check_oi_non_responsive(symbol="NIFTY") is None


# --- 6. rules: evaluate_all aggregates -------------------------------------------

class TestEvaluateAll:
    def test_aggregates_multiple_triggered_rules(self, alerts_db, monkeypatch):
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_OI_WINDOW", 2)
        _log_snapshot(ts="2026-08-09T09:00:00", bias="BULLISH", oi_strength=50, greeks_alignment="BULLISH LEAN")
        _log_snapshot(ts="2026-08-09T09:05:00", bias="BEARISH", oi_strength=50, greeks_alignment="BULLISH LEAN")
        triggered = rules.evaluate_all(symbol="NIFTY")
        rule_names = {r["rule"] for r in triggered}
        assert "bias_flip" in rule_names
        assert "greeks_incoherent" in rule_names
        assert "oi_non_responsive" in rule_names

    def test_no_history_triggers_nothing(self, alerts_db):
        assert rules.evaluate_all(symbol="NIFTY") == []


# --- 7. api.py is a thin, read-only pass-through ---------------------------------

class TestApi:
    def test_get_status_shape(self, alerts_db):
        _log_alert()
        status = alerts_api.get_status()
        assert status["read_only"] is True
        assert status["no_orders_placed"] is True
        assert status["alert_count"] == 1
        assert "telegram_configured" in status
        assert "email_configured" in status

    def test_get_recent_page_shape(self, alerts_db):
        for i in range(3):
            _log_alert(ts=f"2026-08-09T09:0{i}:00")
        page = alerts_api.get_recent_page(limit=2, offset=0)
        assert page["total"] == 3
        assert len(page["items"]) == 2

    def test_get_rules_shape(self, alerts_db):
        rules_dump = alerts_api.get_rules()
        assert "bias_flip" in rules_dump
        assert "confidence_unstable" in rules_dump
        assert "greeks_incoherent" in rules_dump
        assert "oi_non_responsive" in rules_dump


# --- 8. CLI: dry-run performs zero writes and zero sends -------------------------

class TestCliDryRun:
    def test_dry_run_check_performs_no_writes_no_sends(self, alerts_db, monkeypatch, capsys):
        sent = []
        monkeypatch.setattr(intelligence_alerts_cli, "_send_telegram", lambda msg: sent.append(msg) or True)
        _log_snapshot(ts="2026-08-09T09:00:00", bias="BULLISH")
        _log_snapshot(ts="2026-08-09T09:05:00", bias="BEARISH")
        before = alerts_store.count_total()
        intelligence_alerts_cli._cmd_check(argparse.Namespace(symbol="NIFTY", dry_run=True))
        after = alerts_store.count_total()
        assert before == after == 0
        assert sent == []
        out = capsys.readouterr().out
        assert intelligence_alerts_cli.DRY_RUN_BANNER in out

    def test_real_check_writes_and_attempts_telegram(self, alerts_db, monkeypatch, capsys):
        sent = []
        monkeypatch.setattr(intelligence_alerts_cli, "_send_telegram", lambda msg: sent.append(msg) or True)
        _log_snapshot(ts="2026-08-09T09:00:00", bias="BULLISH")
        _log_snapshot(ts="2026-08-09T09:05:00", bias="BEARISH")
        intelligence_alerts_cli._cmd_check(argparse.Namespace(symbol="NIFTY", dry_run=False))
        assert alerts_store.count_total() >= 1
        assert len(sent) >= 1

    def test_no_trigger_writes_nothing(self, alerts_db, monkeypatch, capsys):
        monkeypatch.setattr(intelligence_alerts_cli, "_send_telegram", lambda msg: True)
        intelligence_alerts_cli._cmd_check(argparse.Namespace(symbol="NIFTY", dry_run=False))
        assert alerts_store.count_total() == 0


# --- 9. all HTTP endpoints GET-only, admin-gated ---------------------------------

class TestEndpointsAreGetOnly:
    @pytest.mark.parametrize("path", [
        "/api/intelligence/alerts/status",
        "/api/intelligence/alerts/recent",
        "/api/intelligence/alerts/rules",
    ])
    def test_get_succeeds_for_an_admin(self, client, path):
        _login_admin(client)
        resp = client.get(path)
        assert resp.status_code == 200

    @pytest.mark.parametrize("path", [
        "/api/intelligence/alerts/status",
        "/api/intelligence/alerts/recent",
        "/api/intelligence/alerts/rules",
    ])
    def test_post_returns_405(self, client, path):
        _login_admin(client)
        resp = client.post(path)
        assert resp.status_code == 405

    def test_unauthenticated_is_redirected_to_login(self, client):
        resp = client.get("/api/intelligence/alerts/status")
        assert resp.status_code == 302

    def test_non_admin_gets_403(self, client):
        now = dt.datetime.now().isoformat()
        conn = sqlite3.connect(app.DB_PATH)
        cur = conn.execute(
            "INSERT INTO users (email, username, password_hash, role, is_verified, created_at, updated_at) "
            "VALUES (?,?,?,?,1,?,?)",
            ("sub3@example.com", "sub3", "x", "subscriber", now, now),
        )
        conn.commit()
        user_id = cur.lastrowid
        conn.close()
        with client.session_transaction() as sess:
            sess["user_id"] = user_id
        resp = client.get("/api/intelligence/alerts/status")
        assert resp.status_code == 403

    def test_status_endpoint_reports_read_only_and_no_orders_placed(self, client):
        _login_admin(client)
        data = client.get("/api/intelligence/alerts/status").get_json()
        assert data["read_only"] is True
        assert data["no_orders_placed"] is True


# --- 10. no broker/scheduler modules are imported --------------------------------

class TestNoBrokerImports:
    def test_intelligence_alerts_source_files_have_no_forbidden_imports(self):
        for path in INTELLIGENCE_ALERTS_FILES:
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

    def test_intelligence_alerts_never_imports_app_module(self):
        for path in INTELLIGENCE_ALERTS_FILES:
            source = path.read_text()
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    assert not any(alias.name == "app" for alias in node.names), path
                if isinstance(node, ast.ImportFrom):
                    assert node.module != "app", path

    def test_cli_never_imports_app_module_either(self):
        """intelligence_alerts_cli.py isn't in the package-level check
        above, but it still must never import app.py -- doing so would
        trigger app.py's own module-level Angel One SmartAPI login side
        effect every time the CLI runs (see its own docstring)."""
        source = Path("intelligence_alerts_cli.py").read_text()
        tree = ast.parse(source, filename="intelligence_alerts_cli.py")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any(alias.name == "app" for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                assert node.module != "app"


# --- 11. scheduler flags / locked agents untouched -------------------------------

class TestSchedulerSafetyUntouched:
    def test_runtime_scheduler_enabled_still_false(self):
        assert agents_config.RUNTIME_SCHEDULER_ENABLED is False

    def test_runtime_control_api_enabled_still_false(self):
        assert agents_config.RUNTIME_CONTROL_API_ENABLED is False

    def test_trading_intelligence_still_unschedulable(self):
        assert sc.is_schedulable("trading_intelligence") is False

    def test_shadow_mode_still_unschedulable(self):
        assert sc.is_schedulable("shadow_mode") is False

    def test_intelligence_alerts_never_registered_as_a_runtime_agent(self):
        from agents.runtime import agent_runtime
        assert "intelligence_alerts" not in agent_runtime.RUNTIME_AGENT_NAMES

    def test_intelligence_alerts_auto_enabled_defaults_false(self):
        assert agents_config.INTELLIGENCE_ALERTS_AUTO_ENABLED is False


# --- 12. store: cooldown lookup ---------------------------------------------------

class TestLastAlertTsForRule:
    def test_none_when_never_alerted(self, alerts_db):
        assert alerts_store.last_alert_ts_for_rule(symbol="NIFTY", rule="bias_flip") is None

    def test_returns_most_recent_for_that_exact_symbol_and_rule(self, alerts_db):
        _log_alert(symbol="NIFTY", rule="bias_flip", ts="2026-08-09T09:00:00")
        _log_alert(symbol="NIFTY", rule="bias_flip", ts="2026-08-09T09:05:00")
        _log_alert(symbol="NIFTY", rule="greeks_incoherent", ts="2026-08-09T09:10:00")
        _log_alert(symbol="BANKNIFTY", rule="bias_flip", ts="2026-08-09T09:15:00")
        assert alerts_store.last_alert_ts_for_rule(symbol="NIFTY", rule="bias_flip") == "2026-08-09T09:05:00"


# --- 13. Milestone 14, Phase 2: automatic per-cycle evaluation --------------------

class TestAutoAlertCycle:
    def test_no_snapshot_available_writes_and_sends_nothing(self, alerts_db, monkeypatch):
        import intelligence_orchestrator as orch
        monkeypatch.setattr(orch, "build_snapshot", lambda symbol, timeframe="3m": None)
        sent = []
        monkeypatch.setattr(app, "send_telegram", lambda msg: sent.append(msg))
        app._run_intelligence_alerts_auto_cycle("NIFTY")
        assert history_store.count_total() == 0
        assert alerts_store.count_total() == 0
        assert sent == []

    def test_snapshot_available_logs_history_and_evaluates_rules(self, alerts_db, monkeypatch):
        import intelligence_orchestrator as orch
        _log_snapshot(ts="2026-08-09T09:00:00", bias="BULLISH", greeks_alignment="BULLISH LEAN")
        monkeypatch.setattr(
            orch, "build_snapshot",
            lambda symbol, timeframe="3m": _snapshot(symbol=symbol, bias="BEARISH", greeks_alignment="BULLISH LEAN"),
        )
        sent = []
        monkeypatch.setattr(app, "send_telegram", lambda msg: sent.append(msg))
        app._run_intelligence_alerts_auto_cycle("NIFTY")

        assert history_store.count_total() == 2  # the seeded one + the new auto-logged one
        rule_names = {r["rule"] for r in alerts_store.list_recent(symbol="NIFTY")}
        assert "bias_flip" in rule_names          # BULLISH -> BEARISH
        assert "greeks_incoherent" in rule_names  # BEARISH bias, BULLISH LEAN greeks
        assert len(sent) == len(rule_names)

    def test_cooldown_suppresses_repeat_alert_for_same_rule(self, alerts_db, monkeypatch):
        import intelligence_orchestrator as orch
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERTS_AUTO_COOLDOWN_SECONDS", 900)
        _log_snapshot(ts="2026-08-09T09:00:00", bias="BULLISH", greeks_alignment="BULLISH LEAN")
        _log_alert(symbol="NIFTY", rule="bias_flip", ts=dt.datetime.now().isoformat())  # "just alerted"
        monkeypatch.setattr(
            orch, "build_snapshot",
            lambda symbol, timeframe="3m": _snapshot(symbol=symbol, bias="BEARISH", greeks_alignment="BEARISH LEAN"),
        )
        sent = []
        monkeypatch.setattr(app, "send_telegram", lambda msg: sent.append(msg))
        before = alerts_store.count_total()
        app._run_intelligence_alerts_auto_cycle("NIFTY")
        after = alerts_store.count_total()
        assert after == before  # bias_flip suppressed by cooldown; still logs history though
        assert sent == []

    def test_expired_cooldown_allows_realert(self, alerts_db, monkeypatch):
        import intelligence_orchestrator as orch
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERTS_AUTO_COOLDOWN_SECONDS", 1)
        _log_snapshot(ts="2026-08-09T09:00:00", bias="BULLISH")
        old_ts = (dt.datetime.now() - dt.timedelta(seconds=10)).isoformat()
        _log_alert(symbol="NIFTY", rule="bias_flip", ts=old_ts)
        monkeypatch.setattr(orch, "build_snapshot", lambda symbol, timeframe="3m": _snapshot(symbol=symbol, bias="BEARISH"))
        sent = []
        monkeypatch.setattr(app, "send_telegram", lambda msg: sent.append(msg))
        app._run_intelligence_alerts_auto_cycle("NIFTY")
        rule_names = [r["rule"] for r in alerts_store.list_recent(symbol="NIFTY")]
        assert "bias_flip" in rule_names
        assert len(sent) >= 1

    def test_flag_off_means_run_symbol_loop_never_calls_auto_cycle_source_check(self):
        """Static check (not a live loop test -- run_symbol_loop is an
        infinite background loop, impractical to unit-test directly):
        confirms the call site is gated by INTELLIGENCE_ALERTS_AUTO_ENABLED
        in the source itself."""
        import inspect
        source = inspect.getsource(app.run_symbol_loop)
        assert "INTELLIGENCE_ALERTS_AUTO_ENABLED" in source
        assert "_run_intelligence_alerts_auto_cycle" in source
