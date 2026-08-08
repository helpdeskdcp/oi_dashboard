# Milestone 12 — Phase 2C: NSE/MCX Market Timing Update (Effective 2026-08-03)

**Scope:** update the app's live market-hours gating logic to the new NSE/MCX timing rules. This is a data/config correctness fix to `app.py`'s `is_market_open()` — the function that decides whether to call the broker API at all — and its independent `agents/runtime/market_session.py` counterpart. **No broker, order, scheduler, or runtime-control logic was touched.**

Branch: `worktree-m12-phase2c-market-hours`, based on `master@4b9bb90`.

## What Changed

### NSE (`app.py`'s `MARKET_HOURS` dict)

| Segment | Old | New | Symbols affected |
|---|---|---|---|
| Equity F&O (index options/futures) — `index_option` | 09:15–15:30 | **09:15–15:40** | NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX |
| Cash/normal index session — `index_spot` | 09:15–15:30 | unchanged | INDIA VIX (not F&O-traded, tracks the plain cash window) |
| F&O-eligible cash stocks — new `fno_cash_stock` | — | 09:15–15:15 (continuous), CAS runs 15:15–15:35 | **none currently tracked** — see caveat below |
| Non-F&O cash stocks — new `non_fno_stock` | — | 09:15–15:30 | **none currently tracked** |

### MCX (new agri/non-agri split, replacing the single `commodity_option` type)

| Segment | Old | New | Symbols affected |
|---|---|---|---|
| Agricultural — new `commodity_agri` | — | 09:00–17:00 | **none currently tracked** (this app has no agri commodity symbols) |
| Non-agricultural (metals/energy/bullion) — `commodity_nonagri` (renamed from `commodity_option`) | flat 09:00–23:30 | 09:00, close **shifts seasonally**: 23:55 IST during the DST-linked window, 23:30 IST outside it | CRUDEOIL, CRUDEOILM, NATURALGAS, NATGASMINI, GOLD, GOLDM, SILVER, SILVERM (all 8 currently-tracked MCX symbols) |

New helper functions in `app.py`: `_mcx_nonagri_close(now)` (returns the seasonally-correct close hour/minute) and `_nth_weekday_of_month()` (a small date-arithmetic utility it uses), plus `_resolve_market_hours(cfg, now)` — a shared helper both `is_market_open()` and the intraday auto-square-off buffer calculation (`update_paper_orders`, previously reading the static dict directly) now go through, so both always agree on the real close time including the seasonal MCX shift.

### ⚠️ Important caveat — please read before relying on this near a seasonal boundary

**The exact MCX summer/winter cutover dates are set by MCX's own periodic circular and are not published in a form I had access to for this task.** I implemented the seasonal window using the standard US DST calendar rule (2nd Sunday of March through the 1st Sunday of November) as a documented, clearly-labeled approximation — the same "approximate, verify against exchange circular" caveat this code already carried before this update (`app.py`'s prior comment on the MCX entry). If MCX's actual 2026 cutover dates differ from the US DST calendar, the close time will be off by up to a few weeks around each transition. **I'd recommend verifying against the live MCX circular closer to the actual transition dates**, rather than treating this as authoritative.

Similarly, the exact NSE "Equity F&O 3:40 PM" / "F&O-eligible cash stock 3:15 PM + CAS to 3:35 PM" timings were taken directly from your message as the effective 2026-08-03 rule and implemented as given — I have no independent exchange circular to cross-check them against, so please confirm they match if you have the source document.

### Renamed type, not just retimed

`"commodity_option"` (the old single MCX type) is renamed to `"commodity_nonagri"` for all 8 currently-tracked MCX symbols, since none of them are agricultural. I found and fixed **5 other call sites** beyond market-hours that checked `cfg["type"] == "commodity_option"` for unrelated purposes (broker exchange-segment resolution, expiry-day detection, candle-token resolution) — these now check membership in a new `COMMODITY_TYPES = ("commodity_agri", "commodity_nonagri")` tuple instead of a single string, so a future agricultural commodity wouldn't silently fall through this logic. One of these 5 sites was in `history_engine.py`, not `app.py` — found via a full-repo grep sweep after the initial targeted edit, not part of the original scoped area.

### `agents/runtime/market_session.py`

Independently updated `NSE_CLOSE` from `(15, 30)` to `(15, 40)` to match the new Equity F&O close — this module deliberately never imports `app.py` (documented in its own docstring, to avoid pulling a ~7000-line Flask app with live broker-session machinery into the scheduler process), so it's a second, hand-kept-in-sync copy, same as before this change. It remains NSE-index-only by design (MCX hours are still intentionally not modeled here, per its own existing scope).

## Files Changed

- `app.py` — `MARKET_HOURS` dict restructured (6 types instead of 3), `_mcx_nonagri_close()`/`_nth_weekday_of_month()`/`_resolve_market_hours()` added, `is_market_open()` and the square-off buffer calculation both route through `_resolve_market_hours()`, `COMMODITY_TYPES` added, 8 `SYMBOLS` entries retyped, 5 non-market-hours call sites fixed to check `COMMODITY_TYPES` membership.
- `agents/runtime/market_session.py` — `NSE_CLOSE` updated, docstring updated.
- `history_engine.py` — 1 call site fixed (found via full-repo sweep).
- `test_agents/runtime/test_market_session.py` — 1 existing test's boundary updated (15:30 → 15:40).
- `test_market_hours.py` (new) — 26 tests covering every new rule.

No `app.py` route, broker call, order-placement code, or `agents/runtime/scheduler.py`/`lifecycle.py`/`scheduling_control.py`/`config.py` touched — confirmed via `git diff --stat` against each, zero diff.

## Test Results

```
$ python3 -m pytest test_market_hours.py -q
26 passed

$ python3 -m pytest test_agents/runtime/test_market_session.py test_paper_orders_phase3.py -q
26 passed

$ python3 -m pytest test_agents/runtime/ -q
195 passed

$ python3 -m pytest -q
1541 passed, 1 xfailed
```

1541 = 1515 (pre-existing baseline) + 26 new. Zero failures, zero regressions. The existing `TestIntradaySquareOff` test in `test_paper_orders_phase3.py` (which reads `app.MARKET_HOURS["index_option"]` dynamically rather than hardcoding 15:30) passed unchanged, confirming the square-off buffer logic stays correct after the retiming.

## Status

Implementation complete, fully tested, not yet deployed to the live process. **Given the seasonal-date caveat above**, I'd suggest reviewing the DST-window approximation before this reaches production, and would want explicit confirmation before restarting the live server to pick this up (the live process currently runs the old 15:30/flat-23:30 logic — this only takes effect after a restart, same as every prior config-only change in this project).
