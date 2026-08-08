# Milestone 12 — Phase 2B: Shadow Mode (Read-only Observation)

**Scope:** a fully passive market-observation pipeline — observe already-archived market data, compute a hypothetical signal, log it, and later compare the prediction against what actually happened. No order placement, no paper trading, no scheduler wiring, no automatic execution of any kind.

Branch: `worktree-m12-phase2b-shadow-mode`, based on `master@28f9079`.

---

## 1. Exact File List Changed

| File | Type |
|---|---|
| `agents/shadow_mode/__init__.py` | New |
| `agents/shadow_mode/store.py` | New |
| `agents/shadow_mode/observer.py` | New |
| `agents/shadow_mode/evaluator.py` | New |
| `agents/shadow_mode/api.py` | New |
| `test_shadow_mode_read_only.py` | New |
| `app.py` | Modified |
| `templates/sysadmin.html` | Modified |

No other file changed. `.claude/settings.local.json` (pre-existing, unrelated dirty file) was explicitly excluded from staging.

---

## 2. Diff Summary

```
agents/shadow_mode/__init__.py  |  28 +++
agents/shadow_mode/api.py       |  28 +++
agents/shadow_mode/evaluator.py | 166 ++++++++++++++++++
agents/shadow_mode/observer.py  | 112 ++++++++++++
agents/shadow_mode/store.py     | 252 +++++++++++++++++++++++++++
app.py                          |  47 +++++
templates/sysadmin.html         |  45 +++++
test_shadow_mode_read_only.py   | 377 ++++++++++++++++++++++++++++++++++++++++
8 files changed, 1055 insertions(+), 0 deletions(-)
```

Purely additive — zero lines removed anywhere.

**`app.py` changes:** two new imports (`agents.shadow_mode.api as shadow_api`, `agents.shadow_mode.store as shadow_store`); one new `shadow_store.init_db()` call added to the end of the existing startup `init_db()` (same additive-only `CREATE TABLE IF NOT EXISTS` contract as the Phase 2A DB-init follow-up); three new GET-only routes (`/api/shadow/status`, `/api/shadow/recent`, `/api/shadow/performance`), each `@auth.roles_required("admin")`, declared with no `methods=` argument (Flask/Werkzeug default: GET/HEAD/OPTIONS only — a POST/PUT/DELETE to any of them 405s automatically).

**`templates/sysadmin.html` changes:** one new full-width panel ("🔍 SHADOW MODE — READ ONLY", with the required "NO ORDERS ARE PLACED IN SHADOW MODE" banner, a summary line, and a last-10-observations table) and one new JS poller (`refreshShadowMode()`, same 20s-interval read-only-fetch pattern as every other panel on this page) — no write action, no button, anywhere in this new panel.

### Design decisions worth noting

- **`observer.py` deliberately does NOT call `agents.trading_intelligence.ai_trading_engine.evaluate()`**, even though that's the higher-level, more feature-complete signal function. Investigation found `evaluate()` has a real side effect: `ti_store.close_trade(...)` when an existing open `ti_paper_trades` position's exit condition is hit. Since Shadow Mode must be "100% passive," `observer.py` instead calls the lower-level primitives `evaluate()` itself wraps — `oi_engine.generate_signal()`, `oi_engine.detect_bias()`, `oi_engine.oi_walls()` (all verified, by grepping their full bodies, to contain zero `INSERT`/`UPDATE`/`.execute`/`_store.` calls) plus `market_data.get_snapshot()` and `data_access.load_candles()`/`latest_market_structure()` (all read-only, per their own docstrings: "aggregated from already-stored data only"). This gets the exact same signal-generation quality without any risk of touching a real position.
- **Three tables** (`shadow_observations`, `shadow_predictions`, `shadow_outcomes`), append-only (no `UPDATE` anywhere in `store.py` — `shadow_outcomes.prediction_id` is `UNIQUE`, so `evaluate_prediction()` is naturally idempotent rather than needing to overwrite a row).
- **No automatic execution**: `observer.observe_and_predict()` and `evaluator.evaluate_pending()`/`evaluate_prediction()` are never called from `app.py`, the scheduler, or any thread — verified both by an AST-based static check (no `Call` node in `app.py` references these names) and by confirming none of the four `agents/shadow_mode/*.py` files contain a module-level function call (the only way a background thread could start "automatically on app startup").

---

## 3. Test Results

```
$ python3 -m pytest test_shadow_mode_read_only.py -q
35 passed in 9.87s

$ python3 -m pytest test_agents/runtime/ -q
195 passed in 115.51s (0:01:55)

$ python3 -m pytest -q
1490 passed, 1 xfailed in 301.86s (0:05:01)
```

1490 = 1455 (pre-existing baseline) + 35 new. The 1 xfailed is the same pre-existing marker noted in every prior phase's validation. Zero failures, zero regressions.

### Coverage against the 8 required proofs

| # | Requirement | Test(s) |
|---|---|---|
| 1 | Observation records can be inserted | `TestInsertion` (4 tests) |
| 2 | Performance metrics calculate correctly | `TestPerformanceMetrics` (3 tests) |
| 3 | All endpoints are GET-only | `TestEndpointsAreGetOnly::test_get_succeeds_for_an_admin` (×3, parametrized) |
| 4 | POST requests return 405 | `TestEndpointsAreGetOnly::test_post_returns_405` (×3) + `test_put_and_delete_return_405` (×3, bonus) |
| 5 | No broker modules are imported | `TestNoBrokerImports` (2 tests, AST-based static check of every shadow_mode source file — not a `sys.modules` runtime check, which would false-positive since `app.py`, imported earlier in the same test session, does import the broker SDK) |
| 6 | Scheduler flags remain `False` | `TestSchedulerSafetyUntouched::test_runtime_scheduler_enabled_still_false` / `test_runtime_control_api_enabled_still_false` |
| 7 | Locked agents remain unschedulable | `TestSchedulerSafetyUntouched::test_trading_intelligence_still_unschedulable` / `test_quant_researcher_still_unschedulable` (+ 2 bonus tests confirming `shadow_mode` itself was never added to `RUNTIME_AGENT_NAMES` and isn't schedulable) |
| 8 | Startup does not launch any worker automatically | `TestNoAutomaticWorker` (3 tests: AST call-check on `app.py`, live-thread-name check, module-level-call check on all 4 shadow_mode files) |

Plus 6 additional tests covering the evaluator's four classification outcomes (correct/incorrect/partial/expired, including the "still within window, no data yet" pending case and idempotent re-evaluation) and the observer's graceful degradation when no market snapshot exists — beyond the minimum ask, to actually prove the pipeline's core logic is correct, not just its safety boundary.

---

## 4. Safety Verification Checklist

| Check | Result |
|---|---|
| No broker order-placement function imported or called | **PASS** — zero matches for `place_order`/`SmartConnect`/`smartapi` anywhere in `agents/shadow_mode/` |
| No paper orders created | **PASS** — zero references to `paper_orders`/`paper_trades` outside explanatory comments |
| No trade execution jobs created | **PASS** — `ti_store`/`paper_trading`/`enter_from_recommendation` never called (only named in a docstring explaining why `evaluate()` was avoided) |
| Scheduler not enabled | **PASS** — `RUNTIME_SCHEDULER_ENABLED` unchanged, confirmed `False` |
| `RUNTIME_CONTROL_API_ENABLED` unchanged | **PASS** — confirmed `False`, `agents/config.py` has zero diff against `master` |
| `NEVER_SCHEDULABLE_AGENTS` unmodified | **PASS** — `agents/runtime/scheduling_control.py` has zero diff against `master` |
| `trading_intelligence`/`quant_researcher` not made schedulable | **PASS** — both confirmed `is_schedulable() == False` |
| No background thread starts automatically on app startup | **PASS** — `agents/runtime/agent_runtime.py` has zero diff (`RUNTIME_AGENT_NAMES` untouched); AST + thread-enumeration tests confirm no automatic invocation anywhere |
| Only `CREATE TABLE IF NOT EXISTS` / additive schema | **PASS** — `store.py`'s `init_db()` is one `executescript()` of `CREATE TABLE IF NOT EXISTS`/`CREATE INDEX IF NOT EXISTS` statements only; zero `ALTER`/`DROP` anywhere in the package |
| No existing table modified | **PASS** — `git diff --stat master` shows zero changes to any table-owning module other than the one new `init_db()` call in `app.py` |

---

## 5. Explicit Safety Statement

- **No order placement exists.** No code path in this phase calls any broker function, live or simulated.
- **No paper trading exists.** `shadow_predictions` rows are hypothetical log entries only; nothing in this phase writes to `paper_orders`, `paper_trades`, or `ti_paper_trades`, and nothing in this phase can open or close a real position.
- **No autonomous execution exists.** `observer.observe_and_predict()` and `evaluator.evaluate_pending()`/`evaluate_prediction()` are library functions only — callable manually or by a test, never invoked by `app.py`, the scheduler, or any thread. There is no cron, no timer, no `start_background_task`, nothing that runs Shadow Mode on its own.
- **Scheduler remains disabled.** `RUNTIME_SCHEDULER_ENABLED` is unchanged and confirmed `False`.
- **Trading agents remain locked.** `trading_intelligence` and `quant_researcher` remain `schedulable: False`, enforced by the untouched `NEVER_SCHEDULABLE_AGENTS` code-level constant.

---

## Status

Implementation, tests, and validation complete. **Stopping here, as instructed — work remains on `worktree-m12-phase2b-shadow-mode`, not merged.** Awaiting explicit approval before any merge.
