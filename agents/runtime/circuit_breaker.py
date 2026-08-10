"""
agents/runtime/circuit_breaker.py -- Milestone 15, Phase 4: Self-
Healing Runtime Protection. Prevents a single broken tick() from
destabilizing the scheduler by repeatedly re-attempting work that keeps
failing -- distinct from tick()'s own PRE-EXISTING per-cycle exception
isolation (an exception there is already caught and never propagates,
see scheduler.py's own tick() docstring) and from agents_runtime.py's
own per-AGENT restart-then-escalate mechanism
(RUNTIME_MAX_CONSECUTIVE_FAILURES_BEFORE_ESCALATION). This is a THIRD,
SCHEDULER-level mechanism: after enough consecutive tick() failures,
stop attempting new work entirely for a cooldown period, then allow one
probe tick through to test recovery before resuming normal operation.

State machine:
  closed     -- normal operation, every tick() attempts real work.
  open       -- too many consecutive failures; tick() skips all due-
                agent/task/workflow execution until recovery_seconds
                has elapsed since it opened.
  half_open  -- recovery_seconds has elapsed; the NEXT tick() is let
                through as a probe. A successful probe closes the
                circuit; a failed one reopens it (resetting the
                recovery timer).

In-memory, not SQLite-persisted -- same footprint/rationale as
heartbeat.py's own CycleHeartbeat (Milestone 15, Phase 3): this is
process-instance state, and a fresh process restart legitimately
deserves a closed circuit, not a stale "open" carried over from a
previous run.
"""
import datetime as dt
import logging

logger = logging.getLogger("oi_dashboard.runtime.circuit_breaker")

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, *, failure_threshold: int, recovery_seconds: float):
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self.state = CLOSED
        self.consecutive_failures = 0
        self._opened_at: dt.datetime | None = None

    def should_allow_execution(self, now=None) -> bool:
        """Call this at the START of a cycle, before attempting any
        real work. False means: skip the work entirely this cycle
        (still counts as a completed tick for cycles_executed/duration
        purposes -- see scheduler.py's own tick() docstring -- just one
        that did nothing)."""
        now = now or dt.datetime.now()
        if self.state == CLOSED:
            return True
        if self.state == OPEN:
            if self._opened_at is not None and (now - self._opened_at).total_seconds() >= self.recovery_seconds:
                self.state = HALF_OPEN
                logger.info(f"RUNTIME_CIRCUIT_HALF_OPEN consecutive_failures={self.consecutive_failures}")
                return True  # let exactly one probe through
            return False
        # HALF_OPEN: a probe is already in flight (synchronous scheduler,
        # never re-entered mid-tick) -- allow it.
        return True

    def record_success(self) -> None:
        """Call after a cycle that WAS allowed to run (should_allow_
        execution() returned True) and completed without error. Closes
        the circuit unconditionally -- a successful probe from
        half_open, or simply staying closed."""
        if self.state != CLOSED:
            logger.info(f"RUNTIME_CIRCUIT_CLOSED consecutive_failures_before_reset={self.consecutive_failures}")
        self.state = CLOSED
        self.consecutive_failures = 0
        self._opened_at = None

    def record_failure(self, now=None) -> None:
        """Call after a cycle that WAS allowed to run and raised/
        recovered from an exception."""
        now = now or dt.datetime.now()
        self.consecutive_failures += 1
        if self.state == HALF_OPEN:
            # the probe itself failed -- reopen immediately, reset the timer
            self.state = OPEN
            self._opened_at = now
            logger.info(f"RUNTIME_CIRCUIT_OPENED consecutive_failures={self.consecutive_failures} (probe failed)")
            return
        if self.state == CLOSED and self.consecutive_failures >= self.failure_threshold:
            self.state = OPEN
            self._opened_at = now
            logger.info(f"RUNTIME_CIRCUIT_OPENED consecutive_failures={self.consecutive_failures}")

    def to_dict(self) -> dict:
        return {"circuit_state": self.state, "circuit_consecutive_failures": self.consecutive_failures}
