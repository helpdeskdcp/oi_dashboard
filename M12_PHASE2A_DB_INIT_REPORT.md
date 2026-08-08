# Milestone 12 — Phase 2A Follow-up: Wire Agent Observability Tables into Startup

**Scope approved:** Option A only — wire the existing `init_db()` functions for `audit_log`, `event_bus`, `risk_store`, `supervision_store`, `sysadmin_store`, `runtime_store` into `app.py`'s own startup `init_db()`. No scheduler, broker, order, or trading logic touched.

Branch: `worktree-m12-phase2a-db-init`, based on `master@f351e10`.

## Root Cause (recap from the prior investigation)

The live `oi_history.db` had 21 tables, none of them from `agents/sys_admin` or `agents/runtime` — `app.py`'s `init_db()` never called any of these six modules' own `init_db()`. This left `/api/sysadmin/overview`'s `"runtime"` section and `/api/runtime/status`'s `"control"` section permanently degraded (`"no such table: agent_audit_log"` / `"control-plane state unavailable"`) — safely (no crash, per the Phase 1.1 hotfix), but with no live data.

## Change

`app.py`: added imports for the six modules (`agent_audit_log`, `agent_event_bus`, `agent_risk_store`, `agent_supervision_store`, `agent_sysadmin_store`, `agent_runtime_store`) and, at the very end of the existing `init_db()` function (after its own `conn.commit()`/`conn.close()`), calls each module's `init_db()` in sequence, followed by one `log.info(...)` line. Every one of these six `init_db()` functions is `CREATE TABLE IF NOT EXISTS` only (confirmed by reading each — none contain `DROP`, `ALTER ... DROP`, or destructive migrations), matching the "safe to run every startup" contract every other migration in `app.py`'s `init_db()` already follows.

No other line in `app.py` was touched. No changes to `agents/runtime/scheduler.py`, `policy_engine.py`, `scheduling_control.py`, `agents/trading_intelligence/`, `agents/quant_researcher/`, or any broker/order code.

New test file: `test_app_startup_agent_tables.py` (6 tests), following `test_auth.py`'s established Flask-test-client convention.

## Validation (all 8 required checks)

**1. Fresh startup creates all six tables** — verified against a throwaway DB (never `oi_history.db`): `app.init_db()` on an empty file produces `agent_audit_log`, `agent_events`, `agent_status`, `sysadmin_log`, `runtime_policy`, `runtime_workflow`. PASS.

**2. Existing-DB startup is idempotent** — called `app.init_db()` twice against the same throwaway DB with a seeded `agent_status` row in between; the row was byte-identical before and after the second call (no data loss, no error). PASS.

**3. `/api/sysadmin/overview` returns runtime data without the `agent_audit_log` error** — reproduced the exact live symptom (empty DB, `sysadmin_api.get_overview()` → `runtime.error == "no such table: agent_audit_log"`), then ran the fix (`app.init_db()`) and confirmed `runtime.error is None` and `runtime.policy == "recommendation_only"` on the same DB. PASS.

**4. `/api/runtime/status` returns a populated `"control"` section** — same before/after DB: `control` was `None` before (`scheduling_control.snapshot()` raising on missing `agent_status`), non-`None` with the correct 7-agent shape after. PASS.

**5. SysAdmin page shows live data instead of "unavailable"** — exercised the actual HTTP routes (not direct function calls) via `app.app.test_client()` against a freshly-`init_db()`'d throwaway DB, logged in as an admin: `GET /api/sysadmin/overview` → `runtime.error is None`; `GET /api/runtime/status` → `control is not None`; `GET /admin/sysadmin` → 200, page contains the "Runtime Control" panel. PASS.

**6. `RUNTIME_SCHEDULER_ENABLED` remains `False`** — confirmed via direct import; `agents/config.py` was not touched by this change. PASS.

**7. `trading_intelligence`/`quant_researcher` remain locked** — confirmed via `scheduling_control.is_schedulable()`, both `False`; unaffected by which tables exist (hard code-level constant, not DB-driven). PASS.

**8. Test suite results:**
```
test_app_startup_agent_tables.py (new)                         6 passed
test_agents/sys_admin/ + test_runtime_control_routes.py
  + test_auth.py                                              148 passed
test_agents/runtime/                                          195 passed
Full repo suite (python3 -m pytest -q)          1449 passed, 1 xfailed
```
The 1 xfailed is the same pre-existing marker noted in every prior phase's validation, unrelated to this change. Zero failures, zero regressions.

## Important note on live deployment

All validation above was performed against throwaway databases (never `/root/oi_dashboard/oi_history.db`, the live production file), consistent with this project's established discipline of never writing to the live DB without separate, explicit authorization. **This commit does not itself modify the live database** — the six new tables will only be created in production the next time the live `app.py` process is restarted with this code deployed. That restart/deploy step is a separate operational action outside this implementation phase's scope (code review + merge approval), and should ideally be preceded by a backup of `oi_history.db` given it is a production data file, even though the change itself is purely additive.

## Status

Implementation, tests, and validation complete, exactly matching the approved scope. No scheduler activation, no broker/trading logic touched, no change to `RUNTIME_CONTROL_API_ENABLED`'s default, `trading_intelligence`/`quant_researcher` still unschedulable. Awaiting explicit approval before merge.
