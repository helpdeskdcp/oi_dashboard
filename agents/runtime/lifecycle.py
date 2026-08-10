"""
agents/runtime/lifecycle.py -- Milestone 12, Phase 1: Runtime Scheduler
Activation.

Wires RuntimeScheduler.run_forever() (built in Milestone 9, never
actually invoked in production -- confirmed by this milestone's own
planning survey: no systemd unit, no crontab entry, and no app.py code
path ever started it; the only invocation anywhere in the repo was
scheduler.py's own `if __name__ == "__main__"` block) into the real
application lifecycle, safely:

- OFF by default (config.RUNTIME_SCHEDULER_ENABLED). This is the first
  time in this project's history the scheduler would open real (paper)
  trades and write to the live database unattended -- activation must be
  an explicit, reviewed opt-in, never a side effect of deploying this
  code.
- A single-instance POSIX advisory file lock (fcntl.flock, LOCK_EX |
  LOCK_NB) prevents two OS processes -- two gunicorn workers, or an old
  process still shutting down during a restart/reload -- from both
  running the loop at once. app.py's own header comment documents
  gunicorn as the production WSGI server, so this is a real, not
  hypothetical, concern. The lock is released automatically on process
  exit (the OS releases flock()s when the holding file descriptor
  closes, including on process death) -- no stale-lock cleanup logic is
  needed, unlike a PID-file scheme.
- Runs via an injectable `task_starter` callable, defaulting to a daemon
  threading.Thread. app.py passes socketio.start_background_task -- the
  SAME background-task mechanism this codebase's own
  start_all_symbol_loops() already uses for its per-symbol data-fetch
  loops -- for consistency with the one established pattern for
  long-running background work inside this Flask process.
- Graceful shutdown: signal handlers (SIGINT/SIGTERM) are installed on
  the calling (main) thread by THIS module before the background task
  starts -- Python's signal.signal() raises if called from any thread
  but the main one, so RuntimeScheduler.run_forever() itself is invoked
  with install_signal_handlers=False on the background thread to avoid a
  wrong-thread registration attempt.

Never imports app.py and never imports anything from
agents.trading_intelligence directly (only agents.runtime.agent_runtime,
which itself only reaches trading_intelligence through its own already-
audited, broker-isolated entrypoint) -- matching every other module in
this package.
"""
import atexit
import fcntl
import logging
import os
import subprocess
import threading
import time

import runtime_paths
from .. import config
from . import agent_runtime, policy_engine, scheduling_control
from .scheduler import RuntimeScheduler

logger = logging.getLogger("oi_dashboard.runtime.lifecycle")

_state_lock = threading.Lock()
_scheduler: RuntimeScheduler | None = None
_lock_file = None  # the open file object holding the advisory lock, kept open for the process's lifetime

# Milestone 14 post-deployment hardening: captured once, at import time
# (early in this process's real startup sequence) -- uptime_seconds
# below is measured against this, not against os.getpid()'s own kernel
# start time (which would need /proc parsing or a new psutil dependency
# for marginal extra precision this operational use case doesn't need).
_PROCESS_STARTED_AT = time.time()


def _default_task_starter(func, *args, **kwargs) -> None:
    threading.Thread(target=func, args=args, kwargs=kwargs, daemon=True).start()


def _acquire_singleton_lock(path: str):
    """Returns an open file object holding an exclusive, non-blocking
    advisory lock on `path`, or None if another process already holds
    it. Safe to call repeatedly/concurrently -- only ONE caller across
    all processes on this machine ever gets a non-None result for the
    same path at the same time."""
    lock_dir = os.path.dirname(path)
    if lock_dir:
        os.makedirs(lock_dir, exist_ok=True)
    fh = open(path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def _release_singleton_lock(fh) -> None:
    if fh is None:
        return
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def start_scheduler_background(*, task_starter=None, tick_interval_seconds: float = 5.0) -> bool:
    """Starts the runtime scheduler as a background task, IF
    config.RUNTIME_SCHEDULER_ENABLED is set AND this process wins the
    single-instance lock. Returns True if the scheduler was actually
    started by THIS call, False otherwise (disabled, another process
    already holds the lock, or this process already started one) --
    never raises, matching this framework's "activation must degrade
    honestly, never crash the app" discipline; app.py's own startup
    sequence calls this unconditionally and does not need to guard it.

    `task_starter(func, *args, **kwargs)`: runs `func(*args, **kwargs)`
    in the background. Defaults to a daemon threading.Thread; app.py
    passes socketio.start_background_task. Tests inject a fake or a
    thread-based starter with a short `tick_interval_seconds` to avoid
    depending on Flask-SocketIO and to keep test runtime short."""
    global _scheduler, _lock_file

    if not config.RUNTIME_SCHEDULER_ENABLED:
        logger.info("runtime scheduler activation skipped -- RUNTIME_SCHEDULER_ENABLED is not set")
        return False

    with _state_lock:
        if _scheduler is not None:
            logger.info("runtime scheduler already started in this process -- skipping duplicate start")
            return False

        lock_fh = _acquire_singleton_lock(config.RUNTIME_SCHEDULER_LOCK_PATH)
        if lock_fh is None:
            logger.warning(
                "runtime scheduler NOT started -- another process already holds the singleton lock at %s",
                config.RUNTIME_SCHEDULER_LOCK_PATH,
            )
            return False

        scheduler = RuntimeScheduler(tick_interval_seconds=tick_interval_seconds)
        scheduler.install_signal_handlers()  # main thread only -- must happen here, before the background task starts

        starter = task_starter or _default_task_starter
        starter(scheduler.run_forever, install_signal_handlers=False)

        _scheduler = scheduler
        _lock_file = lock_fh
        atexit.register(stop_scheduler_background)
        logger.info("runtime scheduler started (pid=%s, lock=%s)", os.getpid(), config.RUNTIME_SCHEDULER_LOCK_PATH)
        return True


def stop_scheduler_background() -> None:
    """Graceful shutdown -- signals the scheduler loop to stop after its
    current tick and releases the singleton lock. Safe to call even if
    the scheduler was never started (a no-op) and safe to call more than
    once (idempotent) -- both app.py's signal-driven shutdown path and
    this module's own atexit hook may call it."""
    global _scheduler, _lock_file
    with _state_lock:
        if _scheduler is not None:
            _scheduler.stop()
            _scheduler = None
        if _lock_file is not None:
            _release_singleton_lock(_lock_file)
            _lock_file = None


def get_runtime_status() -> dict:
    """The full Milestone 12 Phase 1 runtime-health payload --
    /api/runtime/status's own data source. Never raises: a scheduler
    that was never started (disabled, or lock lost to another process)
    reports an honest "stopped" state with zero/None metrics, never a
    fabricated "running" claim.

    "active_jobs": reuses agent_runtime.health_snapshot() (Milestone 9's
    own already-populated per-agent currently_running tracking, which
    stays accurate regardless of whether THIS scheduler instance is the
    one that ran them) rather than tracking a second, parallel concept
    of "what's running right now."

    Milestone 12, Phase 1.1 hotfix: health_snapshot() reads the
    agent_status table, which doesn't exist yet on a genuinely
    uninitialized database (true for this project's own live database as
    of Phase 1's own post-merge verification) -- previously this raised
    straight out of get_runtime_status(), meaning /api/runtime/status
    would 500 instead of returning the honest scheduler-level status this
    function otherwise already computes correctly. active_jobs now
    degrades to None (honestly "unknown," never a fabricated 0 -- 0 would
    claim "checked, nothing running," which isn't true here) rather than
    taking the whole response down with it."""
    with _state_lock:
        scheduler = _scheduler
    if scheduler is None:
        status = {
            "scheduler_state": "stopped", "cycles_executed": 0, "recovered_exceptions": 0,
            "last_cycle_timestamp": None, "next_scheduled_cycle": None,
            "last_cycle_duration_ms": None, "runtime_uptime_seconds": None,
            "average_cycle_duration_ms": None, "last_successful_cycle": None,
            "last_failed_cycle": None, "consecutive_failures": 0,
            "circuit_state": "closed", "circuit_consecutive_failures": 0,
        }
    else:
        status = scheduler.get_status()

    try:
        snapshot = agent_runtime.health_snapshot()
        status["active_jobs"] = sum(1 for s in snapshot.values() if s and s.get("currently_running"))
    except Exception:
        logger.exception("failed to read agent health snapshot for active_jobs -- degrading honestly")
        status["active_jobs"] = None

    # Milestone 12, Phase 2 Foundation: the operator control-plane view
    # -- global policy (the "pause everything" kill switch) and each
    # agent's own schedulability/mode. Same degrade-honestly-never-raise
    # discipline as active_jobs above: this is genuinely new surface
    # reading tables that may not exist yet on an uninitialized database.
    try:
        status["control"] = {
            "active_policy": policy_engine.get_active_policy(),
            "emergency_stop": policy_engine.is_emergency_stop(),
            "agents": scheduling_control.snapshot(),
        }
    except Exception:
        logger.exception("failed to read operator control-plane state -- degrading honestly")
        status["control"] = None

    status["deployment"] = get_deployment_status()
    return status


def _git_commit() -> str | None:
    """Best-effort, never raises -- a deployment without git available
    (or not a git checkout at all) degrades to None, not a 500."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=runtime_paths.APP_ROOT,
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


def _supervisor_detected() -> dict:
    """Milestone 14 post-deployment hardening: confirms the REAL
    supervisor (run_forever_vps.sh) owns this process, not just that
    SOME parent process exists. Cross-checks two independent signals --
    this process's own os.getppid() against the PID run_forever_vps.sh
    itself wrote to runtime_paths.PID_FILE via `echo $$ > run_forever.pid`
    -- verified equal for the real live process during the Milestone 14
    deployment this module's own docstring context describes. Read-only,
    never raises -- a missing/unreadable PID file degrades to
    "detected": False with an honest reason, never a guess."""
    ppid = os.getppid()
    try:
        with open(runtime_paths.PID_FILE) as f:
            supervisor_pid = int(f.read().strip())
    except (OSError, ValueError):
        return {"detected": False, "reason": f"could not read {runtime_paths.PID_FILE}", "parent_pid": ppid}

    if supervisor_pid != ppid:
        return {
            "detected": False,
            "reason": f"parent pid {ppid} does not match run_forever.pid's recorded {supervisor_pid} "
                      f"-- this process may not be supervised by run_forever_vps.sh",
            "parent_pid": ppid,
        }
    return {"detected": True, "supervisor_pid": supervisor_pid, "parent_pid": ppid}


def get_deployment_status() -> dict:
    """Milestone 14 post-deployment hardening: the process/deployment-
    level facts an operator needs to confirm a restart actually took --
    which PID is live, whether the real supervisor owns it, how long
    it's been up, which exact database file it's reading, what code
    version is deployed. Deliberately does NOT read anything from
    agents.intelligence_history (app.py's own /api/runtime/status route
    merges in last_snapshot_ts/snapshot_write_lag_seconds itself) --
    this module's own docstring already establishes "never imports
    anything from agents.trading_intelligence directly... matching
    every other module in this package"; keeping this function's own
    dependencies to just runtime_paths/subprocess/os respects that same
    narrow-scope discipline rather than adding a new cross-package
    dependency here. Read-only, degrades honestly (None) on any
    failure -- never a fabricated number."""
    return {
        "app_version": config.APP_VERSION,
        "environment": config.ENVIRONMENT,
        "git_commit": _git_commit(),
        "pid": os.getpid(),
        "supervisor": _supervisor_detected(),
        "uptime_seconds": round(time.time() - _PROCESS_STARTED_AT, 1),
        "database_path": runtime_paths.DATABASE_PATH,
    }
