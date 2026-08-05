"""
agents/registry.py -- plugin registration for BATI autonomous agents.
Adding a new agent (e.g. the future System Administrator, per the
architecture doc's roadmap) means writing one new module and calling
register_agent -- nothing in this file or in base_agent.py changes.
"""

_AGENTS: dict[str, type] = {}


def register_agent(name: str):
    """Class decorator: @register_agent("dev") class DevAgent(BaseAgent): ...
    Raises ValueError on a duplicate name -- two agents silently sharing
    one registry slot would be a much worse failure mode than an import-
    time error."""

    def _decorator(cls):
        if name in _AGENTS:
            raise ValueError(
                f"agent {name!r} is already registered (by {_AGENTS[name].__name__}) -- "
                f"cannot also register {cls.__name__}"
            )
        _AGENTS[name] = cls
        return cls

    return _decorator


def get_agent(name: str) -> type:
    if name not in _AGENTS:
        raise KeyError(f"no agent registered as {name!r} -- known agents: {registered_agents()}")
    return _AGENTS[name]


def registered_agents() -> list[str]:
    return sorted(_AGENTS)


def _reset_for_tests():
    """Test-only: clears the registry so test modules can register
    throwaway agent classes without colliding across test runs/reloads.
    Never called from production code."""
    _AGENTS.clear()
