"""
test_agents/risk_manager/test_gate.py -- regression tests for
agents/risk_manager/gate.py's RiskAssessment -> GateResult mapping.
"""
import datetime as dt

from agents.dev_agent.gates.base import GateStatus
from agents.memory.sqlite_store import SQLiteMemoryStore
from agents.risk_manager import gate


def _trades(n=40, points=10.0):
    return [
        {"points": points, "exit_time": dt.datetime(2026, 5, 4) + dt.timedelta(days=i)}
        for i in range(n)
    ]


class TestRun:
    def test_clean_candidate_passes_the_gate(self, tmp_path):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "mem.db"))
        result, assessment, report = gate.run(
            candidate_name="c", symbol="NIFTY", strategy_family="oi_delta_combo",
            stop_points=15.0, trades=_trades(), candidate_thresholds={"oi_delta_bias": 0.0},
            memory_store=store, capital=1_000_000,
        )
        assert result.status == GateStatus.PASSED
        assert result.gate == "risk_assessment"
        assert result.details["decision"] == "APPROVED"
        assert assessment.decision == "APPROVED"
        assert report.report_type == "promotion"

    def test_rejected_assessment_fails_the_gate(self, tmp_path, monkeypatch):
        from agents.risk_manager import risk_engine
        store = SQLiteMemoryStore(db_path=str(tmp_path / "mem.db"))
        rejected = risk_engine.RiskAssessment(
            risk_score=10, decision="REJECTED", checks=[], var=0.0, cvar=0.0,
            drawdown_simulation={}, stress_test={}, correlations={}, explanation="too risky",
        )
        monkeypatch.setattr(gate.risk_intelligence, "assess", lambda *a, **k: rejected)

        result, assessment, report = gate.run(
            candidate_name="c", symbol="NIFTY", strategy_family="f", stop_points=15.0,
            trades=_trades(), candidate_thresholds={}, memory_store=store,
        )
        assert result.status == GateStatus.FAILED
        assert "too risky" in result.summary

    def test_requires_review_still_passes_the_gate_but_says_so_in_details(self, tmp_path, monkeypatch):
        from agents.risk_manager import risk_engine
        store = SQLiteMemoryStore(db_path=str(tmp_path / "mem.db"))
        review = risk_engine.RiskAssessment(
            risk_score=55, decision="REQUIRES_REVIEW", checks=[], var=0.0, cvar=0.0,
            drawdown_simulation={}, stress_test={}, correlations={}, explanation="borderline",
        )
        monkeypatch.setattr(gate.risk_intelligence, "assess", lambda *a, **k: review)

        result, assessment, report = gate.run(
            candidate_name="c", symbol="NIFTY", strategy_family="f", stop_points=15.0,
            trades=_trades(), candidate_thresholds={}, memory_store=store,
        )
        assert result.status == GateStatus.PASSED
        assert result.details["decision"] == "REQUIRES_REVIEW"
