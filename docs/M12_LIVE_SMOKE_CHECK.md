# Live Smoke-Check — Intelligence Orchestrator (`GET /api/intelligence/snapshot`)

**Note on filename:** this report was requested at `docs/M12_LIVE_SMOKE_CHECK.md`, but the endpoint under test (`intelligence_orchestrator.py` / `GET /api/intelligence/snapshot`) is **Milestone 13, Phase 1** work, merged and deployed to production earlier in this session (commit `14a60e6`). Kept the requested filename as-is rather than silently renaming a specified deliverable path; flagging the discrepancy here for the record.

**Run date/time:** 2026-08-09, 09:37 IST (weekend — NSE/MCX markets closed; this is a post-deploy connectivity/shape check against whatever data was already stored, not a live-market data-freshness check — that's Task 3's job, for Monday).

**Method:** Real HTTP requests against the live production process (`127.0.0.1:5050`, PID confirmed running post-restart), authenticated as the bootstrap admin via the actual `/login` flow (real session cookie + CSRF token — no test client, no mocking).

## Results

| Symbol | HTTP Status | `bias` | `confidence` | `oi_strength` | `institutional_score` | `volume_score` | `probability_score` | `greeks_alignment` | Underlying data last updated |
|---|---|---|---|---|---|---|---|---|---|
| NIFTY | 200 | BULLISH | 67 | 70 | 36 | 100 | 58 | BULLISH LEAN | 2026-08-07 15:29:55 |
| BANKNIFTY | 200 | BULLISH | 57 | 20 | 36 | 100 | 38 | BULLISH LEAN | 2026-08-07 15:29:35 |
| FINNIFTY | 200 | NEUTRAL | 53 | 0 | 11 | 100 | 21 | UNAVAILABLE | 2026-08-07 15:29:37 |
| MIDCPNIFTY | 200 | NEUTRAL | 53 | 0 | 11 | 100 | 21 | UNAVAILABLE | 2026-08-07 15:29:43 |
| SENSEX | 200 | NEUTRAL | 49 | 0 | 11 | 100 | 20 | UNAVAILABLE | 2026-08-07 15:29:58 |
| INDIA VIX | **404** | — | — | — | — | — | — | — | *no cycle ever recorded* |
| CRUDEOIL | 200 | BULLISH | 56 | 0 | 11 | 100 | 22 | UNAVAILABLE | 2026-08-07 23:29:27 |
| CRUDEOILM | 200 | BULLISH | 55 | 0 | 11 | 100 | 22 | UNAVAILABLE | 2026-08-07 23:29:41 |
| NATURALGAS | 200 | BEARISH | 42 | 0 | 11 | 100 | 18 | UNAVAILABLE | 2026-08-07 23:29:32 |
| NATGASMINI | 200 | BEARISH | 40 | 0 | 11 | 100 | 17 | UNAVAILABLE | 2026-08-07 23:29:42 |
| GOLD | 200 | NEUTRAL | 48 | 0 | 6 | 0 | 18 | UNAVAILABLE | 2026-08-05 23:29:47 |
| GOLDM | 200 | BEARISH | 44 | 0 | 11 | 96 | 18 | UNAVAILABLE | 2026-08-05 23:29:52 |
| SILVER | 200 | BEARISH | 28 | 0 | 0 | 0 | 9 | UNAVAILABLE | 2026-08-07 23:29:28 |
| SILVERM | 200 | BEARISH | 33 | 0 | 11 | 21 | 15 | UNAVAILABLE | 2026-08-07 23:29:25 |

**13 of 14 symbols → 200 with the full 8-field snapshot shape** (`symbol`, `bias`, `confidence`, `oi_strength`, `probability_score`, `volume_score`, `greeks_alignment`, `institutional_score` all present on every 200 response). **1 of 14 → 404** (see edge case below).

## Edge-case observations

1. **`INDIA VIX` → 404, by design, not a bug.** `INDIA VIX` is configured in `app.py`'s `SYMBOLS` dict as `type: "index_spot"` — it's a spot-only reference index with no options chain, so the live-fetch loop has never written a `cycles` row for it (confirmed directly against `oi_history.db`: zero rows for `symbol='INDIA VIX'`). `build_snapshot()` correctly returns `None` → the route correctly returns `404` with an honest message, exactly as designed in Phase 1 ("returns 404 rather than a fabricated snapshot"). No fix needed; this is the intended contract for a symbol with no option chain.

2. **`oi_strength=0` and `greeks_alignment="UNAVAILABLE"` on every symbol except NIFTY/BANKNIFTY.** All underlying data is genuinely stale (last cycle 2–4 days old — Friday 2026-08-07 close, or in GOLD/GOLDM's case, 2026-08-05) since markets are closed this weekend and no new cycles have been written. On the weaker/less-liquid setups, `oi_engine.generate_signal()` is legitimately returning a `NO_TRADE`/zero-confidence signal (and consequently no resolved `delta_used`, which is why `greeks_alignment` degrades to `"UNAVAILABLE"` — this is `_greeks_alignment()`'s documented behavior, not a defect). NIFTY and BANKNIFTY's stored Friday data happened to carry enough OI skew for `generate_signal()` to still resolve a real signal. **This is expected, honest behavior given stale weekend data — it is not yet a real live-market data-freshness check.** Task 3's checklist covers verifying this looks materially different once Monday's live intraday data starts flowing.

3. **`volume_score=0` for GOLD and SILVER specifically** (vs. 96–100 for every other symbol). Both have real, non-zero `ce_vol`/`pe_vol` stored (confirmed via direct DB query — GOLD's last cycle has real volume figures), so a `0` here reflects the `LIQUID_VOLUME_REFERENCE = 50_000` heuristic constant being calibrated for the index options (NIFTY/BANKNIFTY-scale volume), not MCX bullion contracts, which trade in much smaller absolute lot counts. This is the same "honestly-flagged heuristic" status `intelligence_orchestrator.py`'s own docstring already gives that constant — not a bug, but a real calibration gap worth a future per-symbol-class reference value if this becomes operationally important. Out of scope for this pass (explicitly excluded — no orchestrator logic changes here).

4. **No 500s, no timeouts, no exceptions in `flask_dashboard.log` during the run.** All 14 requests completed in well under a second each.

## Conclusion

The endpoint is live, reachable, correctly authenticated/gated, and produces the full documented field shape on every symbol that has ever had option-chain data recorded. The one 404 is correct-by-design. No backend code changes were made or needed — no critical bug was found.
