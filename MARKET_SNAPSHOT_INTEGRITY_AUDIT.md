# Market Snapshot Integrity Audit

**Status: root cause conclusively identified for the spot/ATM divergence — a missing timestamp on Telegram messages, not a wrong-instrument, wrong-expiry-resolution, or cross-source data-mixup bug. The expiry-label question could not be conclusively traced to a specific UI element within this pass; reported honestly as an unconfirmed but structurally plausible hypothesis. No production code changed by this document — a specific, minimal fix is identified and documented below, and approval is requested before implementing it, per instruction.**

## 1. Current data-flow diagram (as traced, with real file:line citations)

There are **two entirely separate live data paths** in this application, not one:

```
PATH A -- the live dashboard / option-chain UI (dashboard.html, /charts-pro, etc.)
  app.py's main per-symbol loop (continuous)
    -> build_strike_rows() computes underlying/atm/rows fresh (app.py:4425)
    -> socketio.emit("update", payload, room=symbol)  [app.py:4924]  -- pushed to the browser
       cadence: REFRESH_INTERVAL=1s for the ACTIVE (in-view) symbol,
                BACKGROUND_REFRESH_SECONDS=45s for background symbols
                (app.py:144, 279)
    -> log_cycle_to_db(...)  [app.py:4665]  -- the SAME underlying/atm/rows,
       same iteration, written to the `cycles`/`strikes` tables

PATH B -- the Trading Intelligence engine (Telegram signals, paper trades)
  agents/runtime scheduler, tick cadence 180s for trading_intelligence
    (RUNTIME_CADENCE_TRADING_INTELLIGENCE_SECONDS, agents/config.py:361)
    -> agents.runtime.agent_runtime._trading_intelligence_cycle()
    -> ai_trading_engine.evaluate()
       -> market_data.get_snapshot(symbol)  [market_data.py:89]
          -> data_access.latest_cycle(symbol)  -- reads the MOST RECENT
             row PATH A already wrote to `cycles` (no independent fetch,
             no separate instrument/source -- same table)
          -> returns MarketSnapshot(..., as_of_ts=cycle["ts"], ...)
       -> oi_engine.generate_signal(...) decides action/entry/SL/target
       -> Recommendation(...) constructed  [ai_trading_engine.py:658-671]
          -- as_of_ts is NOT one of its fields (confirmed: no timestamp
             field exists on Recommendation at all -- see SIGNAL_DATA_CONTRACT.md)
    -> api.run_scheduled_cycle() -> _build_telegram_payload(rec)  [api.py:354]
       -- receives ONLY `rec`, which never carried as_of_ts
    -> telegram_notifier.send_trading_intelligence_signal(payload)
       -> _format_html(payload) renders the message -- grepped the
          entire function: zero timestamp/age field anywhere in the
          output
```

**Both paths ultimately read from the same underlying data** (`cycles`/`strikes`, written by Path A). There is no evidence of a wrong instrument, a wrong token, GIFT NIFTY/futures substitution, or a second independent spot source anywhere in this trace -- confirmed by reading every function in the chain, not inferred. The two paths diverge only in **when** they read it and **whether the reader is told how old what they're looking at is**.

## 2. Root cause of the observed 253-point divergence

**Spot (24505 in the Telegram message vs. 24252 in the live UI) is explained by elapsed time between two different reads of the same evolving series, not by two conflicting simultaneous values.**

- `run_scheduled_cycle()` — the function that decided the Telegram signal — only fires once every 180 seconds. The `cycles` row it read via `get_snapshot()` was whatever was freshest **at that specific tick**, not necessarily anywhere near the moment the user later compared it to the live UI.
- A Telegram BUY message is only sent when `action in ("BUY CE","BUY PE") and confidence >= TI_TELEGRAM_MIN_CONFIDENCE` (75 by default) -- **not every 180-second tick produces one.** The most recent Telegram signal for a given symbol can be considerably older than 180 seconds; there is no cap.
- The Telegram message carries **zero indication of its own age** — verified by grep across `telegram_notifier.py`: no `timestamp`, `as_of`, or `_ts` field appears anywhere in the message-building code. A message from 3 minutes ago and one from 3 hours ago render identically.
- Meanwhile, the live dashboard/option-chain UI reflects the current instant (1-second refresh for the focused symbol).

**A recipient comparing an undated Telegram message against the always-current live UI has no way to know they are looking at two different points in time.** Given NIFTY's real intraday range, a 253-point drift over an unknown-but-plausibly-substantial gap is consistent with ordinary market movement, not a data-corruption defect.

**Within each snapshot, the numbers are internally consistent, not corrupted**: 24505 → ATM 24500 and 24252 → ATM 24250 are both correct nearest-50-point roundings for their own respective spot values (Phase E's own consistency check, confirmed by direct arithmetic). The inconsistency is *across* two different-aged reads, not a broken ATM calculation.

## 3. The expiry-label question ("Expiry Tomorrow" vs. "01 Sep 2026 W")

**Not conclusively traced to a specific UI element in this pass — reported honestly rather than guessed.** `expiry_intelligence.global_context_from_flags()` computes `tomorrow_expiry_indexes` (`expiry_intelligence.py:216-242`) as a **global, cross-index** concept: "which watched indexes' own NEAREST expiry happens to be exactly 1 day out" — this is a structurally different question from "is THIS symbol's currently-selected/displayed expiry (which could be a later weekly, e.g. 01 Sep 2026 W, deliberately chosen rather than the nearest one) tomorrow." If a "tomorrow" label anywhere in this application is driven by that global nearest-expiry flag rather than the specific expiry actually being displayed, exactly this class of mismatch would result. No template or route was found (via targeted grep across `templates/*.html` and `app.py`) rendering `tomorrow_expiry_indexes` directly, and no expiry-selector UI element was found in `dashboard.html`/`charts_pro.html` — the exact page and code path producing the "Selected expiry: 01 Sep 2026 W" display the user described was not located within this pass. This is flagged as an **open item requiring the user's help identifying which page/screen showed it**, not resolved here.

## 4. Canonical snapshot design

See `SIGNAL_DATA_CONTRACT.md` for the full `MarketSnapshot` field-by-field design. Headline: `market_data.MarketSnapshot` (the dataclass, `market_data.py:34`) already carries `as_of_ts` — the fix is not building a new snapshot concept, it's **not dropping the field that already exists** on its way into `Recommendation`/the Telegram payload.

## 5. Snapshot integrity rules (proposed, not implemented)

Per the requested LEVEL 0 gate: before any signal is generated, the snapshot's own `as_of_ts` should be checked against a defensible staleness tolerance, and — separately, regardless of tolerance — the Telegram message itself should always disclose the snapshot's age so a stale-but-under-tolerance signal is still honestly labeled.

| Rule | Status |
|---|---|
| Telegram message must carry the snapshot's `as_of_ts` / age | **PROPOSED — STRAIGHTFORWARD.** The data already exists (`snapshot.as_of_ts`); it is dropped between `evaluate()` and `Recommendation`. Adding one field and threading it through is the entire fix. |
| A staleness tolerance beyond which a signal is rejected (`DATA_STALE`) | **PROPOSED — REQUIRES VALIDATION.** No existing tolerance value exists anywhere in this codebase to reuse; inventing one now would violate "do not invent thresholds." A defensible number would need to look at the real distribution of `(signal_time - as_of_ts)` gaps in production, which isn't instrumented yet — this rule itself is the prerequisite for setting the other. |
| Instrument/token identity check | **NOT NEEDED per this trace** — both paths read the same `cycles` table row for the same symbol; no evidence of an instrument/token mixup anywhere. |
| Cross-source (Angel vs. NSE) spot reconciliation | **Out of scope for this divergence** — the trace shows one source (Angel, via `cycles`) feeding both paths; NSE cross-check data is a separate, already-understood secondary signal (see `oi_engine.generate_signal()`'s dual-source logic), not implicated here. |

## 6. Tests required (design only, matching `FAILURE_TAXONOMY.md`'s conventions)

The 8 test cases requested map as follows once the timestamp field exists:
- **Tests 1-4** (spot/ATM consistency checks) are already effectively covered by `test_oi_engine_signal_math.py`'s entry-price and structural-invariant tests — spot and ATM are read from the same row today, so a mismatched-pair scenario as described can't currently arise from this trace; these tests would mainly serve as regression coverage if a genuinely new spot/ATM computation is ever introduced.
- **Test 5** (expiry metadata label vs. real expiry) needs the open item in §3 resolved first — cannot write a meaningful test against code that wasn't located.
- **Test 6** (incompatible spot/option-chain timestamps) and **Test 7** (Telegram payload spanning two snapshot IDs) both require the `as_of_ts`-on-`Recommendation` fix first; today there is only one timestamp in the whole chain (the `cycles` row's own `ts`), so there is nothing to compare against yet.
- **Test 8** (valid snapshot, high confidence, still allowed) already passes today implicitly — nothing in the current pipeline blocks a high-confidence signal on data-integrity grounds, which is itself worth stating plainly: there is currently no `DATA_INVALID`/`SNAPSHOT_INVALID` veto path at all for this specific class of issue.

## 7. Production-risk assessment

**Low risk, real user-trust cost.** No evidence was found of the system trading (paper or otherwise) on inconsistent data — `_check_open_trade_exit()` and `generate_signal()` both operate on one snapshot at a time, read once per decision, never mixing two ages within a single decision. The risk is entirely in the **Telegram communication layer**: a subscriber reading an old, undated signal and reasonably assuming it reflects the current market. This is a trust/UX defect with a small, safe, additive fix, not a trading-safety defect.

## 8. Exact minimal change required (documented, NOT implemented — approval requested)

1. Add `as_of_ts: str | None = None` to `Recommendation` (`ai_trading_engine.py`, alongside the other optional fields near the end of the dataclass).
2. In `evaluate()`, pass `as_of_ts=snapshot.as_of_ts` into every `Recommendation(...)` construction (the real-signal path and the `_no_trade()`/HOLD paths, for consistency).
3. In `api._build_telegram_payload()`, include `"as_of_ts": rec.as_of_ts` in the returned payload dict.
4. In `telegram_notifier._format_html()`, render it — e.g. a line such as `f"🕒 As of: <b>{payload.get('as_of_ts', 'unknown')}</b>"` near the top of the message, before "Suggested Trade."
5. Add regression tests mirroring `test_telegram_notifier.py`'s existing style, asserting the timestamp appears in the rendered message.

This is a small, additive, backward-compatible change (a new optional field, a new rendered line) — no existing field, formula, or behavior is altered. **Not implemented in this PR, per the explicit instruction to stop and request approval first.**
