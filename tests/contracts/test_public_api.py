"""Contract tests for the trader.contracts public import surface."""

import importlib


PUBLIC_NAMES = {
    "Algo",
    "AlgoSpec",
    "AlgoStatus",
    "Bar",
    "Broker",
    "BrokerNotConfigured",
    "Clock",
    "ContractViolation",
    "Fill",
    "Intent",
    "LookaheadError",
    "MarketCalendar",
    "MarketData",
    "Mode",
    "OrderTicket",
    "PortfolioState",
    "PositionState",
    "Rejection",
    "RiskEngine",
    "Side",
}

MODULE_EXPORTS = {
    "algo": {"Algo", "AlgoSpec"},
    "broker": {"Broker"},
    "clock": {"Clock"},
    "errors": {"BrokerNotConfigured", "ContractViolation", "LookaheadError"},
    "intents": {"Intent"},
    "market": {"MarketCalendar", "MarketData"},
    "orders": {
        "Fill",
        "OrderTicket",
        "PortfolioState",
        "PositionState",
        "Rejection",
    },
    "risk": {"RiskEngine"},
    "types": {"AlgoStatus", "Bar", "Mode", "Side"},
}


def test_package_reexports_every_public_contract_name() -> None:
    package = importlib.import_module("trader.contracts")

    assert set(package.__all__) == PUBLIC_NAMES
    for module_name, exported_names in MODULE_EXPORTS.items():
        module = importlib.import_module(f"trader.contracts.{module_name}")
        assert set(module.__all__) == exported_names
        for name in exported_names:
            assert getattr(package, name) is getattr(module, name)
