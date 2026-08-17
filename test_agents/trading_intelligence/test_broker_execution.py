import pytest

from agents.trading_intelligence import broker_execution


class TestNullBrokerExecutorSafety:
    @pytest.fixture()
    def executor(self):
        return broker_execution.get_broker_executor()

    def test_get_broker_executor_returns_null_executor(self, executor):
        assert isinstance(executor, broker_execution.NullBrokerExecutor)

    @pytest.mark.parametrize("method", [
        "place_entry", "place_sl", "place_target", "modify_sl", "modify_target", "exit_position",
    ])
    def test_every_method_returns_disabled_status(self, executor, method):
        result = getattr(executor, method)("EX1", symbol="NIFTY", strike=24500, qty=50)
        assert result["status"] == "DISABLED"
        assert result["broker_order_id"] is None
        assert result["execution_id"] == "EX1"
        assert "not enabled" in result["reason"]

    @pytest.mark.parametrize("method", [
        "place_entry", "place_sl", "place_target", "modify_sl", "modify_target", "exit_position",
    ])
    def test_every_method_accepts_arbitrary_kwargs_without_error(self, executor, method):
        # A future real adapter will need a rich, varying kwarg shape per
        # call site -- the Null implementation must never reject/crash on
        # any of them, only ever return the same safe DISABLED result.
        result = getattr(executor, method)("EX2", price=120.5, quantity=50, order_type="LIMIT", extra_field="x")
        assert result["status"] == "DISABLED"
        assert result["requested"]["price"] == 120.5

    def test_base_interface_methods_are_not_directly_usable(self):
        base = broker_execution.BrokerExecutor()
        with pytest.raises(NotImplementedError):
            base.place_entry("EX3")

    # Structural proof that this module never references a broker name or
    # order-placement call is NOT duplicated here -- test_safety.py's own
    # AST-based scan (TestNoLiveBrokerAccess) already walks every .py file
    # in this package, including this one, and correctly ignores docstring
    # prose (unlike a naive substring search, which was tried here first
    # and produced a false positive on this file's own explanatory
    # docstring). That existing scan is the real safety net; see it run
    # as part of the full trading_intelligence suite in this PR's own
    # verification, not re-implemented a second time here.
