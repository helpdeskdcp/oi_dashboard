"""
agents/runtime/ -- Milestone 9: AI Autonomous Runtime & Orchestration
Engine.

"Everything built so far works as independent modules. Now build the
runtime that makes BATI operate continuously as one autonomous system."

This package does not add a seventh agent and does not change how any
of the six existing agents (dev_agent, memory, quant_researcher,
risk_manager, trading_supervisor, sys_admin) make their own decisions --
it wires them into one continuously operating system: a scheduler that
actually calls each agent on a cadence, a real event bus (extending
agents/event_bus.py, not replacing it), a persisted task queue, a
restartable workflow engine, the human approval mechanism referenced by
name since Milestone 1 but never built (approve_cli.py, at the repo
root), a policy engine, and a runtime dashboard (extending the existing
Operations Dashboard, not a second one).

Closes the #1 finding of AUTONOMOUS_READINESS_REPORT.md: "no dispatcher
ever calls any agent's run_cycle()." See AI_RUNTIME.md for the full
architecture and the safety-scoping decisions specific to this
milestone (most importantly: the Execution workflow stage NEVER calls
into app.py or places any order, paper or live, under any policy --
including "Full Auto" -- because no safe, importable execution
entrypoint exists outside app.py's own broker-adjacent code, and this
framework has never imported app.py from agents/ for exactly that
reason).

Modules:
  runtime_store.py     SQLite persistence: task queue, agent status,
                        workflow state/history, policy override.
  runtime_events.py     The full event taxonomy over agents.event_bus.
  market_session.py       Independent IST market-session check (never
                           imports app.py).
  task_queue.py              Priority queue (High/Medium/Low) + retry/
                              failed queues, bounded retries, timeouts.
  agent_runtime.py              Invokes each of the six agents, tracks
                                 heartbeat/status/duration/failures/
                                 health score.
  policy_engine.py                 Paper Trading / Simulation / Read
                                    Only / Recommendation Only / Semi
                                    Auto / Full Auto / Emergency Stop.
  approval_engine.py                  Shared approve/reject/apply logic
                                       -- used by approve_cli.py and the
                                       dashboard.
  workflow_engine.py                     The nine-stage, restartable
                                          workflow.
  scheduler.py                              Starts, runs continuously,
                                             market-session-aware,
                                             event-driven, graceful
                                             shutdown.
  api.py                                       Runtime dashboard support
                                                functions.
"""
