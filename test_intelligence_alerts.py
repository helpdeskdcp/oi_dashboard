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
from agents.intelligence_alerts import (
    api as alerts_api, cooldown, dedup_store, rules, store as alerts_store, threshold_store,
)
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
    Path("agents/intelligence_alerts/threshold_store.py"),
    Path("agents/intelligence_alerts/cooldown.py"),
    Path("agents/intelligence_alerts/dedup_store.py"),
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
    monkeypatch.setattr(threshold_store, "DB_PATH", db_path)
    monkeypatch.setattr(dedup_store, "DB_PATH", db_path)
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
    monkeypatch.setattr(threshold_store, "DB_PATH", db_path)
    monkeypatch.setattr(dedup_store, "DB_PATH", db_path)
    history_store.init_db()
    alerts_store.init_db()
    threshold_store.init_db()
    dedup_store.init_db()
    return db_path


CSRF_TOKEN = "test-csrf-token"


def _login_admin(client):
    conn = sqlite3.connect(app.DB_PATH)
    admin_id = conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]
    conn.close()
    with client.session_transaction() as sess:
        sess["user_id"] = admin_id
        sess["csrf_token"] = CSRF_TOKEN


def _post(client, path, **kw):
    """auth.csrf_guard() (app.py's global before_request hook) requires a
    valid csrf_token for every POST -- included in the JSON body here,
    matching test_trading_intelligence_run_cycle_route.py's own _post()
    helper."""
    payload = {"csrf_token": CSRF_TOKEN}
    payload.update(kw)
    return client.post(path, json=payload)


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
    def test_flip_triggers_with_min_confirmations_one(self, alerts_db, monkeypatch):
        """min_bias_confirmations=1 reproduces the exact pre-Phase-0
        behavior: a single-snapshot change vs. the immediately prior
        snapshot is itself a confirmed flip."""
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_MIN_BIAS_CONFIRMATIONS", 1)
        _log_snapshot(ts="2026-08-09T09:00:00", bias="BULLISH")
        _log_snapshot(ts="2026-08-09T09:05:00", bias="BEARISH")
        result = rules.check_bias_flip(symbol="NIFTY")
        assert result is not None
        assert result["rule"] == "bias_flip"
        assert "BULLISH -> BEARISH" in result["detail"]

    def test_same_bias_does_not_trigger(self, alerts_db, monkeypatch):
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_MIN_BIAS_CONFIRMATIONS", 1)
        _log_snapshot(ts="2026-08-09T09:00:00", bias="BULLISH")
        _log_snapshot(ts="2026-08-09T09:05:00", bias="BULLISH")
        assert rules.check_bias_flip(symbol="NIFTY") is None

    def test_fewer_than_two_snapshots_does_not_trigger(self, alerts_db, monkeypatch):
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_MIN_BIAS_CONFIRMATIONS", 1)
        _log_snapshot(bias="BULLISH")
        assert rules.check_bias_flip(symbol="NIFTY") is None

    def test_default_min_confirmations_is_two(self):
        assert agents_config.INTELLIGENCE_ALERT_MIN_BIAS_CONFIRMATIONS == 2

    def test_default_cooldown_is_300_seconds(self):
        assert agents_config.INTELLIGENCE_ALERT_BIAS_FLIP_COOLDOWN_SECONDS == 300


# --- 2b. Milestone 15, Phase 0: Bias Flip Stabilization -------------------------

class TestBiasFlipStabilization:
    def test_whipsaw_never_confirms_at_default_window(self, alerts_db):
        """The exact example from the M15 Phase 0 spec: BEARISH, BULLISH,
        BEARISH, BULLISH -- no two consecutive snapshots ever agree, so
        nothing is ever confirmed at the default min_bias_confirmations=2."""
        for i, bias in enumerate(["BEARISH", "BULLISH", "BEARISH", "BULLISH"]):
            _log_snapshot(ts=f"2026-08-09T09:0{i}:00", bias=bias)
            assert rules.check_bias_flip(symbol="NIFTY") is None

    def test_two_consecutive_confirms_the_flip(self, alerts_db):
        _log_snapshot(ts="2026-08-09T09:00:00", bias="BEARISH")
        _log_snapshot(ts="2026-08-09T09:01:00", bias="BULLISH")
        _log_snapshot(ts="2026-08-09T09:02:00", bias="BULLISH")
        result = rules.check_bias_flip(symbol="NIFTY")
        assert result is not None
        assert "BEARISH -> BULLISH" in result["detail"]
        assert "2 consecutive snapshots" in result["detail"]

    def test_does_not_reconfirm_the_same_flip_on_a_later_cycle(self, alerts_db):
        """Once confirmed, a THIRD, FOURTH... snapshot of the same bias
        must not re-trigger -- only the exact snapshot where confirmation
        is first reached should alert."""
        for i, bias in enumerate(["BEARISH", "BULLISH", "BULLISH", "BULLISH", "BULLISH"]):
            _log_snapshot(ts=f"2026-08-09T09:0{i}:00", bias=bias)
        result = rules.check_bias_flip(symbol="NIFTY")
        assert result is None

    def test_custom_confirmation_window(self, alerts_db, monkeypatch):
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_MIN_BIAS_CONFIRMATIONS", 3)
        _log_snapshot(ts="2026-08-09T09:00:00", bias="BEARISH")
        _log_snapshot(ts="2026-08-09T09:01:00", bias="BULLISH")
        _log_snapshot(ts="2026-08-09T09:02:00", bias="BULLISH")
        assert rules.check_bias_flip(symbol="NIFTY") is None  # only 2 of 3 required
        _log_snapshot(ts="2026-08-09T09:03:00", bias="BULLISH")
        result = rules.check_bias_flip(symbol="NIFTY")
        assert result is not None
        assert "3 consecutive snapshots" in result["detail"]

    def test_not_enough_history_for_confirmation_window_does_not_trigger(self, alerts_db, monkeypatch):
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_MIN_BIAS_CONFIRMATIONS", 5)
        _log_snapshot(ts="2026-08-09T09:00:00", bias="BULLISH")
        _log_snapshot(ts="2026-08-09T09:01:00", bias="BULLISH")
        assert rules.check_bias_flip(symbol="NIFTY") is None

    def test_first_ever_confirmed_run_with_no_prior_history_does_not_trigger(self, alerts_db):
        """Exactly min_confirm snapshots exist, all agreeing, but there's
        no earlier snapshot to compare against -- honestly not a 'flip'
        (there's nothing to have flipped from), not fabricated as one."""
        _log_snapshot(ts="2026-08-09T09:00:00", bias="BULLISH")
        _log_snapshot(ts="2026-08-09T09:01:00", bias="BULLISH")
        assert rules.check_bias_flip(symbol="NIFTY") is None

    def test_cooldown_suppresses_a_repeat_confirmation_shortly_after(self, alerts_db):
        alerts_store.record_alert(
            ts=dt.datetime.now().isoformat(), symbol="NIFTY", rule="bias_flip", detail="already alerted",
        )
        _log_snapshot(ts="2026-08-09T09:00:00", bias="BEARISH")
        _log_snapshot(ts="2026-08-09T09:01:00", bias="BULLISH")
        _log_snapshot(ts="2026-08-09T09:02:00", bias="BULLISH")
        assert rules.check_bias_flip(symbol="NIFTY") is None

    def test_cooldown_expires_and_allows_a_real_new_confirmation(self, alerts_db, monkeypatch):
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_BIAS_FLIP_COOLDOWN_SECONDS", 1)
        old_ts = (dt.datetime.now() - dt.timedelta(seconds=10)).isoformat()
        alerts_store.record_alert(ts=old_ts, symbol="NIFTY", rule="bias_flip", detail="old alert")
        _log_snapshot(ts="2026-08-09T09:00:00", bias="BEARISH")
        _log_snapshot(ts="2026-08-09T09:01:00", bias="BULLISH")
        _log_snapshot(ts="2026-08-09T09:02:00", bias="BULLISH")
        result = rules.check_bias_flip(symbol="NIFTY")
        assert result is not None

    def test_cooldown_is_per_symbol(self, alerts_db):
        """A recent bias_flip alert for NIFTY must not suppress a
        genuinely new confirmed flip on a different symbol."""
        alerts_store.record_alert(
            ts=dt.datetime.now().isoformat(), symbol="NIFTY", rule="bias_flip", detail="already alerted",
        )
        _log_snapshot(symbol="BANKNIFTY", ts="2026-08-09T09:00:00", bias="BEARISH")
        _log_snapshot(symbol="BANKNIFTY", ts="2026-08-09T09:01:00", bias="BULLISH")
        _log_snapshot(symbol="BANKNIFTY", ts="2026-08-09T09:02:00", bias="BULLISH")
        result = rules.check_bias_flip(symbol="BANKNIFTY")
        assert result is not None

    def test_bias_computation_itself_is_never_touched(self):
        """Static guarantee for the M15 Phase 0 scope constraint: this
        rule reads the already-logged `bias` field verbatim and never
        calls or imports anything that computes it."""
        source = Path("agents/intelligence_alerts/rules.py").read_text()
        for forbidden in ("detect_bias", "classify_buildup", "generate_signal", "oi_engine", "intelligence_orchestrator"):
            assert forbidden not in source


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


# --- 5b. Milestone 14 observability pass: low-liquidity commodity suppression ----

class TestOiNonResponsiveLiquiditySuppression:
    @pytest.mark.parametrize("symbol", ["GOLD", "SILVER", "CRUDEOIL", "NATURALGAS"])
    @pytest.mark.parametrize("quality", ["NO_LIQUIDITY", "THIN"])
    def test_suppressed_for_named_commodities_when_liquidity_is_low(self, alerts_db, monkeypatch, symbol, quality):
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_OI_WINDOW", 3)
        for i in range(3):
            _log_snapshot(symbol=symbol, ts=f"2026-08-09T09:0{i}:00", oi_strength=0, market_quality=quality)
        assert rules.check_oi_non_responsive(symbol=symbol) is None

    @pytest.mark.parametrize("symbol", ["GOLD", "SILVER", "CRUDEOIL", "NATURALGAS"])
    def test_not_suppressed_for_named_commodities_when_liquidity_is_normal(self, alerts_db, monkeypatch, symbol):
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_OI_WINDOW", 3)
        for i in range(3):
            _log_snapshot(symbol=symbol, ts=f"2026-08-09T09:0{i}:00", oi_strength=50, market_quality="NORMAL")
        result = rules.check_oi_non_responsive(symbol=symbol)
        assert result is not None
        assert result["rule"] == "oi_non_responsive"

    @pytest.mark.parametrize("symbol", ["NIFTY", "BANKNIFTY"])
    @pytest.mark.parametrize("quality", ["NO_LIQUIDITY", "THIN", "NORMAL"])
    def test_index_symbols_never_suppressed_regardless_of_quality(self, alerts_db, monkeypatch, symbol, quality):
        """NIFTY/BANKNIFTY are explicitly NOT in the suppression list --
        an index symbol showing flat oi_strength should still alert even
        if something (e.g. a genuine data outage) also makes it look
        NO_LIQUIDITY/THIN that cycle."""
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_OI_WINDOW", 3)
        for i in range(3):
            _log_snapshot(symbol=symbol, ts=f"2026-08-09T09:0{i}:00", oi_strength=50, market_quality=quality)
        result = rules.check_oi_non_responsive(symbol=symbol)
        assert result is not None

    def test_mini_variant_not_suppressed(self, alerts_db, monkeypatch):
        """GOLDM is deliberately NOT in the suppression list -- only the
        exact 4 base symbols named in the investigation are, on purpose
        (see rules.py's own comment)."""
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_OI_WINDOW", 3)
        for i in range(3):
            _log_snapshot(symbol="GOLDM", ts=f"2026-08-09T09:0{i}:00", oi_strength=0, market_quality="NO_LIQUIDITY")
        result = rules.check_oi_non_responsive(symbol="GOLDM")
        assert result is not None

    def test_suppression_checks_latest_row_not_an_older_one(self, alerts_db, monkeypatch):
        """market_quality can change cycle to cycle -- only the most
        recent reading should decide whether today's flatness is noise."""
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_OI_WINDOW", 3)
        _log_snapshot(symbol="GOLD", ts="2026-08-09T09:00:00", oi_strength=0, market_quality="NO_LIQUIDITY")
        _log_snapshot(symbol="GOLD", ts="2026-08-09T09:01:00", oi_strength=0, market_quality="NO_LIQUIDITY")
        _log_snapshot(symbol="GOLD", ts="2026-08-09T09:02:00", oi_strength=0, market_quality="NORMAL")
        result = rules.check_oi_non_responsive(symbol="GOLD")
        assert result is not None  # latest row says NORMAL -- not suppressed


# --- 6. rules: evaluate_all aggregates -------------------------------------------

class TestEvaluateAll:
    def test_aggregates_multiple_triggered_rules(self, alerts_db, monkeypatch):
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_OI_WINDOW", 2)
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_MIN_BIAS_CONFIRMATIONS", 1)
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
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_MIN_BIAS_CONFIRMATIONS", 1)
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
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_MIN_BIAS_CONFIRMATIONS", 1)
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
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_MIN_BIAS_CONFIRMATIONS", 1)
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

    def test_dedup_suppresses_repeat_alert_for_same_condition(self, alerts_db, monkeypatch):
        """Milestone 15, Phase 1: the auto-cycle's cooldown gate is now
        dedup_store-backed, not alerts_store-backed -- seed the SAME
        (symbol, bias, rule) condition dedup_store would have recorded
        moments ago, and confirm the auto-cycle honors it."""
        import intelligence_orchestrator as orch
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_DEDUP_COOLDOWN_SECONDS", 900)
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_MIN_BIAS_CONFIRMATIONS", 1)
        _log_snapshot(ts="2026-08-09T09:00:00", bias="BULLISH", greeks_alignment="BULLISH LEAN")
        dedup_store.should_suppress(symbol="NIFTY", bias="BEARISH", confidence=60, rule="bias_flip", cooldown_seconds=900)
        dedup_store.should_suppress(symbol="NIFTY", bias="BEARISH", confidence=60, rule="greeks_incoherent", cooldown_seconds=900)
        monkeypatch.setattr(
            orch, "build_snapshot",
            lambda symbol, timeframe="3m": _snapshot(symbol=symbol, bias="BEARISH", greeks_alignment="BEARISH LEAN"),
        )
        sent = []
        monkeypatch.setattr(app, "send_telegram", lambda msg: sent.append(msg))
        before = alerts_store.count_total()
        app._run_intelligence_alerts_auto_cycle("NIFTY")
        after = alerts_store.count_total()
        assert after == before  # both rules suppressed by dedup; still logs history though
        assert sent == []

    def test_expired_dedup_cooldown_allows_realert(self, alerts_db, monkeypatch):
        import intelligence_orchestrator as orch
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_DEDUP_COOLDOWN_SECONDS", 1)
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_MIN_BIAS_CONFIRMATIONS", 1)
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_BIAS_FLIP_COOLDOWN_SECONDS", 1)
        _log_snapshot(ts="2026-08-09T09:00:00", bias="BULLISH")
        old_now = dt.datetime.now() - dt.timedelta(seconds=10)
        dedup_store.should_suppress(symbol="NIFTY", bias="BEARISH", confidence=60, rule="bias_flip", cooldown_seconds=1, now=old_now)
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


# --- 14. Milestone 14, Phase 3: threshold_store -----------------------------------

class TestThresholdStore:
    def test_fresh_install_has_no_overrides(self, alerts_db):
        assert threshold_store.get_override_rows() == []

    def test_effective_config_matches_defaults_when_no_overrides(self, alerts_db):
        config = threshold_store.get_effective_config()
        assert config["confidence_window"] == agents_config.INTELLIGENCE_ALERT_CONFIDENCE_WINDOW
        assert config["confidence_stdev_threshold"] == agents_config.INTELLIGENCE_ALERT_CONFIDENCE_STDEV_THRESHOLD
        assert config["oi_window"] == agents_config.INTELLIGENCE_ALERT_OI_WINDOW
        assert config["auto_cooldown_seconds"] == agents_config.INTELLIGENCE_ALERTS_AUTO_COOLDOWN_SECONDS
        assert config["low_liquidity_suppression_symbols"] == list(
            agents_config.INTELLIGENCE_ALERT_LOW_LIQUIDITY_SUPPRESSION_SYMBOLS
        )

    def test_set_override_then_effective_config_reflects_it(self, alerts_db):
        threshold_store.set_override("confidence_window", 12, updated_by="tester", reason="widen window")
        assert threshold_store.get_effective_config()["confidence_window"] == 12

    def test_set_override_is_idempotent_upsert(self, alerts_db):
        threshold_store.set_override("oi_window", 4, updated_by="tester", reason="first")
        threshold_store.set_override("oi_window", 9, updated_by="tester", reason="second")
        rows = threshold_store.get_override_rows()
        assert len([r for r in rows if r["key"] == "oi_window"]) == 1
        assert threshold_store.get_effective_config()["oi_window"] == 9

    def test_clear_override_reverts_to_default(self, alerts_db):
        threshold_store.set_override("confidence_window", 12, updated_by="tester", reason="widen window")
        threshold_store.clear_override("confidence_window")
        assert threshold_store.get_effective_config()["confidence_window"] == agents_config.INTELLIGENCE_ALERT_CONFIDENCE_WINDOW

    def test_clear_override_on_never_set_key_is_a_noop(self, alerts_db):
        threshold_store.clear_override("confidence_window")  # must not raise
        assert threshold_store.get_override_rows() == []

    def test_set_override_rejects_unknown_key(self, alerts_db):
        with pytest.raises(ValueError):
            threshold_store.set_override("not_a_real_key", 1, updated_by="tester", reason="x")

    def test_get_override_rows_includes_audit_fields(self, alerts_db):
        threshold_store.set_override("confidence_window", 12, updated_by="tester", reason="widen window")
        row = threshold_store.get_override_rows()[0]
        assert row["key"] == "confidence_window"
        assert row["value"] == 12
        assert row["updated_by"] == "tester"
        assert row["reason"] == "widen window"
        assert row["updated_at"]

    def test_symbols_override_round_trips_as_a_list(self, alerts_db):
        threshold_store.set_override(
            "low_liquidity_suppression_symbols", ["GOLD", "SILVER"], updated_by="tester", reason="x",
        )
        assert threshold_store.get_effective_config()["low_liquidity_suppression_symbols"] == ["GOLD", "SILVER"]


# --- 15. Milestone 14, Phase 3: api.py validation, set_threshold/clear_threshold --

class TestThresholdApi:
    def test_get_rules_includes_overrides_list(self, alerts_db):
        assert alerts_api.get_rules()["overrides"] == []
        alerts_api.set_threshold("confidence_window", 12, updated_by="tester", reason="widen")
        rules_dump = alerts_api.get_rules()
        assert len(rules_dump["overrides"]) == 1
        assert rules_dump["confidence_unstable"]["window"] == 12

    def test_set_threshold_rejects_unknown_key(self, alerts_db):
        with pytest.raises(ValueError):
            alerts_api.set_threshold("not_a_real_key", 5, updated_by="tester", reason="x")

    @pytest.mark.parametrize("key", ["confidence_window", "oi_window"])
    def test_set_threshold_rejects_below_min(self, alerts_db, key):
        with pytest.raises(ValueError):
            alerts_api.set_threshold(key, 1, updated_by="tester", reason="x")

    @pytest.mark.parametrize("key", ["confidence_window", "oi_window"])
    def test_set_threshold_rejects_above_max(self, alerts_db, key):
        with pytest.raises(ValueError):
            alerts_api.set_threshold(key, 101, updated_by="tester", reason="x")

    def test_set_threshold_rejects_negative_stdev_threshold(self, alerts_db):
        with pytest.raises(ValueError):
            alerts_api.set_threshold("confidence_stdev_threshold", -1, updated_by="tester", reason="x")

    def test_set_threshold_rejects_negative_cooldown(self, alerts_db):
        with pytest.raises(ValueError):
            alerts_api.set_threshold("auto_cooldown_seconds", -1, updated_by="tester", reason="x")

    def test_set_threshold_rejects_non_numeric(self, alerts_db):
        with pytest.raises(ValueError):
            alerts_api.set_threshold("confidence_window", "twelve", updated_by="tester", reason="x")

    def test_set_threshold_rejects_bool_as_number(self, alerts_db):
        """isinstance(True, int) is True in Python -- explicitly excluded
        so a stray boolean can't silently pass as 0/1."""
        with pytest.raises(ValueError):
            alerts_api.set_threshold("confidence_window", True, updated_by="tester", reason="x")

    def test_set_threshold_accepts_valid_window(self, alerts_db):
        result = alerts_api.set_threshold("confidence_window", 15, updated_by="tester", reason="x")
        assert result["confidence_unstable"]["window"] == 15

    def test_set_threshold_symbols_must_be_list_of_strings(self, alerts_db):
        with pytest.raises(ValueError):
            alerts_api.set_threshold("low_liquidity_suppression_symbols", "GOLD", updated_by="tester", reason="x")
        with pytest.raises(ValueError):
            alerts_api.set_threshold("low_liquidity_suppression_symbols", ["GOLD", 5], updated_by="tester", reason="x")
        with pytest.raises(ValueError):
            alerts_api.set_threshold("low_liquidity_suppression_symbols", ["GOLD", ""], updated_by="tester", reason="x")

    def test_set_threshold_symbols_rejects_duplicates(self, alerts_db):
        with pytest.raises(ValueError):
            alerts_api.set_threshold(
                "low_liquidity_suppression_symbols", ["GOLD", "GOLD"], updated_by="tester", reason="x",
            )

    def test_set_threshold_symbols_uppercases_and_strips(self, alerts_db):
        result = alerts_api.set_threshold(
            "low_liquidity_suppression_symbols", [" gold ", "silver"], updated_by="tester", reason="x",
        )
        assert result["oi_non_responsive"]["low_liquidity_suppression_symbols"] == ["GOLD", "SILVER"]

    def test_clear_threshold_rejects_unknown_key(self, alerts_db):
        with pytest.raises(ValueError):
            alerts_api.clear_threshold("not_a_real_key", updated_by="tester", reason="x")

    def test_clear_threshold_reverts_to_default(self, alerts_db):
        alerts_api.set_threshold("confidence_window", 12, updated_by="tester", reason="x")
        result = alerts_api.clear_threshold("confidence_window", updated_by="tester", reason="x")
        assert result["confidence_unstable"]["window"] == agents_config.INTELLIGENCE_ALERT_CONFIDENCE_WINDOW

    def test_effective_override_actually_changes_rule_behavior(self, alerts_db):
        """Integration check: an override set through api.py must be the
        exact same value rules.py's check_oi_non_responsive() reads on
        its very next call -- not just reflected in get_rules()'s
        display."""
        alerts_api.set_threshold("oi_window", 2, updated_by="tester", reason="tighten window")
        _log_snapshot(ts="2026-08-09T09:00:00", oi_strength=50)
        _log_snapshot(ts="2026-08-09T09:01:00", oi_strength=50)
        result = rules.check_oi_non_responsive(symbol="NIFTY")
        assert result is not None
        assert "last 2 logged snapshots" in result["detail"]


# --- 16. Milestone 14, Phase 3: POST /api/intelligence/alerts/config route --------

class TestConfigRoute:
    def test_flag_defaults_to_false(self):
        assert agents_config.INTELLIGENCE_ALERT_CONFIG_API_ENABLED is False

    def test_returns_403_when_flag_off(self, client, monkeypatch):
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_CONFIG_API_ENABLED", False)
        _login_admin(client)
        resp = _post(client, "/api/intelligence/alerts/config", key="confidence_window", value=10, reason="x")
        assert resp.status_code == 403

    def test_missing_key_returns_400(self, client, monkeypatch):
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_CONFIG_API_ENABLED", True)
        _login_admin(client)
        resp = _post(client, "/api/intelligence/alerts/config", value=10, reason="x")
        assert resp.status_code == 400

    def test_missing_reason_returns_400(self, client, monkeypatch):
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_CONFIG_API_ENABLED", True)
        _login_admin(client)
        resp = _post(client, "/api/intelligence/alerts/config", key="confidence_window", value=10)
        assert resp.status_code == 400

    def test_blank_reason_returns_400(self, client, monkeypatch):
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_CONFIG_API_ENABLED", True)
        _login_admin(client)
        resp = _post(client, "/api/intelligence/alerts/config", key="confidence_window", value=10, reason="  ")
        assert resp.status_code == 400

    def test_missing_value_without_clear_returns_400(self, client, monkeypatch):
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_CONFIG_API_ENABLED", True)
        _login_admin(client)
        resp = _post(client, "/api/intelligence/alerts/config", key="confidence_window", reason="x")
        assert resp.status_code == 400

    def test_invalid_value_returns_400(self, client, monkeypatch):
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_CONFIG_API_ENABLED", True)
        _login_admin(client)
        resp = _post(client, "/api/intelligence/alerts/config", key="confidence_window", value=1, reason="x")
        assert resp.status_code == 400

    def test_unknown_key_returns_400(self, client, monkeypatch):
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_CONFIG_API_ENABLED", True)
        _login_admin(client)
        resp = _post(client, "/api/intelligence/alerts/config", key="not_a_real_key", value=10, reason="x")
        assert resp.status_code == 400

    def test_valid_set_returns_200_and_updates_rules(self, client, monkeypatch):
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_CONFIG_API_ENABLED", True)
        _login_admin(client)
        resp = _post(client, "/api/intelligence/alerts/config", key="confidence_window", value=15, reason="widen")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["rules"]["confidence_unstable"]["window"] == 15
        rules_resp = client.get("/api/intelligence/alerts/rules").get_json()
        assert rules_resp["confidence_unstable"]["window"] == 15

    def test_valid_clear_returns_200_and_reverts(self, client, monkeypatch):
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_CONFIG_API_ENABLED", True)
        _login_admin(client)
        _post(client, "/api/intelligence/alerts/config", key="confidence_window", value=15, reason="widen")
        resp = _post(client, "/api/intelligence/alerts/config", key="confidence_window", clear=True, reason="revert")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["rules"]["confidence_unstable"]["window"] == agents_config.INTELLIGENCE_ALERT_CONFIDENCE_WINDOW

    def test_get_returns_405(self, client, monkeypatch):
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_CONFIG_API_ENABLED", True)
        _login_admin(client)
        resp = client.get("/api/intelligence/alerts/config")
        assert resp.status_code == 405

    def test_unauthenticated_post_is_rejected_at_csrf_layer(self, client, monkeypatch):
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_CONFIG_API_ENABLED", True)
        resp = client.post("/api/intelligence/alerts/config", json={"key": "confidence_window", "value": 10, "reason": "x"})
        assert resp.status_code == 400

    def test_non_admin_gets_403(self, client, monkeypatch):
        monkeypatch.setattr(agents_config, "INTELLIGENCE_ALERT_CONFIG_API_ENABLED", True)
        now = dt.datetime.now().isoformat()
        conn = sqlite3.connect(app.DB_PATH)
        cur = conn.execute(
            "INSERT INTO users (email, username, password_hash, role, is_verified, created_at, updated_at) "
            "VALUES (?,?,?,?,1,?,?)",
            ("sub_thresh@example.com", "sub_thresh", "x", "subscriber", now, now),
        )
        conn.commit()
        user_id = cur.lastrowid
        conn.close()
        with client.session_transaction() as sess:
            sess["user_id"] = user_id
            sess["csrf_token"] = CSRF_TOKEN
        resp = _post(client, "/api/intelligence/alerts/config", key="confidence_window", value=10, reason="x")
        assert resp.status_code == 403

    def test_route_never_touches_scheduler_or_other_feature_flags(self):
        """Static source check: this route's CODE (not its docstring,
        which explains in prose what it deliberately does NOT do) must
        never actually reference the scheduler locks or the other
        off-by-default write-surface flags -- matching the explicit
        constraint this phase was scoped under."""
        import inspect
        source = inspect.getsource(app.api_intelligence_alerts_config)
        tree = ast.parse(source)
        func_body = tree.body[0].body
        if func_body and isinstance(func_body[0], ast.Expr) and isinstance(func_body[0].value, ast.Constant):
            func_body = func_body[1:]  # drop the docstring
        code_only = ast.unparse(ast.Module(body=func_body, type_ignores=[]))
        for forbidden in (
            "RUNTIME_SCHEDULER_ENABLED", "NEVER_SCHEDULABLE_AGENTS",
            "INTELLIGENCE_ALERTS_AUTO_ENABLED", "TI_RUN_CYCLE_API_ENABLED",
        ):
            assert forbidden not in code_only


# --- 17. Milestone 15, Phase 1: cooldown.py -- pure bucket/fingerprint logic -----

class TestCooldownLogic:
    @pytest.mark.parametrize("confidence,expected", [
        (0, "0-9"), (5, "0-9"), (59, "50-59"), (60, "60-69"), (69, "60-69"),
        (70, "70-79"), (79, "70-79"), (80, "80+"), (95, "80+"), (100, "80+"),
    ])
    def test_confidence_bucket_boundaries(self, confidence, expected):
        assert cooldown.confidence_bucket(confidence) == expected

    def test_bucket_rank_is_monotonically_increasing(self):
        ranks = [cooldown.bucket_rank(cooldown.confidence_bucket(c)) for c in (5, 15, 65, 75, 85)]
        assert ranks == sorted(ranks)
        assert len(set(ranks)) == len(ranks)

    def test_fingerprint_is_deterministic(self):
        f1 = cooldown.make_fingerprint(symbol="NIFTY", bias="BULLISH", confidence=65, rule="bias_flip")
        f2 = cooldown.make_fingerprint(symbol="NIFTY", bias="BULLISH", confidence=65, rule="bias_flip")
        assert f1 == f2

    def test_fingerprint_has_no_strike_parameter(self):
        """Explicit guarantee for the Phase 1 scoping decision: nothing
        in this codebase's alert data carries a strike, so
        make_fingerprint() never accepts or fabricates one."""
        import inspect
        params = inspect.signature(cooldown.make_fingerprint).parameters
        assert "strike" not in params


# --- 18. Milestone 15, Phase 1: dedup_store.py -- persisted suppression state ----

class TestDedupStore:
    def test_first_ever_alert_is_never_suppressed(self, alerts_db):
        assert dedup_store.should_suppress(
            symbol="NIFTY", bias="BULLISH", confidence=65, rule="bias_flip", cooldown_seconds=300,
        ) is False

    def test_identical_alert_suppressed_within_cooldown(self, alerts_db):
        dedup_store.should_suppress(symbol="NIFTY", bias="BULLISH", confidence=65, rule="bias_flip", cooldown_seconds=300)
        suppressed = dedup_store.should_suppress(
            symbol="NIFTY", bias="BULLISH", confidence=66, rule="bias_flip", cooldown_seconds=300,
        )
        assert suppressed is True  # 66 is still in the 60-69 bucket -- same condition

    def test_cooldown_expiry_allows_resend(self, alerts_db):
        old_now = dt.datetime.now() - dt.timedelta(seconds=10)
        dedup_store.should_suppress(
            symbol="NIFTY", bias="BULLISH", confidence=65, rule="bias_flip", cooldown_seconds=1, now=old_now,
        )
        suppressed = dedup_store.should_suppress(
            symbol="NIFTY", bias="BULLISH", confidence=65, rule="bias_flip", cooldown_seconds=1,
        )
        assert suppressed is False

    def test_confidence_bucket_upgrade_bypasses_suppression(self, alerts_db):
        dedup_store.should_suppress(symbol="NIFTY", bias="BULLISH", confidence=65, rule="bias_flip", cooldown_seconds=900)
        suppressed = dedup_store.should_suppress(
            symbol="NIFTY", bias="BULLISH", confidence=85, rule="bias_flip", cooldown_seconds=900,
        )
        assert suppressed is False  # 60-69 -> 80+ is a genuine escalation

    def test_confidence_bucket_downgrade_does_not_bypass_suppression(self, alerts_db):
        """The spec's bypass rule is specifically an INCREASE -- a drop
        in confidence for the same (symbol, bias, rule) is still the
        same-or-lesser condition and stays suppressed."""
        dedup_store.should_suppress(symbol="NIFTY", bias="BULLISH", confidence=85, rule="bias_flip", cooldown_seconds=900)
        suppressed = dedup_store.should_suppress(
            symbol="NIFTY", bias="BULLISH", confidence=65, rule="bias_flip", cooldown_seconds=900,
        )
        assert suppressed is True

    def test_bias_change_bypasses_suppression(self, alerts_db):
        dedup_store.should_suppress(symbol="NIFTY", bias="BULLISH", confidence=65, rule="bias_flip", cooldown_seconds=900)
        suppressed = dedup_store.should_suppress(
            symbol="NIFTY", bias="BEARISH", confidence=65, rule="bias_flip", cooldown_seconds=900,
        )
        assert suppressed is False

    def test_trigger_type_change_bypasses_suppression(self, alerts_db):
        dedup_store.should_suppress(symbol="NIFTY", bias="BULLISH", confidence=65, rule="bias_flip", cooldown_seconds=900)
        suppressed = dedup_store.should_suppress(
            symbol="NIFTY", bias="BULLISH", confidence=65, rule="oi_non_responsive", cooldown_seconds=900,
        )
        assert suppressed is False

    def test_different_symbol_is_an_independent_condition(self, alerts_db):
        dedup_store.should_suppress(symbol="NIFTY", bias="BULLISH", confidence=65, rule="bias_flip", cooldown_seconds=900)
        suppressed = dedup_store.should_suppress(
            symbol="BANKNIFTY", bias="BULLISH", confidence=65, rule="bias_flip", cooldown_seconds=900,
        )
        assert suppressed is False

    def test_persistence_survives_store_reload(self, alerts_db):
        """Milestone 15, Phase 1's own explicit requirement: state must
        survive a process restart. dedup_store.py never holds a
        long-lived connection or in-memory cache (every call opens and
        closes its own sqlite3 connection against a real file), so
        re-importing the module and reconnecting to the same DB_PATH is
        an honest simulation of a fresh process picking the state back
        up. importlib.reload() re-executes the module's top level,
        which resets DB_PATH to its hardcoded default -- alerts_db (the
        fixture's own db path string, not the module attribute) is the
        only value that still points at the real throwaway test DB
        after that reset."""
        dedup_store.should_suppress(symbol="NIFTY", bias="BULLISH", confidence=65, rule="bias_flip", cooldown_seconds=900)

        import importlib
        reloaded = importlib.reload(dedup_store)
        reloaded.DB_PATH = alerts_db

        suppressed = reloaded.should_suppress(
            symbol="NIFTY", bias="BULLISH", confidence=66, rule="bias_flip", cooldown_seconds=900,
        )
        assert suppressed is True  # the "restarted" module still sees the pre-restart state

    def test_alert_sent_is_logged_with_fingerprint(self, alerts_db, caplog):
        with caplog.at_level("INFO", logger="intelligence_alerts.dedup"):
            dedup_store.should_suppress(symbol="NIFTY", bias="BULLISH", confidence=65, rule="bias_flip", cooldown_seconds=300)
        assert "ALERT_SENT" in caplog.text
        assert "NIFTY|BULLISH|60-69|bias_flip" in caplog.text

    def test_alert_suppressed_duplicate_is_logged_with_fingerprint_and_remaining_cooldown(self, alerts_db, caplog):
        dedup_store.should_suppress(symbol="NIFTY", bias="BULLISH", confidence=65, rule="bias_flip", cooldown_seconds=300)
        with caplog.at_level("INFO", logger="intelligence_alerts.dedup"):
            dedup_store.should_suppress(symbol="NIFTY", bias="BULLISH", confidence=65, rule="bias_flip", cooldown_seconds=300)
        assert "ALERT_SUPPRESSED_DUPLICATE" in caplog.text
        assert "NIFTY|BULLISH|60-69|bias_flip" in caplog.text
        assert "remaining_cooldown=" in caplog.text

    def test_init_db_is_idempotent(self, alerts_db):
        dedup_store.init_db()
        dedup_store.init_db()  # must not raise


# --- 19. Milestone 15, Phase 1: config defaults + scope guarantees -------------

class TestDedupConfigAndScope:
    def test_default_dedup_cooldown_is_300_seconds(self):
        assert agents_config.INTELLIGENCE_ALERT_DEDUP_COOLDOWN_SECONDS == 300

    def test_dedup_cooldown_shows_up_in_get_rules(self, alerts_db):
        rules_dump = alerts_api.get_rules()
        assert rules_dump["dedup_cooldown_seconds"] == 300

    def test_dedup_cooldown_is_overridable_via_existing_config_api(self, alerts_db):
        result = alerts_api.set_threshold("dedup_cooldown_seconds", 120, updated_by="tester", reason="tighten")
        assert result["dedup_cooldown_seconds"] == 120

    def test_dedup_modules_never_touch_trading_logic(self):
        """Same static guarantee as check_bias_flip's own -- Phase 1 is
        alert-layer only."""
        for path in ("agents/intelligence_alerts/cooldown.py", "agents/intelligence_alerts/dedup_store.py"):
            source = Path(path).read_text()
            for forbidden in ("detect_bias", "classify_buildup", "generate_signal", "oi_engine", "intelligence_orchestrator"):
                assert forbidden not in source

    def test_dedup_modules_never_import_app(self):
        for path in ("agents/intelligence_alerts/cooldown.py", "agents/intelligence_alerts/dedup_store.py"):
            source = Path(path).read_text()
            tree = ast.parse(source, filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    assert not any(alias.name == "app" for alias in node.names)
                if isinstance(node, ast.ImportFrom):
                    assert node.module != "app"

    def test_app_delivery_layer_wiring_never_touches_scheduler_or_other_flags(self):
        """Static source check on the one authorized app.py integration
        point (_run_intelligence_alerts_auto_cycle) -- must only ever
        reference the dedup/threshold machinery, never the scheduler
        locks or the other off-by-default write-surface flags."""
        import inspect
        source = inspect.getsource(app._run_intelligence_alerts_auto_cycle)
        for forbidden in (
            "RUNTIME_SCHEDULER_ENABLED", "NEVER_SCHEDULABLE_AGENTS", "TI_RUN_CYCLE_API_ENABLED",
        ):
            assert forbidden not in source
