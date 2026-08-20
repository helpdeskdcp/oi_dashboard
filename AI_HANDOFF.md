AGENT: Claude
DATE: 2026-08-20
PHASE: Expiry-contract-identity remediation + Trading Intelligence dashboard audit (first handoff -- no prior Codex review this session)

ACTION:
Fixed a bug class where paper-trade tables matched an open position's strike
against the live option chain with no check that the chain still reflected
the SAME contract (expiry). Once a weekly/monthly contract rolled over, the
same strike number silently referred to a different, freshly-priced
instrument. Fixed identically across all 5 paper-trade tables
(ti_paper_trades, paper_orders, paper_trades, scalp_paper_trades,
v3_paper_trades): capture expiry_date_at_entry at open, compare against the
current cycle's resolved expiry before matching by strike, close via
data_access.recent_strike_history()'s pre-rollover reading on mismatch
(never a fabricated price). Root cause traced one level deeper: found
AngelOneFetcher.find_nearest_expiry() had no ">= today" filter at all,
so it could keep resolving an already-expired contract (confirmed in
production logs: stuck on an expired NIFTY weekly expiry for a full
trading day). Fixed by delegating to the already-correct
expiry_intelligence.get_nearest_expiry().

Separately audited /admin/trading-intelligence (full template + API
contract read): found and fixed a null-guard bug that could silently
abort the page's entire 15s refresh cycle, then added/upgraded 3
data-display sections for data the backend was already computing but
never rendering (Recent Closed Trades table, Institutional Intelligence
strike+evidence columns, Multi-Timeframe full OHLC, Execution State live
LTP + hit-status).

Ran a real backtest (not a blind flip) on TI_ENABLE_MOMENTUM_CONFIRMATION
before any decision -- evidence says keep it OFF (see report below).

CHANGED_FILES:
app.py, oi_engine.py, agents/config.py,
agents/trading_intelligence/{execution_state,strategy_registry,momentum_confirmation_backtest}.py,
backtest.py, templates/{sysadmin,trading_intelligence,postmarket_report,_sidebar}.html,
MOMENTUM_CONFIRMATION_BACKTEST_REPORT.md, DUAL_PROBABILITY_CALIBRATION_REPORT.md
(full list: `git log --oneline b0e9316..HEAD`)

TESTS:
~140 new tests across the PRs above. Full suite re-run multiple times this
session: consistently 2900-2915 passed, 1 xfailed. The only recurring
failures are 2 pre-existing git_fsck_ok checks (test_agents/hardening/) --
confirmed via direct `git fsck` to be caused by dangling commits from other
concurrent worktree sessions sharing this repo's object store, unrelated to
any code change here.

FINDINGS:
- Real, live-stuck positions existed before the fix (paper_orders ids
  84/92/123, 2-14 days open; a manually-relabeled ti_paper_trades id 76 --
  see git log for #30/#32/#33 commit messages for full detail). Fixed going
  forward; a code fix alone cannot retroactively unstick a position whose
  TRUE original entry expiry was never recorded, so remediation was manual
  + transparent (exit_reason documents the correction).
- TI_ENABLE_MOMENTUM_CONFIRMATION: real backtest across all 11 watched
  symbols, 5+ weeks of archive. No evidence supports turning it on --
  BANKNIFTY (the one symbol with a real, floor-clearing sample) got
  measurably worse. Full writeup in MOMENTUM_CONFIRMATION_BACKTEST_REPORT.md.

RISKS:
- find_nearest_expiry() is now the single source of truth for expiry_date_obj,
  which feeds Black-Scholes delta/target/SL calc across every symbol every
  cycle -- a regression here would be wide-reaching. Covered by 6 dedicated
  unit tests (test_angel_one_fetcher_find_nearest_expiry.py) plus cross-checked
  against 9 live production symbols post-deploy, but worth Codex's attention
  given the blast radius.
- expiry_date_at_entry backfill (for positions opened before the column
  existed) can only establish a baseline going forward -- cannot recover a
  position's true original contract identity if that data was never recorded.
  This is a known, accepted limitation, not an oversight.

REQUEST_TO_OTHER_AGENT:
1. Independent review of the find_nearest_expiry() fix (app.py, class
   AngelOneFetcher) -- this is the highest-leverage change this session
   given how many downstream call sites depend on its output being correct.
2. Review the expiry-contract-identity fix pattern for any 6th call site
   we may have missed (grep for `r.strike == .*strike` / `next\(\(r for r in rows`
   across app.py and agents/trading_intelligence/ -- confirm every live
   strike-matching site now goes through an expiry check first).
3. Security/production-readiness pass on the two new dashboard panels
   (Execution State live LTP, Post-Market Report date-param handling) --
   both are admin-gated read-only, but worth confirming no injection/auth
   gap in the new ?date= query param handling.

NEXT_ACTION:
No implementation currently in flight. Awaiting Codex review of the above,
or the next user-directed task.
