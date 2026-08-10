"""
agents/ops/ -- Milestone 16: Autonomous Runtime Operations.

Operational observability for the already-built runtime layer
(agents/runtime/) and alert layer (agents/intelligence_alerts/) --
read-only diagnostics, a purgeable operational event log, and a
watchdog. Never touches trading logic, broker execution, or order
placement; never writes to oi_engine.py or intelligence_orchestrator.py.

Modules:
- models.py    -- event-type taxonomy for event_log.py's own table.
- event_log.py -- SQLite-backed, purgeable operational event log (own
                   dedicated table, deliberately separate from
                   agents.event_bus's own agent_events table -- see
                   that module's own docstring for why).
"""
