# Milestone 12, Phase 1 — Runtime Scheduler Activation: Implementation Report

## Objective

Wire `RuntimeScheduler.run_forever()` — built in Milestone 9, confirmed by this project's own Milestone 12 planning survey to have **never actually run in production** (no systemd unit, no crontab entry, no `app.py` code path ever started it; the live database has no `ti_paper_trades`/`agent_status` rows at all) — into the real application lifecycle, safely: orchestration, observability, resilience, safe activation. No new prediction models, no new AI/strategy logic, no broker/order-placement code anywhere in this change.

## Architecture

### 1. Application lifecycle wiring

`agents/runtime/lifecycle.py` (new module) owns activation. `app.py`'s existing `SKIP_AUTOSTART`-gated startup block (the same block that already starts `start_all_symbol_loops()`) gains one call:

```python
if runtime_lifecycle.start_scheduler_background(task_starter=socketio.start_background_task):
    log.info("Runtime scheduler activated (Milestone 12, Phase 1).")
```

`start_scheduler_background()`:
1. Checks `config.RUNTIME_SCHEDULER_ENABLED` (new flag, **default `false`**) — returns `False` immediately, logging a clear reason, if unset. This is the first time in this project's history the scheduler would open real (paper) trades and write to the live database unattended, so activation is an explicit, reviewed opt-in, never a side effect of deploying this code.
2. Acquires a single-instance POSIX advisory file lock (`fcntl.flock`, `LOCK_EX | LOCK_NB`) at `config.RUNTIME_SCHEDULER_LOCK_PATH` (default `/tmp/oi_dashboard_runtime_scheduler.lock`). If another process already holds it, logs a warning and returns `False` — never raises, never starts a duplicate.
3. Installs SIGINT/SIGTERM handlers **on the calling (main) thread** — Python's `signal.signal()` raises if called from any other thread.
4. Starts `scheduler.run_forever(install_signal_handlers=False)` via the injected `task_starter` — `socketio.start_background_task()` in production (the same mechanism `app.py`'s own `start_all_symbol_loops()` already uses for its per-symbol data-fetch loops), a plain daemon `threading.Thread` by default (used by tests).
5. Registers an `atexit` hook to release the lock and stop the scheduler on process exit, as a backstop alongside signal-driven shutdown.

### 2. Runtime observability

- **Scheduler heartbeat logging**: `RuntimeScheduler.tick()` emits a DEBUG-level line every cycle; `run_forever()`'s loop emits an INFO-level "scheduler heartbeat: N cycles executed..." line every `HEARTBEAT_LOG_EVERY_N_CYCLES` (12) cycles — frequent enough to confirm liveness in logs without flooding them at the default 5s tick cadence.
- **Startup/shutdown event logs**: reuses the existing `SCHEDULER_STARTED`/`SCHEDULER_STOPPED` events (`runtime_events.py`, already wired into `start()`/`stop()` since Milestone 9) plus new `logger.info()` lines in both `lifecycle.py` (activation-level: enabled/disabled, lock acquired/lost) and `scheduler.py` (loop entered/exited).
- **Exception isolation for each scheduler cycle**: `agent_runtime.run_agent_cycle()` already isolated per-agent failures (Milestone 9) — never propagates into `tick()`. What was missing: an exception in `tick()`'s own *non-agent* code (`task_queue.process_one()`, `workflow_engine.advance()`) used to propagate straight out of `tick()` and would have killed `run_forever()`'s loop. `tick()` now wraps its full body in a try/except, incrementing `_recovered_exceptions` and emitting a new `SCHEDULER_TICK_RECOVERED` event on any escape — the scheduler keeps running either way. `tick()`'s pre-existing return-value contract (e.g. `{"emergency_stop": True}`) is unchanged; only cycle-metrics bookkeeping and resilience are added around it.
- **Single-instance runtime lock**: see above (Section 1, step 2).
- **`/api/runtime/status` health endpoint**: new, admin-gated (`@auth.roles_required("admin")`, matching the sibling `/api/trading-intelligence/overview`/`/api/sysadmin/overview` routes — `app.py`'s own `_verify_all_routes_protected()` fail-closed startup check requires one of the three recognized auth decorators on every route with no exception, so an unauthenticated health endpoint was not an available option in this codebase). Returns `lifecycle.get_runtime_status()`'s payload directly.

### 3. Runtime metrics

`RuntimeScheduler` gained new instance state and a `get_status()` method:

| Field | Source |
|---|---|
| `scheduler_state` | New `_state` attribute: `"stopped"` (initial/terminal) → `"starting"` → `"running"` → `"stopping"` (on `stop()`) → `"stopped"` (once `run_forever()`'s loop actually exits) → `"error"` (only if something escapes even `tick()`'s own isolation, e.g. `start()` itself failing) |
| `cycles_executed` | Incremented once per `tick()` call, regardless of outcome |
| `last_cycle_timestamp` | Set at the end of every `tick()` |
| `next_scheduled_cycle` | `last_cycle_timestamp + tick_interval_seconds` — **honestly the next time the scheduler LOOP wakes up to check for due work**, not a per-agent prediction (each of the 7 registered agents runs on its own independently staggered cadence — see `config.RUNTIME_CADENCE_SECONDS` — so there is no single honest "next agent cycle" to report) |
| `last_cycle_duration_ms` | Wall-clock duration of the most recent `tick()` call |
| `runtime_uptime_seconds` | `now - started_at`, set when `start()` runs |
| `active_jobs` | **Not** scheduler-instance state — computed by `lifecycle.get_runtime_status()` from `agent_runtime.health_snapshot()`'s already-existing (Milestone 9) `currently_running` per-agent tracking, reused rather than duplicated |
| `recovered_exceptions` | Bonus field beyond the requested 7 — how many `tick()` calls recovered from an unexpected non-agent exception; directly useful alongside `scheduler_state` for diagnosing an "error" state |

A scheduler that was never started (disabled, or lock lost to another process) reports an honest `scheduler_state: "stopped"` with zero/`None` metrics — never a fabricated "running" claim.

### 4. Non-blocking execution

This codebase is a **Flask + Flask-SocketIO** application (not FastAPI/Uvicorn/asyncio) run via `gunicorn app:app` in production per `app.py`'s own header comment, or directly via `socketio.run()` for local/Termux use — there is no asyncio event loop to hang off of. The scheduler runs via `socketio.start_background_task()`, Flask-SocketIO's own background-task mechanism (backed by a real thread under `async_mode="threading"`, which this app already uses), matching the one established pattern already used for `start_all_symbol_loops()`'s per-symbol data-fetch threads — not a bare `threading.Thread` invented fresh for this change, and not an async task in a framework that has no asyncio runtime.

Graceful cancellation: `RuntimeScheduler.stop()` sets an internal flag checked at the top of `run_forever()`'s `while` loop — the loop finishes its current tick, then exits and calls `stop()` again (idempotent) inside its own `finally` block, transitioning to `scheduler_state: "stopped"`.

## Files changed

- **New**: `agents/runtime/lifecycle.py` — activation, singleton lock, `get_runtime_status()`.
- **New**: `scripts/runtime/m12_phase1_sustained_validation.py` — the sustained validation run script (see `M12_PHASE1_VALIDATION.md`).
- **New**: `test_agents/runtime/test_scheduler_lifecycle.py` — 22 tests.
- **Modified**: `agents/runtime/scheduler.py` — `_state`/`_cycles_executed`/`_recovered_exceptions`/`_last_cycle_ts`/`_last_cycle_duration_ms`/`_started_at` instance attributes; `get_status()`; `tick()` restructured for exception isolation + metrics (return-value contract for its existing cases unchanged); `run_forever()` gains `install_signal_handlers: bool = True` (default preserves the only prior caller's behavior, the module's own `__main__` block) plus heartbeat logging and an outer `"error"`-state safety net; heartbeat/debug logging added throughout.
- **Modified**: `agents/runtime/runtime_events.py` — one new event type, `SCHEDULER_TICK_RECOVERED`.
- **Modified**: `agents/config.py` — `RUNTIME_SCHEDULER_ENABLED` (default `false`), `RUNTIME_SCHEDULER_LOCK_PATH`.
- **Modified**: `app.py` — one new import, one new admin-gated route (`/api/runtime/status`), one new call in the existing `SKIP_AUTOSTART` startup block.

No database schema changed. No file belonging to `agents/trading_intelligence/` was touched — this phase only activates the *dispatcher* that already calls `agents.trading_intelligence.api.run_scheduled_cycle()` (Milestone 9/10's own existing, already-tested entrypoint); nothing about how that engine makes decisions changed.

## Constraints honored

- **No broker live-order execution / no real-money trading / no SmartAPI order placement**: unchanged from before this phase. Re-confirmed by this milestone's own planning survey (structural AST/import-based trace of every one of the 7 registered agent cycles found zero references to `SmartConnect`/`AngelOneFetcher`/`app._shared_angel_fetcher` anywhere in `agents/`) and by `test_agents/trading_intelligence/test_safety.py`'s existing AST scan, which this change does not touch.
- **No schema changes**: none were needed. Scheduler-level metrics (`cycles_executed`, `scheduler_state`, etc.) are in-memory, tied to the current process's lifetime — deliberately, since "cycles executed since this process started" resetting to 0 on restart is the honest behavior, not a gap to paper over with a persisted counter. Per-agent execution tracking (`currently_running`, `last_execution_ts`, `failure_counter`, `health_score`) already existed in full from Milestone 9's `agent_status` table — reused via `agent_runtime.health_snapshot()`, not duplicated.
- **No new AI models, prediction engines, or strategy-generation logic**: this phase is pure orchestration/observability/resilience wiring around already-existing, already-tested agent entrypoints.
- **All Milestone 11 regression tests remain green**: confirmed — see Test statistics below.

## Test statistics

- New module suite (`test_scheduler_lifecycle.py`): **22/22 passed.**
- Full `test_agents/runtime/` suite: **154/154 passed** (up from 132 before this phase).
- Full repository suite: **1,391 passed, 1 xfailed** (up from 1,369 at the end of Milestone 11), **zero regressions**.

Coverage by the six required test categories:
1. **Scheduler starts automatically on app startup** — `TestStartSchedulerBackground` (wiring: flag off is a no-op; flag on calls the injected task starter with the correct function/kwargs) + `TestFullLoopIntegration` (a real background thread actually runs `run_forever()` and executes real cycles). `app.py`'s own startup block was verified separately by a subprocess-isolated `SKIP_AUTOSTART=1 python3 -c "import app"` smoke check (this codebase's established convention for touching `app.py` at all — it is never imported directly inside the pytest suite, to avoid any risk of triggering its live broker-session machinery).
2. **Cycles execute repeatedly** — `TestFullLoopIntegration` polls `cycles_executed` climbing past 2 within a bounded timeout on a real thread.
3. **Shutdown is graceful** — same test: `stop()` + `thread.join(timeout=...)` confirms the thread actually exits and `scheduler_state` reaches `"stopped"`.
4. **Duplicate scheduler instances are prevented** — `TestSingletonLock` (direct lock acquire/release/re-acquire) + `TestStartSchedulerBackground::test_a_lost_lock_race_is_a_graceful_no_op` (simulates a second OS process already holding the lock).
5. **`/api/runtime/status` returns live runtime data** — tested at the `lifecycle.get_runtime_status()` level (`TestSchedulerStatus`, `TestRuntimeStatusActiveJobs`) rather than through a Flask test client, consistent with this codebase's established boundary of never importing `app.py` inside the test suite; the route itself is a 3-line wrapper calling this exact function, and was verified end-to-end during the sustained validation run (see `M12_PHASE1_VALIDATION.md`).
6. **Scheduler survives an exception in one cycle and continues running** — `TestTickExceptionIsolation` (a real, injected `workflow_engine.advance()` failure is recovered, counted, and emitted as an event; a second tick immediately afterward still completes normally) — and, independently, genuinely reproduced during the real 16-minute sustained validation run itself (see below).

## Performance impact

- Default (`RUNTIME_SCHEDULER_ENABLED=false`) path: zero. `start_scheduler_background()` returns `False` after one cheap config check; nothing else in this phase executes.
- Enabled path: one additional background thread per process, ticking at 5s intervals (configurable) when the market is open, backing off to a capped sleep (`min(60s, seconds_until_next_open())`) outside market hours — the exact cadence already designed and tested in Milestone 9, now actually reachable. No new per-cycle database reads beyond what `agent_runtime.run_agent_cycle()` already performs for each due agent.

## Risks

- **This is the first real activation path for a system that has never run in production.** Mitigated by the default-off flag, the explicit review gate this report itself represents, and the sustained validation run (see `M12_PHASE1_VALIDATION.md`) proving real, multi-minute stability before any recommendation to enable it live.
- **`risk_manager`'s cycle is expected to fail against a database missing the `paper_orders`/`users` tables its `data_access` layer reads** (an `app.py`-owned schema, not something Milestone 9's own `risk_store`/`runtime_store` `init_db()` calls create) — already a documented characteristic of this codebase's own test fixtures (`test_agents/runtime/conftest.py`'s `risk_data_access_db` fixture docstring states this explicitly), not something Phase 1 introduces. Against the real production `oi_history.db` (which does have these tables), this would not occur.
- **`agents/runtime/scheduler.py` is a working Milestone 9 module this phase modifies directly** — done via purely additive changes (new attributes, a new method, an additive keyword-only parameter with a default preserving the sole existing caller's behavior) with the full pre-existing `test_scheduler.py` suite re-verified passing unmodified (132 → 154 total in the package, zero prior tests altered).

## Commit hash

`7b49063` on `worktree-m12-phase1-scheduler` (code, script, tests, and sustained-run output, all in one commit).
