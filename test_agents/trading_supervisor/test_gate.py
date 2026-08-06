"""
test_agents/trading_supervisor/test_gate.py -- regression tests for
agents/trading_supervisor/gate.py's SupervisionVerdict -> GateResult
mapping.
"""
from agents.dev_agent.gates.base import GateResult, GateStatus
from agents.memory.sqlite_store import SQLiteMemoryStore
from agents.trading_supervisor import gate, supervision_engine


def _passing_risk_gate():
    return [GateResult(gate="risk_assessment", status=GateStatus.PASSED, summary="ok", details={})]


class TestRun:
    def test_approved_verdict_passes_the_gate(self, tmp_path, monkeypatch):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
        approved = supervision_engine.SupervisionVerdict(
            decision="APPROVED", explanation="all clear", risk_gate_status="passed",
            market_state={}, conflicts=[], data_health={},
        )
        monkeypatch.setattr(gate.supervision_engine, "verify", lambda **k: approved)

        result, verdict, report = gate.run(
            candidate_name="c", symbol="NIFTY", direction="long", strategy_family="f",
            gate_results=_passing_risk_gate(), memory_store=store,
        )
        assert result.status == GateStatus.PASSED
        assert result.gate == "trading_supervision"
        assert result.details["decision"] == "APPROVED"
        assert verdict.decision == "APPROVED"
        assert report.decision == "APPROVED"

    def test_rejected_verdict_fails_the_gate(self, tmp_path, monkeypatch):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
        rejected = supervision_engine.SupervisionVerdict(
            decision="REJECTED", explanation="risk gate missing", risk_gate_status="missing",
            market_state={}, conflicts=[], data_health={},
        )
        monkeypatch.setattr(gate.supervision_engine, "verify", lambda **k: rejected)

        result, verdict, report = gate.run(
            candidate_name="c", symbol="NIFTY", direction="long", strategy_family="f",
            gate_results=[], memory_store=store,
        )
        assert result.status == GateStatus.FAILED
        assert "risk gate missing" in result.summary

    def test_requires_review_still_passes_the_gate_but_says_so_in_details(self, tmp_path, monkeypatch):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
        review = supervision_engine.SupervisionVerdict(
            decision="REQUIRES_REVIEW", explanation="conflict detected", risk_gate_status="passed",
            market_state={}, conflicts=[{"strategy_b": "x"}], data_health={},
        )
        monkeypatch.setattr(gate.supervision_engine, "verify", lambda **k: review)

        result, verdict, report = gate.run(
            candidate_name="c", symbol="NIFTY", direction="long", strategy_family="f",
            gate_results=_passing_risk_gate(), memory_store=store,
        )
        assert result.status == GateStatus.PASSED
        assert result.details["decision"] == "REQUIRES_REVIEW"
