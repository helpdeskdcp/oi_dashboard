# Monday Market-Open Checklist — Shadow Mode Live Validation

A step-by-step execution sheet for the first live-market session after deployment. Companion to `docs/SHADOW_MODE_OPERATOR_RUNBOOK.md` — read that first if you haven't already.

Run every command from `/root/oi_dashboard`.

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
