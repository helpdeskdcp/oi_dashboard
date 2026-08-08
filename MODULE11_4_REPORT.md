# Module 11.4 Report — Explainable AI Reasoning

## Objective

Give every live recommendation and every closed trade's Trade Quality Score a human-readable narrative explaining *why* — built purely from real, already-computed BATI evidence (confidence, calibrated probability, regime, timeframe confirmation, institutional persistence/backing, trade quality components), never a second source of truth and never a re-derivation of any existing number.

## Design decisions

1. **Deviation from `MILESTONE11_PLAN.md`'s original M11.4 sketch, per this module's own explicit implementation directive.** The plan originally described an LLM turning the existing structured reasoning strings into one narrative paragraph via `agents.llm_providers.generate_with_fallback()`. This module's actual requirements state explanations must be **deterministic and reproducible for the same inputs** and must **never call an external LLM** or use **fabricated reasoning, hallucinated confidence, or placeholder AI**. An LLM-narrated paragraph cannot satisfy strict determinism by construction — a provider can rephrase, drop, or embellish a detail differently across calls, even with identical inputs. `explainability.py` is therefore a template-based synthesis of exactly the structured signals every M10/M11 module already computes: no model, no network call, no randomness, no clock — both entry points are pure functions of their arguments, and this is verified directly by two dedicated determinism regression tests (`test_same_inputs_produce_identical_output`) rather than left to convention.

2. **Two entry points**, matching the two places this engine has real, measurable reasoning to explain:
   - `explain_recommendation(recommendation, *, regime=None, alignment=None)` — narrates a live BUY/HOLD/NO_TRADE call: confidence + risk score, calibrated probability (or the honest "not yet calibrated" note), regime trend + volatility (Module 11.1), institutional persistence (independently sourced from strikes history — see the bug fix below), timeframe confirmation (Module 11.2), and the existing `institutional_reasoning` string.
   - `explain_trade_quality(trade: dict)` — narrates why a CLOSED trade received the Trade Quality Score it did (Module 11.3): win/loss + points, the overall score/tier, and which of the three entry-time components (regime/timeframe/institutional) actually contributed, citing each one's own value.

3. **Fully additive and standalone**, per requirement 3: no existing dataclass (`Recommendation`, `RegimeProfile`, `TimeframeAlignment`, `TradeQualityScore`) or function from Modules 11.1–11.3 is modified — all are read-only inputs here. `explain_recommendation()` fetches `regime`/`alignment` itself only when not already provided (the same dedup-or-fetch convention `paper_trading.enter_from_recommendation()` established in Module 11.3), so it works standalone with just a `Recommendation` object.

4. **Honest omission, never a filler sentence.** Every section (`regime`, `institutional persistence`, `timeframe alignment`, `probability`, `institutional_reasoning`) is included only when the underlying data is genuinely available; a `None`/`UNKNOWN` input is silently omitted from the narrative rather than described with a vague placeholder sentence. `Explanation.inputs_used` makes this machine-checkable: it lists only the inputs that actually contributed, so a caller (or a test) can verify an explanation isn't quietly citing evidence it doesn't have.

5. **Real bug caught during testing and fixed before commit**: an initial draft nested the institutional-persistence sentence inside the same gate as the trend-regime sentence (both built by one `_regime_sentences()` helper). Since persistence is computed from `strikes` history (Module 11.1's `_persistence()`) and is genuinely independent of ADX/trend-regime data, a trade with real, persistent buildup but no `market_structure_snapshots` row would silently lose the persistence sentence — a caught real bug, not a hypothetical. Fixed by splitting into two independently-gated functions, `_trend_sentence()` and `_persistence_sentence()`, each contributing to `inputs_used` on its own; a dedicated test (`test_institutional_persistence_cited_when_buildup_is_sustained`) now asserts persistence is cited even when `"regime"` is absent from `inputs_used`.

## Files changed

- **New**: `agents/trading_intelligence/explainability.py` — `Explanation` dataclass, `explain_recommendation()`, `explain_trade_quality()`, `_trend_sentence()`, `_persistence_sentence()`, `_alignment_sentence()`.
- **New**: `test_agents/trading_intelligence/test_explainability.py` (21 tests).

No existing file was modified.

## Tests executed

- Module suite: **21/21 passed**.
- Full `test_agents/trading_intelligence/` suite: **215/215 passed** (up from 194).
- Full repository suite: **1,317 passed, 1 xfailed** (up from 1,296), **zero regressions**.

Coverage highlights:
- `TestExplainRecommendationBasics`: every action type (BUY/HOLD/NO_TRADE), confidence/risk-score citation, probability present vs. honestly absent, `institutional_reasoning` inclusion vs. omission.
- `TestExplainRecommendationRegimeAndAlignment`: regime cited only when real ADX data shows a trend; regime honestly omitted when unavailable; **the persistence-independent-of-regime bug fix**, explicitly asserted; timeframe alignment against the real, always-present on-disk NIFTY candle archive; the prefetched `regime`/`alignment` dedup path.
- `TestExplainRecommendationDeterminism`: identical inputs → identical `Explanation`; a stress sweep across every action/direction/symbol combination confirming no exception.
- `TestExplainTradeQuality`: open trade (no outcome yet) explained honestly; closed trade with no captured context explains the gap; closed trade with full context cites every one of the three components by name; win vs. loss wording; the honest "did not back" wording for a non-backing institutional finding.
- `TestExplainTradeQualityDeterminism`: identical trade dict → identical `Explanation`; an 8-trade lifecycle sweep (mixed context/no-context, wins/losses) confirming no exception across the whole set.

## Performance impact

`explain_recommendation()` only fetches `regime`/`alignment` when a direction is set and neither was pre-supplied — the same one `regime_profile.classify()` + one `timeframe_confirmation.check()` cost Module 11.3's `enter_from_recommendation()` already pays at trade-open time; a caller that already has both (e.g. a future dashboard call sharing the same cycle's data) pays nothing extra. `explain_trade_quality()` calls `trade_quality.score()` once, itself a pure computation over an already-fetched trade dict with no I/O. Neither function performs a database write or a second network/LLM round trip.

## Risks

- **Not yet wired into any live caller** (dashboard, scheduler, or `api.py`) — this module is the explanation engine itself, standalone and independently tested, matching the same "implement one module at a time" discipline Modules 11.1–11.3 established. Surfacing it on a dashboard view is a separate, later step, not requested here.
- **Sentence wording is a documented, transparent template, not a tunable model** — if real usage shows a phrasing needs to change, that's a string-literal edit in this file, not a retraining or re-prompting exercise (a direct benefit of the deterministic, non-LLM design).
- **`explain_recommendation()`'s auto-fetch path re-reads `regime_profile`/`timeframe_confirmation` when not pre-supplied** — cheap in isolation, but a caller invoking this function per-recommendation across many symbols without passing pre-fetched values would pay that cost per symbol; the dedup-or-fetch parameters exist specifically so a future wiring point (e.g. `api.get_symbol_overview()`) can avoid this by passing what it already computed.

## Commit hash

`ce3fe7b` on `worktree-m11-intelligence-depth`.

---

Waiting for approval before starting Module 11.5 (Adaptive Risk & Position Sizing).
