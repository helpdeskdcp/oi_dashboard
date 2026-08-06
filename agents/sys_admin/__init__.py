"""
agents/sys_admin/ -- Milestone 8: the AI System Administrator, BATI's
autonomous operating system for the agent ecosystem itself.

"It never replaces their logic. It coordinates, validates, monitors and
recovers them." Every module here operates ON the other five agents
(Developer, Memory, Quant Researcher, Risk Manager, Trading Supervisor)
and the infrastructure they run on -- none of it touches trading logic,
strategy code, or risk math, which stay exactly where Milestones 3-7
put them.

Module map:
  orchestrator.py       -- agent registration/enable-disable, heartbeat
                          tracking, crash detection, a static
                          dependency graph. "Restart" means clearing a
                          crashed flag, never automatically re-running
                          the operation that crashed (that could re-run
                          a costly/risky LLM call or research cycle).
  infra_monitor.py        -- CPU/RAM/disk/GPU/SQLite/DB-latency/queue-
                          length/thread-health, stdlib-only (no psutil
                          in this repo's dependencies).
  deployment_manager.py     -- build/test/benchmark (reuses
                          agents.dev_agent.pipeline.run_gates, never
                          reimplements it), merge-readiness verification
                          (dry-run only -- never merges), version
                          tracking, rollback (delegates to the existing
                          agents/rollback.py, never a second
                          implementation).
  backup_recovery.py         -- real SQLite backups (sqlite3.Connection.
                          backup(), not a file copy) with integrity
                          verification before a backup is ever marked
                          healthy.
  security_audit.py            -- secret-in-code scanning, API-key
                          presence/format validation (never a live call
                          to verify one), the propose-only architectural
                          invariant checked programmatically, unexpected-
                          code-modification detection via git.
  maintenance.py                 -- dead/duplicate code, slow tests,
                          a bounded tracemalloc-based memory-leak probe,
                          dependency vulnerabilities (pip-audit reuse),
                          SQLite fragmentation -- turned into prioritized
                          proposals, never auto-applied.
  self_healing.py                  -- THE central safety decision of
                          this package: automatic recovery only for
                          non-destructive bookkeeping (clearing a
                          crashed flag, retrying a transient connection);
                          anything that could lose data or needs
                          judgment (restoring a backup, reverting
                          config) is proposed, never auto-applied. See
                          this module's own docstring.
  sysadmin_report.py                -- shared explainability report:
                          every autonomous action logs reason,
                          confidence, evidence, affected components, and
                          recovery outcome.
  sysadmin_store.py                   -- SQLite persistence, indexed
                          from the start.
  admin_agent.py                        -- SystemAdministrator(BaseAgent),
                          registered via agents.registry (the third
                          agent in this framework to actually do so,
                          after Milestone 7's TradingSupervisor).
  api.py                                  -- Operations Dashboard support:
                          plain, JSON-serializable read functions.
"""
