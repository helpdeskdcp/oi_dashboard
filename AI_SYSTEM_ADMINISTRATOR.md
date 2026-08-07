# AI System Administrator (Milestone 8)

Status: implemented, tested, merged to `master` (`a30c131`).

Mission: build BATI's autonomous operating system for the agent ecosystem itself — Developer, Memory, Quant Researcher, Risk Manager, Trading Supervisor. **It never replaces their logic. It coordinates, validates, monitors and recovers them.**

## Package layout

```
agents/sys_admin/
  orchestrator.py        Module 1: Agent Orchestrator
  infra_monitor.py         Module 2: Infrastructure Monitor
  deployment_manager.py      Module 3: Deployment Manager
  backup_recovery.py           Module 4: Backup & Recovery
  security_audit.py              Module 5: Security
  maintenance.py                   Module 6: Autonomous Maintenance
  self_healing.py                    Module 7: Self-Healing
  sysadmin_report.py                   Module 8: Explainability (shared report shape)
  sysadmin_store.py                      SQLite persistence
  admin_agent.py                           SystemAdministrator(BaseAgent)
  api.py                                     Module 9: Operations Dashboard support
```

Module 10 (Production Readiness) is a *testing* deliverable, not a runtime module — see `test_agents/sys_admin/test_production_readiness.py`.

## The central design decision: what "self-healing" actually means here

Every agent in this framework, since Milestone 1, holds to one invariant: **propose, don't act**. `agents/base_agent.py` has no `apply`/`execute` method anywhere — verified programmatically now, not just in a docstring, by `security_audit.check_propose_only_invariant()`.

"Self-Healing... recover automatically... never lose data" could read as a request to break that invariant. It isn't implemented that way, and the two requirements are actually consistent once you separate recovery actions by whether they're reversible:

| Failure | Auto-healed? | Why |
|---|---|---|
| Agent crash | **Yes** — `orchestrator.restart_agent()` clears the crashed flag | Bookkeeping only. Never re-invokes the operation that crashed. |
| Transient DB contention | **Yes** — `self_healing.retry_transient_connection()` | Retries the *connection*, never a write's content. |
| Database corruption | No — `self_healing.propose_database_recovery()` recommends a specific verified backup | Restoring automatically risks silently discarding recoverable data. |
| Service failure | No — `self_healing.propose_service_recovery()` | No automatic `app.py` restart exists anywhere in this framework. |
| Deployment failure | No — `self_healing.propose_deployment_recovery()` | `deployment_manager.rollback()` is never called automatically. |
| Configuration corruption | No — `self_healing.propose_config_recovery()` | Reverting a config a human just intentionally changed would be actively harmful. |

This isn't a partial implementation — it's the correct scope, and it's the reason "never lose data" is actually achievable: nothing here ever overwrites something without a human (or an explicit, separately-authorized call) deciding to.

## Module summaries

**1. Agent Orchestrator** (`orchestrator.py`) — this framework's agents are trigger-invoked function modules, not OS daemons (no scheduler exists anywhere, by design). "Start/Stop" is an enable/disable flag (`agent_status` table); "Restart" clears a crashed flag; heartbeat and a static dependency graph (`DEPENDENCY_GRAPH`) round out the orchestration picture. `registry_snapshot()` reuses `agents.trading_supervisor.agent_health` (Milestone 7) for the actual health checks rather than a second implementation.

**2. Infrastructure Monitor** (`infra_monitor.py`) — stdlib-only; `psutil` is not a dependency of this repo. CPU/RAM read `/proc`; disk via `shutil.disk_usage`; GPU via `nvidia-smi` absence (a real "no GPU" answer, not a placeholder); network is a real TCP-connect probe, never fabricated bandwidth numbers; SQLite integrity + latency; "API latency" measures this repo's own DB round-trip, never a live broker call; "queue length" is the real `agent_audit_log` approval backlog (no message broker exists here, by design).

**3. Deployment Manager** (`deployment_manager.py`) — Build/Test/Benchmark reuse `agents.dev_agent.pipeline.run_gates` directly. Merge verification is **dry-run only**: it checks fast-forward-mergeability and runs the real gates in an isolated worktree, but never calls `git merge`. Rollback delegates entirely to the existing `agents/rollback.py` (Milestone 1).

**4. Backup & Recovery** (`backup_recovery.py`) — real, atomic SQLite backups via `sqlite3.Connection.backup()` (not a file copy). A backup is verified (integrity check + row-count comparison against the source) before it's ever marked healthy. `restore_backup()` defaults to a dry run and refuses to restore from anything not verified.

**5. Security** (`security_audit.py`) — secret scanning reuses `agents.dev_agent.sanitizer.find_matches` (added this milestone). API key validation reuses each LLM provider's own `is_configured()` — never a live call. Unexpected code modification detection is git-based, scoped to `agents/`.

**6. Autonomous Maintenance** (`maintenance.py`) — dead code (`vulture`, degrades to "not checked" like `code_quality.py`'s own pattern for missing tools), duplicate code (a real hash-window detector, pure Python), slow tests (parses `pytest --durations` output), a bounded `tracemalloc`-based memory-leak probe, dependency vulnerabilities (`pip-audit` reuse), SQLite fragmentation. Generates proposals; never applies a fix.

**7. Self-Healing** (`self_healing.py`) — see above.

**8. Explainability** (`sysadmin_report.py`) — every report carries reason, confidence, evidence, affected components, and recovery outcome, exactly as specified.

**9. Operations Dashboard** (`api.py` + `/admin/sysadmin` + `/api/sysadmin/overview`, admin-role-gated) — agent health, infrastructure, risk state, supervision state, backup state, security alerts, recovery history, all in one view.

**10. Production Readiness** — `test_agents/sys_admin/test_production_readiness.py`: real concurrent-writer stress tests (genuine `ThreadPoolExecutor` contention against the SQLite hardening from the pre-Milestone-6 review), a full real corruption-to-restore walkthrough, and a bounded `tracemalloc` stability proxy. "Full regression" is the rest of this repo's own suite.

## Two real bugs caught by the production-readiness suite itself

1. `sqlite3.connect()` silently *creates* a missing database file rather than raising — `propose_database_recovery()` was treating "the database is gone" as "the database is healthy and empty." Fixed by checking `os.path.exists()` before ever connecting.
2. `restore_backup()`'s "always snapshot the current state first" safety step called `sqlite3.Connection.backup()` against a genuinely corrupted source/destination, which raises `sqlite3.DatabaseError` unhandled — meaning recovery could never succeed in the exact scenario it exists for. Fixed on both sides: a failed pre-restore safety snapshot is now reported and skipped (there's nothing valid to preserve from an unreadable state anyway), and the corrupted destination file is removed before the restore writes a fresh one, rather than trying to back up into a connection that can't open it.

Also fixed: `test_agents/test_registry.py`'s isolation fixture cleared the agent registry to empty on teardown instead of restoring it — harmless until a real agent used `register_agent`, which `SystemAdministrator` (and Milestone 7's `TradingSupervisor`) now does. Fixed to snapshot/restore.

## Database schema

```sql
sysadmin_log       -- every SysAdminReport across all seven detection/action modules
agent_status       -- current per-agent state (enabled, crashed, last heartbeat) -- one row per agent, upserted
backups            -- backup metadata: path, size, verified, integrity_ok
```

All in `oi_history.db`, indexed from the start, `PRAGMA busy_timeout=5000` on every connection — the stress tests in Module 10 exist specifically to prove that holds under real concurrency.

## Known, documented limitations

- **Heartbeat is not yet wired into every agent's own entrypoint.** `orchestrator.heartbeat()` exists and is tested; calling it from `agents.dev_agent.pipeline.run_proposal()`, `agents.quant_researcher.research_engine.run_research_cycle()`, etc. is additive follow-up work, not required for this module to be complete.
- **Thread health reports the CURRENT process's threads.** If `SystemAdministrator` runs in a different process than the live `app.py`, it reports its own thread state, not the live app's.
- **No dispatcher calls `run_cycle()`/`on_event()` automatically.** Same gap the original architecture doc's "orchestrator.py" phase anticipated and which still doesn't exist for any agent in this framework, including `TradingSupervisor`. `run_cycle()` is directly callable today; a scheduled sweep needs that dispatcher.
- **`SysAdminReport` is a parallel report shape**, matching `risk_report.py`/`supervision_report.py`'s established pattern rather than a shared base class — the same deliberate, documented DRY trade-off Milestone 7 made for the same reason (retrofitting an already-tested shape carries more regression risk than the duplication costs).

## Test summary

~120 new tests across `test_agents/sys_admin/` (every module) plus small extensions to `test_agents/dev_agent/test_sanitizer.py` (`find_matches`) and `test_agents/test_registry.py` (the isolation fix). Full repo suite verified with zero regressions from the pre-Milestone-8 baseline.
