# Milestone 12 — Phase 2B: Market-Open Observation Validation

**Scope:** build the tooling and read-only dashboard counters needed to validate that Shadow Mode's pipeline works end-to-end on real market data, and prove the mechanism mechanically — without adding any autonomous execution path. Full live-tick validation itself is **pending the next trading session** (see §3).

Branch: `worktree-m12-phase2b-market-open-validation`, based on `master@878fec4`.

## Trigger-mechanism decision

You delegated the choice of trigger mechanism to me. I built **Option A (manual CLI only)** — `shadow_mode_cli.py`, mirroring `runtime_control_cli.py`'s exact established pattern: `observe <symbol>`, `evaluate`, `status`. This was the recommended choice because it adds **zero** new automatic execution path and **zero** new write-capable HTTP route — the original Phase 2B spec explicitly forbade both ("no background thread that starts automatically," "No POST, PUT, PATCH, or DELETE endpoints... in this phase"). `observer.observe_and_predict()` and `evaluator.evaluate_pending()` still have exactly one caller each: a human running this script by hand.

## 1. Files Changed

| File | Change |
|---|---|
| `agents/shadow_mode/store.py` | Added `count_observations_since()`, `count_predictions_since()`, `count_evaluated_outcomes_since()` — three new read-only SELECT helpers. |
| `agents/shadow_mode/api.py` | `get_status()` extended with `observations_today`, `predictions_today`, `evaluated_outcomes_today`, `current_win_rate`. |
| `templates/sysadmin.html` | New today-counters row in the existing Shadow Mode panel (4 metrics, read-only, no new button). |
| `shadow_mode_cli.py` (new) | Manual operator CLI — the only caller of `observe_and_predict()`/`evaluate_pending()` anywhere in the codebase. |
| `test_shadow_mode_read_only.py` | +4 tests (`TestTodayCounters`). |

**No changes to:** `app.py` (no new route), any broker/trading/paper-order module, `agents/runtime/scheduler.py`, `policy_engine.py`, `scheduling_control.py`, `agents/config.py`. Confirmed via `git diff --stat master` — only the 5 files above (plus the pre-existing, excluded `.claude/settings.local.json`).

## 2. Read-Only Validation Report

- **Mechanical pipeline validation:** confirmed end-to-end against throwaway databases (never production) — inserting observations/predictions, evaluating them against synthetic candles (all four classifications: correct/incorrect/partial/expired), computing metrics, and running `shadow_mode_cli.py`'s actual subprocess dispatch (`status`/`observe`/`evaluate`) against a fresh DB. All passed.
- **Today-counter correctness:** verified a row timestamped yesterday is excluded from `observations_today`/`predictions_today` while still counting toward the all-time totals; a row timestamped "now" is included in all three today-counters; `current_win_rate` matches `evaluator.compute_metrics()`'s own `win_rate` exactly.
- **Dashboard:** template renders cleanly with the new `#shadow-today-counters` element present.

## 3. Exact SQL Counts from the Three Shadow Tables

Read directly from the live production database (`/root/oi_dashboard/oi_history.db`, read-only connection):

```
shadow_observations = 0
shadow_predictions  = 0
shadow_outcomes     = 0
```

**Why zero, honestly explained:** Shadow Mode has no automatic trigger by design (per the original Phase 2B safety requirement, reaffirmed in this task). Nothing has ever called `observer.observe_and_predict()` against production — not the previous deployment verification (which deliberately used only throwaway databases), and not this implementation pass (writing demo/stale data to the live shadow tables under the label of "validation" would misrepresent what "live market tick" evidence actually means — see §4). These are the true, current, unpadded production counts.

## 4. Screenshot/API Evidence After the Next Live Market Session

**Not available yet — cannot be honestly produced today.** It is currently **Saturday, 2026-08-08, 22:40 IST**; the live server's own logs confirm the market is closed for every tracked symbol (`"Market closed (Weekend) for <symbol> -- skipping API calls"`). You separately confirmed today and tomorrow are both market holidays. I verified read-only that `market_data.get_snapshot('NIFTY')` against the real production database currently returns Friday 2026-08-07's last stored cycle (`as_of_ts: 2026-08-07T15:29:55`) — genuinely stale data, not a live tick. I deliberately did **not** run `shadow_mode_cli.py observe` against production using this stale snapshot: doing so would write a real row to `shadow_predictions` that isn't actually evidence of live-tick observation, and could misleadingly pad the "observations/predictions today" counters or the win-rate sample with a non-representative entry once real trading resumes.

**To capture this evidence once the market reopens:**
```
python3 shadow_mode_cli.py observe NIFTY
python3 shadow_mode_cli.py observe BANKNIFTY
# ... repeat for other tracked symbols, or run periodically through the session ...
python3 shadow_mode_cli.py evaluate
python3 shadow_mode_cli.py status
```
run by an operator during live trading hours (or by me, if you ask me to at that time) — then a follow-up report can show real `shadow_observations`/`shadow_predictions`/`shadow_outcomes` rows and populated dashboard counters. I'm flagging this gap explicitly rather than fabricating results from data that doesn't exist yet.

## 5. Safety Confirmation

```
RUNTIME_SCHEDULER_ENABLED == False
RUNTIME_CONTROL_API_ENABLED == False
trading_intelligence.is_schedulable() == False
quant_researcher.is_schedulable() == False
```
All four confirmed via direct import, immediately before and after this implementation.

## Test Results

```
$ python3 -m pytest test_shadow_mode_read_only.py -q
39 passed in 5.24s   (35 previous + 4 new TestTodayCounters)

$ python3 -m pytest test_agents/runtime/ -q
195 passed in 118.84s

$ python3 -m pytest -q
1494 passed, 1 xfailed in 339.21s   (1490 baseline + 4 new)
```
Zero regressions, zero failures.

## Status

Tooling and dashboard counters built and validated mechanically. **Genuine live-market-tick validation (deliverables 2–3 in the original ask) is honestly incomplete** — it requires the next actual trading session, not available today or tomorrow per your own confirmation. Stopping here per "next steps" scope; awaiting the next trading session (or your instruction) before running the CLI against production.
