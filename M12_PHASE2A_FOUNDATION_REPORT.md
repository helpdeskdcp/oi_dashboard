# Milestone 12 — Phase 2A: Shadow Mode Foundation (Read-only status panel + operator scheduler controls)

**Scope approved:** read-only runtime status panel in the existing sysadmin UI + operator-facing scheduler visibility controls, with any new write endpoint admin-gated, CSRF-protected, audited, and disabled by default. No real trade execution, no broker order placement, no autonomous/unattended trading, `RUNTIME_SCHEDULER_ENABLED` stays `False`, `trading_intelligence`/`quant_researcher` remain unschedulable.

Branch: `worktree-m12-phase2a-shadow-foundation`, based on `master@3a43046`. Implementation followed the plan approved via `ExitPlanMode` (saved at the time to `/root/.claude/plans/cheeky-singing-twilight.md`) with no deviation from its file list.

## Files Modified (exactly as approved)

1. **`agents/config.py`** — added `RUNTIME_CONTROL_API_ENABLED`, same boolean-env idiom as `RUNTIME_SCHEDULER_ENABLED`, default `False`. Governs only the new write routes; the read-only `/api/runtime/status` endpoint (Phase 1/2 Foundation) is never gated by it.
2. **`app.py`** — added `agents.config`, `policy_engine`, `scheduling_control` imports; three new POST routes: `/api/runtime/control/pause`, `/api/runtime/control/resume`, `/api/runtime/control/agent/<agent>/mode`. Each: `@auth.roles_required("admin")`, checks `RUNTIME_CONTROL_API_ENABLED` first (403 if off), calls straight into `policy_engine.set_policy()` / `scheduling_control.set_mode()` (unchanged, reused as-is), catches `ValueError` → 400, logs the acting admin via `log.info(...)`.
3. **`templates/sysadmin.html`** — new "Runtime Control" panel: read-only view (active policy, emergency-stop banner, per-agent schedulable/mode table, `trading_intelligence`/`quant_researcher` rendered locked with no buttons) polling the existing `/api/runtime/status` on the same 20s cadence; write actions (Pause All behind `confirm()`, Resume, per-agent Enable/Disable/Dry-run) wired to the new routes with a `CSRF_TOKEN` JS constant and `X-CSRFToken` header.
4. **`test_runtime_control_routes.py`** (new) — 17 tests, Flask test-client convention matching `test_auth.py`.

No other files changed. No migrations. `agents/runtime/scheduler.py`, `policy_engine.py`, `scheduling_control.py` themselves are untouched — this phase only adds callers.

## New Tests (17, all passing)

- Every write route returns 403 when `RUNTIME_CONTROL_API_ENABLED` is `False` (the default) — 3 tests.
- CSRF enforcement: a request without a valid token is rejected (400) even from an authenticated admin.
- Pause engages `emergency_stop`, resume clears it, both reflected in a subsequent `/api/runtime/status` read.
- Pause/resume require a non-empty `reason`.
- Resume defaults to `agents.config.RUNTIME_DEFAULT_POLICY` when no policy is specified.
- Disabling a schedulable agent (`dev_agent`) is reflected in `/api/runtime/status`.
- An invalid mode string is rejected (400).
- `trading_intelligence`/`quant_researcher` are refused under **all three** modes (parametrized, 6 tests) — the route surfaces `scheduling_control.set_mode()`'s existing `ValueError` as 400, no new exclusion logic added.
- A non-admin (logged-in) session gets 403; an unauthenticated request is rejected (400/403, depending on whether the CSRF or auth layer trips first — both layers were independently confirmed to fail closed).

## API Endpoints Introduced

| Route | Method | Body | Behavior |
|---|---|---|---|
| `/api/runtime/control/pause` | POST | `{"reason": str}` | `policy_engine.set_policy(EMERGENCY_STOP, ...)` |
| `/api/runtime/control/resume` | POST | `{"policy": str?, "reason": str}` | `policy_engine.set_policy(policy or RUNTIME_DEFAULT_POLICY, ...)` |
| `/api/runtime/control/agent/<agent>/mode` | POST | `{"mode": "enabled"\|"disabled"\|"dry_run", "reason": str}` | `scheduling_control.set_mode(agent, mode, ...)` |

All three: `@auth.roles_required("admin")`, gated by `RUNTIME_CONTROL_API_ENABLED` (403 when off — the default), CSRF-protected via the app's existing global `before_request` guard, audited automatically (both underlying functions already write a `sysadmin_report` + best-effort runtime event) plus a `log.info` line naming the acting admin.

## Validation

**Automated:**
- `test_runtime_control_routes.py`: 17/17 passed.
- `test_agents/runtime/`: 195/195 passed (unchanged — no backend runtime module was modified).
- Full repo suite (`python3 -m pytest -q`): **1449 passed, 1 pre-existing xfailed**, zero failures (1432 baseline + 17 new).

**Live-server manual check** (browser extension unavailable in this environment; verified instead via a real running Flask process + `curl`, exercising the actual HTTP/session/CSRF/template stack rather than the test client):
- Started the app with `SKIP_AUTOSTART=1` (no live data threads, no broker session) against a throwaway database (never `oi_history.db`), `RUNTIME_CONTROL_API_ENABLED` forced on for this process only.
- Logged in as an admin via the real `/login` form flow (session cookie + CSRF token extracted from the rendered page).
- Confirmed `/admin/sysadmin` renders the new "Runtime Control" panel HTML and a live `CSRF_TOKEN` value.
- `POST /api/runtime/control/pause` → 200, `/api/runtime/status` immediately showed `emergency_stop: true`.
- `POST .../agent/dev_agent/mode {"mode":"disabled"}` → 200, reflected in `/api/runtime/status`.
- `POST .../agent/trading_intelligence/mode {"mode":"enabled"}` (a forced override attempt) → **400**, refused with the same `NEVER_SCHEDULABLE_AGENTS` error as the CLI/tests.
- `POST /api/runtime/control/resume` → 200; final `/api/runtime/status` showed `emergency_stop: false`, `active_policy: recommendation_only`, `dev_agent` still `disabled` (resume only clears the global pause, matching design), `trading_intelligence`/`quant_researcher` still `schedulable: false`.
- Server process killed and all temporary files (manual server script, cookie jar, throwaway DB under `/tmp`) removed after the check; `git status` confirmed no residue.

**Defaults confirmed post-implementation:**
```
RUNTIME_SCHEDULER_ENABLED = False
RUNTIME_CONTROL_API_ENABLED = False
```

## Status

Implementation, tests, and validation complete, exactly matching the approved plan's file list. No Shadow Mode execution, no broker/order-placement code, no autonomous trading, no scheduler activation. Awaiting explicit approval before merge or any further Phase 2A work.
