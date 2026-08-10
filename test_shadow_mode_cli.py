"""
test_shadow_mode_cli.py -- Milestone 12, Phase 2B (Weekend Completion
Sprint): regression tests for shadow_mode_cli.py's --dry-run and
--export-json modes, plus failure handling.

NOTE on file location: the sprint brief requested "tests/test_shadow_
mode_cli.py". No tests/ directory exists anywhere in this repository --
every one of its 20+ existing test files (test_auth.py, test_runtime_
control_routes.py, test_shadow_mode_read_only.py, ...) lives at the
repo root as test_*.py, matching pytest's default rootdir discovery.
This file follows that established, exclusive convention instead of
introducing a new, inconsistent directory layout; see the weekend
completion report for the explicit callout.

Builds cycles/strikes directly (never by importing app.py -- the same
"no ~7000-line Flask app with real broker-session machinery in a test
process" rule test_agents/trading_intelligence/conftest.py's own module
docstring already documents) so agents.trading_intelligence.market_data.
get_snapshot() has real data to compute a signal from.
"""
import argparse
import datetime as dt
import json
import os
import sqlite3

import pytest

from agents import audit_log, event_bus
from agents.runtime import scheduling_control as sc
from agents.shadow_mode import evaluator, observer, store as shadow_store
from agents.sys_admin import sysadmin_store
from agents.trading_intelligence import data_access, ti_store
from agents import config as agents_config

import shadow_mode_cli


@pytest.fixture()
def cli_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "cli_test.db")
    monkeypatch.setattr(data_access, "DB_PATH", db_path)
    monkeypatch.setattr(ti_store, "DB_PATH", db_path)
    monkeypatch.setattr(audit_log, "DB_PATH", db_path)
    monkeypatch.setattr(event_bus, "DB_PATH", db_path)
    monkeypatch.setattr(sysadmin_store, "DB_PATH", db_path)
    monkeypatch.setattr(shadow_store, "DB_PATH", db_path)

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, ts TEXT, date TEXT, time TEXT,
            underlying_ltp REAL, atm REAL, pcr REAL, max_pain REAL, bias TEXT, note TEXT
        );
        CREATE TABLE strikes (
            cycle_id INTEGER, strike REAL,
            ce_oi INTEGER, ce_oi_chg INTEGER, ce_vol INTEGER, ce_ltp REAL, ce_chg_pct REAL, ce_signal TEXT, ce_iv REAL,
            ce_delta REAL, ce_gamma REAL, ce_theta REAL, ce_vega REAL,
            pe_oi INTEGER, pe_oi_chg INTEGER, pe_vol INTEGER, pe_ltp REAL, pe_chg_pct REAL, pe_signal TEXT, pe_iv REAL,
            pe_delta REAL, pe_gamma REAL, pe_theta REAL, pe_vega REAL
        );
        CREATE TABLE market_structure_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, date TEXT, time TEXT, ts TEXT,
            atr_14 REAL, adx REAL, regime TEXT, pdh REAL, pdl REAL, pdc REAL, vwap REAL,
            swing_high REAL, swing_low REAL,
            mother_candle_json TEXT, liquidity_sweep_json TEXT, custom_levels_json TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    audit_log.init_db()
    event_bus.init_db()
    sysadmin_store.init_db()
    ti_store.init_db()
    shadow_store.init_db()
    return db_path


def _insert_realistic_chain(db_path, *, symbol="NIFTY", underlying_ltp=25000.0, atm=25000.0,
                             pcr=0.9, step=50, strikes_each_side=4):
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO cycles (symbol, ts, date, time, underlying_ltp, atm, pcr, max_pain, bias) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (symbol, dt.datetime.now().isoformat(), "2026-08-08", "10:00:00", underlying_ltp, atm, pcr, atm, None),
    )
    cycle_id = cur.lastrowid
    for i in range(-strikes_each_side, strikes_each_side + 1):
        strike = atm + i * step
        conn.execute(
            "INSERT INTO strikes (cycle_id, strike, ce_oi, ce_oi_chg, ce_vol, ce_ltp, ce_chg_pct, ce_signal, "
            "pe_oi, pe_oi_chg, pe_vol, pe_ltp, pe_chg_pct, pe_signal) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cycle_id, strike, 50000, 500, 15000, 100.0, 1.0, "Neutral", 60000, 500, 8000, 80.0, 0.5, "Neutral"),
        )
    conn.commit()
    conn.close()


def _args(**kwargs):
    defaults = {"symbol": "NIFTY", "timeframe": "3m", "dry_run": False, "export_json": None, "limit": 100}
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# --- Dry-run mode: zero DB writes, banner printed ---------------------------

class TestObserveDryRun:
    def test_dry_run_performs_no_insert(self, cli_db, capsys):
        _insert_realistic_chain(cli_db)
        before_obs, before_pred = shadow_store.count_observations(), shadow_store.count_predictions()

        shadow_mode_cli._cmd_observe(_args(dry_run=True))

        after_obs, after_pred = shadow_store.count_observations(), shadow_store.count_predictions()
        assert before_obs == after_obs == 0
        assert before_pred == after_pred == 0

    def test_dry_run_prints_banner(self, cli_db, capsys):
        _insert_realistic_chain(cli_db)
        shadow_mode_cli._cmd_observe(_args(dry_run=True))
        out = capsys.readouterr().out
        assert shadow_mode_cli.DRY_RUN_BANNER in out

    def test_dry_run_prints_observation_and_prediction_payloads(self, cli_db, capsys):
        _insert_realistic_chain(cli_db)
        shadow_mode_cli._cmd_observe(_args(dry_run=True))
        out = capsys.readouterr().out
        assert "observation:" in out
        assert "prediction:" in out
        assert "signal_type=" in out

    def test_real_run_still_writes_when_dry_run_is_false(self, cli_db, capsys):
        """Control case: proves the dry-run assertions above are
        actually meaningful (i.e. this fixture/chain DOES produce a
        real write when --dry-run is NOT passed) rather than trivially
        passing because nothing would have been written either way."""
        _insert_realistic_chain(cli_db)
        shadow_mode_cli._cmd_observe(_args(dry_run=False))
        assert shadow_store.count_observations() == 1
        assert shadow_store.count_predictions() == 1


class TestEvaluateDryRun:
    def test_dry_run_performs_no_outcome_write(self, cli_db, capsys):
        obs_id = shadow_store.record_observation(ts=dt.datetime.now().isoformat(), symbol="NIFTY", timeframe="3m")
        shadow_store.record_prediction(
            observation_id=obs_id, ts=dt.datetime.now().isoformat(), symbol="NIFTY", timeframe="3m",
            signal_type="BUY CE", expected_direction="CE", confidence=70,
            entry_reference_price=25000, expected_target_low=25000, expected_target_high=25100,
            valid_until_ts=(dt.datetime.now() + dt.timedelta(minutes=45)).isoformat(),
        )
        before = shadow_store.count_predictions()  # sanity: prediction exists
        assert before == 1

        shadow_mode_cli._cmd_evaluate(_args(dry_run=True))

        conn = sqlite3.connect(cli_db)
        outcome_count = conn.execute("SELECT COUNT(*) FROM shadow_outcomes").fetchone()[0]
        conn.close()
        assert outcome_count == 0

    def test_dry_run_prints_banner(self, cli_db, capsys):
        shadow_mode_cli._cmd_evaluate(_args(dry_run=True))
        out = capsys.readouterr().out
        assert shadow_mode_cli.DRY_RUN_BANNER in out

    def test_dry_run_with_no_pending_predictions_prints_zero(self, cli_db, capsys):
        shadow_mode_cli._cmd_evaluate(_args(dry_run=True))
        out = capsys.readouterr().out
        assert "0 pending prediction(s)" in out


# --- Export mode -------------------------------------------------------------

class TestExportJson:
    def test_export_creates_file(self, cli_db, tmp_path):
        _insert_realistic_chain(cli_db)
        export_path = str(tmp_path / "out.json")
        shadow_mode_cli._cmd_observe(_args(dry_run=True, export_json=export_path))
        assert os.path.exists(export_path)

    def test_export_schema_has_required_fields(self, cli_db, tmp_path):
        _insert_realistic_chain(cli_db)
        export_path = str(tmp_path / "out.json")
        shadow_mode_cli._cmd_observe(_args(dry_run=True, export_json=export_path))
        with open(export_path) as f:
            payload = json.load(f)
        for key in ("timestamp", "symbol", "timeframe", "signal_inputs", "generated_signal",
                     "confidence", "target_price", "sl_price", "metadata"):
            assert key in payload, f"missing key: {key}"
        assert payload["symbol"] == "NIFTY"

    def test_export_metadata_marks_dry_run_true(self, cli_db, tmp_path):
        _insert_realistic_chain(cli_db)
        export_path = str(tmp_path / "out.json")
        shadow_mode_cli._cmd_observe(_args(dry_run=True, export_json=export_path))
        with open(export_path) as f:
            payload = json.load(f)
        assert payload["metadata"]["dry_run"] is True

    def test_export_never_touches_the_database(self, cli_db, tmp_path):
        _insert_realistic_chain(cli_db)
        export_path = str(tmp_path / "out.json")
        shadow_mode_cli._cmd_observe(_args(dry_run=True, export_json=export_path))
        assert shadow_store.count_observations() == 0
        assert shadow_store.count_predictions() == 0

    def test_export_without_dry_run_is_refused(self, cli_db, tmp_path):
        export_path = str(tmp_path / "out.json")
        with pytest.raises(SystemExit) as exc_info:
            shadow_mode_cli._cmd_observe(_args(dry_run=False, export_json=export_path))
        assert exc_info.value.code == 2
        assert not os.path.exists(export_path)


# --- Failure handling ---------------------------------------------------------

class TestFailureHandling:
    def test_invalid_symbol_degrades_gracefully(self, cli_db, capsys):
        """No cycle has ever been logged for this made-up symbol --
        must print an honest "no snapshot" message, never raise."""
        shadow_mode_cli._cmd_observe(_args(symbol="NOT_A_REAL_SYMBOL_XYZ", dry_run=False))
        out = capsys.readouterr().out
        assert "no market snapshot available yet" in out
        assert shadow_store.count_observations() == 0

    def test_invalid_symbol_dry_run_degrades_gracefully(self, cli_db, capsys):
        shadow_mode_cli._cmd_observe(_args(symbol="NOT_A_REAL_SYMBOL_XYZ", dry_run=True))
        out = capsys.readouterr().out
        assert "no market snapshot available yet" in out
        assert shadow_mode_cli.DRY_RUN_BANNER in out

    def test_missing_market_data_returns_none_from_compute(self, cli_db):
        """Direct check on the underlying pure function: zero cycles
        logged anywhere in this DB -> None, not an exception."""
        result = observer.compute_observation_and_prediction("ANYTHING")
        assert result is None

    def test_unwritable_export_path_exits_cleanly(self, cli_db, capsys):
        _insert_realistic_chain(cli_db)
        unwritable_path = "/nonexistent_directory_xyz/out.json"
        with pytest.raises(SystemExit) as exc_info:
            shadow_mode_cli._cmd_observe(_args(dry_run=True, export_json=unwritable_path))
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "could not write export file" in err

    def test_unwritable_export_path_performs_no_db_write_either(self, cli_db):
        _insert_realistic_chain(cli_db)
        unwritable_path = "/nonexistent_directory_xyz/out.json"
        with pytest.raises(SystemExit):
            shadow_mode_cli._cmd_observe(_args(dry_run=True, export_json=unwritable_path))
        assert shadow_store.count_observations() == 0


# --- Safety re-confirmation (this sprint touched no scheduler/config file) --

class TestSafetyUnaffectedByCliChanges:
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
