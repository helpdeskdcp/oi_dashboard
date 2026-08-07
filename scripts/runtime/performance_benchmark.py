"""
scripts/runtime/performance_benchmark.py -- Milestone 9, Module 11:
"Measure: workflow latency, agent latency, queue latency, memory usage,
CPU usage, database latency." Real wall-clock timing (time.perf_counter,
N repeats, min/median/max), same methodology
scripts/hardening/performance_profile.py already established for the
six pre-existing agents -- this script covers the NEW runtime-layer
surfaces that sprint didn't (workflow stage advances, scheduler ticks,
task queue claim/complete cycles).

Usage: python3 scripts/runtime/performance_benchmark.py
Writes runtime_results/performance_benchmark.json.
"""
import datetime as dt
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _time_it(fn, *, repeats: int) -> dict:
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return {
        "repeats": repeats, "min_ms": round(min(samples), 3),
        "median_ms": round(statistics.median(samples), 3), "max_ms": round(max(samples), 3),
    }


def main():
    tmp_db = "/tmp/runtime_perf_bench.db"
    from agents import audit_log, event_bus
    from agents.memory.sqlite_store import SQLiteMemoryStore
    from agents.risk_manager import risk_store
    from agents.runtime import policy_engine, runtime_store, task_queue, workflow_engine
    from agents.sys_admin import sysadmin_store
    from agents.trading_supervisor import supervision_store

    for m in (audit_log, event_bus, risk_store, supervision_store, sysadmin_store, runtime_store):
        m.DB_PATH = tmp_db
        m.init_db()
    store = SQLiteMemoryStore(db_path=tmp_db)
    policy_engine.set_policy("full_auto", changed_by="benchmark", reason="performance_benchmark.py")

    def queue_roundtrip():
        task_queue.enqueue(priority="high", task_type="bench", payload={"x": 1})
        task = runtime_store.claim_next_task()
        runtime_store.complete_task(task["id"])

    def workflow_stage_advance():
        wid = runtime_store.create_workflow(
            workflow_type="promotion", symbol="NIFTY", first_stage=workflow_engine.STAGE_HUMAN_APPROVAL,
            state={"symbol": "NIFTY", "decision": "APPROVED"},
        )
        workflow_engine.advance(wid, memory_store=store, repo_dir=REPO_ROOT)  # human_approval -> execution

    def full_workflow_run():
        wid = runtime_store.create_workflow(
            workflow_type="promotion", symbol="NIFTY", first_stage=workflow_engine.STAGE_HUMAN_APPROVAL,
            state={"symbol": "NIFTY", "decision": "APPROVED"},
        )
        workflow_engine.run_to_completion(wid, memory_store=store, repo_dir=REPO_ROOT)

    def db_ping():
        import sqlite3
        conn = sqlite3.connect(tmp_db)
        conn.execute("SELECT 1").fetchone()
        conn.close()

    from agents.runtime.scheduler import RuntimeScheduler
    scheduler = RuntimeScheduler(repo_dir=REPO_ROOT, memory_store=store, tick_interval_seconds=0)

    def scheduler_tick():
        scheduler.tick()

    results = {
        "task_queue.enqueue+claim+complete (one round trip)": _time_it(queue_roundtrip, repeats=100),
        "workflow_engine.advance (one stage, human_approval->execution)": _time_it(workflow_stage_advance, repeats=50),
        "workflow_engine.run_to_completion (execution->learning->memory_update, full_auto)":
            _time_it(full_workflow_run, repeats=20),
        "sqlite ping (SELECT 1, real file on disk)": _time_it(db_ping, repeats=200),
        "scheduler.tick() (full agent+queue+workflow sweep, all agents already run once)":
            _time_it(scheduler_tick, repeats=5),
    }

    os.remove(tmp_db)
    out = {"generated_at": dt.datetime.now().isoformat(), "results": results}
    out_dir = os.path.join(REPO_ROOT, "runtime_results")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "performance_benchmark.json")
    with open(out_path, "w") as fh:
        json.dump(out, fh, indent=2, default=str)
    print(json.dumps(out, indent=2, default=str))
    print(f"\nWritten to {out_path}")


if __name__ == "__main__":
    main()
