AGENT: Claude
DATE: 2026-08-23
PHASE: Frontend redesign continuation (sidebar rebrand + persistent desktop rail)

ACTION:
Picked up an in-progress, uncommitted frontend redesign found sitting directly
in the shared checkout's working tree (attributed to Codex; not something
this session started). Verified it for real before touching anything --
`git diff` showed genuine, partial work: `templates/_sidebar.html` rebranded
the sidebar from "BATI" to "IDaddy" (logo mark + "OI SCALPING TERMINAL"
subtitle), reorganized/renamed the nav groups, and dropped most emoji icons;
`templates/_sidebar_css.html` added a `@media (min-width: 1180px)` block that
keeps the sidebar permanently open on desktop (no more toggle/overlay) and
pushes `.page-content` over by 280px.

That work was incomplete in a way that would have broken production if
deployed as-is: `.page-content` is a wrapper class only `dashboard.html`
uses. The other 27 templates render their content straight under `<body>`
with no such wrapper, so on any desktop viewport >=1180px the now-always-
visible sidebar would sit directly on top of their content with nothing
pushing it clear.

Fixed by switching the shared push rule from `.page-content` to `body`
(works for every page without requiring markup changes) -- but that alone
collided with a second, subtler bug: 21 of the 28 sidebar-including
templates each have their own `body { margin: 0; ... }` (or `html, body
{ margin: 0; }`) rule later in the same `<style>` block, which zeroes
`margin-left` back to 0 via the cascade (same specificity, later source
order wins). Added an explicit `@media (min-width: 1180px) { body
{ margin-left: 280px; } }` override immediately after each of those rules
so the desktop push actually survives the cascade on every page, not just
`dashboard.html`. This is a real, previously-invisible bug in the redesign
that had not been exercised yet (no live server run to catch it).

Did NOT start the larger requested redesign (information-hierarchy reorg
per page, mobile-specific ordering, tabs/collapsible sections for advanced
data) -- that's a page-by-page content task across up to 21 templates and
out of scope for a single verification-first pass. Documented as explicitly
NOT done in PRODUCTION_STATE.md so it isn't mistaken for finished.

The shared checkout (`/root/oi_dashboard`, not this worktree) still has
unrelated uncommitted changes this PR does not touch and did not read into:
an Ollama LLM-provider rewrite in `app.py` (matches the untracked
`app.py.before_ollama_provider_fix` / `app.py.before_json_parser_fix`
backup files sitting there) and a batch of `data/history/*` CSV/parquet
diffs from the live candle recorder. Left exactly as found, per the explicit
"do not reset/revert/discard/overwrite" instruction -- this PR only carries
the frontend template diff, extracted as a patch and replayed in an isolated
worktree so it wouldn't get bundled with that unrelated backend work.

CHANGED_FILES:
templates/_sidebar.html, templates/_sidebar_css.html, templates/dashboard.html,
templates/access_restricted.html, templates/admin_users.html,
templates/advisor.html, templates/auto_trading_settings.html,
templates/backtest.html, templates/calibration.html, templates/charts.html,
templates/charts_pro.html, templates/dev_settings.html,
templates/dynamic_sr.html, templates/engine_v2.html, templates/engine_v3.html,
templates/live_positions.html, templates/manual_trading.html,
templates/postmarket_report.html, templates/premarket_report.html,
templates/signal_history.html, templates/signals.html,
templates/smart_analysis.html, templates/sysadmin.html,
templates/trading_intelligence.html, .gitignore (from the original WIP:
excludes `static/structure_charts/previews/*`), PRODUCTION_STATE.md,
AI_HANDOFF.md, CODEX_REVIEW.md (new)

TESTS:
- Jinja2 syntax parse on all 24 sidebar-including templates: 24/24 OK.
- Flask test_client render check (isolated temp DB, SKIP_AUTOSTART=1,
  matching this repo's own test fixture pattern -- never touched the real
  DB or a live broker session) against 8 representative pages (`/`,
  `/signals`, `/admin/trading-intelligence`, `/calibration`, `/dynamic-sr`,
  `/backtest`, `/charts-pro`, `/advisor`): all 200, sidebar markup present,
  desktop-push CSS present in the rendered output.
- Ran the full route-level test suite (12 `test_*route*.py` files, 103
  tests): 101 passed, 2 pre-existing failures unrelated to this change
  (`test_trading_intelligence_run_cycle_route.py`'s two flag-default
  assertions read the real production `.env` via `load_dotenv(override=True)`
  walking up from a nested worktree -- same known issue already documented
  in this file's own test comments; confirmed unrelated by inspection, not
  caused by any template edit here).
- Did NOT run a live `python3 app.py` server for a browser screenshot check:
  attempted once, and it surfaced a real hazard worth recording --
  `load_dotenv(override=True)` in `app.py` walks up from a nested worktree
  and loads the REAL production `.env`, silently overriding any PORT/
  DB_PATH/ADMIN_BOOTSTRAP_* env vars set for the attempt. It crashed
  immediately on a port-5050 collision with the actual running production
  process before doing any harm, but a slower failure could have pointed a
  throwaway preview server at the live database. Do not run `python3 app.py`
  directly from inside a worktree for a preview -- use Flask's test_client
  with DB_PATH monkeypatched in Python after import (as done here), never
  via env vars.

CODEX FINDINGS RESOLVED: N/A (no new Codex review was provided this phase --
this phase continues Codex's own frontend WIP, not a review-fix cycle).

RISKS:
- The desktop persistent-sidebar layout is now consistent across all 28
  templates, but only sidebar/rail-level -- it has not been visually
  screenshotted in a real browser (see TESTS above for why), only confirmed
  server-side (renders, correct CSS present, no exceptions). Recommend an
  actual browser check before/shortly after deploy.
- `access_restricted.html` also got the fix even though it's a pre-auth-
  adjacent page (shown to logged-in users without full-view access) --
  confirmed via grep it does include the sidebar, so it was in scope.
- The unrelated uncommitted Ollama/app.py work and data-file diffs sitting
  in the shared checkout remain uncommitted and untouched. They are not
  part of this PR and were not evaluated for correctness -- flagging so
  the next agent doesn't assume they were reviewed.

REQUEST_TO_OTHER_AGENT:
1. If Codex resumes the frontend redesign, the remaining scope is the
   actual page-by-page content reorganization (info hierarchy, mobile
   ordering, tabs/collapsible advanced sections) -- the sidebar/rail
   plumbing this phase fixed should be treated as a stable foundation, not
   something to re-touch.
2. A real browser screenshot pass (desktop >=1180px and mobile) on a few
   pages would close the one gap this phase couldn't safely verify (see
   RISKS above).
3. Please independently confirm the app.py/data-file uncommitted state in
   the shared checkout before assuming it's related to or safe to merge
   with any frontend work -- it looks like a separate, in-progress backend
   hotfix (Ollama provider) that this phase deliberately left alone.

NEXT_ACTION:
PR open for the frontend fix once pushed. No restart needed if merged
alone (template-only diff, `TEMPLATES_AUTO_RELOAD=True` covers it) --
CSS/HTML changes take effect on next page load, no deploy step required
beyond the merge itself.
