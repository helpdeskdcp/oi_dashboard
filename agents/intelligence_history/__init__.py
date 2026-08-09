"""
agents/intelligence_history/ -- Milestone 13, Phase 2: Live Observational
Validation.

Persists a history of intelligence_orchestrator.build_snapshot() results
over time and computes read-only drift/stability statistics against that
history -- does bias flip a lot, does confidence stay stable, does
greeks_alignment stay coherent with bias, does oi_strength actually
respond to fresh option-chain data, does bias agree with the underlying's
subsequent price movement. No broker call, no scheduler wiring, no
automatically-started thread anywhere in this package -- every entrypoint
here is called manually (via intelligence_history_cli.py) or by a test/API
request, never on a timer or at app startup. Mirrors agents/shadow_mode/'s
own architecture and safety discipline exactly.

Modules:
- store.py     -- intelligence_snapshots_log table (own isolated
                   namespace, CREATE TABLE IF NOT EXISTS only) and its
                   append-only write / read functions.
- analytics.py -- compute_bias_stability()/compute_confidence_stability()/
                   compute_greeks_coherence()/compute_oi_responsiveness()/
                   compute_bias_price_correlation()/compute_report():
                   read stored history, compute derived statistics.
                   Zero writes.
- api.py       -- read-only aggregation functions backing the three
                   GET-only /api/intelligence/history/* routes in app.py.
"""
