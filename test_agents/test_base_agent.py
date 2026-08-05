"""
test_agents/test_base_agent.py -- regression tests for
agents/base_agent.py (RiskTier, Finding, ProposedAction, BaseAgent).
"""
import pytest

from agents import audit_log
from agents.base_agent import BaseAgent, Finding, ProposedAction, RiskTier


class TestRiskTier:
    def test_values_are_plain_strings_for_json_sqlite_round_tripping(self):
        assert RiskTier.READ_ONLY.value == "read_only"
        assert RiskTier.NEEDS_APPROVAL.value == "needs_approval"
        assert RiskTier.HARD_BLOCKED.value == "hard_blocked"
        assert isinstance(RiskTier.READ_ONLY, str)


class TestFinding:
    def test_valid_finding_constructs(self):
        f = Finding(severity="warning", summary="disk low", evidence={"free_gb": 2})
        assert f.proposed_action is None

    def test_invalid_severity_rejected(self):
        with pytest.raises(ValueError):
            Finding(severity="urgent", summary="x", evidence={"k": "v"})

    def test_empty_evidence_rejected(self):
        with pytest.raises(ValueError):
            Finding(severity="info", summary="x", evidence={})

    def test_none_evidence_rejected(self):
        with pytest.raises(ValueError):
            Finding(severity="info", summary="x", evidence=None)


class TestProposedAction:
    def test_only_risk_tier_and_description_required(self):
        a = ProposedAction(risk_tier=RiskTier.READ_ONLY, description="a health check ran")
        assert a.diff is None
        assert a.test_results is None
        assert a.backtest_comparison is None


class _ToyAgent(BaseAgent):
    name = "toy"


class TestBaseAgentPropose:
    def test_needs_approval_action_writes_pending_approval(self, agent_db):
        agent = _ToyAgent()
        action = ProposedAction(risk_tier=RiskTier.NEEDS_APPROVAL, description="fix a bug",
                                 diff="--- a\n+++ b\n", test_results={"passed": 10, "failed": 0})
        row_id = agent.propose(action)
        row = audit_log.get(row_id)
        assert row["outcome"] == "pending_approval"
        assert row["risk_tier"] == "needs_approval"
        assert row["agent"] == "toy"
        assert row["payload_json"]["diff"] == "--- a\n+++ b\n"
        assert row["payload_json"]["test_results"] == {"passed": 10, "failed": 0}

    def test_hard_blocked_action_writes_rejected_not_pending(self, agent_db):
        agent = _ToyAgent()
        action = ProposedAction(risk_tier=RiskTier.HARD_BLOCKED, description="tried to edit agents/base_agent.py")
        row_id = agent.propose(action)
        row = audit_log.get(row_id)
        assert row["outcome"] == "rejected"
        assert row["risk_tier"] == "hard_blocked"
        assert "hard_blocked" in row["payload_json"]["reason"]

    def test_read_only_action_still_goes_through_propose_as_pending(self, agent_db):
        # propose() itself doesn't special-case READ_ONLY -- run_cycle()
        # implementations decide whether to call propose() at all for a
        # read-only Finding (most won't); propose() only special-cases
        # HARD_BLOCKED, since that's the one tier with nothing to approve.
        agent = _ToyAgent()
        action = ProposedAction(risk_tier=RiskTier.READ_ONLY, description="just a log")
        row_id = agent.propose(action)
        assert audit_log.get(row_id)["outcome"] == "pending_approval"

    def test_agent_name_override(self, agent_db):
        agent = _ToyAgent()
        action = ProposedAction(risk_tier=RiskTier.NEEDS_APPROVAL, description="x")
        row_id = agent.propose(action, agent_name="explicit-override")
        assert audit_log.get(row_id)["agent"] == "explicit-override"

    def test_run_cycle_not_implemented_by_default(self, agent_db):
        class Bare(BaseAgent):
            name = "bare"

        with pytest.raises(NotImplementedError):
            Bare().run_cycle()

    def test_on_event_default_is_a_harmless_noop(self, agent_db):
        agent = _ToyAgent()
        agent.on_event({"event_type": "anything"})   # must not raise

    def test_has_no_apply_or_execute_method(self):
        # The safety model depends on this being true: there is no method
        # on BaseAgent (or anywhere in this module) that applies, merges,
        # or executes a proposed change.
        forbidden = {"apply", "execute", "merge", "run"}
        assert not (forbidden & set(dir(BaseAgent)))
