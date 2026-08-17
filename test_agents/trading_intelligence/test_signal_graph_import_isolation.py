"""
Verifies the fix for the review finding on PR #14: a top-level
`from . import signal_graph` in api.py meant any failure to import
langgraph itself (missing package, broken/partial install, a future
dependency conflict) would take down the ENTIRE trading-intelligence
module -- not just the shadow feature -- regardless of
TI_ENABLE_SIGNAL_GRAPH_SHADOW's value, since that flag is only checked
at call time and the crash would happen at import time, before any
flag check could run.

This simulates langgraph being genuinely unavailable (by poisoning
sys.modules the same way CPython's own import system treats an
uninstalled package) and re-imports agents.trading_intelligence.api
fresh, asserting it still imports successfully with signal_graph=None
rather than raising.
"""
import importlib
import sys

import agents.trading_intelligence as ti_pkg


def _force_fresh_signal_graph_import(monkeypatch):
    # None in sys.modules is CPython's own documented signal that a
    # module failed to import -- any `import langgraph` or `from
    # langgraph.graph import ...` raises ImportError immediately, the
    # exact failure mode a broken/missing dependency produces.
    monkeypatch.setitem(sys.modules, "langgraph", None)
    monkeypatch.setitem(sys.modules, "langgraph.graph", None)
    # Removing the sys.modules cache entry alone is NOT enough: `from .
    # import signal_graph` resolves via _handle_fromlist's own
    # `hasattr(package, "signal_graph")` shortcut, which is still True
    # because the earlier successful import already set that attribute
    # on the `agents.trading_intelligence` package object -- deleting
    # only the sys.modules entry leaves that stale attribute in place
    # and the "import failure" never actually gets exercised. Both must
    # be cleared to force a genuinely fresh (and now failing) import.
    monkeypatch.delitem(sys.modules, "agents.trading_intelligence.signal_graph", raising=False)
    monkeypatch.delattr(ti_pkg, "signal_graph", raising=False)
    monkeypatch.delitem(sys.modules, "agents.trading_intelligence.api", raising=False)
    monkeypatch.delattr(ti_pkg, "api", raising=False)


class TestSignalGraphImportIsolation:
    def test_api_imports_fine_when_langgraph_is_unavailable(self, monkeypatch):
        _force_fresh_signal_graph_import(monkeypatch)

        api = importlib.import_module("agents.trading_intelligence.api")

        assert api.signal_graph is None

    def test_run_scheduled_cycle_still_importable_and_callable_when_langgraph_unavailable(
        self, ti_db, monkeypatch,
    ):
        _force_fresh_signal_graph_import(monkeypatch)

        api = importlib.import_module("agents.trading_intelligence.api")
        monkeypatch.setattr(api.config, "TI_ENABLE_SIGNAL_GRAPH_SHADOW", True)

        # Even with the shadow flag ON, a None signal_graph must never
        # be called -- the real cycle must complete normally regardless.
        results = api.run_scheduled_cycle(symbols=["NOT_A_REAL_SYMBOL"])
        assert results["NOT_A_REAL_SYMBOL"]["available"] is False
