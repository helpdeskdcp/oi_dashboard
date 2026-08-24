# Signal Contract (Proposed)

A canonical, immutable signal object — what one real trading decision should carry. Every field below is marked **EXISTS** (with exact location) or **PROPOSED** (not invented as if real). No code changed by this document.

| Field | Status | Where |
|---|---|---|
| `signal_id` | **PROPOSED** | No unique per-signal identity exists today. `ti_signal_log`/`ti_paper_trades` use auto-increment DB primary keys as their identity, not a portable signal ID computable before persistence. |
| `timestamp` | EXISTS (implicit) | `ti_store.record_signal()` stamps a row time; `Recommendation` itself carries no explicit timestamp field — **PROPOSED as an explicit field on the object itself**, not just the DB row. |
| `symbol` | EXISTS | `Recommendation.symbol` (`ai_trading_engine.py:95`) |
| `timeframe` | **PROPOSED** | No timeframe field exists — this engine operates on the current option-chain cycle only, not a named candle timeframe. |
| `direction` | EXISTS | `Recommendation.direction` (`:97`) |
| `instrument` | EXISTS (as `strike`) | `Recommendation.strike` (`:98`) — no separate expiry-qualified instrument identifier beyond `expiry_date_resolved` (`:119`) |
| `entry` | EXISTS | `Recommendation.entry_price` (`:106`) |
| `stop_loss` | EXISTS | `Recommendation.sl_price` (`:107`) |
| `target` | EXISTS | `Recommendation.target_price` (T1, `:108`) + `targets` (T1-T3 list, `:109`) |
| `risk_reward` | **PROPOSED** | Not computed anywhere in `Recommendation` today — `risk_score` (`:105`) is a different, capital-relative measure (`_compute_risk_score()`), not a reward:risk ratio. (The uncommitted `failure_gate.py`'s `check_reward_risk()` does compute one, but only as a PASS/FAIL check, not a stored field.) |
| `market_state` | **PROPOSED** | No field; `market_bias` (`:101`) is the closest (CE/PE/neutral lean, not a state machine) |
| `structure_state` | Partial — EXISTS as shadow | `market_regime` (`:135`, populated only when `TI_ENABLE_REGIME_FILTER_SHADOW` is on) |
| `oi_state` | **PROPOSED** | No discrete field — folded into `oi_reasoning` free text (`:115`) and `signal["reason"]`, never a structured SUPPORTING/CONTRADICTING value |
| `flow_state` | **PROPOSED** | Same as above — no discrete field |
| `trap_state` | **PROPOSED, no backing calculation exists at all** — confirmed by `ARCHITECTURE_AUDIT.md` §L: no crowding/trap concept anywhere in the repo |
| `probability` | EXISTS | `Recommendation.probability` (`:103`) — genuinely distinct from `confidence`, already never conflated (see `PROBABILITY_CALIBRATION_AUDIT.md`) |
| `expected_value` | **PROPOSED** | No EV calculation exists anywhere in the repo (see `EXPECTED_VALUE_GATE_DESIGN.md`) |
| `decision` | EXISTS (as `action`) | `Recommendation.action` (`:96`) — `"BUY CE"\|"BUY PE"\|"HOLD"\|"NO_TRADE"` |
| `veto_reason` | Partial | `reasoning` (`:113`) carries a free-text reason for NO_TRADE, but it's prose, not a structured taxonomy — see `FAILURE_TAXONOMY.md` |
| `confidence` | EXISTS | `Recommendation.confidence` (`:102`) |
| `source` | **PROPOSED** | No field distinguishing which engine produced a given output — implicit (always `oi_engine.generate_signal()` for anything in `ti_signal_log`), but not explicit on the object |
| `data_timestamp` | **PROPOSED** | No field distinguishing "when was the underlying data as-of" from "when was this decision made" — currently implicit/same-cycle |
| `expiry/invalidation state` | EXISTS | `expiry_date_resolved` (`:119`), `expiry_context` (`:124`) |

## One thing already done correctly, worth preserving explicitly

`confidence` (rule-based, same-cycle) and `probability` (historical win-rate calibration, `_calibrated_probability()`, honestly `None` below 5 samples) are already two distinct, never-conflated fields on `Recommendation` — the codebase does not rename confidence to probability today. This satisfies "do not force confidence to equal probability" already, without any change needed.

## Status vocabulary for evidence-bearing fields

For `oi_state`, `flow_state`, `trap_state`, `structure_state`, and any future evidence field:

- **`UNKNOWN`** — the underlying computation was never attempted this cycle
- **`NOT_AVAILABLE`** — attempted, but the required input (market_structure, NSE data, candles) was absent this cycle
- **`NEUTRAL`** — evaluated, genuinely no directional lean in the evidence
- **`SUPPORTING`** — evaluated, agrees with the trade's direction
- **`CONTRADICTED`** — evaluated, disagrees with the trade's direction
- **`VETOED`** — this specific evidence independently blocked the trade (only applies to hard-veto-capable checks)

## Why missing evidence must never become NEUTRAL

This codebase already holds to this discipline in three places: `snapshot.available` (a `False` here is a hard NO_TRADE, never silently treated as "neutral market"), `regime_profile.classify()` (degrades to `"UNKNOWN"` trend/volatility regime when history is insufficient, never guesses a regime), and `_calibrated_probability()` (returns `None` with an explicit "insufficient history" note below `CALIBRATION_MIN_SAMPLE`, never a fabricated percentage). A `NEUTRAL` verdict is an evaluated, meaningful absence-of-lean; `NOT_AVAILABLE`/`UNKNOWN` is an absence of evaluation entirely — collapsing the two would let missing data silently pass a check it was never actually run against.
