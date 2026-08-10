"""
agents/runtime/watchdog.py -- Milestone 16, Phase 3: Watchdog &
Stale-Cycle Detection. Detects a scheduler that has stopped making
progress -- no successful tick() completion within cycle_interval *
WATCHDOG_STALE_MULTIPLIER -- even if nothing ever raised an exception
(a genuine hang/deadlock produces no error for tick()'s own exception
isolation or the circuit breaker to react to at all; this is the one
mechanism in this file that would catch that case).

Deliberately does NOT restart, kill, or touch any process -- only ever
exposes a "restart_recommended" boolean for a human or an external
supervisor (run_forever_vps.sh's own watch loop, restart.sh) to act on.
Same in-memory, per-process-instance footprint as heartbeat.py's
CycleHeartbeat and circuit_breaker.py's CircuitBreaker (Milestone 15,
Phases 3 and 4) -- a fresh restart legitimately deserves a clean,
non-stale start.
"""
import datetime as dt
import logging

from agents.ops import event_log as ops_event_log, models as ops_models

logger = logging.getLogger("oi_dashboard.runtime.watchdog")


class Watchdog:
    def __init__(self, *, enabled: bool, stale_multiplier: float):
        self.enabled = enabled
        self.stale_multiplier = stale_multiplier
        self.is_stale = False
        self.stale_count = 0

    def check(self, *, last_successful_cycle, cycle_interval_seconds: float, now=None) -> dict:
        """Called once per tick(). `last_successful_cycle` is
        heartbeat.CycleHeartbeat's own last_successful_cycle
        (None if the scheduler has never had one yet -- honestly not
        stale in that case, same "don't fabricate a trigger from
        insufficient data" discipline as every check_*() function in
        agents/intelligence_alerts/rules.py). Returns
        {"stale": bool, "restart_recommended": bool} -- the second key
        is always identical to the first; kept as its own explicit key
        because that's the literal contract this phase's spec asked
        for, and a future version of this check could recommend a
        restart under a condition that isn't simply "currently stale."

        WATCHDOG_STALE_CYCLE is emitted (and stale_count incremented)
        only on the TRANSITION into staleness, not on every subsequent
        check while it remains stale -- deduplication, not a flood of
        identical events every 5 seconds."""
        now = now or dt.datetime.now()
        if not self.enabled or last_successful_cycle is None:
            self.is_stale = False
            return {"stale": False, "restart_recommended": False}

        threshold_seconds = cycle_interval_seconds * self.stale_multiplier
        elapsed = (now - last_successful_cycle).total_seconds()
        newly_stale = elapsed > threshold_seconds

        if newly_stale and not self.is_stale:
            self.stale_count += 1
            logger.warning(
                "WATCHDOG_STALE_CYCLE: no successful cycle in %.0fs (threshold %.0fs, stale_count=%d)",
                elapsed, threshold_seconds, self.stale_count,
            )
            ops_event_log.record_event_safe(
                ops_models.WATCHDOG_STALE_CYCLE,
                {"elapsed_seconds": round(elapsed), "threshold_seconds": round(threshold_seconds), "stale_count": self.stale_count},
                now=now,
            )

        self.is_stale = newly_stale
        return {"stale": self.is_stale, "restart_recommended": self.is_stale}

    def to_dict(self) -> dict:
        return {
            "watchdog_stale": self.is_stale,
            "watchdog_restart_recommended": self.is_stale,
            "watchdog_stale_count": self.stale_count,
        }
