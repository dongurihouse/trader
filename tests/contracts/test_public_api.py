"""Contract tests for the trader.contracts public import surface."""

import importlib


PUBLIC_NAMES = {
    "Algo",
    "AlgoErrorEvent",
    "AlgoMetrics",
    "AlgoSpec",
    "AlgoStatus",
    "Bar",
    "Broker",
    "BrokerNotConfigured",
    "Clock",
    "ContractViolation",
    "EVENT_TYPES",
    "Fill",
    "FillEvent",
    "Intent",
    "IntentEvent",
    "LookaheadError",
    "MarketCalendar",
    "MarketData",
    "MetricsEvent",
    "Mode",
    "OrderTicket",
    "PortfolioState",
    "PositionClosedEvent",
    "PositionState",
    "Rejection",
    "RejectionEvent",
    "RiskEngine",
    "SessionEndEvent",
    "SessionStartEvent",
    "Side",
    "TelemetryWriter",
    "TickEvent",
    "TicketEvent",
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
    "telemetry": {
        "AlgoErrorEvent",
        "AlgoMetrics",
        "EVENT_TYPES",
        "FillEvent",
        "IntentEvent",
        "MetricsEvent",
        "PositionClosedEvent",
        "RejectionEvent",
        "SessionEndEvent",
        "SessionStartEvent",
        "TelemetryWriter",
        "TickEvent",
        "TicketEvent",
    },
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
