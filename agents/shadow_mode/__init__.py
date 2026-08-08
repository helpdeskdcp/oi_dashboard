"""
agents/shadow_mode/ -- Milestone 12, Phase 2B: Shadow Mode (Read-only
Observation).

A fully passive market-observation pipeline: observe already-archived
market data, compute a hypothetical signal via the SAME rule-based
generator app.py's live dashboard already runs (oi_engine.generate_signal),
record it, and later compare the prediction against what actually
happened using already-archived candles. No broker call, no paper order,
no scheduler wiring, no automatically-started thread anywhere in this
package -- every entrypoint here is called manually or by a test/API
request, never on a timer or at app startup.

Modules:
- store.py     -- shadow_observations/shadow_predictions/shadow_outcomes
                   tables (own isolated namespace, CREATE TABLE IF NOT
                   EXISTS only) and their CRUD/read functions.
- observer.py  -- observe_and_predict(): computes one hypothetical
                   signal from already-stored data and records it.
                   Read-only against every OTHER table in this database;
                   the only INSERTs are into this package's own tables.
- evaluator.py -- evaluate_prediction()/evaluate_pending(): classifies
                   a recorded prediction against already-archived
                   candles (correct/incorrect/partial/expired) and
                   compute_metrics() for rolling win-rate/calibration.
- api.py       -- read-only aggregation functions backing the three
                   GET-only /api/shadow/* routes in app.py.
"""
