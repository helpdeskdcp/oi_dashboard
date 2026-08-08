# Shadow Mode Operator Runbook

A guide for a human operator running Milestone 12's Shadow Mode — no assumed system/engineering knowledge beyond "how to run a command in a terminal." If you can SSH into the server and type commands, you can operate Shadow Mode.

---

## 1. Purpose

Shadow Mode watches the market and writes down what the AI *would have* traded — direction, confidence, target — without ever actually trading. It's a way to find out, honestly, whether the AI's signals are any good, before anyone risks real (or even paper) money on them.

Nothing in Shadow Mode places an order. Nothing in Shadow Mode is automatic — every observation happens because a human ran a command.

---

## 2. Safety Guarantees

These are not promises — they are things you can verify yourself, right now, with a command:

| Guarantee | How to verify it yourself |
|---|---|
| No broker order is ever placed | `grep -rn "place_order\|SmartConnect" agents/shadow_mode/` → no output |
| No paper trade is ever created | `grep -rn "paper_orders\|paper_trades" agents/shadow_mode/store.py` → only appears in comments explaining it's avoided |
| The runtime scheduler is off | `python3 -c "from agents import config; print(config.RUNTIME_SCHEDULER_ENABLED)"` → `False` |
| Nothing runs automatically | `shadow_mode_cli.py` is the *only* thing that ever calls the observation/evaluation functions. Nothing in `app.py`'s startup, no thread, no cron job, calls them. |
| `trading_intelligence` and `quant_researcher` can never be scheduled | `python3 -c "from agents.runtime import scheduling_control as sc; print(sc.is_schedulable('trading_intelligence'), sc.is_schedulable('quant_researcher'))"` → `False False` |

If any of these ever come back differently than shown above, **stop and escalate** — do not continue operating Shadow Mode.

---

## 3. Preconditions Before Market Open

Before the market opens, confirm:

1. The live server is running:
   ```
   curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5050/login
   ```
   Expect `200`.

2. You are in the project directory:
   ```
   cd /root/oi_dashboard
   ```

3. Today's date/market status — if it's a weekend or a market holiday, there is nothing to observe; skip to another day.

That's it. There's no service to "start" — Shadow Mode has no running process of its own; every command below runs, does its work, and exits.

---

## 4. Manual Observation Command

This is the command that watches the market and logs what the AI would have done:

```
python3 shadow_mode_cli.py observe NIFTY
```

Replace `NIFTY` with any tracked symbol (`BANKNIFTY`, `FINNIFTY`, `SENSEX`, etc.).

**What it does:** reads the most recent already-stored market snapshot for that symbol, computes a hypothetical signal, and writes exactly one row to `shadow_observations` and one row to `shadow_predictions`. It does **not** contact the broker — it only reads data the main dashboard already fetched and stored.

**What you'll see:**
```
NIFTY: observation_id=1 prediction_id=1
  signal_type='BUY CE' direction='CE' confidence=72
  reason: Bullish OI buildup confirmed by price action...
```
or, if no market data has been logged yet for that symbol this cycle:
```
NIFTY: no market snapshot available yet -- nothing recorded.
```
Both are normal, healthy outcomes — the second just means "nothing to observe right now," not an error.

**If you want to preview without writing anything** (safe to run anytime, including outside market hours):
```
python3 shadow_mode_cli.py observe NIFTY --dry-run
```
This prints exactly what *would* be recorded and ends with:
```
DRY RUN — NO DATABASE WRITES PERFORMED
```

**To save that preview to a file for offline inspection:**
```
python3 shadow_mode_cli.py observe NIFTY --dry-run --export-json out.json
```

---

## 5. Manual Evaluation Command

Once enough time has passed for a prediction's outcome to be judgeable (predictions have a ~45-minute validity window), run:

```
python3 shadow_mode_cli.py evaluate
```

**What it does:** looks at every prediction that hasn't been graded yet, compares it against the archived price candles since it was made, and records whether it was `correct`, `incorrect`, `partial`, or `expired` (not enough data / no directional signal to judge).

**What you'll see:**
```
Evaluated 2 prediction(s).
  prediction_id=1 -> correct
  prediction_id=2 -> expired
```

**Dry-run version** (shows what the grading *would* be, changes nothing):
```
python3 shadow_mode_cli.py evaluate --dry-run
```

---

## 6. Counter Interpretation

Run `python3 shadow_mode_cli.py status` at any time to see:

| Field | What it means |
|---|---|
| `observation_count` / `prediction_count` | All-time totals, since Shadow Mode started keeping records |
| `observations_today` / `predictions_today` | Just today's activity — resets to 0 each day at midnight |
| `evaluated_outcomes_today` | How many predictions got graded (correct/incorrect/partial/expired) today |
| `current_win_rate` | Of everything ever graded as correct/incorrect/partial (not counting `expired`), what fraction was `correct`. `null`/`None` means nothing has been graded yet — not "0% win rate," genuinely "no data yet." |
| `last_prediction_ts` | Timestamp of the most recent prediction, or `never` |

A `current_win_rate` near 50% with only a handful of predictions isn't meaningful yet — this needs weeks of data before it says anything real about the AI's edge. Don't over-interpret an early number.

---

## 7. Dashboard Interpretation

Open `/admin/sysadmin` in a browser (admin login required) and find the **"🔍 SHADOW MODE — READ ONLY"** panel. You should see:

- A green **"NO ORDERS ARE PLACED IN SHADOW MODE"** banner — always present, always green. If this banner is ever missing, something is wrong with the deployment; escalate.
- A row of four numbers: observations today, predictions today, evaluated outcomes today, current win rate.
- A table of the last 10 predictions, each with an "Outcome" column showing a green badge (correct), red badge (incorrect), or an amber "partial"/"expired"/"pending" label.

The panel refreshes itself every 20 seconds — you don't need to reload the page.

---

## 8. Expected Monday Workflow

See `docs/MONDAY_MARKET_OPEN_CHECKLIST.md` for the exact step-by-step sheet. In short: verify the server is healthy, run `observe` a few times through the morning session for each tracked symbol, run `evaluate` later in the day once predictions have had time to mature, and check the dashboard/`status` command shows non-zero activity.

---

## 9. Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| `observe SYMBOL` always says "no market snapshot available yet" | The main dashboard hasn't logged a fresh cycle for that symbol yet (market closed, or the symbol isn't in the tracked list) | Wait for the next cycle, or check the symbol name is spelled exactly as the dashboard uses it (e.g. `BANKNIFTY`, not `bank nifty`) |
| `evaluate` says "0 pending prediction(s)" every time | Either nothing has been observed yet, or everything already has an outcome | Run `observe` first; check `status` for `prediction_count` |
| `--export-json` fails with "could not write export file" | The path/directory doesn't exist or you don't have permission to write there | Use a path in a directory you can write to, e.g. your home directory |
| Dashboard panel shows "control-plane state unavailable" or similar | Unrelated to Shadow Mode — this is the separate Runtime Control panel's own message, not Shadow Mode's | Not a Shadow Mode issue; Shadow Mode's own panel is a distinct section further down |
| Any command raises a Python traceback you don't understand | Something genuinely unexpected | Copy the full error and escalate — do not try to work around it by guessing |

---

## 10. Rollback Procedure

Shadow Mode has no "on" switch to roll back — there's no process to stop, no flag to flip. If something goes wrong:

1. **Stop running commands.** Since nothing is automatic, simply not running `observe`/`evaluate` again halts all Shadow Mode activity immediately.
2. **The data is isolated.** Everything Shadow Mode has ever written lives in exactly three tables: `shadow_observations`, `shadow_predictions`, `shadow_outcomes`. No other table in the database is ever touched by Shadow Mode.
3. **If you need to wipe Shadow Mode's data** (e.g. it was polluted by a mistaken test run against production): this requires a database operation and should not be done without the same care as any other production database change — take a backup first, and don't do this unilaterally. Escalate to whoever manages the deployment rather than running `DROP TABLE`/`DELETE` commands yourself.
4. **There is nothing to "restart."** Shadow Mode isn't a running service — if the main dashboard process itself needs restarting for an unrelated reason, Shadow Mode's code comes back with it automatically; no separate action is needed.
