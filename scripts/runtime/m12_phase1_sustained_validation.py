"""
scripts/runtime/m12_phase1_sustained_validation.py -- Milestone 12,
Phase 1: Runtime Scheduler Activation, sustained validation run.

A real, bounded, time-based validation (the same "real, bounded, honest
-- never a claim of having run for hours" posture
scripts/runtime/long_runtime_simulation.py already holds to, extended
here to a genuine wall-clock duration rather than a fixed tick count):
drives RuntimeScheduler.tick() directly, in real time, at the SAME
tick_interval_seconds/market-hours-aware sleep logic run_forever() uses
in production, against a fully isolated throwaway database -- never the
real oi_history.db. Milestone 12's own planning survey already confirmed
every registered agent cycle is structurally isolated from any broker
code, so this is safe to run for real, but activation of the real
production database is a deliberate, separate, explicit step (flipping
config.RUNTIME_SCHEDULER_ENABLED on the live deployment) that this
script does not take.

Per-tick durations are collected directly in this script (scheduler.
get_status() only exposes the MOST RECENT tick's duration, not a
history) -- this is the same tick()-calling-loop technique
long_runtime_simulation.py already established, just run for real
minutes instead of a fixed tick count.

Usage: python3 scripts/runtime/m12_phase1_sustained_validation.py [minutes]
Writes runtime_results/m12_phase1_sustained_validation.json.
"""
import datetime as dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DURATION_MINUTES = 15
SAMPLE_INTERVAL_SECONDS = 30


def main():
    duration_minutes = float(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DURATION_MINUTES
    duration_seconds = duration_minutes * 60

    tmp_db = "/tmp/m12_phase1_sustained_validation.db"
    from agents import audit_log, event_bus
    from agents.memory.sqlite_store import SQLiteMemoryStore
    from agents.risk_manager import risk_store
    from agents.runtime import agent_runtime, market_session, runtime_store
    from agents.sys_admin import infra_monitor, sysadmin_store
    from agents.trading_intelligence import data_access as ti_data_access
    from agents.trading_intelligence import ti_store
    from agents.trading_supervisor import supervision_store

    for m in (audit_log, event_bus, risk_store, supervision_store, sysadmin_store, runtime_store, ti_store):
        m.DB_PATH = tmp_db
        m.init_db()
    ti_data_access.DB_PATH = tmp_db  # data_access.py owns no tables of its own -- reads cycles/strikes/
                                      # market_structure_snapshots, which this throwaway DB legitimately has none of
                                      # (an honest "no data yet" state, exactly like a freshly deployed production DB)
    store = SQLiteMemoryStore(db_path=tmp_db)

    from agents.runtime.scheduler import RuntimeScheduler
    scheduler = RuntimeScheduler(repo_dir=REPO_ROOT, memory_store=store)  # real default tick_interval_seconds=5.0

    market_open_at_start, market_reason_at_start = market_session.is_nse_session_open()

    tick_durations_ms = []
    cpu_samples = []
    memory_samples = []
    last_sample_at = -SAMPLE_INTERVAL_SECONDS  # force an immediate first sample

    scheduler.start()
    run_start = time.monotonic()
    started_wall_clock = dt.datetime.now().isoformat()

    while time.monotonic() - run_start < duration_seconds:
        scheduler.tick()
        tick_durations_ms.append(scheduler.get_status()["last_cycle_duration_ms"])

        elapsed = time.monotonic() - run_start
        if elapsed - last_sample_at >= SAMPLE_INTERVAL_SECONDS:
            cpu_samples.append({"elapsed_seconds": round(elapsed, 1), **infra_monitor.cpu_status()})
            memory_samples.append({"elapsed_seconds": round(elapsed, 1), **infra_monitor.memory_status()})
            last_sample_at = elapsed

        market_open, _reason = market_session.is_nse_session_open()
        sleep_for = (
            scheduler._tick_interval_seconds if market_open
            else min(scheduler._tick_interval_seconds * 12, market_session.seconds_until_next_open())
        )
        # Same real sleep logic run_forever() uses -- capped here only so
        # a single off-hours sleep can't overshoot the requested
        # validation window by more than one interval.
        time.sleep(min(sleep_for, max(1.0, duration_seconds - (time.monotonic() - run_start))))

    scheduler.stop()
    ended_wall_clock = dt.datetime.now().isoformat()
    actual_duration_seconds = round(time.monotonic() - run_start, 1)

    final_status = scheduler.get_status()
    stuck_running = [
        agent for agent, status in agent_runtime.health_snapshot().items()
        if status and status.get("currently_running") == 1
    ]

    # This script drives tick() directly in ITS OWN loop (for precise
    # per-tick duration measurement) rather than calling run_forever() --
    # so scheduler.stop() here only ever reaches "stopping" (the honest
    # state for "a stop was requested" outside of run_forever()'s own
    # loop). The "stopped" terminal state is run_forever()'s own
    # finalization step (see scheduler.py's run_forever() docstring) and
    # is proven separately by test_agents/runtime/test_scheduler_lifecycle.py
    # ::TestFullLoopIntegration, which DOES run a real run_forever() on a
    # background thread. "Continuously active" for THIS script's own loop
    # shape means: it completed the full requested duration without an
    # unhandled exception (we're past scheduler.stop() precisely because
    # nothing crashed out of the while loop above), executed at least one
    # real cycle, and left no agent stuck mid-execution.
    scheduler_remained_continuously_active = final_status["cycles_executed"] > 0 and not stuck_running

    out = {
        "generated_at": dt.datetime.now().isoformat(),
        "requested_duration_minutes": duration_minutes,
        "actual_duration_seconds": actual_duration_seconds,
        "started_at": started_wall_clock,
        "ended_at": ended_wall_clock,
        "market_open_at_start": market_open_at_start,
        "market_reason_at_start": market_reason_at_start,
        "total_cycles_executed": final_status["cycles_executed"],
        "recovered_exceptions": final_status["recovered_exceptions"],
        "average_cycle_duration_ms": round(sum(tick_durations_ms) / len(tick_durations_ms), 2) if tick_durations_ms else None,
        "max_cycle_duration_ms": max(tick_durations_ms) if tick_durations_ms else None,
        "min_cycle_duration_ms": min(tick_durations_ms) if tick_durations_ms else None,
        "final_scheduler_state": final_status["scheduler_state"],
        "scheduler_remained_continuously_active": scheduler_remained_continuously_active,
        "agents_left_stuck_running_at_end": stuck_running,
        "final_agent_health": agent_runtime.health_snapshot(),
        "cpu_samples": cpu_samples,
        "memory_samples": memory_samples,
        "cpu_load1_per_core_trend": [s.get("load1_per_core") for s in cpu_samples],
        "memory_used_pct_trend": [s.get("used_pct") for s in memory_samples],
    }

    if os.path.exists(tmp_db):
        os.remove(tmp_db)
    out_dir = os.path.join(REPO_ROOT, "runtime_results")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "m12_phase1_sustained_validation.json")
    with open(out_path, "w") as fh:
        json.dump(out, fh, indent=2, default=str)
    print(json.dumps(out, indent=2, default=str))
    print(f"\nWritten to {out_path}")


if __name__ == "__main__":
    main()
