# Milestone 12 — Phase 2 Foundation Work: Implementation & Validation Report

**Scope approved:** Operator-facing Kill Switch + Agent Scheduling Controls, exactly as specified in the Phase 2 Foundation approval message. No Shadow Mode, Passive Mode, or Autonomous Mode work was started. No broker integration, order-placement, or scheduler-enablement changes were made.

Branch: `worktree-m12-phase2-foundation`, based on `master` at `c1df745` (Phase 1.1 post-merge verification report).

---

## 1. Design Summary

Two capabilities were added, both building on infrastructure Milestone 9-12 already established rather than inventing a new mechanism:

### 1.1 Operator-facing Kill Switch (global)

Reuses `agents/runtime/policy_engine.py`'s existing `EMERGENCY_STOP` policy as the global kill switch. `policy_engine.set_policy(EMERGENCY_STOP, ...)` was already the one thing `RuntimeScheduler.tick()` checks *first*, before anything else (`if policy_engine.is_emergency_stop(): result = {"emergency_stop": True}` — no agent runs, no workflow advances, no task drains). This lever already existed and was already durable (backed by `runtime_store`'s `runtime_policy` table) and already audited (`sysadmin_report` on every change) — Phase 2 Foundation's job was to make it *best-effort observable* and *operator-reachable*, not to build a new mechanism:

- `policy_engine.set_policy()` now also emits a best-effort `POLICY_CHANGED` runtime event (`agents/runtime/policy_engine.py`), so pausing/resuming shows up in the same event stream every other runtime action does.
- `runtime_control_cli.py` (new, top-level) gives an operator a `pause`/`resume` command instead of requiring a raw Python shell call.

### 1.2 Agent Scheduling Controls (per-agent)

A new module, `agents/runtime/scheduling_control.py`, adds a second, narrower kind of control: per-agent scheduling eligibility and mode, sitting *inside* `RuntimeScheduler._due_agents()`, checked after the global emergency-stop gate.

Two deliberately separate mechanisms inside it, because they answer different questions and need different guarantees:

1. **"Is this agent even eligible to be scheduled, ever?"** — `SCHEDULABLE_AGENTS` / `NEVER_SCHEDULABLE_AGENTS` / `is_schedulable()`. This is a **hard, code-level Python `frozenset` constant**, not a database row:
   ```python
   NEVER_SCHEDULABLE_AGENTS = frozenset({"trading_intelligence", "quant_researcher"})
   SCHEDULABLE_AGENTS = tuple(a for a in agent_runtime.RUNTIME_AGENT_NAMES if a not in NEVER_SCHEDULABLE_AGENTS)
   ```
   `set_mode()` raises `ValueError` for either agent under **any** requested mode, including `"enabled"` — there is no code path in this module that can make either agent schedulable. This was a deliberate choice over a database flag: a mutable flag can always be flipped by a bug, a bad row, or a future caller who doesn't know better; a module-level constant checked in code cannot be overridden by any operator action this module itself exposes.

2. **"For an agent that IS schedulable, what mode is it in?"** — `get_mode()` / `set_mode()`, backed by a new `schedule_mode` column on the existing `agent_status` table (`agents/sys_admin/sysadmin_store.py`, added via the table's existing self-migrating `PRAGMA table_info()` + `ALTER TABLE` pattern — no new table, no manual migration step). Three modes: `enabled` (runs normally), `disabled` (never runs), `dry_run` (scheduler logs "would have run" and reports it, but never calls `run_agent_cycle()` — scheduling metadata only, explicitly **not** Phase 2A Shadow Mode execution).

### 1.3 Why a new module instead of extending `orchestrator.py`

`agents/sys_admin/orchestrator.py` (Milestone 8) already has its own enable/disable mechanism, but it is explicitly scoped to four Milestone 2-7 agents (`dev_agent`, `quant_researcher`, `risk_manager`, `trading_supervisor`) and doesn't cover `memory`, `sys_admin`, or `trading_intelligence` at all. `agent_runtime.py`'s own module docstring already establishes the precedent this decision follows: a new Milestone-12-scoped runtime concern gets a new, purpose-built module rather than stretching a previous milestone's file past its documented scope. `scheduling_control.py` applies uniformly to every entry in `agent_runtime.RUNTIME_AGENT_NAMES`, which `orchestrator.py` was never designed to do.

### 1.4 Why the "never schedulable" guard lives in the scheduler, not in `run_agent_cycle()`

`agent_runtime.run_agent_cycle()` is invoked directly (not just via the scheduler) by existing, passing tests — including `test_trading_intelligence_cycle_opens_a_paper_trade_from_a_real_buy_signal`, which legitimately expects `trading_intelligence` to execute when called directly. Putting the guard inside `run_agent_cycle()` itself would have broken that legitimate direct-invocation use case. Instead, the guard lives in exactly the two places an *autonomous scheduling* decision is made: `RuntimeScheduler._due_agents()` (checks `scheduling_control.is_schedulable()` first, before cadence) and `scheduling_control.set_mode()` (refuses to even record a mode for a never-schedulable agent). Direct, deliberate invocation of `run_agent_cycle()` — a human or a test calling it explicitly — is untouched, exactly as before.

### 1.5 Best-effort observability, shared

Phase 1.1's private `scheduler._safe_emit()` was promoted into a shared `runtime_events.emit_safe()` utility (try/except around `emit()`, logs and swallows on failure, never raises) so the same "observability must never be able to defeat the action it's observing" discipline is available to `policy_engine.set_policy()` and `scheduling_control.set_mode()`, not just the scheduler's own recovery path. `scheduler._safe_emit()` now delegates to it (kept as a thin wrapper for its 3 existing call sites — zero behavior change there).

---

## 2. Exact Files Modified

| File | Change |
|---|---|
| `agents/sys_admin/sysadmin_store.py` | Added `schedule_mode TEXT NOT NULL DEFAULT 'enabled'` column to `agent_status` (self-migrating). Extended `upsert_agent_status()` with `schedule_mode=None` kwarg, following the existing "only touch what's passed" pattern. |
| `agents/runtime/runtime_events.py` | Added `AGENT_MODE_CHANGED` event type. Added shared `emit_safe()` (best-effort wrapper around `emit()`). |
| `agents/runtime/scheduler.py` | Added `scheduling_control` import. Extracted `_is_due_by_cadence()` from the old `_due_agents()` body. Rewrote `_due_agents()` to filter through `scheduling_control.is_schedulable()`/`get_mode()` while preserving its exact flat-list return contract. Added new `_dry_run_due_agents()` method. `tick()` now computes and reports `dry_run_agents`. `_safe_emit()` now delegates to `runtime_events.emit_safe()`. |
| `agents/runtime/policy_engine.py` | `set_policy()` now also calls `runtime_events.emit_safe(..., POLICY_CHANGED, ...)` after its existing `sysadmin_report` write. |
| `agents/runtime/lifecycle.py` | `get_runtime_status()` extended with a new, defensively-wrapped `"control"` key: `{active_policy, emergency_stop, agents: scheduling_control.snapshot()}`. Degrades to `None` (never raises) if the underlying tables don't exist yet, matching the `active_jobs` precedent from the Phase 1.1 hotfix. |
| `app.py` | Docstring-only update on the existing `/api/runtime/status` route, documenting the new `"control"` key. **No route added, no route logic changed.** |
| `agents/runtime/scheduling_control.py` | **New file.** Core module — see Design Summary §1.2–1.4. |
| `runtime_control_cli.py` | **New file**, top-level, mirroring `approve_cli.py`'s structure. `pause` / `resume` / `enable-agent` / `disable-agent` / `dry-run-agent` / `status` subcommands, each a thin wrapper calling straight into `policy_engine`/`scheduling_control`. |
| `test_agents/runtime/test_scheduling_control.py` | **New file.** 21 tests. |
| `test_agents/runtime/test_scheduler.py` | Extended `TestDueAgents` (6 new tests) and added `TestSchedulingControlIntegration` (4 new tests). |
| `test_agents/runtime/test_scheduler_uninitialized_db_hotfix.py` | Added one assertion (`status["control"] is None`) to the existing uninitialized-database degrade-safely test. |

Files explicitly **not** touched: `agents/config.py` (`RUNTIME_SCHEDULER_ENABLED` default untouched — verified in §6.3), `agents/trading_intelligence/**`, `agents/quant_researcher/**`, any broker/SmartAPI integration file, `agents/sys_admin/orchestrator.py`, any HTML/template/frontend file.

---

## 3. New Tests Added

| File | Tests | Purpose |
|---|---|---|
| `test_agents/runtime/test_scheduling_control.py` | 21 | `NEVER_SCHEDULABLE_AGENTS`/`SCHEDULABLE_AGENTS` contents; `set_mode()` raises for trading_intelligence/quant_researcher under every mode (parametrized); `set_mode()` raises for invalid mode strings; refused `set_mode()` writes no row; `get_mode()`/`set_mode()` round-trip for schedulable agents; persistence across a fresh read against the same DB file (restart-equivalent); `sysadmin_report` + `AGENT_MODE_CHANGED` event audit trail; `snapshot()` shape and contents. |
| `test_agents/runtime/test_scheduler.py` — `TestDueAgents` (+6) | `trading_intelligence` never due despite "never-run agents are always due"; `quant_researcher` never due even during market hours; a `disabled` agent is not due; a `dry_run` agent is excluded from `_due_agents()` but appears in `_dry_run_due_agents()`; an `enabled` agent does not appear in `_dry_run_due_agents()`. |
| `test_agents/runtime/test_scheduler.py` — `TestSchedulingControlIntegration` (new, +4) | A disabled agent never appears in `tick()`'s `agents_run`; a dry-run agent never executes but is reported in `dry_run_agents`; `trading_intelligence` never executes across 5 consecutive real ticks; an explicit attempt to force-enable `trading_intelligence` via `set_mode()` is refused (`ValueError`) and it still doesn't run. |
| `test_agents/runtime/test_scheduler_uninitialized_db_hotfix.py` (+1 assertion) | `get_runtime_status()`'s new `"control"` key degrades to `None`, never raises, against a genuinely uninitialized database. |

No dedicated test file was written for `runtime_control_cli.py` itself, following this project's established precedent (`approve_cli.py` has no dedicated test file either — CLI wrappers aren't separately unit-tested; the underlying module they call is). The CLI's argparse wiring and end-to-end dispatch were manually exercised instead (§5, §6.1) before this report was written.

---

## 4. API Endpoints Introduced

**None added.** The existing `/api/runtime/status` route (`app.py`, admin-gated via `@auth.roles_required("admin")`) already returns `agents.runtime.lifecycle.get_runtime_status()` directly. Extending that function's return value with a new `"control"` key (§1, §2) automatically surfaces the kill-switch and per-agent scheduling state through the same, already-authenticated endpoint — satisfying "expose read-only status through an API endpoint" via reuse rather than a second, parallel status route. This follows the project's established "one canonical status source" discipline (the same reasoning Phase 1 used for `active_jobs`).

Example response shape (fields other than `"control"` are unchanged from Phase 1/1.1):

```json
{
  "scheduler_state": "stopped",
  "cycles_executed": 0,
  "active_jobs": 0,
  "control": {
    "active_policy": "recommendation_only",
    "emergency_stop": false,
    "agents": {
      "memory":               {"schedulable": true,  "mode": "enabled"},
      "dev_agent":             {"schedulable": true,  "mode": "enabled"},
      "quant_researcher":      {"schedulable": false, "mode": "disabled"},
      "risk_manager":          {"schedulable": true,  "mode": "enabled"},
      "trading_supervisor":    {"schedulable": true,  "mode": "enabled"},
      "sys_admin":             {"schedulable": true,  "mode": "enabled"},
      "trading_intelligence":  {"schedulable": false, "mode": "disabled"}
    }
  }
}
```

---

## 5. Example Operator Workflow

Using the new `runtime_control_cli.py` (mirrors `approve_cli.py`'s existing pattern — every subcommand calls straight into `agents.runtime.policy_engine`/`agents.runtime.scheduling_control`, the same functions a future dashboard would call):

```
$ python3 runtime_control_cli.py status
scheduler_state:  stopped
active_policy:    recommendation_only
emergency_stop:   False
agents:
  memory               schedulable        mode=enabled
  dev_agent            schedulable        mode=enabled
  quant_researcher     NEVER SCHEDULABLE  mode=disabled
  risk_manager         schedulable        mode=enabled
  trading_supervisor   schedulable        mode=enabled
  sys_admin            schedulable        mode=enabled
  trading_intelligence NEVER SCHEDULABLE  mode=disabled

$ python3 runtime_control_cli.py pause --by "ops" --reason "reviewing overnight signals"
PAUSED. All schedulable agents stop from the scheduler's next tick. (by=ops)

$ python3 runtime_control_cli.py disable-agent dev_agent --by "ops" --reason "flaky commit last night"
dev_agent: schedule_mode=disabled (by=ops)

$ python3 runtime_control_cli.py resume --by "ops" --reason "review complete"
RESUMED. Active policy is now 'recommendation_only'. (by=ops)

$ python3 runtime_control_cli.py status
...
  dev_agent            schedulable        mode=disabled     # stays disabled; resume only cleared the GLOBAL pause
...
```

This example (global pause → disable one agent → resume) was executed for real, end-to-end, against a throwaway database (subprocess-invoking the actual CLI script, not just the underlying library calls) as part of validation — see §6.1.

---

## 6. Validation Report

### 6.1 Disabled agents never execute

Executed the actual `runtime_control_cli.py` script (as a subprocess, exercising real argparse dispatch) end-to-end against a throwaway SQLite database:

1. `status` → baseline: all schedulable agents `mode=enabled`; `trading_intelligence`/`quant_researcher` show `NEVER SCHEDULABLE`.
2. `pause` → `active_policy` becomes `emergency_stop`.
3. `disable-agent dev_agent` → `dev_agent` mode becomes `disabled`.
4. `enable-agent trading_intelligence` → **refused**, exit code 1, stderr contains `"'trading_intelligence' can never be scheduled ... refusing to set a schedule_mode for it under any circumstance"`.
5. `resume` → `active_policy` restored to `recommendation_only`, `emergency_stop` False.
6. `status` → confirms `dev_agent` mode remains `disabled` (resume only clears the *global* pause, not per-agent state — matches design).

Separately, a direct-module workflow (bypassing the CLI, driving `policy_engine`/`scheduling_control`/`RuntimeScheduler` directly against a throwaway DB) ran a real `RuntimeScheduler.tick()` after the above sequence:

```
agents_run this tick: {'memory', 'sys_admin', 'risk_manager'}
```

`dev_agent` (disabled), `trading_intelligence`, and `quant_researcher` (never schedulable) are absent — proven by actual execution, not just by inspecting `_due_agents()`'s return value. This is also covered by the automated regression suite: `TestSchedulingControlIntegration::test_disabled_agent_never_appears_in_agents_run` and `test_trading_intelligence_never_executes_even_across_repeated_ticks` (5 consecutive real ticks).

### 6.2 Kill-switch survives restart

`policy_engine`/`scheduling_control` hold no in-memory state beyond a module-level `DB_PATH`; a restart's only observable effect on them is a fresh process re-reading the same on-disk file. This was validated directly: after `set_mode("risk_manager", DISABLED, ...)`, a wholly separate, unrelated `get_mode("risk_manager")` call (i.e., exactly what a new process reading the same DB would see) returns `"disabled"` — see `test_agents/runtime/test_scheduling_control.py::TestSchedulableAgentModes::test_mode_persists_across_a_fresh_read_against_the_same_db`. The manual end-to-end run (§6.1) confirmed the same for the global policy: `resume` set the policy to `recommendation_only`, and a subsequent independent `status` call read that value back correctly from the DB.

### 6.3 Scheduler remains disabled by default

```
$ python3 -c "from agents import config; print('RUNTIME_SCHEDULER_ENABLED =', config.RUNTIME_SCHEDULER_ENABLED)"
RUNTIME_SCHEDULER_ENABLED = False
```

`git status --porcelain agents/config.py` shows **no diff** — this file was not touched in this round of work at all.

### 6.4 `trading_intelligence` cannot be scheduled even if explicitly requested

Direct, explicit attempts to force it schedulable were made at three layers, all refused:

- `scheduling_control.set_mode("trading_intelligence", "enabled", ...)` → raises `ValueError` (both in the automated suite, parametrized across all three modes, and manually via the CLI's `enable-agent` subcommand, which exits non-zero).
- `RuntimeScheduler._due_agents()` never includes it regardless of cadence state (it has never run, so by the "never-run agents are always due" rule that governs every other agent it would otherwise qualify — `is_schedulable()` is checked first and short-circuits this).
- A real `tick()`, run 5 consecutive times via `run_for(iterations=5)`, never includes it in `agents_run` on any iteration.

Same three checks were performed for `quant_researcher`, including specifically during simulated market-open hours (where it was previously eligible to run, per the existing Phase-1-era `test_market_session_gated_agents_are_skipped_outside_trading_hours` test) — still never due.

### 6.5 Regression Suite Results

```
$ python3 -m pytest test_agents/runtime/ -q
164 passed   (pre-existing baseline, unaffected)
```
run immediately before this round's new tests were added, confirming zero regressions from the `scheduler.py`/`lifecycle.py`/`policy_engine.py`/`runtime_events.py`/`sysadmin_store.py` edits alone.

```
$ python3 -m pytest test_agents/runtime/test_scheduling_control.py -q
21 passed
$ python3 -m pytest test_agents/runtime/test_scheduler.py -q
21 passed   (includes the 10 new tests added this round)
$ python3 -m pytest test_agents/runtime/test_scheduler_uninitialized_db_hotfix.py -q
10 passed   (includes the 1 new assertion added this round)
```

Full suite, after all changes (all files in §2 + all new tests in §3):

```
$ python3 -m pytest test_agents/runtime/ -q
195 passed in 163.65s
```

164 (Phase 1.1 baseline) + 31 new (21 `test_scheduling_control.py` + 10 `test_scheduler.py` new tests; the `test_scheduler_uninitialized_db_hotfix.py` addition was a new assertion on an existing test, not a new test) = 195. Zero regressions, zero skips, zero failures.

Full repository regression suite (`python3 -m pytest -q`, all suites including sys_admin, risk_manager, trading_supervisor, trading_intelligence, dev_agent, memory):

```
$ python3 -m pytest -q
1432 passed, 1 xfailed in 352.09s (0:05:52)
```

The one `xfailed` is a pre-existing, unrelated expected-failure marker (not introduced or affected by this round of work). Zero failures, zero regressions across the entire repository.

---

## 7. Release Recommendation

All six required validation proofs hold. `RUNTIME_SCHEDULER_ENABLED` remains `False` and `agents/config.py` was not touched. No broker, order-placement, or trading_intelligence/quant_researcher code was modified. No Shadow Mode, Passive Mode, or Autonomous Mode work was started, per the explicit stop instruction in the approval message.

**Stopping here, as instructed.** Awaiting explicit approval before any further Milestone 12 work (Phase 2A Shadow Mode or otherwise).
