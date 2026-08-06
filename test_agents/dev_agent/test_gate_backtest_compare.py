"""
test_agents/dev_agent/test_gate_backtest_compare.py -- Gate 3. The actual
backtest replay is mocked at the subprocess boundary (synthetic
baseline/candidate stats JSON) -- per the plan, gates are "independently
unit-tested against synthetic pass/fail fixtures, no LLM/real-backtest
call needed" for Milestone 2. touches_strategy_file() itself IS exercised
directly against the real config.DETECTION_PRIORITY[1] list.
"""
import json

from agents.dev_agent.gates import backtest_compare as bc
from agents.dev_agent.gates.base import GateStatus


class _FakeCompleted:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _payload(net_pnl, max_drawdown=10.0, total_trades=5):
    stats = {
        "net_pnl": net_pnl, "profit_factor": 2.0, "win_rate": 55.0,
        "max_drawdown": max_drawdown, "expectancy": net_pnl / total_trades,
        "sharpe_ratio": 0.5, "total_trades": total_trades,
    }
    points = [net_pnl / total_trades] * total_trades
    return json.dumps({"stats": stats, "points": points, "cycle_count": 100})


class TestTouchesStrategyFile:
    def test_true_for_a_known_priority_1_file(self):
        assert bc.touches_strategy_file(["exit_engine_v4.py"])

    def test_false_when_only_non_strategy_files_changed(self):
        assert not bc.touches_strategy_file(["README.md", "templates/index.html"])

    def test_false_for_empty_changeset(self):
        assert not bc.touches_strategy_file([])


class TestBacktestCompareGate:
    def test_skipped_when_no_strategy_file_touched(self):
        result = bc.run("/baseline", "/candidate", ["README.md"])
        assert result.status == GateStatus.SKIPPED

    def test_passes_when_candidate_matches_baseline(self, monkeypatch):
        payload = _payload(net_pnl=100.0)
        monkeypatch.setattr(bc.subprocess, "run", lambda *a, **k: _FakeCompleted(0, stdout=payload + "\n"))
        result = bc.run("/baseline", "/candidate", ["backtest.py"])
        assert result.status == GateStatus.PASSED
        assert result.details["comparison"]["regressions"] == []

    def test_fails_when_candidate_regresses(self, monkeypatch):
        calls = {"n": 0}

        def fake_run(*args, **kwargs):
            calls["n"] += 1
            net_pnl = 100.0 if calls["n"] == 1 else 50.0  # baseline first, candidate worse
            return _FakeCompleted(0, stdout=_payload(net_pnl=net_pnl) + "\n")

        monkeypatch.setattr(bc.subprocess, "run", fake_run)
        result = bc.run("/baseline", "/candidate", ["backtest.py"])
        assert result.status == GateStatus.FAILED
        assert len(result.details["comparison"]["regressions"]) > 0

    def test_scenario_failure_is_reported_as_failed_not_raised(self, monkeypatch):
        monkeypatch.setattr(bc.subprocess, "run", lambda *a, **k: _FakeCompleted(1, stderr="no data"))
        result = bc.run("/baseline", "/candidate", ["backtest.py"])
        assert result.status == GateStatus.FAILED
        assert "could not be run" in result.summary
