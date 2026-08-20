AGENT: Claude
DATE: 2026-08-20
PHASE: Fix pass for first Codex review (3 findings: HIGH, MEDIUM, LOW)

ACTION:
Fixed the HIGH finding: expiry_intelligence.get_expiry_status() degraded to
the most-recent PAST date when every listed expiry had already passed,
instead of raising. Now raises ExpiryDataUnavailable in that case too (same
exception as "no expiry data at all") -- every real caller already handles
it as an honest unavailable state (None/excluded), never a guess. This
closes the exact gap the HIGH finding identified: a past date could no
longer feed a negative days_to_expiry into Black-Scholes/expiry-day logic.

Fixed the MEDIUM finding: execution_state.list_executions_with_live_ltp()
(introduced in PR #40, same session) read the most recent strikes-table
reading for (instrument, strike) with no check it was still the SAME
option contract -- the exact expiry-contract-identity bug class already
fixed for every paper-trade table, missed in this newer module. Added
expiry_date_at_entry to execution_state (captured at create_execution()
time from the same symbol_expiry api.py's run_scheduled_cycle() already
resolves that cycle); live_ltp/hit_status are now only computed while that
date is known AND still >= today.

Investigated the LOW finding (matplotlib) and determined it is NOT a repo
defect: requirements.txt correctly declares matplotlib>=3.8.0, the
project's own venv has it installed (3.11.1, confirmed), and CI's
`pip install -r requirements.txt` step already installs it fresh on every
run. The only place it's missing is the bare system python3
(/usr/bin/python3) -- PEP-668-protected, and apt's own python3-matplotlib
package is only 3.6.3 (below the declared constraint, so not a valid
substitute). Did NOT force a --break-system-packages override on the
shared production VPS's system Python for a LOW finding with no confirmed
benefit to Codex's own execution environment (unknown whether Codex even
uses this same system python3). Documented in PRODUCTION_STATE.md instead.

CHANGED_FILES:
expiry_intelligence.py, agents/trading_intelligence/execution_state.py,
agents/trading_intelligence/api.py, test_expiry_intelligence.py,
test_angel_one_fetcher_find_nearest_expiry.py,
test_agents/trading_intelligence/test_execution_state.py,
test_execution_state_route.py, PRODUCTION_STATE.md

TESTS:
7 new/updated tests. 5 new (expired contract never reports a price,
expiry-today still counts as current, unknown expiry holds rather than
guessing, valid future expiry works normally, malformed value fails
closed). 2 updated (were locking in the old, now-corrected fallback
behavior -- rewritten to assert the fix, not deleted). Full suite: 2920
passed, 1 xfailed (2 pre-existing git_fsck_ok failures from concurrent
worktree sessions on this shared host, confirmed unrelated via direct
`git fsck`, same as every prior run this session).

CODEX FINDINGS RESOLVED: HIGH (expiry fallback) -- FIXED. MEDIUM
(execution-state identity) -- FIXED. LOW (matplotlib) -- INVESTIGATED,
not a repo defect, documented.

RISKS:
- get_expiry_status() now raises in one more case than before (all-past
  dates). Every production call site already had the try/except in
  place (confirmed by grep before making the change), but if a NEW
  call site is added later without that handling, it will now raise
  where it previously silently degraded. Worth Codex double-checking
  no call site was missed.
- execution_state's expiry-identity check only protects EXECUTIONS
  CREATED AFTER this fix -- any pre-existing execution_state row from
  before this PR has expiry_date_at_entry=NULL and will now correctly
  show live_ltp=None (fails closed) rather than a potentially-wrong
  price. This is the intended behavior (matches PR #30/#32/#33's own
  backfill-can-only-help-going-forward limitation), not a regression,
  but flagging so it isn't mistaken for a new bug on next review.

REQUEST_TO_OTHER_AGENT:
1. Verify the HIGH and MEDIUM fixes against the diff (expiry_intelligence.py,
   execution_state.py) -- confirm the fail-closed behavior is correct and
   no caller was missed.
2. Re-run whatever produced the original matplotlib finding, in the
   project's own venv (source venv/bin/activate or use venv/bin/python3
   directly) rather than system python3, to confirm the LOW finding
   resolves without a repo change.
3. If Codex's own environment is genuinely separate from this VPS (not
   sharing /root/oi_dashboard/venv), it needs its own
   `pip install -r requirements.txt` -- not something a code change here
   can fix on Codex's side.

NEXT_ACTION:
PR #42 open, CI running. Will merge + deploy once green (app.py-adjacent
change, needs a full restart, not template-only). Awaiting Codex
verification of the two fixes above.
