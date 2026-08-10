"""
agents/runtime/heartbeat.py -- Milestone 15, Phase 3: Runtime Scheduler
Observability. A small, in-memory tracker for the two things
RuntimeScheduler.get_status() didn't already have: separate last-
SUCCESSFUL vs. last-FAILED cycle timestamps, and a consecutive-failure
count that resets to zero on any success (distinct from that class's
own _recovered_exceptions, which is a cumulative lifetime total, never
reset).

In-memory, not SQLite-persisted, deliberately -- this mirrors
RuntimeScheduler's own existing cycles_executed/last_cycle_ts/
_started_at, all of which are already plain instance state that resets
on every process restart. A restart legitimately deserves a clean
slate for "how healthy has THIS run been" questions; nothing here needs
to survive one. One CycleHeartbeat instance per RuntimeScheduler
instance, same lifetime/ownership as every other piece of that class's
own state.
"""
import datetime as dt


class CycleHeartbeat:
    def __init__(self):
        self.last_successful_cycle: dt.datetime | None = None
        self.last_failed_cycle: dt.datetime | None = None
        self.consecutive_failures: int = 0

    def record_success(self, ts: dt.datetime) -> None:
        self.last_successful_cycle = ts
        self.consecutive_failures = 0

    def record_failure(self, ts: dt.datetime) -> None:
        self.last_failed_cycle = ts
        self.consecutive_failures += 1

    def to_dict(self) -> dict:
        return {
            "last_successful_cycle": self.last_successful_cycle.isoformat() if self.last_successful_cycle else None,
            "last_failed_cycle": self.last_failed_cycle.isoformat() if self.last_failed_cycle else None,
            "consecutive_failures": self.consecutive_failures,
        }
