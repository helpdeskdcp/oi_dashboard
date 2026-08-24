# Telegram Signal Integrity

**Status: the real trading-signal Telegram path is already a single, clean delivery point. No duplication risk found for actual trading signals. No code changed by this document.**

## Trace: who actually sends what

`telegram_notifier.send_trading_intelligence_signal()` (the real BUY-signal formatter, already fixed for a CE/PE label bug this session, PR #46) has **exactly one call site** for a real trading signal: `agents/trading_intelligence/api.py:328`, inside `run_scheduled_cycle()` — the same single production cycle `PAPER_TRADING_DUPLICATION_AUDIT.md` traces. The call is explicitly gated: `if rec.action in ("BUY CE", "BUY PE") and (rec.confidence or 0) >= config.TI_TELEGRAM_MIN_CONFIDENCE` (`TI_TELEGRAM_MIN_CONFIDENCE=75` by default) — it fires only alongside a real, already-decided actionable recommendation, never independently.

The surrounding comment in `api.py` states this explicitly and correctly: *"ONLY source is this engine's own actionable Recommendation, never the S/R Engine... that pipeline was disconnected."* This is not aspirational — it's the real, current behavior, confirmed by direct read.

**Other real Telegram callers, and why they are not competing publishers of the same event:**

| Caller | Function | Event type |
|---|---|---|
| `structure_alerts.py` | `send_structure_update()` | A **separate S/R system's** (`institutional_levels.py`) role-reversal alerts — a different, already-documented-as-intentional S/R engine (see `ENGINE_REGISTRY.md`), never a trading entry signal |
| `trade_guardian_graph.py` | `send_trade_guardian_update()` | Position-monitoring advisory for an *already-open* position — never a new-entry signal |
| `production_watchdog.py` | `send_admin_alert()` | System health, not a trading signal at all |
| `structure_chart.py` | `send_structure_update()` (preview mode) | Same structure-alert pipeline as above, chart-image variant |

None of these four can produce a `BUY CE`/`BUY PE` entry message — that's structurally reserved to `api.py:328`. They are genuinely different event types (structural-level alerts, position-management advisories, system-health notices), not redundant publishers of the same trading decision. They can fire close together in time (e.g. a structure alert and a real signal for the same symbol on the same day), but that's two different, correctly-labeled message types, not a duplicate or a contradiction of the same signal.

**"Telegram is a delivery layer, never a decision engine"** — already true today for the real trading-signal path. `telegram_notifier.py` only formats and sends what it's given; the gating condition (`action`, `confidence` threshold) lives in `api.py`, upstream of the notifier, matching the same "orchestration decides, delivery just delivers" split the codebase's own comments already describe for `paper_trading.enter_from_recommendation()`.

## Signal state machine

The proposed lifecycle (`CANDIDATE → VALIDATING → VETOED`, or `VALIDATING → APPROVED → PUBLISHED → ACTIVE → TARGET_HIT/SL_HIT/INVALIDATED/EXPIRED`) already has a close, real, already-hardened (PR #42) counterpart: `execution_state.py`'s 13-state machine (`agents/trading_intelligence/execution_state.py:54-75`):

```
SIGNAL → APPROVED → READY → ORDER_INTENT → SUBMITTED → FILLED → MONITORING
  → {TARGET_UPDATE, SL_UPDATE, TRAILING, EXIT_INTENT} → EXIT → COMPLETED
```

**Mapping**: `SIGNAL` ≈ the proposed `CANDIDATE`; `APPROVED` ≈ `APPROVED`; `READY/ORDER_INTENT/SUBMITTED/FILLED` collapse instantly in paper mode (no distinct broker lifecycle) to roughly `PUBLISHED`; `MONITORING` ≈ `ACTIVE`; `EXIT`/`COMPLETED` ≈ `TARGET_HIT`/`SL_HIT`/`EXPIRED` (the specific reason is captured in the transition's `reason` text, not as a distinct top-level state).

**What's missing relative to the proposed design**: there is no `VETOED` state — `execution_state.py`'s machine only ever starts at `SIGNAL` for a trade that already got created; a rejected candidate (NO_TRADE, or a future failure-gate `BLOCKED` verdict) never enters this state machine at all today, it just doesn't produce a row. Adding a `VETOED`/`REJECTED` terminal state reachable from `SIGNAL`/`CANDIDATE` would be a real, well-scoped extension — not proposed for implementation here, but it's the most direct gap between what exists and what was asked for.

## What this document does NOT do

No code changed. No new state added to `execution_state.py`. No Telegram gating logic changed. The `VETOED`-state gap and the paper-trading race condition (see `PAPER_TRADING_DUPLICATION_AUDIT.md`) are both documented as findings for a future, separately-approved implementation phase.
