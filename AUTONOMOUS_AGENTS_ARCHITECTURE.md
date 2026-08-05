# BATI Autonomous Agents — Architecture Proposal

**Status: proposed, not implemented.** No code ships from this document until
it's reviewed and approved; then agents ship one at a time, each gated on its
own tests before the next one starts (per explicit instruction).

## Why this exists

Right now "the AI developer" is a chat session: you ask, the assistant reads
logs and code, isolates the work in a git worktree, writes tests, runs the
full suite, and asks before merging. Every agent below is that exact loop,
running unattended and on a schedule — plus seven more specialists doing the
analogous job for ops, research, risk, trade supervision, performance,
security, and docs.

## Design principles

1. **Propose, don't act.** Every agent's default output is a *proposal* — a
   described change plus evidence — written to an approval queue. Only a
   short allow-list of read-only actions (health checks, research reports,
   log summaries) ever runs unattended.
2. **Tests gate everything.** A code or parameter proposal without a full
   green test run — and, for anything strategy-affecting, a backtest
   comparison against the current baseline — is rejected before a human ever
   sees it.
3. **No new always-on infrastructure.** No Kafka, no Redis, no message
   broker. Two SQLite tables are the event bus and the audit log — this is a
   single-VPS deployment already built entirely on SQLite (`oi_history.db`,
   `backtest_profiles`, `users`); a distributed broker would solve a problem
   BATI doesn't have.
4. **Worktree-isolated, always.** Every code-touching proposal happens in a
   git worktree — never directly against the live checkout.
5. **Not every agent needs a language model.** Drawdown math, disk usage,
   and permission checks are closed-form calculations — running them
   through an LLM adds cost and non-determinism for no benefit. Each agent
   below is tagged for how it actually reasons.
6. **Full audit, forever.** Every finding, proposal, approval, and rejection
   is one append-only row. Nothing is ever deleted from the log — including
   an agent's own mistakes.

## The mechanism

Every agent's action resolves to exactly one of three lanes:

- **`READ_ONLY`** — a Finding (health check, research report, log summary)
  logs itself to `agent_audit_log` as `auto_run` and stops. No approval
  needed.
- **`NEEDS_APPROVAL`** — a `ProposedAction` (code patch, config/parameter
  change, service restart) is written to `agent_audit_log` as
  `pending_approval`, with its diff and test results attached. A human
  reviews it and it becomes either `approved` (merged from its worktree
  branch) or `rejected` (logged, nothing changes).
- **`HARD_BLOCKED`** — `rm -rf`, `DROP TABLE`, writing `.env`,
  `git push --force`, merging to master, disabling a test to force a
  proposal through. These are refused **in code**, before a proposal is
  ever created — there is no flag that unlocks them.

This is the entire safety model. Everything else in this document is what
feeds into it.

## Folder structure

```
agents/
  __init__.py
  base_agent.py       # RiskTier, Finding, ProposedAction, BaseAgent
  event_bus.py         # publish() / subscribe() over agent_events
  audit_log.py          # append-only writes/reads of agent_audit_log
  orchestrator.py        # dispatches events, runs each agent's cadence
  config.py                # per-agent enable flag, cadence, risk overrides
  sysadmin_agent.py
  dev_agent.py
  quant_agent.py
  risk_agent.py
  supervisor_agent.py
  perf_agent.py
  security_agent.py
  docs_agent.py

test_agents/
  test_base_agent.py
  test_event_bus.py
  test_audit_log.py
  test_orchestrator.py
  # one test_<agent>.py added alongside each agent, same commit
```

## Core interfaces

```python
class RiskTier(Enum):
    READ_ONLY       # runs unattended -- health check, research report
    NEEDS_APPROVAL  # code patch, config/parameter change, service restart
    HARD_BLOCKED    # never automatable, see Safety Layer below

class Finding:
    severity: "info" | "warning" | "critical"
    summary: str
    evidence: dict                          # log line, query result, diff -- never a vibe
    proposed_action: ProposedAction | None

class ProposedAction:
    risk_tier: RiskTier
    description: str
    diff: str | None                    # unified diff, for code changes
    test_results: dict | None            # {"passed": N, "failed": N, "output": ...}
    backtest_comparison: dict | None    # {"baseline": {...}, "candidate": {...}}

class BaseAgent(ABC):
    name: str
    def run_cycle(self) -> list[Finding]: ...      # scheduled
    def on_event(self, event: Event) -> None: ...  # reactive
    def propose(self, action: ProposedAction):
        # writes ONE row to agent_audit_log, outcome="pending_approval"
        # this method never executes the action itself
        ...
```

## Communication protocol

Two tables, not a broker. Agents publish to `agent_events`; the orchestrator
dispatches each event to whichever agents subscribed to that `event_type`,
and every agent also runs its own `run_cycle()` on a cadence from
`config.py` independent of any event. Every `Finding` that carries a
`ProposedAction` becomes exactly one row in `agent_audit_log`.

```sql
CREATE TABLE agent_events (
    id            INTEGER PRIMARY KEY,
    ts            TEXT NOT NULL,
    source_agent  TEXT NOT NULL,
    event_type    TEXT NOT NULL,   -- "backtest.completed", "log.error_spike", "disk.low"
    payload_json  TEXT NOT NULL,
    severity      TEXT NOT NULL    -- info | warning | critical
);

CREATE TABLE agent_audit_log (
    id            INTEGER PRIMARY KEY,
    ts            TEXT NOT NULL,
    agent         TEXT NOT NULL,
    action_type   TEXT NOT NULL,   -- finding | proposal | approval | rejection | auto_run
    description   TEXT NOT NULL,
    payload_json  TEXT,
    risk_tier     TEXT NOT NULL,
    outcome       TEXT NOT NULL,   -- pending_approval | approved | rejected | applied | failed
    approved_by   TEXT,
    approved_at   TEXT
);
```

Both tables live in `oi_history.db` unless review decides a separate
`agents.db` is cleaner — everything else in this codebase already shares one
SQLite file per concern, so this follows that convention either way (see
"Decisions needed" below).

## The eight agents

Each is tagged by how it actually reasons — **rule-based** (closed-form
checks, no model call, fully deterministic), **llm-driven** (needs judgment
or generation), or **hybrid** (rule-based detection feeding an LLM only for
the write-up or the fix) — and by its typical risk tier.

| # | Agent | Mode | Typical tier | Job |
|---|---|---|---|---|
| AGT-01 | AI System Administrator | rule-based | mostly read-only | CPU/RAM/disk/network + VPS health via `psutil`; tails logs for error-rate spikes; flags suspicious log patterns. Service restarts stay `NEEDS_APPROVAL`. |
| AGT-02 | AI Developer | llm-driven | needs approval | This session's own workflow, scheduled: reads code/logs, isolates a fix in a worktree, writes tests, runs the suite, opens a proposal with diff + test output. **Recommended build:** a scheduled Claude Code session with a role-scoped instructions file, reusing the exact worktree/test/audit discipline already proven here — rather than a bespoke LLM-API client. |
| AGT-03 | AI Quant Researcher | hybrid | needs approval | Proposes strategy ideas/parameter ranges (LLM); `backtest.py`'s existing sweep plus a new walk-forward split (train/validate/out-of-sample) mechanically scores them. The model never declares a strategy profitable from prose — only computed profit factor/drawdown/expectancy on held-out data can. |
| AGT-04 | AI Risk Manager | rule-based | mostly read-only | Drawdown, exposure, risk-of-ruin — closed-form math, not judgment. Builds on `position_sizing.py` (already shipped). Escalates hard-limit breaches to the Trading Supervisor. |
| AGT-05 | AI Trading Supervisor | hybrid | needs approval | Threshold/anomaly checks on live trade flow (rule-based); escalates to an LLM write-up only for cases it can't classify. Can propose pausing auto-trading for a symbol; cannot resume it without approval. |
| AGT-06 | AI Performance Optimizer | hybrid | needs approval | Profiling/slow-query detection is mechanical; the refactor proposal is where an LLM earns its keep. Lowest priority at BATI's current single-VPS scale. |
| AGT-07 | AI Security Officer | rule-based | audits everyone | Deliberately not LLM-driven — predictability matters more than creativity here. Verifies permissions, scans for exposed secrets, is the only agent that reads `.env` (every other agent requests a secret through it, logged). |
| AGT-08 | AI Documentation Manager | llm-driven | needs approval | Writes changelogs and keeps docs current from the audit log's own activity and real diffs. Writes docs only, never code. |

## Safety layer — enforced in code, not policy

- **`HARD_BLOCKED` actions have no override flag** — `rm -rf`, `DROP TABLE`,
  writing `.env`, `git push --force`, merging to master, disabling a test
  to make a proposal pass. These are refusals, not defaults someone can
  flip.
- **A failing test always loses.** No proposal reaches `pending_approval`
  without a full green run attached.
- **Never auto-deploy, never auto-merge.** Every applied change is a
  fast-forward a human triggered.
- **Append-only data.** No agent deletes trade history, user accounts, or
  log rows — ever, approved or not.
- **No evidence, no Finding.** A claim without a log line, test result, or
  query result attached is treated as a bug in the agent that produced it.

## Implementation roadmap

This ordering is a dependency chain, not a preference — several agents ship
earlier than their original numbering because later agents build on what
they produce.

| Phase | Agent | Why this order |
|---|---|---|
| P0 | Foundation | `event_bus.py`, `audit_log.py`, `base_agent.py`, `orchestrator.py` + tests. Plumbing only — nothing else can exist without it. |
| P1 | AI Security Officer | The audit/permission substrate every later agent's proposals flow through — built early on purpose, despite being #7 in the original list. |
| P2 | AI Developer | Closest to the proven manual workflow. Highest immediate value; proposal-only keeps incremental risk low. |
| P3 | AI System Administrator | Monitoring-only first. Service-restart stays `NEEDS_APPROVAL` indefinitely, revisited only after monitoring runs clean for a while. |
| P4 | AI Risk Manager | Read-only drawdown/exposure dashboards first, extending `position_sizing.py`. |
| P5 | AI Quant Researcher | Builds directly on `backtest.py`; walk-forward validation is this agent's core job. |
| P6 | AI Trading Supervisor | Needs Risk Manager + Quant Researcher output to decide when to propose pausing a symbol. |
| P7 | AI Performance Optimizer | Lowest urgency at current scale. |
| P8 | AI Documentation Manager | Low-risk alone, sequenced last so there's real audit-log activity worth documenting. |

## Decisions needed before P0

1. **Storage:** reuse `oi_history.db` for the two new tables, or a separate
   `agents.db`? Separate keeps the trading DB untouched by agent chatter;
   shared keeps everything in one file, matching today's convention.
2. **AI Developer's engine:** scheduled Claude Code sessions, or a
   standalone Python service calling an LLM API directly? The former reuses
   proven infrastructure fast; the latter runs independent of any
   interactive coding product.
3. **Approval channel:** review `pending_approval` rows in a page inside
   BATI itself, or via CLI/notification? Affects P0's scope — a review UI
   is its own small feature, not just the two tables.
4. **Confirm the phase order** above, or reprioritize before P0 starts.
