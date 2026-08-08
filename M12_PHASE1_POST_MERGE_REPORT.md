# Milestone 12, Phase 1 — Post-Merge Verification Report

## Merge

| | |
|---|---|
| Branch merged | `worktree-m12-phase1-scheduler` → `master` |
| Merge commit | `de9891b0147f332f3c79b451a731d2d3d5051765` |
| Included commits | `7b49063` (Phase 1 code/tests/sustained-run output), `8fbdbe7` (implementation + validation reports) |
| Files changed | Exactly the 10 Phase 1 files (verified via `git diff master...worktree-m12-phase1-scheduler --stat` before merging) — zero overlap with `data/history/*` or `.claude/settings.local.json`, both confirmed untouched by any commit on this branch |
| Merge type | `--no-ff`, guaranteed fast-forward-compatible (verified via `git merge-base --is-ancestor` before merging — zero conflict risk) |

## Critical finding from verification — please read before anything else

Verifying "`/api/runtime/status` returns healthy metrics" surfaced a **real bug**, not just a pre-existing gap, and I have **not fixed it** — your instructions this round scoped the authorized actions to merge + verify + report, so I stopped at reporting it rather than silently expanding scope. Flagging it prominently here so you can decide how to proceed before any further work.

**What's true right now, verified directly against the real `oi_history.db`:**
- None of `agents/runtime`/`agents/sys_admin`'s own tables (`agent_status`, `agent_events`, `runtime_policy`, `runtime_workflow`, etc.) exist in the live database. This is **pre-existing, not introduced by Phase 1** — confirmed by grepping `app.py` for `sysadmin_store`/any `.init_db()` call besides its own local one: there are none. Nothing has ever called `sysadmin_store.init_db()` (or `runtime_store.init_db()`, `event_bus.init_db()`) against the real deployment. This directly matches Milestone 12's own planning-phase finding that the runtime layer has never actually executed in production.
- As a direct consequence, **`/api/runtime/status` would currently return a 500 error, not healthy metrics**, if hit against the live database — `lifecycle.get_runtime_status()` calls `agent_runtime.health_snapshot()`, which raises `OperationalError: no such table: agent_status`. (For the same underlying reason, the *pre-existing* `/api/sysadmin/overview` route — unrelated to this phase, built in Milestone 8 — would fail identically today; this is not new.)
- **A genuine bug I did introduce**: `RuntimeScheduler.tick()`'s new exception-recovery path (`except Exception: ... runtime_events.emit(...)`) calls `runtime_events.emit()` unconditionally to record the recovery as an event. I verified directly (in an isolated, throwaway DB deliberately shaped to match the real DB's actual missing-table set — never touching the real `oi_history.db` itself) that if the `agent_events` table doesn't exist, **that emit call itself raises**, escaping the except block entirely. This means: if `RUNTIME_SCHEDULER_ENABLED` were flipped on against the live database exactly as it stands today, the very first `tick()` would raise uncaught, and `run_forever()`'s loop — the very thing Phase 1's resilience work was built to protect — would crash on its first iteration, not recover as designed and validated.
- **Why the 22-test suite didn't catch this**: every test uses this project's own `agent_db`/`ti_db` fixtures, which — reasonably, for isolated unit testing — initialize *all* relevant tables including `agent_events` before running. None of them reproduce the real database's actual "several core tables were never created" state. I verified this gap by deliberately building a throwaway DB shaped to match production's real (uninitialized) table set, which is not what any existing fixture does.

**Assessment**: this does not affect anything that already ran (the sustained validation run used a fully-initialized throwaway DB and is unaffected; `RUNTIME_SCHEDULER_ENABLED` remains `false` by default, so nothing in production is at risk right now). But it means two of the specific things you asked me to verify — "`/api/runtime/status` returns healthy metrics" and (transitively) "the scheduler continues operating after a recoverable cycle exception" — are **not actually true against the real database as it stands today**, only against the isolated environments Phase 1's own tests and validation run used.

**I have not applied a fix.** A safe, small, additive fix exists (wrap the `runtime_events.emit()` call in the except block in its own try/except so a failed observability write can never defeat the recovery itself), and separately, ensuring `sysadmin_store.init_db()`/`runtime_store.init_db()`/`event_bus.init_db()` actually run during `app.py` startup would resolve the root cause. Both are outside this round's authorized scope (merge/verify/report only) — I'm surfacing this for your explicit decision rather than deciding unilaterally.

## Verification results (against the real repository/database, as merged)

### 1. `RUNTIME_SCHEDULER_ENABLED` remains `false` by default

```
>>> from agents import config
>>> config.RUNTIME_SCHEDULER_ENABLED
False
>>> config.RUNTIME_SCHEDULER_LOCK_PATH
'/tmp/oi_dashboard_runtime_scheduler.lock'
```
**Confirmed.**

### 2. `/api/runtime/status` returns healthy metrics

**Not confirmed as-is** — see the critical finding above. Tested by calling `lifecycle.get_runtime_status()` directly (the exact function the route wraps — this codebase never imports `app.py` inside automated checks, to avoid any risk of touching its live broker-session machinery; a subprocess-isolated `SKIP_AUTOSTART=1` boot check is the sanctioned way to touch `app.py` at all):

```
>>> from agents.runtime import lifecycle
>>> lifecycle.get_runtime_status()
OperationalError: no such table: agent_status
```

With the scheduler disabled (today's actual state), this exception would surface to a caller of `/api/runtime/status` as an HTTP 500, not the honest `scheduler_state: "stopped"` payload the function's own docstring promises. This is a gap in `lifecycle.get_runtime_status()` itself (it should catch this and degrade honestly, matching every other data-reading function's contract in this codebase) — not something I fixed this round.

### 3. Startup works normally with the scheduler disabled

**Confirmed.**

```
$ SKIP_AUTOSTART=1 python3 -c "import app"
2026-08-08 13:17:17,318 | INFO | Route protection self-check passed -- every route is either public or access-controlled.
app.py imports and boots cleanly on master with the scheduler disabled
```

`_verify_all_routes_protected()` (the fail-closed startup self-check requiring every route to carry a recognized auth decorator) passed, confirming `/api/runtime/status` is correctly protected on `master`. Since `RUNTIME_SCHEDULER_ENABLED` is `false`, `start_scheduler_background()` returns immediately after one cheap config check — the finding above about a missing `agent_events` table only matters once the flag is flipped on, which it is not.

### 4. All 22 tests still pass

**Confirmed.**

```
$ python3 -m pytest test_agents/runtime/test_scheduler_lifecycle.py -q
......................                                                  [100%]
22 passed in 106.33s
```

## What I did not do

- Did not modify `.claude/settings.local.json` or any `data/history/*` file (verified clean both pre- and post-merge — `git status` shows only the same 28 pre-existing dirty files as before this session began).
- Did not apply any fix for the finding above.
- Did not push to a remote (none is configured for this repository, confirmed again).
- Did not start Milestone 12 Phase 2 or any further work.

## Recommendation

Given the finding above, I'd suggest a small, tightly-scoped follow-up before `RUNTIME_SCHEDULER_ENABLED` is ever considered for a real activation:
1. Make `lifecycle.get_runtime_status()` degrade honestly instead of raising (matching this codebase's own established contract for every other data-reading function).
2. Make `tick()`'s exception-recovery path's own `runtime_events.emit()` call safe against a missing `agent_events` table (so a recovery can never itself fail).
3. Separately decide whether/when `sysadmin_store`/`runtime_store`/`event_bus`'s `init_db()` calls should run during normal `app.py` startup (today, nothing does this in production) — a broader decision with more blast radius than the two code fixes above, worth deciding deliberately rather than as a quick patch.

Happy to implement 1–2 now if you'd like — they're small and additive — or fold them (plus 3) into a defined next step. Waiting for your direction; not proceeding to Phase 2 either way.
