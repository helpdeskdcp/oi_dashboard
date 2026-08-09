# Release Notes — Milestone 13, Phase 1: Read-Only Intelligence Orchestrator

Merge commits: `14a60e6` (Phase 1 — orchestrator + API), `5b41180` (Phase 2 — live verification, UI panel, docs). Merged into `master`, fast-forward both times. Deployed to production (live restart with verified SQLite backup, `backup_id=1` then `backup_id=2`).

## What this milestone is

A controlled, read-only runtime intelligence aggregation layer: one `GET /api/intelligence/snapshot?symbol=<SYMBOL>` call combines already-computed, already-tested outputs from this project's real engines into a single `MarketIntelligenceSnapshot` per symbol — bias, confidence, OI strength, probability score, volume score, Greeks alignment, institutional score. It observes and summarizes; it does not decide, place, or execute anything.

## Features completed

**Phase 1 — orchestrator + API** (`intelligence_orchestrator.py`, `intelligence_models.py`):
- Engine adapters mapped to what actually exists in this repo (not the `backend/engines/*` paths originally requested — no `backend/` directory exists here): `oi_engine.py` for bias/signal/OI strength, `sr_probability_engine.py` for institutional score, a derived volume/liquidity adapter from real `ce_vol`/`pe_vol`, Greeks alignment reused from `generate_signal()`'s own resolved delta rather than a second Black-Scholes call, and this module's own deterministic mean-aggregation as the "mathematics" layer.
- `GET /api/intelligence/snapshot` — admin-gated, GET-only, returns `404` (not a fabricated snapshot) when no market data exists yet for a symbol.
- 27 new tests (`test_intelligence_orchestrator.py`): engine-adapter score ranges, bias resolution, determinism, honest degradation, zero-write read-only proof, full route access-control matrix.

**Phase 2 — live verification, UI, docs**:
- Live smoke-check of all 14 supported symbols against the running production process (real HTTP, real admin session) — 13/14 return `200` with the full snapshot shape; `INDIA VIX` correctly `404`s (spot-only index, no option chain — by design). Report: `docs/M12_LIVE_SMOKE_CHECK.md`.
- Read-only "🧠 Market Intelligence" card added to `/admin/trading-intelligence` (`templates/trading_intelligence.html`) — Bias, Confidence, Institutional Score, OI Strength, Volume Score, Probability Score, Greeks Alignment for the active symbol tab, on the page's existing 15s poll loop. Pure frontend; no new backend route.
- `docs/MONDAY_MARKET_OPEN_CHECKLIST.md` extended (not overwritten — a Shadow Mode checklist already lived at that path) with a dedicated Intelligence Orchestrator live-market validation section.

## Safety

Zero `.py` files were touched in Phase 2. Across both phases: no broker/Angel One import anywhere in the new code, no new database tables, no background thread or scheduler registration, `RUNTIME_SCHEDULER_ENABLED` and `RUNTIME_CONTROL_API_ENABLED` unchanged (`False` throughout, re-verified live post-deploy), `NEVER_SCHEDULABLE_AGENTS` unchanged. `ai_trading_engine.evaluate()` is deliberately never called (its real side effect of closing open paper-trade positions is exactly what Shadow Mode's own `observer.py` already established as unsafe for a read-only layer — the same lesson applied again here).

## Architecture state after this deploy

- **Data layer**: OI, probability, volume, Greeks — real, already-existing engines.
- **Aggregation layer**: `intelligence_orchestrator.py` / `GET /api/intelligence/snapshot`.
- **Presentation layer**: live Market Intelligence dashboard card, polling only.
- **Control layer**: still disabled (`RUNTIME_SCHEDULER_ENABLED = False`, `RUNTIME_CONTROL_API_ENABLED = False`).
- **Action layer**: not implemented — no signal execution, no autonomous trading, no order placement.

This milestone adds observability. It does not grant execution authority. That boundary was maintained deliberately throughout — every phase's brief explicitly excluded scheduler activation, broker integration, and autonomous execution, and the code matches that.

## Known limitations

- `institutional_score`'s `price_structure` input is honestly passed as `None` — no swing-structure classifier exists yet in this codebase (documented Phase 1 gap, not a bug).
- `volume_score`'s `LIQUID_VOLUME_REFERENCE = 50_000` constant is calibrated for index options (NIFTY/BANKNIFTY-scale volume); it under/over-reads for MCX bullion contracts trading in much smaller absolute lot counts (observed directly in the Phase 2 smoke check: GOLD/SILVER read `volume_score=0` despite real non-zero stored volume). Flagged as a heuristic, same status as `oi_engine.py`'s own documented constants — not fixed in this milestone.
- All Phase 2 live verification happened over a weekend with markets closed; every symbol's underlying data was 2-4 days stale. The endpoint's shape and reachability are confirmed; its behavior against live, intraday-moving data is not yet observed — that's exactly what `docs/MONDAY_MARKET_OPEN_CHECKLIST.md`'s new section is for.

## Explicitly out of scope for this milestone

No persistent snapshot history, no drift/correlation logging, no scheduled/autonomous computation of any kind. A proposal for that ("Phase 2: Live Observational Validation" — intraday snapshot drift logging, bias-vs-price-structure correlation, confidence stability tracking) has been raised but not scoped or started; it would be new backend logging infrastructure, not a docs/UI change, and needs its own explicit brief before implementation, matching how every other phase in this project began.
