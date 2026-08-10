"""
test_shadow_mode_read_only.py -- Milestone 12, Phase 2B: regression
tests proving agents/shadow_mode/ is fully read-only/passive.

Same SKIP_AUTOSTART=1 + throwaway-DB technique as test_auth.py/
test_runtime_control_routes.py: no live thread, no real oi_history.db
touched by any test here.
"""
import ast
import datetime as dt
import os
import sqlite3
from pathlib import Path
from unittest import mock

os.environ["SKIP_AUTOSTART"] = "1"

import pandas as pd
import pytest

import app
import auth
import billing
from agents import config as agents_config
from agents.risk_manager import risk_store
from agents.runtime import runtime_store, scheduling_control as sc
from agents.shadow_mode import api as shadow_api
from agents.shadow_mode import evaluator, observer, store as shadow_store
from agents.sys_admin import sysadmin_store
from agents.trading_intelligence import data_access as ti_data_access
from agents.trading_supervisor import supervision_store
from agents import audit_log, event_bus

AGENT_MODULES = (audit_log, event_bus, risk_store, supervision_store, sysadmin_store, runtime_store)
SHADOW_MODE_FILES = [
    Path("agents/shadow_mode/store.py"),
    Path("agents/shadow_mode/observer.py"),
    Path("agents/shadow_mode/evaluator.py"),
    Path("agents/shadow_mode/api.py"),
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
    monkeypatch.setattr(shadow_store, "DB_PATH", db_path)
    monkeypatch.setattr(ti_data_access, "DB_PATH", db_path)
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_USERNAME", "testadmin")
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_PASSWORD", "Testpass123")
    monkeypatch.setattr(app, "ADMIN_BOOTSTRAP_EMAIL", None)
    app.init_db()
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        yield c


@pytest.fixture()
def shadow_db(monkeypatch, tmp_path):
    """Just the shadow_mode tables, for tests that don't need a full
    Flask client (store/evaluator unit tests)."""
    db_path = str(tmp_path / "shadow.db")
    monkeypatch.setattr(shadow_store, "DB_PATH", db_path)
    shadow_store.init_db()
    return db_path


def _login_admin(client):
    conn = sqlite3.connect(app.DB_PATH)
    admin_id = conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]
    conn.close()
    with client.session_transaction() as sess:
        sess["user_id"] = admin_id


def _seed_prediction(observation_kwargs=None, **prediction_overrides):
    obs_defaults = dict(ts="2026-08-08T10:00:00", symbol="NIFTY", timeframe="3m")
    obs_defaults.update(observation_kwargs or {})
    obs_id = shadow_store.record_observation(**obs_defaults)
    defaults = dict(
        observation_id=obs_id, ts="2026-08-08T10:00:00", symbol="NIFTY", timeframe="3m",
        signal_type="BUY CE", expected_direction="CE", confidence=75,
        reasoning_snapshot="test", entry_reference_price=25000,
        expected_target_low=25000, expected_target_high=25100,
        valid_until_ts="2026-08-08T10:45:00",
    )
    defaults.update(prediction_overrides)
    pred_id = shadow_store.record_prediction(**defaults)
    return obs_id, pred_id


# --- 1. observation records can be inserted -------------------------------

class TestInsertion:
    def test_record_observation_and_prediction(self, shadow_db):
        obs_id, pred_id = _seed_prediction()
        assert shadow_store.count_observations() == 1
        assert shadow_store.count_predictions() == 1
        prediction = shadow_store.get_prediction(pred_id)
        assert prediction["symbol"] == "NIFTY"
        assert prediction["observation_id"] == obs_id

    def test_record_outcome(self, shadow_db):
        _, pred_id = _seed_prediction()
        shadow_store.record_outcome(
            prediction_id=pred_id, evaluated_ts="2026-08-08T11:00:00",
            classification="correct", actual_direction="CE", actual_move_pts=100, actual_move_pct=0.4,
        )
        outcome = shadow_store.get_outcome_for_prediction(pred_id)
        assert outcome["classification"] == "correct"

    def test_last_prediction_ts_reflects_most_recent(self, shadow_db):
        _seed_prediction()
        _seed_prediction(observation_kwargs={"ts": "2026-08-08T12:00:00"}, ts="2026-08-08T12:00:00")
        assert shadow_store.last_prediction_ts() == "2026-08-08T12:00:00"

    def test_list_recent_predictions_with_outcomes_left_joins_pending(self, shadow_db):
        _, pred_id = _seed_prediction()
        recent = shadow_store.list_recent_predictions_with_outcomes(limit=10)
        assert len(recent) == 1
        assert recent[0]["classification"] is None   # not yet evaluated


# --- Observer: read-only, degrades honestly when no snapshot exists -------

class TestObserver:
    def test_observe_and_predict_returns_none_without_a_market_snapshot(self, client):
        """No cycle has ever been logged for this symbol in the
        throwaway DB -- market_data.get_snapshot() returns
        available=False, and observe_and_predict() must degrade to
        None rather than raise, the same honest-degradation contract
        every other reader in this framework holds to. Also implicitly
        proves observe_and_predict() never wrote a shadow_predictions
        row for data that doesn't exist."""
        result = observer.observe_and_predict("NIFTY")
        assert result is None
        assert shadow_store.count_observations() == 0
        assert shadow_store.count_predictions() == 0


# --- Evaluator classification correctness ----------------------------------

class TestEvaluatorClassification:
    def test_favorable_move_past_target_is_correct(self, shadow_db):
        _, pred_id = _seed_prediction()
        candles = pd.DataFrame({
            "datetime": pd.to_datetime(["2026-08-08T10:03:00"]),
            "open": [25000], "high": [25150], "low": [24990], "close": [25120],
        })
        with mock.patch.object(evaluator.data_access, "load_candles", return_value=candles):
            outcome = evaluator.evaluate_prediction(pred_id, now=dt.datetime(2026, 8, 8, 10, 30))
        assert outcome["classification"] == "correct"

    def test_adverse_move_is_incorrect(self, shadow_db):
        _, pred_id = _seed_prediction()
        candles = pd.DataFrame({
            "datetime": pd.to_datetime(["2026-08-08T10:03:00"]),
            "open": [25000], "high": [25000], "low": [24900], "close": [24920],
        })
        with mock.patch.object(evaluator.data_access, "load_candles", return_value=candles):
            outcome = evaluator.evaluate_prediction(pred_id, now=dt.datetime(2026, 8, 8, 10, 30))
        assert outcome["classification"] == "incorrect"

    def test_small_favorable_move_short_of_target_is_partial(self, shadow_db):
        _, pred_id = _seed_prediction()
        candles = pd.DataFrame({
            "datetime": pd.to_datetime(["2026-08-08T10:03:00"]),
            "open": [25000], "high": [25030], "low": [24990], "close": [25020],
        })
        with mock.patch.object(evaluator.data_access, "load_candles", return_value=candles):
            outcome = evaluator.evaluate_prediction(pred_id, now=dt.datetime(2026, 8, 8, 10, 30))
        assert outcome["classification"] == "partial"

    def test_no_data_after_window_closes_is_expired(self, shadow_db):
        _, pred_id = _seed_prediction()
        empty = pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])
        with mock.patch.object(evaluator.data_access, "load_candles", return_value=empty):
            outcome = evaluator.evaluate_prediction(pred_id, now=dt.datetime(2026, 8, 8, 11, 0))
        assert outcome["classification"] == "expired"

    def test_still_within_window_with_no_data_yet_stays_pending(self, shadow_db):
        _, pred_id = _seed_prediction()
        empty = pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])
        with mock.patch.object(evaluator.data_access, "load_candles", return_value=empty):
            outcome = evaluator.evaluate_prediction(pred_id, now=dt.datetime(2026, 8, 8, 10, 20))
        assert outcome is None
        assert shadow_store.get_outcome_for_prediction(pred_id) is None

    def test_evaluating_an_already_evaluated_prediction_is_a_safe_no_op(self, shadow_db):
        _, pred_id = _seed_prediction()
        candles = pd.DataFrame({
            "datetime": pd.to_datetime(["2026-08-08T10:03:00"]),
            "open": [25000], "high": [25150], "low": [24990], "close": [25120],
        })
        with mock.patch.object(evaluator.data_access, "load_candles", return_value=candles):
            first = evaluator.evaluate_prediction(pred_id, now=dt.datetime(2026, 8, 8, 10, 30))
            second = evaluator.evaluate_prediction(pred_id, now=dt.datetime(2026, 8, 8, 10, 31))
        assert first is not None
        assert second is None   # already has an outcome -- no duplicate row


# --- 2. performance metrics calculate correctly -----------------------------

class TestPerformanceMetrics:
    def test_win_rate_and_prediction_count(self, shadow_db):
        _, correct_id = _seed_prediction()
        shadow_store.record_outcome(
            prediction_id=correct_id, evaluated_ts="2026-08-08T11:00:00",
            classification="correct", actual_move_pct=0.5,
        )
        _, incorrect_id = _seed_prediction()
        shadow_store.record_outcome(
            prediction_id=incorrect_id, evaluated_ts="2026-08-08T11:00:00",
            classification="incorrect", actual_move_pct=-0.3,
        )
        metrics = evaluator.compute_metrics(symbol="NIFTY")
        assert metrics["prediction_count"] == 2
        assert metrics["evaluated_count"] == 2
        assert metrics["win_rate"] == 0.5
        assert metrics["average_move_captured_pct"] == pytest.approx(0.1, abs=1e-6)

    def test_metrics_with_zero_predictions_degrade_honestly(self, shadow_db):
        metrics = evaluator.compute_metrics()
        assert metrics["prediction_count"] == 0
        assert metrics["win_rate"] is None

    def test_confidence_calibration_buckets_by_classification(self, shadow_db):
        _, pred_id = _seed_prediction(confidence=90)
        shadow_store.record_outcome(
            prediction_id=pred_id, evaluated_ts="2026-08-08T11:00:00", classification="correct",
        )
        metrics = evaluator.compute_metrics()
        assert metrics["confidence_calibration"]["correct"]["avg_confidence"] == 90


# --- Market-Open Observation Validation: today-scoped counters -------------

class TestTodayCounters:
    def test_counters_exclude_yesterdays_rows(self, shadow_db):
        yesterday = (dt.datetime.now() - dt.timedelta(days=1)).isoformat()
        obs_id = shadow_store.record_observation(ts=yesterday, symbol="NIFTY", timeframe="3m")
        shadow_store.record_prediction(
            observation_id=obs_id, ts=yesterday, symbol="NIFTY", timeframe="3m", signal_type="NO_TRADE",
        )
        status = shadow_api.get_status()
        assert status["observations_today"] == 0
        assert status["predictions_today"] == 0
        assert status["observation_count"] == 1   # still counted in the all-time total

    def test_counters_include_todays_rows(self, shadow_db):
        _, pred_id = _seed_prediction()   # _seed_prediction's default ts is a fixed 2026-08-08 date
        status = shadow_api.get_status()
        # ts is fixed in the past relative to "today" at test-run time in CI,
        # so assert via the same accessor the counters themselves use rather
        # than assuming "today" -- record with an actually-current timestamp.
        now = dt.datetime.now().isoformat()
        shadow_store.record_outcome(prediction_id=pred_id, evaluated_ts=now, classification="correct")
        obs_id = shadow_store.record_observation(ts=now, symbol="NIFTY", timeframe="3m")
        shadow_store.record_prediction(
            observation_id=obs_id, ts=now, symbol="NIFTY", timeframe="3m", signal_type="BUY CE", confidence=70,
        )
        status = shadow_api.get_status()
        assert status["observations_today"] >= 1
        assert status["predictions_today"] >= 1
        assert status["evaluated_outcomes_today"] >= 1

    def test_current_win_rate_matches_compute_metrics(self, shadow_db):
        _, pred_id = _seed_prediction()
        shadow_store.record_outcome(prediction_id=pred_id, evaluated_ts="2026-08-08T11:00:00", classification="correct")
        status = shadow_api.get_status()
        assert status["current_win_rate"] == evaluator.compute_metrics()["win_rate"]

    def test_status_endpoint_returns_all_four_counters(self, client):
        _login_admin(client)
        data = client.get("/api/shadow/status").get_json()
        for key in ("observations_today", "predictions_today", "evaluated_outcomes_today", "current_win_rate"):
            assert key in data


# --- 3 & 4. all endpoints GET-only, POST returns 405 ------------------------

class TestEndpointsAreGetOnly:
    @pytest.mark.parametrize("path", ["/api/shadow/status", "/api/shadow/recent", "/api/shadow/performance"])
    def test_get_succeeds_for_an_admin(self, client, path):
        _login_admin(client)
        resp = client.get(path)
        assert resp.status_code == 200

    @pytest.mark.parametrize("path", ["/api/shadow/status", "/api/shadow/recent", "/api/shadow/performance"])
    def test_post_returns_405(self, client, path):
        _login_admin(client)
        resp = client.post(path)
        assert resp.status_code == 405

    @pytest.mark.parametrize("path", ["/api/shadow/status", "/api/shadow/recent", "/api/shadow/performance"])
    def test_put_and_delete_return_405(self, client, path):
        _login_admin(client)
        assert client.put(path).status_code == 405
        assert client.delete(path).status_code == 405

    def test_status_endpoint_reports_read_only_and_no_orders_placed(self, client):
        _login_admin(client)
        data = client.get("/api/shadow/status").get_json()
        assert data["read_only"] is True
        assert data["no_orders_placed"] is True


# --- 5. no broker modules are imported --------------------------------------

class TestNoBrokerImports:
    def test_shadow_mode_source_files_have_no_forbidden_imports(self):
        """Static AST check -- independent of what else this pytest
        process has already imported (e.g. app.py, imported by other
        test files in the same session, DOES import the broker SDK;
        checking sys.modules would give false positives). Parses each
        shadow_mode source file's own import statements directly."""
        for path in SHADOW_MODE_FILES:
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
                        assert forbidden not in lowered, (
                            f"{path}: forbidden import {name!r} contains {forbidden!r}"
                        )

    def test_shadow_mode_never_imports_app_module(self):
        for path in SHADOW_MODE_FILES:
            source = path.read_text()
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    assert not any(alias.name == "app" for alias in node.names), path
                if isinstance(node, ast.ImportFrom):
                    assert node.module != "app", path


# --- 6 & 7. scheduler flags / locked agents untouched -----------------------

class TestSchedulerSafetyUntouched:
    def test_runtime_scheduler_enabled_still_false(self):
        assert agents_config.RUNTIME_SCHEDULER_ENABLED is False

    def test_runtime_control_api_enabled_still_false(self):
        assert agents_config.RUNTIME_CONTROL_API_ENABLED is False

    def test_trading_intelligence_now_schedulable(self):
        """Milestone 17: trading_intelligence was deliberately removed
        from NEVER_SCHEDULABLE_AGENTS -- quant_researcher and
        shadow_mode remain permanently blocked."""
        assert sc.is_schedulable("trading_intelligence") is True
        assert sc.is_schedulable("quant_researcher") is False
        assert sc.is_schedulable("shadow_mode") is False

    def test_quant_researcher_still_unschedulable(self):
        assert sc.is_schedulable("quant_researcher") is False

    def test_shadow_mode_is_registered_but_permanently_locked(self):
        # Milestone 12, Phase 3: shadow_mode WAS added to
        # RUNTIME_AGENT_NAMES (for status/health tracking only -- see
        # agent_runtime._shadow_mode_cycle()'s own docstring), but is
        # simultaneously locked into NEVER_SCHEDULABLE_AGENTS on the
        # same permanent, code-level footing as trading_intelligence/
        # quant_researcher, so it can never actually be scheduled.
        from agents.runtime import agent_runtime
        assert "shadow_mode" in agent_runtime.RUNTIME_AGENT_NAMES
        assert "shadow_mode" in sc.NEVER_SCHEDULABLE_AGENTS

    def test_shadow_mode_is_not_schedulable(self):
        assert sc.is_schedulable("shadow_mode") is False


# --- 8. startup does not launch any Shadow Mode worker automatically -------

class TestNoAutomaticWorker:
    def test_app_source_never_calls_observer_or_evaluator_at_startup(self):
        """AST-based check on app.py itself -- confirms no actual CALL
        to observer.observe_and_predict()/evaluator.evaluate_pending()/
        evaluate_prediction() exists anywhere in app.py (a plain
        substring search would false-positive on this very module's own
        explanatory comments, which mention these names in prose while
        explaining why they're absent). Only the read-only api.*
        functions and shadow_store.init_db() -- schema creation, not
        execution -- may appear as actual calls."""
        forbidden_calls = {"observe_and_predict", "evaluate_pending", "evaluate_prediction"}
        tree = ast.parse(Path("app.py").read_text(), filename="app.py")
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name in forbidden_calls:
                    found.add(name)
        assert not found, f"app.py actually CALLS forbidden Shadow Mode function(s): {found}"

    def test_agent_runtime_source_never_calls_observer_or_evaluator(self):
        """Same AST-based check, on agents/runtime/agent_runtime.py --
        Milestone 12, Phase 3 registered "shadow_mode" as a runtime
        agent for status/health tracking only (_shadow_mode_cycle()),
        and that cycle function must never actually execute Shadow
        Mode's observation/evaluation logic. Closes the exact gap
        flagged when that registration was added: the app.py-only scan
        above would not have caught a violation introduced here."""
        forbidden_calls = {"observe_and_predict", "evaluate_pending", "evaluate_prediction"}
        path = Path("agents/runtime/agent_runtime.py")
        tree = ast.parse(path.read_text(), filename=str(path))
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name in forbidden_calls:
                    found.add(name)
        assert not found, f"agent_runtime.py actually CALLS forbidden Shadow Mode function(s): {found}"

    def test_importing_app_creates_no_new_non_main_threads_from_shadow_mode(self, client):
        import threading
        shadow_threads = [
            t for t in threading.enumerate()
            if "shadow" in t.name.lower()
        ]
        assert shadow_threads == []

    def test_shadow_mode_package_has_no_module_level_side_effects(self):
        """Confirms none of the four shadow_mode files call anything
        at import time (module-level statements are only def/class/
        import/constant assignments, no bare function calls) -- the
        only way a background thread or scheduled job could start
        "automatically on app startup" is a module-level call, and
        there are none."""
        for path in SHADOW_MODE_FILES:
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in tree.body:
                assert not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call), (
                    f"{path} has a module-level function call: {ast.dump(node)}"
                )
