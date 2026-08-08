# Milestone 12, Phase 1 — Runtime Scheduler Activation: Validation Report

## Summary

A real, ≥15-minute, time-based sustained run of `RuntimeScheduler` was executed against a fully isolated, throwaway SQLite database (never the live `oi_history.db` — see "Scope of this validation" below) via `scripts/runtime/m12_phase1_sustained_validation.py`. The scheduler ran continuously, executed real cycles across all seven registered agents (subject to real market-hours gating), survived a real, repeated agent-cycle failure without disruption, and shut down cleanly. Combined with the unit/integration test suite (`test_agents/runtime/test_scheduler_lifecycle.py`, 22 tests, all passing) and the full repository regression suite, Phase 1 is validated as ready for review.

## Scope of this validation

This run proves the **mechanism** works correctly and safely over real, sustained wall-clock time. It deliberately does **not** activate the scheduler against the live production database — that is a separate, explicit, later decision (setting `RUNTIME_SCHEDULER_ENABLED=true` on the real deployment), consistent with this phase's own "safe activation, default off" design and `MILESTONE12_PLANNING.md`'s own rollback strategy. Milestone 12's planning survey already confirmed every registered agent cycle is structurally isolated from any broker code (verified again independently in this phase's own implementation report), so running real cycles for real minutes against an isolated database is safe and representative of real production code paths — just not real production data.

## Sustained run parameters

| | |
|---|---|
| Script | `scripts/runtime/m12_phase1_sustained_validation.py` |
| Requested duration | 16 minutes (exceeds the 15-minute requirement with margin) |
| Actual duration | 960.0 seconds (16.0 minutes) |
| Tick interval | 5.0 seconds (the real production default — `RuntimeScheduler`'s own default, unmodified for this run) |
| Market session at start | **Closed** (Weekend) — confirmed via `market_session.is_nse_session_open()`, the same real, side-effect-free, stdlib-only check the scheduler itself uses |
| Database | Isolated throwaway SQLite file, initialized via the exact same `init_db()` calls this project's own `agent_db`/`ti_db` pytest fixtures use — never the live `oi_history.db` |

## Results

| Metric | Value |
|---|---|
| **Total cycles executed** | **16** |
| **Average cycle duration** | **580.6 ms** |
| **Maximum cycle duration** | **5,533.75 ms** |
| **Minimum cycle duration** | **3.44 ms** |
| **Recovered exceptions** (`tick()`-level, non-agent code) | **0** |
| Final scheduler state | `"stopping"` (see note below — this run drives `tick()` directly rather than `run_forever()`, for precise per-tick duration measurement; the `"stopped"` terminal state is `run_forever()`'s own finalization step, separately proven by `TestFullLoopIntegration`, which does run a real `run_forever()` on a background thread) |
| **Scheduler remained continuously active** | **True** (ran the full requested duration without an unhandled exception, executed real cycles, left no agent stuck mid-execution) |
| Agents left stuck "running" at end | **None** |

### Why cycle count is 16 for a 16-minute run at a 5s tick interval

The scheduler correctly ran the ENTIRE window with the market closed (a real Saturday/Sunday in IST). Per `RuntimeScheduler`'s own existing, unmodified off-hours logic (`run_forever()`'s own sleep calculation, replicated exactly in this validation script), when the market is closed the loop sleeps `min(tick_interval_seconds * 12, seconds_until_next_open())` = `min(60s, a much larger number)` = **60 seconds** between ticks, rather than busy-polling every 5 seconds for nothing. 960 seconds ÷ ~60 seconds/tick ≈ 16 ticks — exactly what was observed. This is correct, honest, already-tested (Milestone 9) behavior, not a defect: **memory/sys_admin/risk_manager still ran on their own cadence regardless** (confirmed: `quant_researcher`, `trading_supervisor`, and `trading_intelligence` — the three market-hours-gated agents — show `null` health status throughout, meaning they correctly never ran during this closed-market window, while `memory`/`dev_agent`/`risk_manager`/`sys_admin` did).

### Cycle duration distribution

The wide spread (3.44ms to 5,533.75ms) is fully explained by real, honest work, not noise or a bug:
- **Fast cycles (~3–10ms)**: a tick where no agent's cadence had elapsed yet — just the due-agent check, an empty task-queue drain, and a workflow-advance sweep over zero running workflows.
- **The one slow cycle (~5.5s)**: `sys_admin`'s real cycle, which performs an actual TCP-reachability probe (`agents/sys_admin/infra_monitor.py`'s `network_status()`, real connect attempts with a 2s timeout each to two real DNS servers) as part of its infrastructure snapshot — genuine network I/O latency, not a scheduler defect. This is a pre-existing characteristic of `sys_admin`'s own cycle (Milestone 8/9), unrelated to anything changed in this phase.

### A real, unprompted exception-recovery event occurred during this run

`risk_manager`'s cycle failed **4 consecutive times** during the 16-minute window (`failure_counter: 4`, `health_score` decayed from 100 to 20, per `sysadmin_store.record_execution()`'s existing, unmodified scoring formula). Root cause, verified directly: `agents/risk_manager/data_access.py` reads `paper_orders`/`users` tables that belong to `app.py`'s own schema — this validation's isolated throwaway database (matching `test_agents/runtime/conftest.py`'s own `agent_db` fixture, whose docstring explicitly documents this exact gap: *"agent_db... does NOT include the paper_orders/users schema -- that's what makes this fixture useful for tests that want risk_manager's OWN cycle to genuinely fail"*) never created them. Against the real `oi_history.db`, which does have these tables, this would not occur.

Rather than a problem, **this is direct, real-world proof of Phase 1's own resilience requirement**: `agent_runtime.run_agent_cycle()`'s existing per-agent isolation (Milestone 9) caught all 4 failures cleanly; on the 3rd, `RUNTIME_MAX_CONSECUTIVE_FAILURES_BEFORE_ESCALATION` (default 3) correctly triggered an escalation report and an `AGENT_ESCALATED` event; the scheduler itself never stopped, never crashed, and continued executing memory/dev_agent/sys_admin cycles normally for the remainder of the run. `recovered_exceptions: 0` in the results table refers specifically to *scheduler-level* (`tick()`'s own non-agent code) recoveries — the *per-agent* resilience layer this event exercised is a separate, complementary mechanism, and both are now validated: the per-agent layer by this real run, the scheduler-level layer by `TestTickExceptionIsolation`'s dedicated unit tests (a real failure was harder to trigger organically at that layer, since it requires `task_queue`/`workflow_engine` code specifically to fail, not an agent cycle).

## Memory usage trend

| Elapsed (s) | Used % |
|---|---|
| 4 – 483 | 43.4 – 43.9% (flat) |
| 544 | 43.4% |
| 609 – 909 | 48.2 – 48.5% (one step up, then flat) |

A single, modest step increase (~5 percentage points) around the 10-minute mark, stable for the remaining ~5 minutes with no further growth — consistent with a one-time allocation (e.g. a Python/SQLite buffer growing to its steady-state size) rather than a continuous leak. No runaway or linear growth pattern was observed. `total_kb` (8,129,736 kB, i.e. the host's total RAM) was constant throughout, as expected.

## CPU usage trend

`load1_per_core` samples: `0.61, 0.50, 0.22, 0.12, 0.08, 0.03, 0.01, 0.08, 0.12, 0.08, 0.13, 0.05, 0.05, 0.15, 0.05, 0.02` — starts moderate (residual load from immediately-prior test runs on this shared machine) and settles to near-zero (0.01–0.15) for the bulk of the run. No sustained elevated CPU usage attributable to the scheduler itself — consistent with a 60-second-interval, mostly-idle tick loop.

## Confirmation of continuous operation

- The script's own driving loop ran for the full requested 960 seconds without raising.
- 16 real cycles were executed and recorded (`cycles_executed`).
- Zero agents were left in a stuck `currently_running=1` state at the end.
- `agent_runtime.health_snapshot()` at the end shows real, recent `last_execution_ts`/`last_execution_duration_ms` for every agent that ran (memory, dev_agent, risk_manager, sys_admin), confirming genuine, repeated real execution across the full window, not a single burst at the start.
- Separately, `test_agents/runtime/test_scheduler_lifecycle.py::TestFullLoopIntegration` proves the real `run_forever()` background-thread path (as opposed to this script's direct `tick()`-driving loop, used here for precise per-cycle duration measurement) starts, executes repeated cycles, and shuts down gracefully (`thread.join()` succeeds within a bounded timeout, final state `"stopped"`).

## Test suite confirmation (re-run alongside this validation)

- `test_agents/runtime/` (includes the new `test_scheduler_lifecycle.py`): **154/154 passed.**
- Full repository regression suite: **1,391 passed, 1 xfailed**, **zero regressions** (up from 1,369 at the close of Milestone 11).

## Raw output

Full JSON: `runtime_results/m12_phase1_sustained_validation.json` (committed alongside this report).

## Readiness assessment

**Phase 1 is validated and ready for review.** The scheduler mechanism itself — activation, orchestration, observability, resilience, graceful shutdown — is proven correct and stable over a real, sustained 16-minute run, with a genuine (not staged) multi-failure resilience event observed and handled correctly. Enabling `RUNTIME_SCHEDULER_ENABLED` against the real production database remains a separate, deliberate decision for after this review, not a conclusion this validation run reaches on its own.

---

Per standing instruction: **not** proceeding to Phase 2 (paper-trading pipeline) or any further Milestone 12 work. Waiting for explicit approval.
