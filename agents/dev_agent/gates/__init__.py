"""
agents/dev_agent/gates -- the five mandatory validation gates every
candidate change must pass before a patch can be proposed: unit tests,
integration tests, backtest comparison, benchmark comparison, and code
quality (lint/type/security/dependency scan). Every gate returns a
GateResult (see gates/base.py); none of them merge, apply, or write
anything outside the worktree they're given.
"""
