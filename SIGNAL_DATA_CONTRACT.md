# Signal Data Contract

The canonical contract between Market Data → Signal Engine → Risk Engine → Telegram, as it exists today, with the one confirmed gap (`MARKET_SNAPSHOT_INTEGRITY_AUDIT.md`) called out explicitly. No code changed by this document.

## The canonical `MarketSnapshot` (already exists)

`agents/trading_intelligence/market_data.py:34`, `MarketSnapshot`:

| Field | Populated from |
|---|---|
| `symbol` | caller's argument |
| `as_of_ts` | `cycle.get("ts")` — the exact `cycles` table row's timestamp |
| `underlying_ltp` | `cycle.get("underlying_ltp")` |
| `atm` | `cycle.get("atm")` |
| `pcr`, `pcr_change`, `max_pain`, `bias` | same cycle row / derived from the last 2 cycles |
| `total_ce_oi`, `total_pe_oi`, `total_ce_oi_change`, `total_pe_oi_change` | summed from that cycle's `strikes` rows |
| `vwap`, `latest_candle`, `volume_today` | derived from the day's candle archive |
| `strikes` | the full list of `StrikeRow`s for that cycle |
| `available` | `False` (with `.reason`) when no cycle has ever been logged for this symbol |

**This is already, structurally, ONE snapshot per read** — every field above comes from the same `data_access.latest_cycle(symbol)` call, the same DB row. There is no evidence anywhere in this trace of one field being read from a different row than another *within* a single `get_snapshot()` call. The integrity problem identified in `MARKET_SNAPSHOT_INTEGRITY_AUDIT.md` is not "fields within one snapshot disagree" — it's "the snapshot a signal was built from is not identified by the time it reaches the recipient."

## Market Data → Signal Engine

`ai_trading_engine.evaluate()` calls `market_data.get_snapshot(symbol, expiry_date=expiry_date)` once per cycle (`ai_trading_engine.py:523`) and derives everything downstream — bias, OI walls, `generate_signal()`'s entry/SL/target, institutional findings, sizing — from that one `snapshot` object and the `rows`/`atm`/`pcr` pulled from it. Confirmed: no second, independent data fetch occurs anywhere inside `evaluate()`.

## Signal Engine → Risk Engine

`agents/risk_manager/risk_decision.py`'s account-level gate consumes the already-decided `Recommendation` (direction, entry, sl, qty) to decide whether to permit the trade at the portfolio level — it does not re-fetch market data or recompute the signal. Out of scope for this specific audit (no divergence found here), noted for completeness of the requested contract.

## Signal Engine → Telegram — the confirmed gap

`Recommendation` (`ai_trading_engine.py:94-138`) carries `symbol`, `action`, `direction`, `strike`, `market_bias`, `confidence`, `probability`, `entry_price`, `sl_price`, `target_price`, `targets`, and several more fields — **but not `as_of_ts`**, even though `evaluate()` has it in scope the entire time (`snapshot.as_of_ts`, available from line 523 onward, never referenced again after that point). `_build_telegram_payload()` (`api.py:354`) can only pass through what `Recommendation` gives it; `_format_html()` (`telegram_notifier.py`) can only render what the payload gives it. The information is not lost due to any transformation bug — it is simply never carried past the point `Recommendation` is constructed.

**This is the entire contract gap.** Every other field in the chain (entry, SL, target, confidence, direction, strike) is threaded through correctly and consistently, verified by direct read of each hop. Only the snapshot's own timestamp is dropped.

## What "ONE SIGNAL = ONE SNAPSHOT" already means and doesn't yet mean in this codebase

**Already true**: every value used to build one `Recommendation` (entry, SL, target, bias, OI totals, PCR) comes from exactly one `market_data.get_snapshot()` call, exactly one `cycles`/`strikes` row. There is no code path found that lets one signal mix fields from two different cycles.

**Not yet true**: the *recipient* of that signal (a human reading Telegram, or a future consumer inspecting `ti_signal_log`) has no way to independently verify which snapshot a given signal came from, or how old it was relative to "now" at read time — because the snapshot's own identity (`as_of_ts`, and more generally a `signal_id`/`snapshot_id` per `SIGNAL_CONTRACT.md`) isn't carried to the point of consumption. The fix identified in `MARKET_SNAPSHOT_INTEGRITY_AUDIT.md` §8 closes this specific gap for `as_of_ts`; a full `snapshot_id` (per the broader `SIGNAL_CONTRACT.md` design from the prior PR) would close it completely, but is a larger change not proposed here.
