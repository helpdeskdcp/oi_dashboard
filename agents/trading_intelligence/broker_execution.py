"""
agents/trading_intelligence/broker_execution.py -- Post-launch upgrade,
Phase A: the broker execution ADAPTER INTERFACE, and its only
implementation for now, NullBrokerExecutor.

THIS MODULE NEVER CONTACTS A BROKER. NullBrokerExecutor.place_entry()/
place_sl()/place_target()/modify_sl()/modify_target()/exit_position()
each return a plain, safe {"status": "DISABLED", ...} dict and do
nothing else -- no network call, no AngelOneFetcher/SmartConnect
import, no side effect of any kind. Verified structurally (not just by
this docstring) by this package's own AST safety scan,
test_agents/trading_intelligence/test_safety.py, exactly like every
other module in agents/trading_intelligence/.

WHY THIS INTERFACE EXISTS NOW, BEFORE ANY REAL EXECUTION IS APPROVED:
a future Angel One adapter (a SEPARATE, explicitly-approved phase, not
started here) will implement this SAME interface. Every call site that
will eventually need to place/modify/cancel a real order is written
against THIS interface today, so introducing a real adapter later is a
one-line swap (which BrokerExecutor implementation gets constructed),
never a rewrite of the call sites themselves. Until that future phase:
every execution policy in this codebase is wired to NullBrokerExecutor
and NOTHING else -- there is no configuration flag, environment
variable, or code path anywhere in this repository that can make a
call to this module reach a real broker.

The interface signature intentionally mirrors execution_state.py's own
vocabulary (execution_id as the idempotency/correlation key) so a
future real adapter can use it directly for order-side idempotency
(e.g. an Angel One order tag) without inventing a second key.
"""


class BrokerExecutor:
    """The interface every broker adapter (real or null) implements.
    Never instantiate this base class directly -- it exists only to
    document the contract. Every method takes the SAME execution_id a
    future real adapter would use as its own idempotency/order-tag key."""

    def place_entry(self, execution_id: str, **kwargs) -> dict:
        raise NotImplementedError

    def place_sl(self, execution_id: str, **kwargs) -> dict:
        raise NotImplementedError

    def place_target(self, execution_id: str, **kwargs) -> dict:
        raise NotImplementedError

    def modify_sl(self, execution_id: str, **kwargs) -> dict:
        raise NotImplementedError

    def modify_target(self, execution_id: str, **kwargs) -> dict:
        raise NotImplementedError

    def exit_position(self, execution_id: str, **kwargs) -> dict:
        raise NotImplementedError


class NullBrokerExecutor(BrokerExecutor):
    """The ONLY BrokerExecutor implementation that exists in this
    codebase. Every method is a pure, side-effect-free no-op: no
    network call, no broker session, no order of any kind -- just an
    honest {"status": "DISABLED", ...} result, so a caller can log
    exactly what WOULD have been attempted without anything real ever
    happening."""

    def _disabled(self, action: str, execution_id: str, **kwargs) -> dict:
        return {
            "status": "DISABLED", "action": action, "execution_id": execution_id,
            "broker_order_id": None, "reason": "NullBrokerExecutor -- live execution is not enabled",
            "requested": kwargs,
        }

    def place_entry(self, execution_id: str, **kwargs) -> dict:
        return self._disabled("place_entry", execution_id, **kwargs)

    def place_sl(self, execution_id: str, **kwargs) -> dict:
        return self._disabled("place_sl", execution_id, **kwargs)

    def place_target(self, execution_id: str, **kwargs) -> dict:
        return self._disabled("place_target", execution_id, **kwargs)

    def modify_sl(self, execution_id: str, **kwargs) -> dict:
        return self._disabled("modify_sl", execution_id, **kwargs)

    def modify_target(self, execution_id: str, **kwargs) -> dict:
        return self._disabled("modify_target", execution_id, **kwargs)

    def exit_position(self, execution_id: str, **kwargs) -> dict:
        return self._disabled("exit_position", execution_id, **kwargs)


def get_broker_executor() -> BrokerExecutor:
    """The one place any future caller obtains a BrokerExecutor.
    Unconditionally returns NullBrokerExecutor -- there is no flag or
    branch here that could ever return anything else in this PR. A
    future, separately-approved phase introducing a real adapter would
    change ONLY this function (gated behind the explicit, multi-layer
    flag hierarchy from that phase's own design -- AUTONOMOUS_TRADING_
    ENABLED / LIVE_EXECUTION_ENABLED / per-admin-pilot / per-instrument
    -- none of which exist yet, deliberately, since this phase is
    advisory/persisted-only)."""
    return NullBrokerExecutor()
