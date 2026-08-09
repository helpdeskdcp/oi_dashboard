# Milestone 12 — Phase 3: Shadow Runtime Integration

**Scope, as clarified before implementation:** register Shadow Mode as a formal runtime agent for **status/health tracking only** — participating in `health_snapshot()`, `/api/runtime/status`'s `"control"` section, and the sysadmin dashboard's per-agent table the same way every other runtime agent does — while remaining **permanently excluded from scheduling**, on the same hard, code-level footing `trading_intelligence`/`quant_researcher` already carry. Explicitly **not** in scope: automatic/scheduled execution of Shadow Mode's actual observation/evaluation logic. `shadow_mode_cli.py` remains the only caller of `observer.observe_and_predict()`/`evaluator.evaluate_pending()` anywhere in the codebase — unchanged by this phase.

Branch: `worktree-m12-phase3-shadow-runtime-integration`, based on `master@96ed089`.

## Design

- **`agents/runtime/agent_runtime.py`**: added `"shadow_mode"` as an eighth entry to `RUNTIME_AGENT_NAMES`, and a new `_shadow_mode_cycle()` function registered in `_CYCLE_FUNCS`. This cycle function is a **read-only heartbeat only** — it calls `agents.shadow_mode.api.get_status()` (an existing, pure read function) and returns a health finding describing current observation/prediction counts. It never calls `observer.observe_and_predict()` or `evaluator.evaluate_pending()`. Like `trading_intelligence`, `shadow_mode` is deliberately **not** added to `agents.sys_admin.orchestrator.AGENT_NAMES` (that module is scoped to four Milestone 2-7 agents) — always considered enabled here, consistent with `memory`/`sys_admin`/`trading_intelligence`'s own treatment.
- **`agents/runtime/scheduling_control.py`**: added `"shadow_mode"` to `NEVER_SCHEDULABLE_AGENTS` — the same permanent, code-level `frozenset` lock (never sourced from the database, never configurable via `set_mode()`) that already protects `trading_intelligence`/`quant_researcher`. This is the belt-and-suspenders half of the design: registering an agent in `RUNTIME_AGENT_NAMES` without a corresponding `NEVER_SCHEDULABLE_AGENTS` entry would make it schedulable by default (confirmed during research before writing any code) — both changes were made together, in the same commit.

### Why the cycle function never executes real Shadow Mode logic

This preserves the invariant `shadow_mode_cli.py`'s own docstring and `test_shadow_mode_read_only.py`'s AST-verified test already establish: `shadow_mode_cli.py` is "the ONLY way `observer.observe_and_predict()`/`evaluator.evaluate_pending()` are ever invoked in this codebase." Had the new cycle function called either, that claim would become false the moment `run_agent_cycle("shadow_mode", ...)` is ever invoked directly (by a human, a test, or — in principle, since scheduling and direct invocation are different code paths — even while permanently unschedulable). The existing AST-based test only scanned `app.py`; I extended it (see Tests below) to also scan `agents/runtime/agent_runtime.py`, closing exactly this gap before it could be introduced.

## Files Changed

- `agents/runtime/agent_runtime.py` — `RUNTIME_AGENT_NAMES` +1, new `_shadow_mode_cycle()`, `_CYCLE_FUNCS` +1, new `agents.shadow_mode.api` import, module docstring extended.
- `agents/runtime/scheduling_control.py` — `NEVER_SCHEDULABLE_AGENTS` +1 (`"shadow_mode"`).
- `test_agents/conftest.py` — the shared `agent_db` fixture now also initializes `agents.shadow_mode.store`'s tables (needed for any test — including ones unrelated to Shadow Mode — that calls `health_snapshot()` or `run_agent_cycle("shadow_mode", ...)`), mirroring the same additive pattern used for the Phase 2A DB-init work.
- `test_agents/runtime/test_agent_runtime.py` — new `TestShadowModeCycle` class (6 tests).
- `test_shadow_mode_read_only.py` — one test rewritten (`shadow_mode` is now correctly *in* `RUNTIME_AGENT_NAMES`, not absent) plus one new AST-based test extending the "never calls observe_and_predict/evaluate_pending" scan to `agent_runtime.py`.

No `app.py`, `agents/runtime/scheduler.py`, `agents/runtime/lifecycle.py`, or `agents/config.py` change — confirmed via `git diff --stat master`, zero diff on all four.

## Validation

**Direct verification (throwaway DBs only, never production):**
- `run_agent_cycle("shadow_mode", ...)` succeeds, records execution bookkeeping, reports honest zero-observation status when none exist and real counts when they do.
- `RuntimeScheduler.run_for(iterations=5)` — `shadow_mode` never appears in `agents_run` across 5 real ticks.
- `scheduling_control.set_mode("shadow_mode", ENABLED, ...)` — refused with the same `ValueError` as `trading_intelligence`/`quant_researcher`.
- `/api/runtime/status`'s `control.agents.shadow_mode` → `{"schedulable": False, "mode": "disabled"}`.
- `/api/sysadmin/overview`'s `runtime.agents` correctly includes `shadow_mode` (health `None` until its first cycle runs).

## Test Results

```
$ python3 -m pytest test_agents/runtime/test_agent_runtime.py -q
24 passed   (18 previous + 6 new TestShadowModeCycle)

$ python3 -m pytest test_shadow_mode_read_only.py test_shadow_mode_cli.py -q
61 passed   (39 + 21, net +1 in the former: 1 rewritten + 1 new AST test)

$ python3 -m pytest test_agents/ -q
1114 passed

$ python3 -m pytest -q
1563 passed, 1 xfailed   (1556 baseline + 7 new)
```
Zero failures, zero regressions.

## Safety Confirmation

```
RUNTIME_SCHEDULER_ENABLED == False
RUNTIME_CONTROL_API_ENABLED == False
trading_intelligence.is_schedulable() == False
quant_researcher.is_schedulable() == False
shadow_mode.is_schedulable() == False   (new)
NEVER_SCHEDULABLE_AGENTS == {'quant_researcher', 'trading_intelligence', 'shadow_mode'}
SCHEDULABLE_AGENTS == ('memory', 'dev_agent', 'risk_manager', 'trading_supervisor', 'sys_admin')
```

`shadow_mode_cli.py` remains the sole caller of `observer.observe_and_predict()`/`evaluator.evaluate_pending()` — verified both by the pre-existing AST test on `app.py` and the new AST test on `agent_runtime.py`. No automatic execution, no scheduler activation, no broker/paper-trade code touched.

## Status

Implementation, tests, and validation complete. Not merged, not deployed. Awaiting review.
