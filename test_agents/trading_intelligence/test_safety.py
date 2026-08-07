"""
test_agents/trading_intelligence/test_safety.py -- Milestone 10's own
central safety verification. "Never place real orders. Recommendation
mode and paper trading only." Verified programmatically, not just by
docstring claim -- the same "check the invariant in code" posture
Milestone 8's security_audit.check_propose_only_invariant() and the
Hardening Sprint's own AST broker-touch scan already established.
"""
import ast
import os

TI_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "agents", "trading_intelligence")

FORBIDDEN_NAMES = ("SmartConnect", "smartApi", "AngelOneFetcher", "_shared_angel_fetcher")
FORBIDDEN_CALL_SUBSTRINGS = (
    "place_order", "placeOrder", "modifyOrder", "cancelOrder",  # broker order-placement shapes
)


def _all_py_files():
    files = []
    for root, _dirs, names in os.walk(TI_DIR):
        for name in names:
            if name.endswith(".py"):
                files.append(os.path.join(root, name))
    return files


class TestNoLiveBrokerAccess:
    def test_no_forbidden_names_referenced_anywhere_in_the_package(self):
        """AST-based (not a raw substring grep, which the Hardening
        Sprint's own equivalent test already found gives false positives
        inside docstrings) -- flags an actual Name/Attribute reference to
        any of the live broker session's own identifiers."""
        violations = []
        for path in _all_py_files():
            with open(path, "r", errors="replace") as fh:
                source = fh.read()
            tree = ast.parse(source, filename=path)
            for node in ast.walk(tree):
                name = None
                if isinstance(node, ast.Name):
                    name = node.id
                elif isinstance(node, ast.Attribute):
                    name = node.attr
                if name in FORBIDDEN_NAMES:
                    violations.append((path, name, node.lineno))
        assert violations == [], f"agents/trading_intelligence/ references the live broker session: {violations}"

    def test_no_order_placement_function_names(self):
        violations = []
        for path in _all_py_files():
            with open(path, "r", errors="replace") as fh:
                source = fh.read()
            for needle in FORBIDDEN_CALL_SUBSTRINGS:
                if needle in source:
                    violations.append((path, needle))
        assert violations == [], f"agents/trading_intelligence/ references an order-placement function: {violations}"

    def test_no_module_imports_app(self):
        """agents/trading_intelligence/ is imported BY app.py (the new
        /api/trading-intelligence/overview route) -- it must never import
        app.py itself (a circular dependency, and the exact "import the
        ~7000-line Flask app with real broker machinery into agents/"
        landmine every prior milestone has refused since Milestone 6)."""
        violations = []
        for path in _all_py_files():
            with open(path, "r", errors="replace") as fh:
                source = fh.read()
            tree = ast.parse(source, filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "app":
                            violations.append((path, node.lineno))
                elif isinstance(node, ast.ImportFrom) and node.module == "app":
                    violations.append((path, node.lineno))
        assert violations == [], f"agents/trading_intelligence/ imports app.py: {violations}"

    def test_paper_trading_never_calls_a_broker_only_pure_sqlite(self):
        """A direct, semantic check on top of the AST scans above:
        ti_store.open_trade()/close_trade() (the only two functions that
        ever write a "trade" anywhere in this package) must be pure
        sqlite3 -- no import of oi_engine's live-adjacent siblings that
        DO touch the broker (there are none reused here, by design)."""
        import ast as ast_mod
        path = os.path.join(TI_DIR, "ti_store.py")
        with open(path) as fh:
            tree = ast_mod.parse(fh.read())
        imported_modules = set()
        for node in ast_mod.walk(tree):
            if isinstance(node, ast_mod.Import):
                imported_modules.update(a.name for a in node.names)
            elif isinstance(node, ast_mod.ImportFrom) and node.module:
                imported_modules.add(node.module)
        assert imported_modules <= {"datetime", "json", "sqlite3"}, (
            f"ti_store.py imports more than the stdlib it needs for a pure DB store: {imported_modules}"
        )
