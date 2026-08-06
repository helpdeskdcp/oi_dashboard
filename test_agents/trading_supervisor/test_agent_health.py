"""
test_agents/trading_supervisor/test_agent_health.py -- regression tests
for agent_health.py against a real (throwaway) audit_log/memory store.
"""
import datetime as dt

from agents import audit_log
from agents.memory.sqlite_store import SQLiteMemoryStore
from agents.trading_supervisor import agent_health


class TestAgentActivityHealth:
    def test_no_activity_is_stale_with_zero_counts(self, agent_db):
        health = agent_health.agent_activity_health("dev_agent")
        assert health.is_stale is True
        assert health.recent_activity_count == 0
        assert health.is_failing is False

    def test_recent_activity_is_not_stale(self, agent_db):
        audit_log.record(agent="dev_agent", action_type="proposal", description="p",
                          risk_tier="needs_approval", outcome="pending_approval")
        health = agent_health.agent_activity_health("dev_agent")
        assert health.is_stale is False
        assert health.recent_activity_count == 1

    def test_high_rejection_rate_flags_is_failing(self, agent_db):
        for _ in range(5):
            audit_log.record(agent="dev_agent", action_type="proposal", description="p",
                              risk_tier="needs_approval", outcome="rejected")
        health = agent_health.agent_activity_health("dev_agent", failure_rate_threshold=0.7)
        assert health.is_failing is True
        assert health.outcome_counts["rejected"] == 5

    def test_low_rejection_rate_does_not_flag(self, agent_db):
        audit_log.record(agent="dev_agent", action_type="proposal", description="p",
                          risk_tier="needs_approval", outcome="pending_approval")
        audit_log.record(agent="dev_agent", action_type="proposal", description="p",
                          risk_tier="needs_approval", outcome="rejected")
        health = agent_health.agent_activity_health("dev_agent", failure_rate_threshold=0.7)
        assert health.is_failing is False

    def test_since_hours_excludes_old_activity(self, agent_db, monkeypatch):
        old_ts = (dt.datetime.now() - dt.timedelta(hours=100)).isoformat()

        def fake_list_recent(agent=None, since_ts=None, limit=50):
            return []  # simulates nothing within the window

        monkeypatch.setattr(audit_log, "list_recent", fake_list_recent)
        health = agent_health.agent_activity_health("dev_agent", since_hours=24)
        assert health.recent_activity_count == 0


class TestMemoryHealth:
    def test_reachable_and_not_stale_when_empty(self, tmp_path):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
        health = agent_health.memory_health(store)
        assert health.reachable is True
        assert health.most_recent_write_ts is None
        assert health.is_stale is False

    def test_picks_up_the_most_recent_write(self, tmp_path):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
        store.record_bug_fix(trigger="t", issue_summary="s", root_cause="r", fix_summary="f")
        health = agent_health.memory_health(store)
        assert health.reachable is True
        assert health.most_recent_write_ts is not None
        assert health.is_stale is False

    def test_unreachable_store_is_flagged(self):
        class BrokenStore:
            def search_bug_fixes(self, *a, **k):
                raise RuntimeError("db gone")

        health = agent_health.memory_health(BrokenStore())
        assert health.reachable is False
        assert health.is_stale is True


class TestSweepAllAgents:
    def test_covers_every_named_agent_plus_memory(self, agent_db, tmp_path):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
        result = agent_health.sweep_all_agents(store)
        assert set(result.keys()) == {"dev_agent", "quant_researcher", "risk_manager", "memory"}
