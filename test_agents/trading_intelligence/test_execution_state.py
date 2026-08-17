import sqlite3

from agents.trading_intelligence import execution_state as es


def _create(execution_id="NIFTY_24500_CE_sig1", **overrides):
    kwargs = dict(
        instrument="NIFTY", direction="CE", strike=24500, entry_price=120.0, quantity=50,
        sl=100.0, t1=140.0, t2=160.0, t3=180.0, confidence=82.0, decision_reason="test signal",
        signal_reference="ti_signal_log:1",
    )
    kwargs.update(overrides)
    return es.create_execution(execution_id, **kwargs)


class TestCreateExecutionIdempotency:
    def test_create_returns_signal_state(self, ti_db):
        row = _create()
        assert row["current_state"] == "SIGNAL"
        assert row["instrument"] == "NIFTY"
        assert row["direction"] == "CE"

    def test_create_rejects_invalid_direction(self, ti_db):
        import pytest
        with pytest.raises(ValueError):
            _create(execution_id="X", direction="LONG")

    def test_duplicate_create_returns_existing_row_untouched(self, ti_db):
        first = _create()
        second = _create(entry_price=999.0, sl=1.0, confidence=1.0)  # different values, same execution_id
        assert second["entry_price"] == first["entry_price"] == 120.0
        assert second["sl"] == first["sl"] == 100.0
        assert second["confidence"] == first["confidence"] == 82.0

    def test_duplicate_create_does_not_insert_a_second_row(self, ti_db):
        _create()
        _create()
        _create()
        assert len(es.list_executions()) == 1

    def test_duplicate_create_does_not_reset_state(self, ti_db):
        _create()
        es.transition("NIFTY_24500_CE_sig1", "APPROVED")
        es.transition("NIFTY_24500_CE_sig1", "READY")
        _create()  # duplicate create attempt after progressing state
        row = es.get_execution("NIFTY_24500_CE_sig1")
        assert row["current_state"] == "READY"  # untouched by the duplicate create


class TestValidTransitions:
    def test_full_happy_path_to_completed(self, ti_db):
        eid = "NIFTY_24500_CE_full"
        _create(execution_id=eid)
        path = ["APPROVED", "READY", "ORDER_INTENT", "SUBMITTED", "FILLED", "MONITORING",
                "EXIT_INTENT", "EXIT", "COMPLETED"]
        for state in path:
            result = es.transition(eid, state)
            assert result["ok"] is True, result
        assert es.get_execution(eid)["current_state"] == "COMPLETED"

    def test_monitoring_hub_target_update_returns_to_monitoring(self, ti_db):
        eid = "NIFTY_24500_CE_target"
        _create(execution_id=eid)
        for state in ["APPROVED", "READY", "ORDER_INTENT", "SUBMITTED", "FILLED", "MONITORING"]:
            es.transition(eid, state)
        assert es.transition(eid, "TARGET_UPDATE")["ok"] is True
        assert es.transition(eid, "MONITORING")["ok"] is True
        assert es.get_execution(eid)["current_state"] == "MONITORING"

    def test_monitoring_hub_sl_update_returns_to_monitoring(self, ti_db):
        eid = "NIFTY_24500_CE_sl"
        _create(execution_id=eid)
        for state in ["APPROVED", "READY", "ORDER_INTENT", "SUBMITTED", "FILLED", "MONITORING"]:
            es.transition(eid, state)
        assert es.transition(eid, "SL_UPDATE")["ok"] is True
        assert es.transition(eid, "MONITORING")["ok"] is True

    def test_monitoring_hub_trailing_returns_to_monitoring(self, ti_db):
        eid = "NIFTY_24500_CE_trail"
        _create(execution_id=eid)
        for state in ["APPROVED", "READY", "ORDER_INTENT", "SUBMITTED", "FILLED", "MONITORING"]:
            es.transition(eid, state)
        assert es.transition(eid, "TRAILING")["ok"] is True
        assert es.transition(eid, "MONITORING")["ok"] is True

    def test_can_cycle_monitoring_multiple_times(self, ti_db):
        eid = "NIFTY_24500_CE_cycle"
        _create(execution_id=eid)
        for state in ["APPROVED", "READY", "ORDER_INTENT", "SUBMITTED", "FILLED", "MONITORING"]:
            es.transition(eid, state)
        for _ in range(5):
            assert es.transition(eid, "TARGET_UPDATE")["ok"] is True
            assert es.transition(eid, "MONITORING")["ok"] is True
        assert es.get_execution(eid)["current_state"] == "MONITORING"


class TestInvalidTransitionsRejectedSafely:
    def test_skipping_states_is_rejected(self, ti_db):
        eid = "NIFTY_24500_CE_skip"
        _create(execution_id=eid)
        result = es.transition(eid, "FILLED")  # SIGNAL -> FILLED directly, invalid
        assert result["ok"] is False
        assert "invalid transition" in result["reason"]
        assert es.get_execution(eid)["current_state"] == "SIGNAL"  # unchanged

    def test_backwards_transition_is_rejected(self, ti_db):
        eid = "NIFTY_24500_CE_back"
        _create(execution_id=eid)
        es.transition(eid, "APPROVED")
        result = es.transition(eid, "SIGNAL")  # APPROVED -> SIGNAL, invalid
        assert result["ok"] is False
        assert es.get_execution(eid)["current_state"] == "APPROVED"

    def test_transition_out_of_completed_is_always_rejected(self, ti_db):
        eid = "NIFTY_24500_CE_terminal"
        _create(execution_id=eid)
        for state in ["APPROVED", "READY", "ORDER_INTENT", "SUBMITTED", "FILLED", "MONITORING",
                      "EXIT_INTENT", "EXIT", "COMPLETED"]:
            es.transition(eid, state)
        result = es.transition(eid, "MONITORING")
        assert result["ok"] is False
        assert es.get_execution(eid)["current_state"] == "COMPLETED"

    def test_unknown_state_name_is_rejected(self, ti_db):
        eid = "NIFTY_24500_CE_unknown"
        _create(execution_id=eid)
        result = es.transition(eid, "NOT_A_REAL_STATE")
        assert result["ok"] is False
        assert "not a valid state" in result["reason"]

    def test_unknown_execution_id_is_rejected_not_crash(self, ti_db):
        result = es.transition("NEVER_CREATED", "APPROVED")
        assert result["ok"] is False
        assert "no execution record" in result["reason"]

    def test_rejected_transitions_are_still_logged(self, ti_db):
        eid = "NIFTY_24500_CE_logged_reject"
        _create(execution_id=eid)
        es.transition(eid, "FILLED")  # invalid, rejected
        transitions = es.recent_transitions(eid, limit=10)
        rejected = [t for t in transitions if t["accepted"] == 0]
        assert len(rejected) == 1
        assert rejected[0]["to_state"] == "FILLED"


class TestEveryRejectionPathIsAudited:
    """Regression tests for the PR #17 review finding: transition()'s
    early-return branches for an unknown execution_id and an unknown
    state name used to return BEFORE writing to
    execution_transition_log, silently contradicting the module's own
    documented "every attempt, accepted or rejected, is logged"
    contract. Each test here asserts a real row exists with
    accepted=0, not just that the return value says ok=False."""

    def test_unknown_execution_id_writes_an_audit_row(self, ti_db):
        # No execution_state row exists for this id at all -- the audit
        # record must still be written (no FK dependency on execution_state).
        result = es.transition("NEVER_CREATED_XYZ", "APPROVED")
        assert result["ok"] is False
        rows = es.recent_transitions("NEVER_CREATED_XYZ", limit=5)
        assert len(rows) == 1
        assert rows[0]["accepted"] == 0
        assert rows[0]["to_state"] == "APPROVED"
        assert "no execution record" in rows[0]["reason"]

    def test_unknown_state_name_writes_an_audit_row(self, ti_db):
        eid = "NIFTY_24500_CE_audit_unknown_state"
        _create(execution_id=eid)
        result = es.transition(eid, "GARBAGE_STATE")
        assert result["ok"] is False
        rows = es.recent_transitions(eid, limit=5)
        rejected = [r for r in rows if r["accepted"] == 0]
        assert len(rejected) == 1
        assert rejected[0]["to_state"] == "GARBAGE_STATE"
        assert "not a valid state" in rejected[0]["reason"]

    def test_invalid_transition_writes_an_audit_row(self, ti_db):
        eid = "NIFTY_24500_CE_audit_invalid"
        _create(execution_id=eid)
        result = es.transition(eid, "FILLED")  # SIGNAL -> FILLED, skips states
        assert result["ok"] is False
        rows = es.recent_transitions(eid, limit=5)
        rejected = [r for r in rows if r["accepted"] == 0]
        assert len(rejected) == 1
        assert rejected[0]["from_state"] == "SIGNAL"
        assert rejected[0]["to_state"] == "FILLED"
        assert "invalid transition" in rejected[0]["reason"]

    def test_terminal_state_rejection_writes_an_audit_row(self, ti_db):
        eid = "NIFTY_24500_CE_audit_terminal"
        _create(execution_id=eid)
        for state in ["APPROVED", "READY", "ORDER_INTENT", "SUBMITTED", "FILLED", "MONITORING",
                      "EXIT_INTENT", "EXIT", "COMPLETED"]:
            es.transition(eid, state)
        result = es.transition(eid, "MONITORING")  # attempted out of COMPLETED
        assert result["ok"] is False
        rows = es.recent_transitions(eid, limit=20)
        rejected = [r for r in rows if r["accepted"] == 0]
        assert len(rejected) == 1
        assert rejected[0]["from_state"] == "COMPLETED"
        assert rejected[0]["to_state"] == "MONITORING"


class TestIdempotentSameStateTransition:
    def test_transition_to_current_state_is_a_successful_noop(self, ti_db):
        eid = "NIFTY_24500_CE_noop"
        _create(execution_id=eid)
        es.transition(eid, "APPROVED")
        result = es.transition(eid, "APPROVED")  # same state again
        assert result["ok"] is True
        assert "no-op" in result["reason"]
        assert es.get_execution(eid)["current_state"] == "APPROVED"

    def test_repeated_order_intent_does_not_create_duplicate_submissions(self, ti_db):
        # Duplicate execution intent must never create duplicate orders --
        # even the STATE ITSELF cannot be pushed to ORDER_INTENT twice in
        # a way that would look like two independent order attempts.
        eid = "NIFTY_24500_CE_dup_intent"
        _create(execution_id=eid)
        for state in ["APPROVED", "READY", "ORDER_INTENT"]:
            es.transition(eid, state)
        # Retry the same "submit an order intent" call -- idempotent no-op.
        result = es.transition(eid, "ORDER_INTENT")
        assert result["ok"] is True
        assert "no-op" in result["reason"]
        # Still exactly ONE execution row -- no second lifecycle was created.
        assert len(es.list_executions()) == 1


class TestPersistenceAndRestartRecovery:
    def test_state_survives_a_fresh_connection(self, ti_db):
        eid = "NIFTY_24500_CE_restart"
        _create(execution_id=eid)
        es.transition(eid, "APPROVED")
        conn = sqlite3.connect(ti_db)
        row = conn.execute("SELECT current_state FROM execution_state WHERE execution_id=?", (eid,)).fetchone()
        conn.close()
        assert row[0] == "APPROVED"

    def test_transition_log_is_append_only(self, ti_db):
        eid = "NIFTY_24500_CE_append"
        _create(execution_id=eid)
        es.transition(eid, "APPROVED")
        es.transition(eid, "READY")
        es.transition(eid, "FILLED")  # invalid, logged as rejected too
        transitions = es.recent_transitions(eid, limit=10)
        assert len(transitions) == 4  # create + APPROVED + READY + rejected FILLED

    def test_list_executions_active_only_excludes_completed(self, ti_db):
        _create(execution_id="A")
        _create(execution_id="B")
        for state in ["APPROVED", "READY", "ORDER_INTENT", "SUBMITTED", "FILLED", "MONITORING",
                      "EXIT_INTENT", "EXIT", "COMPLETED"]:
            es.transition("A", state)
        active = [e["execution_id"] for e in es.list_executions(active_only=True)]
        assert active == ["B"]


class TestExitIntentAndTrailing:
    def test_exit_intent_reaches_completed(self, ti_db):
        eid = "NIFTY_24500_CE_exit"
        _create(execution_id=eid)
        for state in ["APPROVED", "READY", "ORDER_INTENT", "SUBMITTED", "FILLED", "MONITORING"]:
            es.transition(eid, state)
        assert es.transition(eid, "EXIT_INTENT")["ok"] is True
        assert es.transition(eid, "EXIT")["ok"] is True
        assert es.transition(eid, "COMPLETED")["ok"] is True

    def test_trailing_then_exit_is_a_valid_full_path(self, ti_db):
        eid = "NIFTY_24500_CE_trail_exit"
        _create(execution_id=eid)
        for state in ["APPROVED", "READY", "ORDER_INTENT", "SUBMITTED", "FILLED", "MONITORING",
                      "TRAILING", "MONITORING", "EXIT_INTENT", "EXIT", "COMPLETED"]:
            result = es.transition(eid, state)
            assert result["ok"] is True, (state, result)


class TestErrorStatus:
    def test_set_error_status_does_not_change_state(self, ti_db):
        eid = "NIFTY_24500_CE_err"
        _create(execution_id=eid)
        es.transition(eid, "APPROVED")
        es.set_error_status(eid, "example: broker rejected order")
        row = es.get_execution(eid)
        assert row["current_state"] == "APPROVED"
        assert row["error_status"] == "example: broker rejected order"
