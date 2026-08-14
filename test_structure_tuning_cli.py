"""
test_structure_tuning_cli.py -- regression tests for
structure_tuning_cli.py (Milestone 20, Phase 7). Repo-root location
matches every other *_cli.py's own test file convention.
"""
import argparse
import datetime as dt
import types

import pandas as pd
import pytest

import institutional_levels as il
import structure_tuning_cli
from agents import config as agents_config
from agents.trading_intelligence import structure_backtest, structure_tuning


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(structure_tuning, "DB_PATH", str(tmp_path / "tuning_test.db"))
    structure_tuning.init_db()
    monkeypatch.setattr(il, "MAX_RETEST_CANDLES", 3)
    monkeypatch.setattr(il, "MIN_VOLUME_MULTIPLIER", 1.2)
    monkeypatch.setattr(agents_config, "TI_WATCHED_SYMBOLS", ["NIFTY"])


def _args(**kwargs):
    defaults = {"dry_run": False, "symbol": "NIFTY", "parameter": None, "limit": 20}
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _mock_backtest(monkeypatch, win_rate_by_params: dict, *, sample_size=50):
    def fake_backtest_parameters(symbol, candles, *, max_retest_candles, min_volume_multiplier):
        key = (max_retest_candles, min_volume_multiplier)
        rate = win_rate_by_params.get(key)
        if rate is None:
            return types.SimpleNamespace(wins=0, losses=0)
        wins = round(sample_size * rate)
        return types.SimpleNamespace(wins=wins, losses=sample_size - wins)

    monkeypatch.setattr(structure_backtest, "backtest_parameters", fake_backtest_parameters)
    monkeypatch.setattr(structure_backtest.data_access, "load_candles",
                         lambda sym, **kw: pd.DataFrame([{"datetime": dt.datetime.now()}]))


class TestCmdRun:
    def test_dry_run_never_mutates_live_constants(self, monkeypatch, capsys):
        _mock_backtest(monkeypatch, {
            (2, 1.2): 0.40, (3, 1.2): 0.45, (4, 1.2): 0.70, (5, 1.2): 0.42,
            (3, 1.0): 0.44, (3, 1.5): 0.43, (3, 1.8): 0.41, (3, 2.0): 0.40,
        })
        before = il.MAX_RETEST_CANDLES
        structure_tuning_cli._cmd_run(_args(dry_run=True))
        assert il.MAX_RETEST_CANDLES == before

    def test_real_run_applies_when_a_candidate_clearly_wins(self, monkeypatch, capsys):
        _mock_backtest(monkeypatch, {
            (2, 1.2): 0.40, (3, 1.2): 0.45, (4, 1.2): 0.70, (5, 1.2): 0.42,
            (3, 1.0): 0.44, (3, 1.5): 0.43, (3, 1.8): 0.41, (3, 2.0): 0.40,
        })
        structure_tuning_cli._cmd_run(_args(dry_run=False))
        assert il.MAX_RETEST_CANDLES == 4

    def test_run_prints_json_output(self, monkeypatch, capsys):
        _mock_backtest(monkeypatch, {
            (2, 1.2): 0.40, (3, 1.2): 0.45, (4, 1.2): 0.47, (5, 1.2): 0.42,
            (3, 1.0): 0.44, (3, 1.5): 0.43, (3, 1.8): 0.41, (3, 2.0): 0.40,
        })
        structure_tuning_cli._cmd_run(_args(dry_run=True))
        out = capsys.readouterr().out
        assert "decisions" in out
        assert "max_retest_candles" in out


class TestCmdBacktest:
    def test_prints_the_full_grid(self, monkeypatch, capsys):
        candles = [{"datetime": dt.datetime(2026, 1, 1) + dt.timedelta(minutes=3 * i),
                    "open": 100, "high": 100.1, "low": 99.9, "close": 100, "volume": 500} for i in range(200)]
        monkeypatch.setattr(structure_backtest.data_access, "load_candles",
                             lambda sym, **kw: pd.DataFrame(candles))
        structure_tuning_cli._cmd_backtest(_args(symbol="NIFTY"))
        out = capsys.readouterr().out
        assert "NIFTY" in out
        assert "max_retest_candles=" in out


class TestCmdHistory:
    def test_no_history_prints_a_clear_message(self, capsys):
        structure_tuning_cli._cmd_history(_args())
        out = capsys.readouterr().out
        assert "No tuning evaluations logged yet." in out

    def test_prints_current_live_values(self, capsys):
        structure_tuning_cli._cmd_history(_args())
        out = capsys.readouterr().out
        assert "max_retest_candles = 3" in out
        assert "min_volume_multiplier = 1.2" in out

    def test_prints_logged_decisions_after_a_run(self, monkeypatch, capsys):
        _mock_backtest(monkeypatch, {
            (2, 1.2): 0.40, (3, 1.2): 0.45, (4, 1.2): 0.47, (5, 1.2): 0.42,
            (3, 1.0): 0.44, (3, 1.5): 0.43, (3, 1.8): 0.41, (3, 2.0): 0.40,
        })
        structure_tuning_cli._cmd_run(_args(dry_run=True))
        capsys.readouterr()
        structure_tuning_cli._cmd_history(_args())
        out = capsys.readouterr().out
        assert "max_retest_candles" in out
        assert "min_volume_multiplier" in out
