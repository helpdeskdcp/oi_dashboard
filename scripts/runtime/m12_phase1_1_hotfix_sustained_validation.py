"""
scripts/runtime/m12_phase1_1_hotfix_sustained_validation.py -- Milestone
12, Phase 1.1 hotfix: sustained validation against a genuinely
UNINITIALIZED database.

Unlike scripts/runtime/m12_phase1_sustained_validation.py (which builds
a fully-initialized throwaway database, matching Phase 1's own test
fixtures), this script calls NO init_db() on any agents.runtime/agents.
sys_admin/agents.trading_intelligence module -- reproducing the real
oi_history.db's actual current state (confirmed directly during Phase
1's own post-merge verification: agent_status, agent_events,
runtime_policy, runtime_workflow, ti_paper_trades, and every other
agents/runtime-or-later table genuinely do not exist there yet) as
closely as a throwaway file can. The point of this run is specifically
to prove the Phase 1.1 hotfix (best-effort runtime_events.emit(),
resilient start()/tick(), a degrading-not-raising get_runtime_status())
lets the scheduler run continuously for real, sustained minutes under
exactly this condition -- not the friendlier, fully-initialized
condition Phase 1's own validation already covered.

Usage: python3 scripts/runtime/m12_phase1_1_hotfix_sustained_validation.py [minutes]
Writes runtime_results/m12_phase1_1_hotfix_sustained_validation.json.
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

    tmp_db = "/tmp/m12_phase1_1_hotfix_uninitialized.db"
    if os.path.exists(tmp_db):
        os.remove(tmp_db)
    # A real, existing SQLite file with ZERO tables -- deliberately no
    # init_db() call on anything. This is the whole point of this run.
    import sqlite3
    sqlite3.connect(tmp_db).close()

    from agents import audit_log, event_bus
    from agents.memory.sqlite_store import SQLiteMemoryStore
    from agents.runtime import agent_runtime, lifecycle, market_session, runtime_store
    from agents.sys_admin import infra_monitor, sysadmin_store
    from agents.trading_intelligence import data_access as ti_data_access
    from agents.trading_intelligence import ti_store

    for m in (audit_log, event_bus, sysadmin_store, runtime_store, ti_store, ti_data_access):
        m.DB_PATH = tmp_db  # set, but NEVER call m.init_db() -- see module docstring
    store = SQLiteMemoryStore(db_path="/tmp/m12_phase1_1_hotfix_memory.db")

    from agents.runtime.scheduler import RuntimeScheduler

    market_open_at_start, market_reason_at_start = market_session.is_nse_session_open()

    tick_durations_ms = []
    tick_errors = []  # {"cycle": i, "error": "..."} for every recovered (non-crashing) tick failure
    cpu_samples = []
    memory_samples = []
    last_sample_at = -SAMPLE_INTERVAL_SECONDS

    scheduler = RuntimeScheduler(repo_dir=REPO_ROOT, memory_store=store)  # real default tick_interval_seconds=5.0

    crashed = False
    crash_error = None
    run_start = time.monotonic()
    started_wall_clock = dt.datetime.now().isoformat()
    try:
        scheduler.start()  # must not raise -- this is the Phase 1.1 fix's own first real test
        while time.monotonic() - run_start < duration_seconds:
            result = scheduler.tick()
            tick_durations_ms.append(scheduler.get_status()["last_cycle_duration_ms"])
            if result.get("recovered"):
                tick_errors.append({
                    "cycle": scheduler.get_status()["cycles_executed"], "error": result.get("error"),
                })

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
            time.sleep(min(sleep_for, max(1.0, duration_seconds - (time.monotonic() - run_start))))
    except Exception as exc:
        crashed = True
        crash_error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            scheduler.stop()
        except Exception:
            pass

    ended_wall_clock = dt.datetime.now().isoformat()
    actual_duration_seconds = round(time.monotonic() - run_start, 1)

    final_status = scheduler.get_status()

    # /api/runtime/status's own real data source -- must not raise even
    # though this run's whole database is deliberately uninitialized.
    try:
        runtime_status_response = lifecycle.get_runtime_status()
        runtime_status_error = None
    except Exception as exc:
        runtime_status_response = None
        runtime_status_error = f"{type(exc).__name__}: {exc}"

    out = {
        "generated_at": dt.datetime.now().isoformat(),
        "scenario": "genuinely uninitialized database -- zero agents.runtime/agents.sys_admin/"
                    "agents.trading_intelligence tables exist, no init_db() was called on any of them",
        "requested_duration_minutes": duration_minutes,
        "actual_duration_seconds": actual_duration_seconds,
        "started_at": started_wall_clock,
        "ended_at": ended_wall_clock,
        "market_open_at_start": market_open_at_start,
        "market_reason_at_start": market_reason_at_start,
        "scheduler_crashed": crashed,
        "crash_error": crash_error,
        "total_cycles_executed": final_status["cycles_executed"],
        "recovered_exceptions_scheduler_level": final_status["recovered_exceptions"],
        "recovered_tick_errors": tick_errors,
        "average_cycle_duration_ms": round(sum(tick_durations_ms) / len(tick_durations_ms), 2) if tick_durations_ms else None,
        "max_cycle_duration_ms": max(tick_durations_ms) if tick_durations_ms else None,
        "min_cycle_duration_ms": min(tick_durations_ms) if tick_durations_ms else None,
        "final_scheduler_state": final_status["scheduler_state"],
        "scheduler_reached_running_state": final_status["scheduler_state"] in ("running", "stopping", "stopped"),
        "scheduler_remained_continuously_active": (not crashed) and final_status["cycles_executed"] > 0,
        "runtime_status_endpoint_raised": runtime_status_error is not None,
        "runtime_status_endpoint_error": runtime_status_error,
        "runtime_status_endpoint_response": runtime_status_response,
        "cpu_samples": cpu_samples,
        "memory_samples": memory_samples,
        "cpu_load1_per_core_trend": [s.get("load1_per_core") for s in cpu_samples],
        "memory_used_pct_trend": [s.get("used_pct") for s in memory_samples],
    }

    if os.path.exists(tmp_db):
        os.remove(tmp_db)
    if os.path.exists("/tmp/m12_phase1_1_hotfix_memory.db"):
        os.remove("/tmp/m12_phase1_1_hotfix_memory.db")
    out_dir = os.path.join(REPO_ROOT, "runtime_results")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "m12_phase1_1_hotfix_sustained_validation.json")
    with open(out_path, "w") as fh:
        json.dump(out, fh, indent=2, default=str)
    print(json.dumps(out, indent=2, default=str))
    print(f"\nWritten to {out_path}")


if __name__ == "__main__":
    main()
