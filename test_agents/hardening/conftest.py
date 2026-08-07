"""
test_agents/hardening/ -- Production Hardening & Validation Sprint (run
after Milestone 8, explicitly NOT a new milestone / new agent). Real
fault injection, DB integrity, extended stress/recovery, extended
memory-leak sweeps, and a real security audit run against this repo --
covering the objectives that aren't already exercised by
test_agents/sys_admin/test_production_readiness.py (Milestone 8's own
Module 10, which this package deliberately does not duplicate).

30-day market replay and performance profiling live as real, runnable
scripts under scripts/hardening/ (see PRODUCTION_HARDENING_SPRINT.md for
the actual numbers they produced) with a fast smoke-test counterpart
here so they stay part of the regression suite without re-running a full
sweep on every `pytest` invocation.
"""
from test_agents.risk_manager.conftest import (
    paper_db,  # noqa: F401  (re-exported fixture)
)
