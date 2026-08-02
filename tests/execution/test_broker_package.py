"""Tests for the public broker package surface."""


def test_package_reexports_brokers_and_sim_helpers() -> None:
    from trader.execution.broker import (
        ApiBroker,
        ManualBroker,
        SimBroker,
        apply_slippage,
        check_exit,
        slippage_bps_for,
    )
    from trader.execution.broker.api import ApiBroker as ApiBrokerImplementation
    from trader.execution.broker.manual import (
        ManualBroker as ManualBrokerImplementation,
    )
    from trader.execution.broker.sim import (
        SimBroker as SimBrokerImplementation,
        apply_slippage as apply_slippage_implementation,
        check_exit as check_exit_implementation,
        slippage_bps_for as slippage_bps_for_implementation,
    )

    assert ApiBroker is ApiBrokerImplementation
    assert ManualBroker is ManualBrokerImplementation
    assert SimBroker is SimBrokerImplementation
    assert apply_slippage is apply_slippage_implementation
    assert check_exit is check_exit_implementation
    assert slippage_bps_for is slippage_bps_for_implementation
