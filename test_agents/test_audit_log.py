"""
test_agents/test_audit_log.py -- regression tests for agents/audit_log.py
(the append-only agent_audit_log table).
"""
import pytest

from agents import audit_log


class TestRecordAndGet:
    def test_record_returns_id_and_get_round_trips(self, agent_db):
        row_id = audit_log.record(
            agent="dev", action_type="finding", description="found a thing",
            risk_tier="read_only", outcome="applied", payload={"k": "v"},
        )
        row = audit_log.get(row_id)
        assert row["agent"] == "dev"
        assert row["action_type"] == "finding"
        assert row["description"] == "found a thing"
        assert row["risk_tier"] == "read_only"
        assert row["outcome"] == "applied"
        assert row["payload_json"] == {"k": "v"}

    def test_get_missing_id_returns_none(self, agent_db):
        assert audit_log.get(99999) is None

    def test_payload_none_round_trips_as_none(self, agent_db):
        row_id = audit_log.record(
            agent="dev", action_type="finding", description="no payload",
            risk_tier="read_only", outcome="applied",
        )
        assert audit_log.get(row_id)["payload_json"] is None

    def test_invalid_outcome_rejected(self, agent_db):
        with pytest.raises(ValueError):
            audit_log.record(
                agent="dev", action_type="finding", description="x",
                risk_tier="read_only", outcome="not-a-real-outcome",
            )

    def test_invalid_action_type_rejected(self, agent_db):
        with pytest.raises(ValueError):
            audit_log.record(
                agent="dev", action_type="not-a-real-type", description="x",
                risk_tier="read_only", outcome="applied",
            )

    def test_every_valid_outcome_accepted(self, agent_db):
        for outcome in audit_log.VALID_OUTCOMES:
            row_id = audit_log.record(
                agent="dev", action_type="proposal", description=outcome,
                risk_tier="needs_approval", outcome=outcome,
            )
            assert audit_log.get(row_id)["outcome"] == outcome


class TestListPending:
    def test_lists_only_pending_approval_rows(self, agent_db):
        audit_log.record(agent="dev", action_type="proposal", description="p1",
                          risk_tier="needs_approval", outcome="pending_approval")
        audit_log.record(agent="dev", action_type="proposal", description="p2",
                          risk_tier="needs_approval", outcome="approved")
        pending = audit_log.list_pending()
        assert len(pending) == 1
        assert pending[0]["description"] == "p1"

    def test_filters_by_agent(self, agent_db):
        audit_log.record(agent="dev", action_type="proposal", description="dev-p",
                          risk_tier="needs_approval", outcome="pending_approval")
        audit_log.record(agent="sysadmin", action_type="proposal", description="sysadmin-p",
                          risk_tier="needs_approval", outcome="pending_approval")
        pending = audit_log.list_pending(agent="dev")
        assert len(pending) == 1
        assert pending[0]["description"] == "dev-p"

    def test_oldest_first(self, agent_db):
        id1 = audit_log.record(agent="dev", action_type="proposal", description="first",
                                risk_tier="needs_approval", outcome="pending_approval")
        id2 = audit_log.record(agent="dev", action_type="proposal", description="second",
                                risk_tier="needs_approval", outcome="pending_approval")
        pending = audit_log.list_pending()
        assert [p["id"] for p in pending] == [id1, id2]


class TestSetOutcome:
    def test_transitions_outcome_and_stamps_approver(self, agent_db):
        row_id = audit_log.record(agent="dev", action_type="proposal", description="p",
                                   risk_tier="needs_approval", outcome="pending_approval")
        audit_log.set_outcome(row_id, "approved", approved_by="helpdeskdcp")
        row = audit_log.get(row_id)
        assert row["outcome"] == "approved"
        assert row["approved_by"] == "helpdeskdcp"
        assert row["approved_at"] is not None

    def test_never_touches_immutable_fields(self, agent_db):
        row_id = audit_log.record(agent="dev", action_type="proposal", description="original description",
                                   risk_tier="needs_approval", outcome="pending_approval",
                                   payload={"diff": "original diff"})
        audit_log.set_outcome(row_id, "rejected")
        row = audit_log.get(row_id)
        assert row["description"] == "original description"
        assert row["payload_json"] == {"diff": "original diff"}
        assert row["agent"] == "dev"

    def test_sha_columns_preserved_when_not_passed_again(self, agent_db):
        row_id = audit_log.record(agent="dev", action_type="proposal", description="p",
                                   risk_tier="needs_approval", outcome="approved")
        audit_log.set_outcome(row_id, "applied", pre_merge_sha="abc123", merge_commit_sha="def456")
        audit_log.set_outcome(row_id, "rolled_back")   # no SHAs passed this time
        row = audit_log.get(row_id)
        assert row["pre_merge_sha"] == "abc123"
        assert row["merge_commit_sha"] == "def456"
        assert row["outcome"] == "rolled_back"

    def test_unknown_id_raises(self, agent_db):
        with pytest.raises(ValueError):
            audit_log.set_outcome(99999, "approved")

    def test_invalid_outcome_rejected(self, agent_db):
        row_id = audit_log.record(agent="dev", action_type="proposal", description="p",
                                   risk_tier="needs_approval", outcome="pending_approval")
        with pytest.raises(ValueError):
            audit_log.set_outcome(row_id, "not-a-real-outcome")


class TestListRecent:
    def test_orders_most_recent_first(self, agent_db):
        audit_log.record(agent="dev", action_type="proposal", description="first",
                          risk_tier="needs_approval", outcome="pending_approval")
        audit_log.record(agent="dev", action_type="proposal", description="second",
                          risk_tier="needs_approval", outcome="pending_approval")
        rows = audit_log.list_recent(agent="dev")
        assert [r["description"] for r in rows] == ["second", "first"]

    def test_filters_by_agent(self, agent_db):
        audit_log.record(agent="dev", action_type="proposal", description="a",
                          risk_tier="needs_approval", outcome="pending_approval")
        audit_log.record(agent="quant_researcher", action_type="proposal", description="b",
                          risk_tier="needs_approval", outcome="pending_approval")
        assert len(audit_log.list_recent(agent="quant_researcher")) == 1

    def test_filters_by_since_ts(self, agent_db):
        audit_log.record(agent="dev", action_type="proposal", description="a",
                          risk_tier="needs_approval", outcome="pending_approval")
        rows = audit_log.list_recent(agent="dev", since_ts="2099-01-01T00:00:00")
        assert rows == []

    def test_respects_limit(self, agent_db):
        for i in range(5):
            audit_log.record(agent="dev", action_type="proposal", description=str(i),
                              risk_tier="needs_approval", outcome="pending_approval")
        assert len(audit_log.list_recent(agent="dev", limit=2)) == 2


class TestNoDeleteFunctionExists:
    def test_module_has_no_delete_function(self):
        # Append-only by construction, not just by convention -- there is
        # no function anywhere in this module capable of removing a row.
        assert not any(name.startswith("delete") for name in dir(audit_log))
