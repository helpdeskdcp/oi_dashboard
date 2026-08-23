# Production State

Live snapshot of `bramha.cloud` (Brahma Autonomous Trading Intelligence / IDaddy AI).
Update this file, don't append to it -- it reflects current state, not history.

## Deployment

- **main @ latest merged**: PR #42 (`2508019`) + pending frontend-redesign-continuation PR (this update)
- **Process**: `run_forever_vps.sh` crash-loop supervisor + `python3 app.py`, port 5050
- **Deploy procedure**: `kill -TERM` (always ignored by this threading/SocketIO app) → `kill -KILL` → supervisor auto-restarts within ~8s. Template-only changes need no restart (`TEMPLATES_AUTO_RELOAD=True`); any `app.py`/module change does.
- **Trading mode**: PAPER only. No code path anywhere in the repo can reach a real broker order (`broker_execution.py`'s `NullBrokerExecutor`; enforced by `test_agents/trading_intelligence/test_safety.py`'s AST scan). Confirmed structural, not just flag-off.

## Recently merged (this session, chronological)

| PR | What |
|---|---|
| #30 | Expiry-contract-identity bug fix — `ti_paper_trades` (original fix) |
| #32 | Same bug class — `paper_orders` |
| #33 | Same bug class — `paper_trades`/`scalp_paper_trades`/`v3_paper_trades` |
| #34 | Candle-freshness data wired into Operations Dashboard (was computed, never shown) |
| #35 | Post-Market Summary Report (mirrors existing Pre-Market Report) |
| #36 | Strategy Registry — inventory of every `TI_ENABLE_*` flag |
| #37 | Momentum-confirmation backtest (real evidence: keep OFF), dual-probability blocker documented, `ti_paper_trades` #76 relabeled |
| #38 | Fixed `AngelOneFetcher.find_nearest_expiry()` — was resolving an already-expired contract (no `>= today` filter); now delegates to `expiry_intelligence.get_nearest_expiry()` |
| #39 | Fixed a null-guard crash bug in `trading_intelligence.html` (`total_ce_oi.toLocaleString()` with no guard, could abort the whole 15s refresh cycle); added Recent Closed Trades table + upgraded 2 sections |
| #40 | Execution State panel: added live LTP + TARGET_HIT/SL_HIT/ACTIVE status, reusing already-logged data (zero new broker calls) |
| #41 | `PRODUCTION_STATE.md` + `AI_HANDOFF.md` added for dual-agent (Claude/Codex) collaboration |
| #42 | Codex review fixes: `get_expiry_status()` fails closed instead of degrading to a past date; `execution_state` is now expiry-aware (won't report live LTP past its own contract's expiry); matplotlib dependency confirmed correct |
| (pending) | Frontend redesign continuation: sidebar rebrand (BATI → IDaddy) and persistent desktop rail were left mid-flight in the working tree with only `dashboard.html` accounting for the new push-content layout; fixed the other 20 templates that would otherwise render with content hidden behind the now-always-visible sidebar at ≥1180px. `CODEX_REVIEW.md` added as the third dual-agent handoff file. |

## Feature flags (`.env`, live as of last check)

**ON**: `RUNTIME_SCHEDULER_ENABLED`, `TI_ENABLE_STRUCTURE_ALERTS`, `TI_ENABLE_STRUCTURE_TUNING`, `TI_ENABLE_VIRTUAL_TRAILING`, `TI_ENABLE_CONTROL_CENTER_UI`, `TI_ENABLE_AI_LIVE_SNAPSHOT_UI`, `TI_ENABLE_PERFORMANCE_ANALYTICS_UI`, `TI_ENABLE_TRADE_GUARDIAN_SHADOW`, `TI_ENABLE_EXECUTION_STATE_UI`, `TI_ENABLE_EXECUTION_STATE_SHADOW`

**OFF (deliberate)**:
- `TI_ENABLE_MOMENTUM_CONFIRMATION` — real backtest evidence against it (see `MOMENTUM_CONFIRMATION_BACKTEST_REPORT.md`): the one symbol with a trustworthy sample (BANKNIFTY) got measurably worse.
- `TI_ENABLE_SIGNAL_GRAPH_SHADOW`, `TI_ENABLE_REGIME_FILTER_SHADOW` — shadow-computation flags (not pure UI), left for an explicit decision, not auto-enabled.

## Known-good invariants (don't regress)

- Paper-trading only — no `place_order`/broker-order code exists.
- Every trade-lifecycle table (`ti_paper_trades`, `paper_orders`, `paper_trades`, `scalp_paper_trades`, `v3_paper_trades`) stamps `expiry_date_at_entry` at open and checks it before matching by strike on every subsequent cycle — this is the fix for the expiry-contract-identity bug class; don't reintroduce strike-only matching.
- `find_nearest_expiry()` must never return an already-past date (PR #38's regression class). `expiry_intelligence.get_expiry_status()` now enforces this at the source: if every listed expiry is already past, it raises `ExpiryDataUnavailable` (fail closed) rather than degrading to the most-recent past date — every real caller already handles that exception as an honest unavailable state.
- `execution_state` rows carry their own `expiry_date_at_entry` (set at creation from the SAME `symbol_expiry` the live cycle resolved that moment). `list_executions_with_live_ltp()` only reports `live_ltp`/`hit_status` while that date is known AND still `>= today` — cycles/strikes carry no expiry column, so a strike-number match alone can't prove contract identity once the original contract has expired.
- Tests never touch a real broker session (`AngelOneFetcher.__new__()` bypass pattern) and never hit `/live-positions` (real Angel One login risk).
- `matplotlib>=3.8.0` (requirements.txt) is correctly declared and installed in this project's own venv (`/root/oi_dashboard/venv`, confirmed 3.11.1) and via CI's `pip install -r requirements.txt`. It is NOT installed in the bare system `python3` (`/usr/bin/python3`, PEP-668-protected, apt's own `python3-matplotlib` package is only 3.6.3 — below the declared constraint). Always run tests through the project venv, never bare `python3`/system `pytest`.

- Any template that includes `_sidebar_css.html` and has its own `body { margin: 0; ... }` (or `html, body { margin: 0; }`) rule MUST re-declare `@media (min-width: 1180px) { body { margin-left: 280px; } }` AFTER that rule in the same `<style>` block — the shared include's desktop-push rule is a longhand `margin-left`, and a same-specificity `margin: 0` shorthand appearing later in source order silently wins the cascade and zeroes it back out. All 21 templates that render the sidebar do this today (see the frontend-redesign-continuation PR); a new page copying an existing `<style>` block needs the same override line, not just the include.

## Open engineering decisions (not bugs, need a call)

- `dual_probability_store.py` / shadow-node wiring — blocked on evidence (see `DUAL_PROBABILITY_CALIBRATION_REPORT.md`), not a code task.
- `TI_ENABLE_SIGNAL_GRAPH_SHADOW` / `TI_ENABLE_REGIME_FILTER_SHADOW` — safe-by-design (shadow-only, no broker path) but adds live computation load; needs a decision, not a default flip.
- Frontend redesign scope beyond the sidebar/rail: the requested institutional-style information hierarchy reorg (market status bar, primary/secondary sectioning per page), mobile-specific priority ordering, and tabs/collapsible sections for advanced data are **not started**. Only the sidebar rebrand + persistent-desktop-rail layer (already mid-flight when this session picked it up) has been completed and made consistent across all pages. Whoever continues this should treat each page's actual content reorganization as a separate, page-by-page pass — 21 templates is too much surface to redesign blind in one shot.
