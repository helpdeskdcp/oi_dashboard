# Monday Market-Open Checklist — Shadow Mode + Intelligence Orchestrator Live Validation

A step-by-step execution sheet for the first live-market session after deployment. Companion to `docs/SHADOW_MODE_OPERATOR_RUNBOOK.md` — read that first if you haven't already.

Run every command from `/root/oi_dashboard`.

This checklist now covers two independent, unrelated read-only features deployed to production: **Shadow Mode** (below) and the **Intelligence Orchestrator** (Milestone 13, Phase 1 — see the dedicated section near the end of this file). Kept as one file rather than a second `MONDAY_MARKET_OPEN_CHECKLIST.md` since an operator doing first-live-session validation wants a single execution sheet, not two to remember.

---

## 09:05 — Pre-open health check

- [ ] Verify the server is healthy:
  ```
  curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5050/login
  ```
  Expect `200`.

- [ ] Verify the Shadow Mode endpoints exist and are correctly gated (expect `302` — redirect to login — for an unauthenticated request; a `404` here means Shadow Mode isn't deployed on the running process):
  ```
  curl -s -o /dev/null -w "status: %{http_code}\n" http://127.0.0.1:5050/api/shadow/status
  curl -s -o /dev/null -w "recent: %{http_code}\n" http://127.0.0.1:5050/api/shadow/recent
  curl -s -o /dev/null -w "performance: %{http_code}\n" http://127.0.0.1:5050/api/shadow/performance
  ```

- [ ] Log into `/admin/sysadmin` in a browser and capture a screenshot of the full page, including the "🔍 SHADOW MODE — READ ONLY" panel and its green "NO ORDERS ARE PLACED IN SHADOW MODE" banner. This is your **before** baseline — save it alongside the **after** screenshot from 09:20 below.

- [ ] Sanity-check current counts (should still be from before today, or zero if this is the very first live session):
  ```
  python3 shadow_mode_cli.py status
  ```

---

## 09:10 — First live observation

- [ ] Run, for each tracked symbol you want to validate (start with NIFTY):
  ```
  python3 shadow_mode_cli.py observe NIFTY
  ```
  Repeat for `BANKNIFTY`, `FINNIFTY`, `SENSEX`, or any other symbol you want live evidence for.

- [ ] Verify via `status`:
  ```
  python3 shadow_mode_cli.py status
  ```
  - [ ] `observations_today > 0`
  - [ ] `predictions_today > 0`

- [ ] If either is still `0` after running `observe`, check the command's own output first — a message like `"NIFTY: no market snapshot available yet"` means the main dashboard hasn't logged a fresh cycle yet; wait a few minutes and retry rather than treating this as a failure.

---

## 09:20 — First evaluation pass

- [ ] Run:
  ```
  python3 shadow_mode_cli.py evaluate
  ```

- [ ] Verify:
  - [ ] `evaluated_outcomes_today >= 0` (it's normal for this to still be `0` this early — predictions have a ~45-minute validity window before they're gradeable; don't expect real outcomes yet at 09:20 for a 09:10 prediction)
  - [ ] No exceptions/tracebacks appeared in the command's output
  - [ ] Check the app's log file for any errors during this window:
    ```
    grep -i "error\|traceback" app_stdout.log | tail -20
    ```

- [ ] Capture the **after** screenshot of `/admin/sysadmin`'s Shadow Mode panel, showing the updated counters and the new row(s) in the "last 10 observations" table.

---

## Evidence to Capture (for the validation report)

- [ ] CLI output from every `observe`/`evaluate` command run this session (copy/paste, or `script`/redirect to a log file)
- [ ] `curl` output (or browser screenshot) of `/api/shadow/status`
- [ ] `curl` output (or browser screenshot) of `/api/shadow/recent`
- [ ] Before/after dashboard screenshots (09:05 and 09:20)
- [ ] Exact `shadow_observations`/`shadow_predictions`/`shadow_outcomes` row counts, before and after (read-only, from an operator with database access):
  ```
  python3 -c "
  import sqlite3
  conn = sqlite3.connect('file:oi_history.db?mode=ro', uri=True)
  for t in ('shadow_observations', 'shadow_predictions', 'shadow_outcomes'):
      print(t, conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0])
  conn.close()
  "
  ```

## Repeat Through the Session

Continue running `observe` every 15–30 minutes through the trading session for each symbol you're tracking, and `evaluate` periodically (every hour is reasonable) to keep grading maturing predictions. There is no automation — this only happens when you run the commands.

## Success Criteria

If, by the end of the session:
- `shadow_observations` and `shadow_predictions` both show real rows with today's timestamps,
- at least some `shadow_outcomes` rows exist (classified `correct`/`incorrect`/`partial`/`expired`),
- the dashboard panel displays these counts and the last-10-observations table,
- and **no order was placed, no paper trade was created, and the scheduler never activated** (re-verify with the commands in the Runbook's §2 at the end of the session, same as at the start) —

then:

> **"Shadow Mode is receiving live market observations while remaining fully read-only and non-executing."**

---

## Intelligence Orchestrator (Milestone 13, Phase 1) — Live-Market Validation

Companion to `M13_PHASE1_INTELLIGENCE_ORCHESTRATOR_REPORT.md` and `docs/M12_LIVE_SMOKE_CHECK.md` — the smoke check confirmed the endpoint is reachable and shape-correct against stale/weekend data; this section is what to actually watch once NIFTY starts trading for real. Endpoint: `GET /api/intelligence/snapshot?symbol=<SYMBOL>`, admin-gated, read-only, no scheduler involvement.

### 09:05 — Pre-open baseline

- [ ] Capture a baseline snapshot before the market opens (should still reflect Friday's stale close):
  ```
  curl -s -b <your session cookie jar> "http://127.0.0.1:5050/api/intelligence/snapshot?symbol=NIFTY" | python3 -m json.tool
  ```
- [ ] Note the `confidence`, `oi_strength`, and `greeks_alignment` values — expect low/zero `oi_strength` and `"UNAVAILABLE"` greeks alignment on anything but NIFTY/BANKNIFTY (matches the weekend smoke-check findings), or an equivalently stale pattern.

### Through the session — intracycle checks (repeat every 15-30 minutes)

- [ ] **Snapshot updates intracycle.** Re-fetch the same URL and confirm `confidence`/`oi_strength`/`probability_score` actually change between calls once fresh option-chain cycles are being written (they should track the live cycle data, not stay frozen at the 09:05 baseline).
- [ ] **Bias changes when price structure changes.** If NIFTY's underlying makes a real directional move intraday, confirm `bias` eventually reflects it (BULLISH ⇄ BEARISH ⇄ NEUTRAL) rather than staying stuck — a bias that never moves all session on a trending day is worth investigating.
- [ ] **Confidence score movement.** `confidence` (from `compute_trend_meter()`) should show real intraday variation, not a flat constant.
- [ ] **Greeks alignment coherence with bias.** `greeks_alignment` should agree directionally with `bias` (`"BULLISH LEAN"` alongside a `BULLISH` bias, `"BEARISH LEAN"` alongside `BEARISH`) on any cycle where both are resolved (non-`NEUTRAL`, non-`"UNAVAILABLE"`). A persistent mismatch (e.g. `BULLISH` bias with `"BEARISH LEAN"` greeks) would be a real finding worth flagging, since `_greeks_alignment()` derives from the same `generate_signal()` call `bias` normalization is downstream of.
- [ ] **OI strength reacting to fresh option-chain updates.** `oi_strength` (= `generate_signal()`'s own `confidence`) should move off the weekend's `0` once real OI is flowing, at least for NIFTY.
- [ ] **UI panel refreshing correctly.** Open `/admin/trading-intelligence`, confirm the "🧠 Market Intelligence" card updates every ~15s in sync with the existing symbol tabs (switch tabs and confirm the card updates to the newly-selected symbol, not stuck on the previous one).
- [ ] **Snapshot latency observations.** Time a handful of requests (`curl -w "%{time_total}\n"`); note anything unexpectedly slow (the orchestrator does 3-4 real function calls per request — `get_snapshot()`, `load_candles()`, `generate_signal()`, `compute_institutional_entry_score()` — none of which should be slow against locally-stored data, but note it if a request takes more than ~1s).
- [ ] **Logging any stale data conditions.** If any symbol still shows `oi_strength=0`/`"UNAVAILABLE"` greeks well after market open (i.e. real cycles are being written but the orchestrator's output doesn't reflect it), log the symbol, timestamp, and the raw snapshot JSON — that would be a genuine finding, not the expected weekend behavior.

### Success criteria

If, by end of session:
- NIFTY's snapshot has visibly moved off its 09:05 baseline (different `confidence`/`bias`/`oi_strength` at least once),
- the UI panel is confirmed refreshing and symbol-switching correctly,
- no `greeks_alignment`/`bias` contradiction was observed on a resolved cycle,
- and no snapshot request took more than ~1-2s —

then:

> **"The Intelligence Orchestrator is producing snapshots that track real live-market data, remains fully read-only, and the dashboard panel reflects it correctly."**
