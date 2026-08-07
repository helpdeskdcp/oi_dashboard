"""
agents/runtime/api.py -- "Runtime Dashboard. Display: Running agents,
Queue status, Current workflow, Market status, Runtime events, Errors,
Warnings, Health score, Memory usage, CPU, Latency."

Extends the EXISTING Operations Dashboard (agents/sys_admin/api.py's
get_overview(), templates/sysadmin.html) with a "runtime" section rather
than building a second dashboard -- get_runtime_overview() below is
wired into agents.sys_admin.api.get_overview() as one more key, the same
way every other section there already is.
"""
from .. import event_bus
from ..sys_admin import infra_monitor
from . import agent_runtime, market_session, policy_engine, runtime_store, task_queue


def get_runtime_overview(*, db_path: str = "oi_history.db", since_ts: str | None = None) -> dict:
    market_open, market_reason = market_session.is_nse_session_open()
    infra = infra_monitor.snapshot(db_path=db_path, check_network=False)
    recent_events = event_bus.events_since(since_ts) if since_ts else []
    return {
        "policy": policy_engine.get_active_policy(),
        "market": {"open": market_open, "reason": market_reason},
        "agents": agent_runtime.health_snapshot(),
        "queue": task_queue.status(),
        "workflows_running": runtime_store.list_workflows(status="running", limit=20),
        "workflows_waiting_approval": runtime_store.list_workflows(status="waiting_approval", limit=20),
        "recent_events": recent_events,
        "infrastructure": {
            "cpu": infra.cpu, "memory": infra.memory, "sqlite": infra.sqlite, "thread_count": infra.thread_count,
        },
    }


def get_workflow_detail(workflow_id: int) -> dict | None:
    workflow = runtime_store.get_workflow(workflow_id)
    if workflow is None:
        return None
    return {**workflow, "history": runtime_store.workflow_history(workflow_id)}


def get_queue_detail() -> dict:
    return {
        "depth": task_queue.status(),
        "retrying": task_queue.retry_queue(limit=20),
        "failed": task_queue.failed_queue(limit=20),
    }
