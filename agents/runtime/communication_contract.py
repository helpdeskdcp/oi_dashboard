"""
agents/runtime/communication_contract.py -- "Agent Communication.
Agents communicate only through: Event Bus, Shared Memory, Approved
APIs. No direct hidden coupling."

Scoped specifically to this milestone's OWN new modules
(agents/runtime/*.py) -- not retroactively enforced on the pre-existing
seven-gate pipeline (agents.dev_agent/agents.quant_researcher/
agents.risk_manager/agents.trading_supervisor), which legitimately calls
directly between gate modules as one synchronous chain (Gate 6 calls
Gate 7's inputs, research_engine calls risk_intelligence.assess()
directly, etc.) -- a different, already-tested, already-documented
pattern this milestone was explicitly told not to redesign. What IS
checked, programmatically (not just by convention): no module under
agents/runtime/ reaches into another agents/runtime/ module's PRIVATE
(underscore-prefixed) names from outside that module -- the same
"verify the invariant in code, not just in a docstring" posture
agents.sys_admin.security_audit.check_propose_only_invariant() already
established for the propose-only rule.

"Event Bus" = agents.runtime.runtime_events / agents.event_bus.
"Shared Memory" = agents.memory (every agent's own store) plus
agents.runtime.runtime_store (this milestone's own persisted state).
"Approved APIs" = any module's PUBLIC (non-underscore) functions.
"""
import ast
import os

_RUNTIME_DIR = os.path.dirname(os.path.abspath(__file__))
_RUNTIME_MODULE_NAMES = (
    "agent_runtime", "approval_engine", "communication_contract", "market_session",
    "policy_engine", "runtime_events", "runtime_store", "scheduler", "task_queue", "workflow_engine",
)


def _module_name_of(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def find_private_cross_module_access(*, runtime_dir: str | None = None) -> list:
    """Scans every agents/runtime/*.py file's AST for `<name>.<attr>`
    attribute access where `<name>` is one of this package's OWN module
    names (imported as e.g. `from . import runtime_store`) and `<attr>`
    starts with `_` -- a private-internals reach-in from outside the
    module that owns it. Returns [{"file", "accessed", "line"}, ...];
    empty means the contract holds."""
    runtime_dir = runtime_dir or _RUNTIME_DIR
    violations = []
    for fname in sorted(os.listdir(runtime_dir)):
        if not fname.endswith(".py") or fname == "__init__.py":
            continue
        this_module = _module_name_of(fname)
        path = os.path.join(runtime_dir, fname)
        with open(path, "r", errors="replace") as fh:
            source = fh.read()
        tree = ast.parse(source, filename=path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if not node.attr.startswith("_") or node.attr.startswith("__"):
                continue
            if not isinstance(node.value, ast.Name):
                continue
            referenced_module = node.value.id
            if referenced_module == this_module:
                continue  # a module referencing its own private names (rare, e.g. via an alias) is fine
            if referenced_module in _RUNTIME_MODULE_NAMES:
                violations.append({"file": fname, "accessed": f"{referenced_module}.{node.attr}", "line": node.lineno})
    return violations
