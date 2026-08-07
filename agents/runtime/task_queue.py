"""
agents/runtime/task_queue.py -- "Implement priority queues. High.
Medium. Low. Retry Queue. Failed Queue. Support retries and timeout
protection."

Priority/retry/failed are all ONE table (runtime_store.runtime_task_queue,
status in queued/running/retrying/dead) with different WHERE clauses --
matching this codebase's "one SQLite file, query it differently for
different views" convention (agent_audit_log's own pending/approved/
rejected/applied/rolled_back states work the same way) rather than four
separate physical queues.

Timeout protection: a worker function is run in a bounded
ThreadPoolExecutor call (stdlib only, same real-concurrency tool
test_agents/hardening's stress tests already use) -- a worker that hangs
past config.RUNTIME_TASK_TIMEOUT_SECONDS is treated as a failure (and
retried/dead-lettered like any other failure), not left to block the
queue forever. The underlying thread itself is NOT forcibly killed
(Python has no safe way to do that) -- it's abandoned to finish or die
on its own while the queue moves on, same trade-off every
ThreadPoolExecutor-based timeout in the stdlib has.
"""
import concurrent.futures
import traceback

from .. import config
from . import runtime_store

_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="runtime-task")


def enqueue(*, priority: str, task_type: str, payload: dict | None = None, max_attempts: int | None = None) -> int:
    return runtime_store.enqueue(
        priority=priority, task_type=task_type, payload=payload,
        max_attempts=max_attempts or config.RUNTIME_TASK_MAX_ATTEMPTS,
    )


def process_one(handlers: dict, *, timeout_seconds: float | None = None) -> dict | None:
    """Claims the next due task and runs handlers[task["task_type"]]
    (payload) -> None inside a bounded-time call. Returns a result dict
    ({"task_id", "task_type", "outcome": "completed"|"retrying"|"dead",
    ...}) or None if the queue is empty. `handlers` is supplied by the
    caller (agents/runtime/scheduler.py) rather than hardcoded here, so
    this module has zero knowledge of what a "task" actually does --
    the same separation runtime_store.py already keeps from the agents
    it stores state for."""
    task = runtime_store.claim_next_task()
    if task is None:
        return None

    handler = handlers.get(task["task_type"])
    if handler is None:
        outcome = runtime_store.fail_task(
            task["id"], error=f"no handler registered for task_type={task['task_type']!r}",
            retry_backoff_seconds=config.RUNTIME_TASK_RETRY_BACKOFF_SECONDS,
        )
        return {"task_id": task["id"], "task_type": task["task_type"], "outcome": outcome}

    timeout = timeout_seconds if timeout_seconds is not None else config.RUNTIME_TASK_TIMEOUT_SECONDS
    future = _EXECUTOR.submit(handler, task["payload_json"])
    try:
        future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        outcome = runtime_store.fail_task(
            task["id"], error=f"timed out after {timeout}s",
            retry_backoff_seconds=config.RUNTIME_TASK_RETRY_BACKOFF_SECONDS,
        )
        return {"task_id": task["id"], "task_type": task["task_type"], "outcome": outcome, "timed_out": True}
    except Exception as exc:
        outcome = runtime_store.fail_task(
            task["id"], error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
            retry_backoff_seconds=config.RUNTIME_TASK_RETRY_BACKOFF_SECONDS,
        )
        return {"task_id": task["id"], "task_type": task["task_type"], "outcome": outcome}

    runtime_store.complete_task(task["id"])
    return {"task_id": task["id"], "task_type": task["task_type"], "outcome": "completed"}


def status() -> dict:
    return runtime_store.queue_depth()


def failed_queue(*, limit: int = 50) -> list:
    return runtime_store.list_tasks(status="dead", limit=limit)


def retry_queue(*, limit: int = 50) -> list:
    return runtime_store.list_tasks(status="retrying", limit=limit)
