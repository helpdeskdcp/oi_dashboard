import pytest

from agents.runtime import policy_engine as pe


class TestGetActivePolicy:
    def test_defaults_to_config_when_no_override(self, agent_db):
        assert pe.get_active_policy() == "recommendation_only"

    def test_override_wins_over_default(self, agent_db):
        pe.set_policy("full_auto", changed_by="tester", reason="x")
        assert pe.get_active_policy() == "full_auto"


class TestSetPolicy:
    def test_rejects_unknown_policy(self, agent_db):
        with pytest.raises(ValueError):
            pe.set_policy("not_a_real_policy", changed_by="t", reason="x")

    def test_records_an_explainable_report(self, agent_db):
        report = pe.set_policy("paper_trading", changed_by="tester", reason="testing paper mode")
        assert report.module == "policy_engine"
        assert "paper_trading" in report.reason
        assert report.evidence["changed_by"] == "tester"

    def test_emergency_stop_is_severity_warning(self, agent_db):
        report = pe.set_policy("emergency_stop", changed_by="tester", reason="halt")
        assert report.severity == "warning"


class TestRules:
    @pytest.mark.parametrize("policy,advances,reaches_exec,auto", [
        ("read_only", False, False, False),
        ("recommendation_only", True, False, False),
        ("simulation", True, True, False),
        ("paper_trading", True, True, False),
        ("semi_auto", True, True, False),
        ("full_auto", True, True, True),
        ("emergency_stop", False, False, False),
    ])
    def test_every_policy_has_the_documented_rules(self, agent_db, policy, advances, reaches_exec, auto):
        pe.set_policy(policy, changed_by="t", reason="x")
        rules = pe.rules_for()
        assert rules["advances_workflow"] == advances
        assert rules["reaches_execution"] == reaches_exec
        assert rules["auto_approve"] == auto

    def test_rules_for_explicit_policy_ignores_active_override(self, agent_db):
        pe.set_policy("full_auto", changed_by="t", reason="x")
        assert pe.rules_for("read_only")["advances_workflow"] is False

    def test_rules_for_unknown_policy_raises(self, agent_db):
        with pytest.raises(ValueError):
            pe.rules_for("not_a_real_policy")

    def test_is_emergency_stop_reflects_active_policy(self, agent_db):
        assert pe.is_emergency_stop() is False
        pe.set_policy("emergency_stop", changed_by="t", reason="x")
        assert pe.is_emergency_stop() is True

    def test_all_policies_tuple_matches_documented_seven(self):
        assert set(pe.ALL_POLICIES) == {
            "read_only", "recommendation_only", "simulation", "paper_trading",
            "semi_auto", "full_auto", "emergency_stop",
        }
