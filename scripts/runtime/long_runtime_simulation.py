"""
scripts/runtime/long_runtime_simulation.py -- Milestone 9 Final
Validation: "Long runtime simulation." A real, bounded proxy for
sustained continuous operation (the same "real, bounded, honest -- never
a claim of having run for hours" posture agents.sys_admin.maintenance's
own leak probe and this project's prior hardening-sprint scripts already
hold to): runs RuntimeScheduler.tick() 50 times in a row, injecting a
handful of workflows and queued tasks along the way, and reports whether
anything crashed, leaked memory, or left an agent stuck "running."

Usage: python3 scripts/runtime/long_runtime_simulation.py
Writes runtime_results/long_runtime_simulation.json.
"""
import datetime as dt
import json
import os
import sys
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TICKS = 50


def main():
    tmp_db = "/tmp/runtime_long_sim.db"
    from agents import audit_log, event_bus
    from agents.memory.sqlite_store import SQLiteMemoryStore
    from agents.risk_manager import risk_store
    from agents.runtime import (
        agent_runtime,
        policy_engine,
        runtime_store,
        task_queue,
        workflow_engine,
    )
    from agents.sys_admin import sysadmin_store
    from agents.trading_supervisor import supervision_store

    for m in (audit_log, event_bus, risk_store, supervision_store, sysadmin_store, runtime_store):
        m.DB_PATH = tmp_db
        m.init_db()
    store = SQLiteMemoryStore(db_path=tmp_db)
    policy_engine.set_policy("full_auto", changed_by="simulation", reason="long_runtime_simulation.py")

    from agents.runtime.scheduler import RuntimeScheduler
    scheduler = RuntimeScheduler(repo_dir=REPO_ROOT, memory_store=store, tick_interval_seconds=0)
    scheduler.start()

    tracemalloc.start()
    before = tracemalloc.take_snapshot()

    errors = []
    workflows_started = 0
    for i in range(TICKS):
        if i % 5 == 0:
            try:
                workflow_engine.start("NIFTY", memory_store=store)
                workflows_started += 1
            except workflow_engine.WorkflowError:
                pass
        if i % 3 == 0:
            task_queue.enqueue(priority="medium", task_type="dev_agent_trigger",
                                payload={"trigger": f"sim_{i}", "target_files": ["README.md"]})
        try:
            scheduler.tick()
        except Exception as exc:
            errors.append({"tick": i, "error": f"{type(exc).__name__}: {exc}"})

    after = tracemalloc.take_snapshot()
    tracemalloc.stop()
    growth_kb = sum(s.size_diff for s in after.compare_to(before, "lineno")) / 1024

    scheduler.stop()

    stuck_running = [
        agent for agent, status in agent_runtime.health_snapshot().items()
        if status and status["currently_running"] == 1
    ]

    out = {
        "generated_at": dt.datetime.now().isoformat(),
        "ticks_run": TICKS,
        "workflows_started": workflows_started,
        "crashes_during_ticks": errors,
        "agents_left_stuck_running": stuck_running,
        "final_agent_health": agent_runtime.health_snapshot(),
        "final_queue_depth": task_queue.status(),
        "final_workflow_counts": {
            "running": len(runtime_store.list_workflows(status="running", limit=1000)),
            "completed": len(runtime_store.list_workflows(status="completed", limit=1000)),
            "failed": len(runtime_store.list_workflows(status="failed", limit=1000)),
        },
        "memory_growth_kb_over_50_ticks": round(growth_kb, 1),
    }
    os.remove(tmp_db)
    out_dir = os.path.join(REPO_ROOT, "runtime_results")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "long_runtime_simulation.json")
    with open(out_path, "w") as fh:
        json.dump(out, fh, indent=2, default=str)
    print(json.dumps(out, indent=2, default=str))
    print(f"\nWritten to {out_path}")


if __name__ == "__main__":
    main()
