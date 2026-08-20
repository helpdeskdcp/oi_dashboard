import datetime as dt
import sqlite3

from agents.trading_intelligence import execution_state as es

# Computed relative to the real current date (not a hardcoded literal) so
# these tests stay correct no matter when they actually run -- a fixed
# "2026-08-25"-style string would silently become a PAST date someday and
# start failing the expiry-identity check for reasons unrelated to the
# thing under test.
FUTURE_EXPIRY = (dt.date.today() + dt.timedelta(days=7)).isoformat()
PAST_EXPIRY = (dt.date.today() - dt.timedelta(days=1)).isoformat()


def _create(execution_id="NIFTY_24500_CE_sig1", **overrides):
    kwargs = dict(
        instrument="NIFTY", direction="CE", strike=24500, entry_price=120.0, quantity=50,
        sl=100.0, t1=140.0, t2=160.0, t3=180.0, confidence=82.0, decision_reason="test signal",
        signal_reference="ti_signal_log:1", expiry_date=FUTURE_EXPIRY,
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


def _insert_strike(db_path, *, symbol, strike, ce_ltp=None, pe_ltp=None, ts="2026-08-20T10:00:00"):
    """Minimal real cycles/strikes row -- same tables app.py's own
    log_cycle_to_db() writes in production, same helper pattern used
    throughout this repo's own backtest/institutional-flow tests."""
    conn = sqlite3.connect(db_path)
    date, time = ts.split("T")
    cur = conn.execute(
        "INSERT INTO cycles (symbol, ts, date, time, underlying_ltp, atm) VALUES (?,?,?,?,?,?)",
        (symbol, ts, date, time, strike, strike),
    )
    conn.execute(
        "INSERT INTO strikes (cycle_id, strike, ce_ltp, pe_ltp) VALUES (?,?,?,?)",
        (cur.lastrowid, strike, ce_ltp, pe_ltp),
    )
    conn.commit()
    conn.close()


class TestListExecutionsWithLiveLtp:
    """Purely informational enrichment on top of list_executions() -- never
    writes to execution_state, never calls transition(), never touches a
    broker (live_ltp comes from the same already-logged cycles/strikes
    archive every other panel on this page already reads)."""

    def test_active_execution_gets_live_ltp_and_active_status(self, ti_db):
        eid = "NIFTY_24500_CE_live"
        _create(execution_id=eid)
        es.transition(eid, "APPROVED")
        es.transition(eid, "READY")
        es.transition(eid, "ORDER_INTENT")
        es.transition(eid, "SUBMITTED")
        es.transition(eid, "FILLED")
        es.transition(eid, "MONITORING")
        _insert_strike(es.DB_PATH, symbol="NIFTY", strike=24500, ce_ltp=125.0)

        rows = es.list_executions_with_live_ltp()
        row = next(r for r in rows if r["execution_id"] == eid)
        assert row["live_ltp"] == 125.0
        assert row["hit_status"] == "ACTIVE"   # entry=120, sl=100, t1=140 -- 125 is between

    def test_price_at_or_above_target_is_target_hit(self, ti_db):
        eid = "NIFTY_24500_CE_target"
        _create(execution_id=eid)
        for state in ["APPROVED", "READY", "ORDER_INTENT", "SUBMITTED", "FILLED", "MONITORING"]:
            es.transition(eid, state)
        _insert_strike(es.DB_PATH, symbol="NIFTY", strike=24500, ce_ltp=145.0)   # >= t1=140

        row = next(r for r in es.list_executions_with_live_ltp() if r["execution_id"] == eid)
        assert row["hit_status"] == "TARGET_HIT"

    def test_price_at_or_below_sl_is_sl_hit(self, ti_db):
        eid = "NIFTY_24500_CE_sl"
        _create(execution_id=eid)
        for state in ["APPROVED", "READY", "ORDER_INTENT", "SUBMITTED", "FILLED", "MONITORING"]:
            es.transition(eid, state)
        _insert_strike(es.DB_PATH, symbol="NIFTY", strike=24500, ce_ltp=95.0)   # <= sl=100

        row = next(r for r in es.list_executions_with_live_ltp() if r["execution_id"] == eid)
        assert row["hit_status"] == "SL_HIT"

    def test_pe_direction_reads_pe_ltp_not_ce_ltp(self, ti_db):
        eid = "NIFTY_24500_PE_live"
        _create(execution_id=eid, direction="PE")
        for state in ["APPROVED", "READY", "ORDER_INTENT", "SUBMITTED", "FILLED", "MONITORING"]:
            es.transition(eid, state)
        _insert_strike(es.DB_PATH, symbol="NIFTY", strike=24500, ce_ltp=999.0, pe_ltp=110.0)

        row = next(r for r in es.list_executions_with_live_ltp() if r["execution_id"] == eid)
        assert row["live_ltp"] == 110.0

    def test_completed_execution_never_gets_a_live_status(self, ti_db):
        """A resolved shadow execution's real outcome already lives in
        ti_paper_trades.exit_reason -- computing a fresh hit_status
        against CURRENT price here would be actively misleading."""
        eid = "NIFTY_24500_CE_done"
        _create(execution_id=eid)
        for state in ["APPROVED", "READY", "ORDER_INTENT", "SUBMITTED", "FILLED", "MONITORING",
                      "EXIT_INTENT", "EXIT", "COMPLETED"]:
            es.transition(eid, state)
        _insert_strike(es.DB_PATH, symbol="NIFTY", strike=24500, ce_ltp=145.0)

        row = next(r for r in es.list_executions_with_live_ltp() if r["execution_id"] == eid)
        assert row["live_ltp"] is None
        assert row["hit_status"] is None

    def test_no_matching_strike_history_holds_honestly_not_fabricating(self, ti_db):
        eid = "NIFTY_24500_CE_nohist"
        _create(execution_id=eid)
        for state in ["APPROVED", "READY", "ORDER_INTENT", "SUBMITTED", "FILLED", "MONITORING"]:
            es.transition(eid, state)
        # no _insert_strike call -- no history exists for this strike at all

        row = next(r for r in es.list_executions_with_live_ltp() if r["execution_id"] == eid)
        assert row["live_ltp"] is None
        assert row["hit_status"] is None

    def test_never_writes_to_execution_state(self, ti_db):
        eid = "NIFTY_24500_CE_readonly"
        _create(execution_id=eid)
        for state in ["APPROVED", "READY", "ORDER_INTENT", "SUBMITTED", "FILLED", "MONITORING"]:
            es.transition(eid, state)
        _insert_strike(es.DB_PATH, symbol="NIFTY", strike=24500, ce_ltp=145.0)

        es.list_executions_with_live_ltp()
        row = es.get_execution(eid)
        assert row["current_state"] == "MONITORING"   # untouched -- TARGET_HIT is informational only


class TestLiveLtpExpiryContractIdentity:
    """Regression coverage for the SAME expiry-contract-identity bug class
    PR #30/#32/#33 fixed for every paper-trade table (Codex review finding,
    MEDIUM, fixed 2026-08-20): cycles/strikes carry no expiry column, so a
    strikes-table reading for (instrument, strike) says nothing about which
    option contract it belongs to. Once an execution's OWN expiry_date_at_entry
    has passed, any current reading for that strike number necessarily
    belongs to a different, freshly-priced instrument (strike numbers are
    reused every expiry cycle)."""

    def _monitoring_execution(self, eid, **overrides):
        _create(execution_id=eid, **overrides)
        for state in ["APPROVED", "READY", "ORDER_INTENT", "SUBMITTED", "FILLED", "MONITORING"]:
            es.transition(eid, state)

    def test_expired_contract_never_reports_a_live_price(self, ti_db):
        """Even though a strikes-table reading genuinely exists for this
        (instrument, strike) right now, this execution's OWN contract
        expired -- that reading necessarily belongs to a DIFFERENT,
        newly-rolled contract at the same strike number. Must hold
        (None), never attribute the new contract's price to this
        execution."""
        eid = "NIFTY_24500_CE_expired"
        self._monitoring_execution(eid, expiry_date=PAST_EXPIRY)
        _insert_strike(es.DB_PATH, symbol="NIFTY", strike=24500, ce_ltp=145.0)   # would be TARGET_HIT if trusted

        row = next(r for r in es.list_executions_with_live_ltp() if r["execution_id"] == eid)
        assert row["live_ltp"] is None
        assert row["hit_status"] is None

    def test_expiry_today_itself_still_counts_as_current(self, ti_db):
        """The expiry date itself is still a valid trading day -- only
        AFTER it passes does the contract-identity risk apply."""
        eid = "NIFTY_24500_CE_expiry_today"
        today = dt.date.today().isoformat()
        self._monitoring_execution(eid, expiry_date=today)
        _insert_strike(es.DB_PATH, symbol="NIFTY", strike=24500, ce_ltp=125.0)

        row = next(r for r in es.list_executions_with_live_ltp() if r["execution_id"] == eid)
        assert row["live_ltp"] == 125.0

    def test_unknown_expiry_holds_rather_than_guessing(self, ti_db):
        """expiry_date was never captured at creation time (e.g. a legacy
        execution predating this fix) -- fail closed, same honest
        "can't verify" contract as a missing strikes reading, never a
        guess based on strike-only matching."""
        eid = "NIFTY_24500_CE_unknown_expiry"
        self._monitoring_execution(eid, expiry_date=None)
        _insert_strike(es.DB_PATH, symbol="NIFTY", strike=24500, ce_ltp=125.0)

        row = next(r for r in es.list_executions_with_live_ltp() if r["execution_id"] == eid)
        assert row["live_ltp"] is None
        assert row["hit_status"] is None

    def test_valid_future_expiry_reports_normally(self, ti_db):
        """Sanity check the positive path still works -- this is what
        every other test in TestListExecutionsWithLiveLtp already
        exercises via the FUTURE_EXPIRY default, asserted explicitly
        here as the direct counterpart to the two failure-mode tests
        above."""
        eid = "NIFTY_24500_CE_valid_expiry"
        self._monitoring_execution(eid, expiry_date=FUTURE_EXPIRY)
        _insert_strike(es.DB_PATH, symbol="NIFTY", strike=24500, ce_ltp=125.0)

        row = next(r for r in es.list_executions_with_live_ltp() if r["execution_id"] == eid)
        assert row["live_ltp"] == 125.0
        assert row["hit_status"] == "ACTIVE"

    def test_malformed_expiry_value_fails_closed_not_crashing(self, ti_db):
        """A corrupted/malformed expiry_date_at_entry value must never
        crash the whole panel -- degrade this one row honestly instead."""
        eid = "NIFTY_24500_CE_malformed"
        self._monitoring_execution(eid, expiry_date="not-a-date")
        _insert_strike(es.DB_PATH, symbol="NIFTY", strike=24500, ce_ltp=125.0)

        rows = es.list_executions_with_live_ltp()   # must not raise
        row = next(r for r in rows if r["execution_id"] == eid)
        assert row["live_ltp"] is None
        assert row["hit_status"] is None
