# AI Developer Agent — Implementation Plan

**Status: implemented and merged** (`agents/dev_agent/`, commit `90b04dd`,
later extended by Milestones 3-6). Extends
[`AUTONOMOUS_AGENTS_ARCHITECTURE.md`](AUTONOMOUS_AGENTS_ARCHITECTURE.md)
(approved) — this plan covered **only** the AI Developer agent (AGT-02); see
that doc's "Implementation roadmap" for every agent shipped since.

## Scope

Everything the Developer agent needs to run safely — a minimal audit log, a
minimal event log, worktree tooling — is built as part of this plan, not as
a separate prerequisite phase. A future System Administrator agent (AGT-01)
starts only after this one is judged stable (see Exit Criteria below).

## Engine decision

The architecture proposal left this open: a scheduled Claude Code session,
or a standalone Python service. The 14 requirements below — dry-run mode,
rollback, patch-not-direct-edit, automatic benchmark comparison — describe a
self-contained pipeline with one LLM-reasoning step inside it, not an
interactive coding session.

**This plan proposes a standalone Python service** (`agents/dev_agent/`),
invoked on a cron cadence, using the same `OPENAI_API_KEY` / `OPENAI_MODEL`
pattern `advisory_chatbot.py` already establishes in this codebase — reusing
a proven integration point rather than introducing a second one. Only the
"what's the bug, what's the fix" reasoning step calls an LLM; worktree
management, test/backtest/benchmark execution, audit logging, and the
approval gate are ordinary deterministic Python.

## The pipeline

1. **Detect** a candidate issue (LLM-assisted).
2. **Create an isolated worktree** — `agent/dev-<ts>-<slug>`.
3. **Generate a patch** inside it (LLM).
4. **Unit tests** — full `pytest` suite (324 tests today, ~50s).
5. **Integration tests** — the Flask-`test_client`-based route tests this
   codebase already has (`test_backtest_profiles.py` and similar), plus an
   app-boot smoke check (`python -c "import app"`).
6. **Backtest comparison** — only if the diff touches a strategy-relevant
   file (`exit_engine_v4.py`, `backtest.py`, `dynamic_sr_engine.py`,
   `sr_engine_v3.py`, `position_sizing.py`, `oi_engine.py`): baseline vs.
   candidate on a fixed scenario, same method used by hand for this
   session's SL-cap and target-hit-ratchet fixes.
7. **Benchmark comparison** — diffs net P&L / profit factor / expectancy /
   drawdown / win rate (from step 6) and test-suite wall-clock time (from
   step 4) against baseline. Flags an across-the-board regression; never
   silently rejects — a real bug fix can legitimately change backtest
   numbers, as it did this session.
8. **Proposal** — a surviving patch is written to `agent_audit_log` as
   `pending_approval` with its diff, full test output, and (when relevant)
   backtest/benchmark numbers already attached. The worktree stays alive
   for review.

Steps 4–7 are a strict AND gate: any failure ends the run immediately,
discards the worktree, and logs why. Step 8's proposal then waits for a
human to approve (merge; record the pre-merge SHA for rollback) or reject
(worktree discarded).

## Every requirement, mapped to a mechanism

| Requirement | Mechanism | File(s) |
|---|---|---|
| Modular architecture | Detection, patching, and each validation gate are separate, independently testable units. | `agents/dev_agent/*.py` |
| Plugin-based agents | `@register_agent("dev")` decorator + shared `BaseAgent` — adding System Administrator later is one new file, not a change to this one. | `agents/base_agent.py`, `agents/registry.py` |
| Event-driven communication | Real SQLite event table, published to on every pipeline step. Only one publisher exists yet; the bus is real infrastructure other agents plug into later. | `agents/event_bus.py` |
| Complete audit logging | Every step — not just the final proposal — writes one append-only row. | `agents/audit_log.py` |
| Human approval by default | `propose()` only ever writes `outcome="pending_approval"`. No config flag exists in v1 to skip it. | `agents/dev_agent/proposal.py` |
| Dry-run mode | `--dry-run` (default **on**): runs steps 1–7 for real, never writes `pending_approval`, always discards the worktree. | `agents/dev_agent/runner.py` |
| Rollback support | Every applied proposal's audit row stores the pre-merge SHA. Rollback is `git revert <merge_commit>` — never `reset --hard`. | `agents/rollback.py` |
| Git branch isolation | Step 2, always — same `EnterWorktree`/`ExitWorktree` discipline as this session's own workflow. | `agents/dev_agent/worktree.py` |
| Automatic unit testing | Step 4, full suite, always. | `agents/dev_agent/gates/unit_tests.py` |
| Automatic integration testing | Step 5, existing Flask-test-client route tests + boot smoke check. | `agents/dev_agent/gates/integration_tests.py` |
| Automatic backtesting | Step 6, gated on whether strategy files were touched. | `agents/dev_agent/gates/backtest_compare.py` |
| Automatic benchmark comparison | Step 7, flags rather than silently rejects. | `agents/dev_agent/gates/benchmark.py` |
| Patch generation, not direct edits | The only write primitive is inside the worktree; the proposal stores `git diff base..branch` as text. | `agents/dev_agent/patcher.py` |
| Automatic documentation updates | A checklist item inside patch generation itself — doc updates ship in the same patch, not a separate mechanical pass. | `agents/dev_agent/patcher.py` |
| No self-modifying production code | Any diff touching `agents/**` is refused before a worktree is even created — out of scope, not a stricter approval tier. | `agents/dev_agent/detector.py` |

## New files

```
agents/
  base_agent.py           # Finding, ProposedAction, RiskTier, BaseAgent
  registry.py               # register_agent(name) / get_agent(name)
  event_bus.py               # publish() / events_since() over agent_events
  audit_log.py                # record() / get() / set_outcome() over agent_audit_log, append-only
  rollback.py                  # rollback(audit_log_id) -> git revert, logged
  dev_agent/
    __init__.py                # exposes DevAgent, registers under "dev"
    detector.py                 # candidate-issue scan; enforces the agents/** refusal FIRST
    worktree.py                  # git worktree add/remove wrapper
    patcher.py                    # the one LLM-calling module + docstring-currency check
    gates/
      unit_tests.py                # pytest, structured pass/fail + output
      integration_tests.py          # Flask test-client suite + boot smoke check
      backtest_compare.py            # baseline vs. candidate, skipped if no strategy file touched
      benchmark.py                    # aggregates the above into one comparison table
    proposal.py                        # assembles ProposedAction, writes pending_approval
    runner.py                           # orchestrates steps 1-8, owns dry_run
  approve_cli.py             # v1 approval channel:
                              #   python agents/approve_cli.py <audit_log_id> [approve|reject]

test_agents/
  test_base_agent.py
  test_event_bus.py
  test_audit_log.py
  test_rollback.py
  dev_agent/
    test_detector.py     # includes the self-modification refusal as an explicit regression test
    test_worktree.py
    test_gates.py         # each gate tested in isolation, synthetic pass + synthetic failure
    test_runner.py         # end-to-end against a seeded, deliberately-broken TOY fixture --
                            # never against the real app; covers dry-run vs. live and full rejection
```

## Data model

```sql
CREATE TABLE agent_events (
    id            INTEGER PRIMARY KEY,
    ts            TEXT NOT NULL,
    source_agent  TEXT NOT NULL,
    event_type    TEXT NOT NULL,   -- "dev_agent.detected", "dev_agent.gate_failed", "dev_agent.proposed"
    payload_json  TEXT NOT NULL,
    severity      TEXT NOT NULL
);

CREATE TABLE agent_audit_log (
    id               INTEGER PRIMARY KEY,
    ts               TEXT NOT NULL,
    agent            TEXT NOT NULL,
    action_type      TEXT NOT NULL,   -- finding | gate_result | proposal | approval | rejection | applied | rollback
    description      TEXT NOT NULL,
    payload_json     TEXT,             -- diff, gate output, benchmark table -- whatever the step produced
    risk_tier        TEXT NOT NULL,
    outcome          TEXT NOT NULL,   -- pending_approval | approved | rejected | applied | failed | rolled_back
    approved_by      TEXT,
    approved_at      TEXT,
    pre_merge_sha    TEXT,             -- master HEAD before merge -- what rollback reverts to
    merge_commit_sha TEXT
);
```

Both tables live in `oi_history.db`, matching every other piece of
persistent state in this codebase — no new database file.

## Non-negotiables for this agent specifically

- **Never touches `agents/**`.** The detector refuses before a worktree is
  even created — the literal implementation of "no self-modifying
  production code," not a softer approval tier.
- **Never merges its own proposal.** `approve_cli.py` is a separate
  entrypoint the agent process never calls itself.
- **Never skips a gate.** Steps 4–7 run in the stated order every time —
  no flag runs a subset.
- **Rollback is `git revert`, never `reset --hard`** — history-preserving,
  matching this session's own git safety rules.
- **Dry-run discards its worktree unconditionally** — a preview run leaves
  zero trace beyond the log entry that it happened.

## Build sequence

1. **M1 — Data + audit substrate.** `base_agent.py`, `registry.py`,
   `event_bus.py`, `audit_log.py`, `rollback.py` + tests. Nothing below can
   be tested without this.
2. **M2 — Worktree + gates.** `worktree.py` and the four gate modules, each
   independently unit-tested against synthetic pass/fail fixtures — no LLM
   call needed yet.
3. **M3 — Detector + patcher (the LLM step).** Including the hard-coded
   `agents/**` refusal, tested first and separately from the happy path.
4. **M4 — Runner + proposal + approve_cli.** Wires M1–M3 into the full
   pipeline; dry-run end-to-end against a seeded toy bug before live mode
   is ever exercised.
5. **M5 — Full-suite regression + benchmark.** Run the existing test suite
   (must stay green) and a representative backtest comparison, confirming
   the agent framework itself adds zero drag to the app it's meant to
   improve.

## Exit criteria — before System Administrator starts

- M1–M5 shipped, full suite green, no regression on the benchmark run.
- At least one real (not synthetic) proposal has gone through dry-run, then
  live, then explicit approval or rejection via `approve_cli.py`.
- The rollback path has been exercised at least once against a real merge,
  not just its unit test.
- A week or so of its audit log has been reviewed and judged reasonable.

## Decisions needed before M1

1. **Engine:** standalone Python + OpenAI (proposed above). Default:
   proceed as proposed unless told otherwise.
2. **Detection scope:** the whole repo, or narrower (e.g. only
   `exit_engine_v4.py`/`backtest.py`) until the agent has earned more
   trust? Narrower makes the first live proposals easier to review.
3. **Cadence:** how often should `runner.py` run — hourly, daily, or
   on-demand via CLI only for now?
4. **Backtest scenario for gate 6:** confirm symbol + date range (defaults
   to NIFTY, the same 3-month window used for this session's SL-cap and
   ratchet-fix comparisons) or specify a different fixed baseline.
