# Backtest / Paper / Live Exit Contract

**Finding: two genuinely separate, non-consolidated exit implementations exist — one real (the canonical paper-trading path), one backtest-only (a separate feature). They were never meant to be the same thing, but the canonical path's own exit logic is simpler than what a production system usually wants, and that gap is real. No code changed by this document.**

## The real live exit path

`agents/trading_intelligence/ai_trading_engine.py`'s `_check_open_trade_exit()` (line 350) is what actually closes a `ti_paper_trades` row. Verified by reading it fresh:

- **Uses the same entry/SL/target values `oi_engine.generate_signal()` produced** — `trade["target_price"]`/`trade["sl_price"]` are the exact fields stored at entry from `signal["target_price"]`/`signal["sl_price"]`. No divergence between what generated the signal and what exits it.
- **Two conditions only**: `current_ltp >= target_price` → `TARGET HIT`; `current_ltp <= sl_price` → `STOP LOSS`.
- **Expiry-rollover handling exists and is real** (PR #42, this session): before any strike-matching, `trade["expiry_date_at_entry"]` is compared against the current cycle's resolved expiry; on a detected rollover it closes at the last known pre-rollover price rather than comparing against a freshly-rolled contract's unrelated price.
- **No time-based exit.** Grepped for `MAX_HOLD`/`TIME_EXIT`/`TIME EXIT` across `agents/trading_intelligence/` — the only hits are in three *backtest* modules referencing `backtest.MAX_HOLD_MINUTES` for their own replay purposes. The live path has no equivalent. A trade that never hits target or SL can sit open indefinitely (bounded only by an eventual expiry rollover, which closes it at whatever price happened to be last recorded, not a decision).

## The backtest-only exit path

`exit_engine_v4.py`'s `open_position()`/`manage_exit()` implement trailing stop, VWAP-cross exit, momentum-fade exit, and adaptive time exit (`TIME EXIT`, `VWAP CROSS`, `MOMENTUM FADE` reasons). Re-verified by grep: these two functions are called **only** from `backtest.py` (lines 1661, 1680) and `test_exit_engine_v4.py` — zero calls from `app.py`. `app.py`'s own `analyze_open_position()` (line 8379) is a distinct function serving `dynamic_sr_engine.py`'s separate, advisory-only feature; it doesn't call `exit_engine_v4` at all. This reconfirms `ARCHITECTURE_AUDIT.md`'s finding exactly: `exit_engine_v4`'s real logic never executes live.

## Are these the same contract?

No, and they were never meant to be — they belong to two different features (`ENGINE_REGISTRY.md`: `oi_engine`'s canonical paper-trading path vs. `dynamic_sr_engine`'s separate advisory display). Unifying them is out of scope; `dynamic_sr_engine`/`exit_engine_v4` is a distinct strategy, not a broken half of the canonical one.

What *is* in scope for the canonical path specifically: the requirement that "backtest, paper, and live evaluate the SAME entry/SL/target/invalidation/exit contract" is **already mostly true** for `oi_engine`'s own path — `backtest.simulate_trades()` and `_check_open_trade_exit()` both check target-hit/SL-hit against the same generated values. The one real, concrete gap: `simulate_trades()` has a `MAX_HOLD_MINUTES` time exit; the live path has none. This is a genuine contract mismatch, not a hypothetical one — a backtest report's win/loss/expectancy numbers assume every trade resolves within 30 minutes or gets marked `TIME EXIT`; the live path's real trades have no such boundary, so a live trade's true holding-time distribution is unmeasured and could differ from what every backtest report in this repo (`ENTRY_SL_TARGET_BACKTEST_REPORT.md` onward) implicitly assumed.

## Proposed future contract (design only, not implemented)

1. Add a time-exit boundary to `_check_open_trade_exit()` matching `backtest.MAX_HOLD_MINUTES`'s semantics (same constant, or a documented live-specific value if a different one is justified by evidence) — closing on "no target/SL hit within N minutes" the same way the backtest already does, rather than leaving live trades open indefinitely.
2. Document explicitly that `exit_engine_v4` remains a separate feature's exit logic, not a candidate for merging into the canonical path — the contract requirement applies within each feature's own family, not across unrelated features.
3. Any such change needs its own before/after backtest validation (same discipline as `SL_TARGET_RETUNE_REPORT.md`) before touching the live `_check_open_trade_exit()` — not attempted here.
