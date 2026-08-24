# Paper-Trading Duplication Audit

**Status: one confirmed structural gap — no idempotency guard exists between the three entrypoints that can trigger a real cycle. Not proven to have caused a duplicate trade in production, but architecturally unguarded. No code changed by this document.**

## Trace: the one function that can open a trade

`paper_trading.enter_from_recommendation()` is a pure SQLite `INSERT` into `ti_paper_trades` (`agents/trading_intelligence/paper_trading.py:45`). Grepped across the entire real repository (excluding docstring mentions and the uncommitted `build-failure-gate` worktree): it has **exactly one real call site** — `agents/trading_intelligence/api.py:266`, inside `run_scheduled_cycle()`.

`run_scheduled_cycle()` itself has **three real callers**:

1. `agents/runtime/agent_runtime.py:207` — the automatic scheduler's `_trading_intelligence_cycle()`. Runs on the scheduler's tick loop (`agents/runtime/scheduler.py:82`, default `tick_interval_seconds=5.0`); `trading_intelligence` was removed from `NEVER_SCHEDULABLE_AGENTS` at Milestone 17 (confirmed active in production this session).
2. `app.py:6097` — `POST /api/trading-intelligence/run-cycle`, the manual "Run Cycle" button, gated behind `TI_RUN_CYCLE_API_ENABLED` (confirmed ON in production per prior session memory) and requiring an admin + a `reason` string.
3. `trading_intelligence_cli.py:81` — a manual CLI entrypoint, same function, no additional gating.

**Dashboard reads never open a trade.** `get_symbol_overview()` (`api.py:127`, called by every `/api/trading-intelligence/overview` poll, including the client's own 15s auto-refresh) invokes `ai_trading_engine.evaluate()` but never calls `enter_from_recommendation()`. Confirmed by direct read, not just the docstring's own claim.

## The guard that exists, and its real limit

`evaluate()` (`agents/trading_intelligence/ai_trading_engine.py:546-547`) reads `ti_store.list_open_trades(symbol=symbol)` and returns `HOLD` (no new signal) if one is already open for that symbol. This is a real, working guard for the common case — but it is a **read-then-act check, not an atomic reservation**. Nothing holds a lock between that read and `enter_from_recommendation()`'s eventual `INSERT` a few function calls later.

**Root cause of the duplication risk**: if the automatic scheduler and a manual trigger (button or CLI) both call `run_scheduled_cycle()` for the same symbol within the same narrow window — both reading `list_open_trades()` as empty before either has written its row — both can independently decide to open a trade. This is a classic TOCTOU (time-of-check-to-time-of-use) race. No mutex, `SELECT ... FOR UPDATE`-equivalent, or unique constraint prevents it.

**Likelihood, honestly assessed**: narrow. It requires a human-triggered run (button or CLI) to land within roughly the same few-hundred-millisecond-to-second window as a scheduler tick for the same symbol — not "constantly duplicating," but not structurally impossible either. This audit did not find evidence it has actually happened (no duplicate-row incident is documented in this session's history), but the absence of a guard is real and independently verifiable regardless of whether it has fired yet.

## Desired contract

The same logical signal — same symbol, same direction, same underlying decision cycle — must produce at most one `ti_paper_trades` row, enforced structurally (a database-level constraint or an atomic claim), not just by the current best-effort sequential read-check.

## Minimum future fix (design only, not implemented)

A `UNIQUE` constraint on `ti_paper_trades(symbol)` scoped to open trades (e.g. a partial unique index on `symbol` `WHERE closed_at IS NULL`, if the schema supports it) would convert the current race into a clean `INSERT` failure instead of a silent duplicate — cheaper and more robust than trying to serialize the three call sites with an application-level lock. This is a proposal, not a change; it needs its own review before implementation, including checking whether SQLite's schema here can express a partial unique index cleanly and whether any legitimate flow currently relies on being able to have more than one open row per symbol (none found in this audit, but not exhaustively ruled out).

## Signal identity rule

Using only existing repository concepts, "the same signal" for idempotency purposes should be: **`(symbol, direction, the specific decision cycle that produced it)`**. In practice, the decision cycle is already identified by the cycle's own timestamp (`snapshot.as_of_ts` / the `cycles` table row the signal was computed from) — two evaluations of the *same* underlying market cycle for the *same* symbol and direction are the same logical signal; a new cycle with new data is a genuinely new signal, even if direction happens to repeat.

**Test cases this rule should satisfy** (described, not implemented):
1. Scheduler and manual button both fire `run_scheduled_cycle()` for NIFTY within the same cycle window → only one `ti_paper_trades` row for NIFTY.
2. Scheduler fires twice for NIFTY across two genuinely different cycles (new snapshot each time), no open position between them → two separate rows are correct, not a duplicate (normal operation, not a bug).
3. Manual CLI run overlaps with an already-open position for the same symbol → `evaluate()`'s existing `HOLD` guard already handles this correctly today, no change needed.
4. Two different symbols evaluated in the same scheduler tick → independent, no cross-symbol interference (already true — `list_open_trades(symbol=symbol)` is symbol-scoped).
