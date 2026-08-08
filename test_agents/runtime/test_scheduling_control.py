"""
test_agents/runtime/test_scheduling_control.py -- Milestone 12, Phase 2
Foundation: agents.runtime.scheduling_control.

Reuses agent_db (test_agents/conftest.py -- a real throwaway SQLite file
with sysadmin_store's tables initialized) so set_mode()/get_mode()
round-trips exercise the real upsert_agent_status() path, not a mock --
"persists across process restarts" is proven here by reading the mode
back via a completely fresh module-level call against the same on-disk
DB file, which is the only thing a real restart changes (the module
itself holds no in-memory state to lose).
"""
import pytest

from agents.runtime import runtime_events, scheduling_control as sc
from agents.sys_admin import sysadmin_store


class TestNeverSchedulableAgents:
    def test_trading_intelligence_is_never_schedulable(self, agent_db):
        assert sc.is_schedulable("trading_intelligence") is False
        assert "trading_intelligence" not in sc.SCHEDULABLE_AGENTS
        assert "trading_intelligence" in sc.NEVER_SCHEDULABLE_AGENTS

    def test_quant_researcher_is_never_schedulable(self, agent_db):
        assert sc.is_schedulable("quant_researcher") is False
        assert "quant_researcher" not in sc.SCHEDULABLE_AGENTS
        assert "quant_researcher" in sc.NEVER_SCHEDULABLE_AGENTS

    def test_get_mode_is_always_disabled_for_a_never_schedulable_agent(self, agent_db):
        assert sc.get_mode("trading_intelligence") == sc.DISABLED
        assert sc.get_mode("quant_researcher") == sc.DISABLED

    @pytest.mark.parametrize("mode", [sc.ENABLED, sc.DISABLED, sc.DRY_RUN])
    def test_set_mode_refuses_trading_intelligence_under_any_mode(self, agent_db, mode):
        with pytest.raises(ValueError, match="trading_intelligence"):
            sc.set_mode("trading_intelligence", mode, changed_by="operator", reason="attempted override")

    @pytest.mark.parametrize("mode", [sc.ENABLED, sc.DISABLED, sc.DRY_RUN])
    def test_set_mode_refuses_quant_researcher_under_any_mode(self, agent_db, mode):
        with pytest.raises(ValueError, match="quant_researcher"):
            sc.set_mode("quant_researcher", mode, changed_by="operator", reason="attempted override")

    def test_refused_set_mode_writes_no_row_at_all(self, agent_db):
        with pytest.raises(ValueError):
            sc.set_mode("trading_intelligence", sc.ENABLED, changed_by="operator", reason="x")
        assert sysadmin_store.get_agent_status("trading_intelligence") is None


class TestSchedulableAgentModes:
    def test_default_mode_for_a_never_run_schedulable_agent_is_enabled(self, agent_db):
        assert sc.get_mode("sys_admin") == sc.ENABLED

    def test_set_mode_disabled_round_trips(self, agent_db):
        sc.set_mode("sys_admin", sc.DISABLED, changed_by="operator", reason="pausing for maintenance")
        assert sc.get_mode("sys_admin") == sc.DISABLED

    def test_set_mode_dry_run_round_trips(self, agent_db):
        sc.set_mode("dev_agent", sc.DRY_RUN, changed_by="operator", reason="validating before enabling")
        assert sc.get_mode("dev_agent") == sc.DRY_RUN

    def test_set_mode_rejects_an_invalid_mode_string(self, agent_db):
        with pytest.raises(ValueError, match="mode must be one of"):
            sc.set_mode("sys_admin", "paused", changed_by="operator", reason="typo")

    def test_mode_persists_across_a_fresh_read_against_the_same_db(self, agent_db):
        """Simulates a process restart: the module holds no in-memory
        state, so a set_mode() followed by an unrelated fresh get_mode()
        call is exactly what a new process reading the same DB file
        would see."""
        sc.set_mode("risk_manager", sc.DISABLED, changed_by="operator", reason="incident response")
        # A brand new read, as a separate process would perform on restart.
        assert sc.get_mode("risk_manager") == sc.DISABLED


class TestAuditTrail:
    def test_set_mode_records_a_sysadmin_report(self, agent_db):
        sc.set_mode("trading_supervisor", sc.DISABLED, changed_by="alice", reason="on-call pause")
        reports = sysadmin_store.list_reports(module="scheduling_control")
        assert len(reports) == 1
        assert "alice" in reports[0]["report_json"]["reason"]
        assert "trading_supervisor" in reports[0]["report_json"]["reason"]

    def test_set_mode_emits_an_agent_mode_changed_event(self, agent_db):
        from agents import event_bus
        sc.set_mode("memory", sc.ENABLED, changed_by="bob", reason="resuming after review")
        events = event_bus.events_since("2020-01-01", event_type=runtime_events.AGENT_MODE_CHANGED)
        assert len(events) == 1
        assert events[0]["payload_json"]["agent"] == "memory"
        assert events[0]["payload_json"]["changed_by"] == "bob"

    def test_set_mode_returns_the_report_it_recorded(self, agent_db):
        report = sc.set_mode("sys_admin", sc.DRY_RUN, changed_by="carol", reason="testing")
        assert report.module == "scheduling_control"


class TestSnapshot:
    def test_snapshot_includes_every_runtime_agent(self, agent_db):
        from agents.runtime import agent_runtime
        snap = sc.snapshot()
        assert set(snap.keys()) == set(agent_runtime.RUNTIME_AGENT_NAMES)

    def test_snapshot_marks_never_schedulable_agents_correctly(self, agent_db):
        snap = sc.snapshot()
        assert snap["trading_intelligence"] == {"schedulable": False, "mode": sc.DISABLED}
        assert snap["quant_researcher"] == {"schedulable": False, "mode": sc.DISABLED}

    def test_snapshot_reflects_a_prior_set_mode_call(self, agent_db):
        sc.set_mode("dev_agent", sc.DISABLED, changed_by="operator", reason="x")
        snap = sc.snapshot()
        assert snap["dev_agent"] == {"schedulable": True, "mode": sc.DISABLED}
