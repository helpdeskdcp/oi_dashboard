"""
agents/dev_agent -- the AI Developer agent (AGT-02). Milestone 2 ships the
validation pipeline (worktree isolation, five gates, regression analysis,
patch generation, approval decisioning) that any future candidate change
must pass through -- whether that candidate comes from a human's manual
commit today or the LLM-driven detector/patcher landing in Milestone 3.

No agent-registration or LLM-calling code lives here yet; this package
does not import agents.llm_providers.
"""
