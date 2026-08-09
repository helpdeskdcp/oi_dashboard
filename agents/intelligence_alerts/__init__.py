"""
agents/intelligence_alerts/ -- Milestone 14, Phase 1: Intelligence
Alerting Layer.

Evaluates already-logged intelligence_snapshots_log rows (Milestone 13,
Phase 2's own table -- never touched or re-derived here) against fixed
threshold rules (bias flip, confidence instability, Greeks incoherence,
OI non-responsiveness) and records the result. Delivery (Telegram/email)
happens only from intelligence_alerts_cli.py, the ONLY way a rule
evaluation is ever triggered in this codebase -- no scheduler wiring, no
background thread, no HTTP write route. Mirrors agents/shadow_mode/'s and
agents/intelligence_history/'s architecture and safety discipline
exactly: read history, evaluate, record -- never write to any other
table, never call a broker, never place an order.

Modules:
- store.py -- intelligence_alerts_log table (own isolated namespace,
               CREATE TABLE IF NOT EXISTS only) and its append-only
               write / read functions.
- rules.py -- check_bias_flip()/check_confidence_unstable()/
               check_greeks_incoherent()/check_oi_non_responsive()/
               evaluate_all(): read agents.intelligence_history.store,
               return rule-trigger dicts. Zero writes.
- api.py   -- read-only aggregation functions backing the three
               GET-only /api/intelligence/alerts/* routes in app.py.
"""
