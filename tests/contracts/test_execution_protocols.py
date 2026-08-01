"""Contract tests for broker and risk engine protocols."""

from datetime import datetime, timezone
import importlib
from typing import get_type_hints

import pytest


def _broker_type():
    return importlib.import_module("trader.contracts.broker").Broker


def _risk_engine_type():
    return importlib.import_module("trader.contracts.risk").RiskEngine


def _sample_ticket():
    return importlib.import_module("trader.contracts.orders").OrderTicket(
        ticket_id="ticket-001",
        algo_id="breakout",
        intent_ts=datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc),
        instrument="SNXX",
        side="long",
        shares=100,
        entry="market_next_open",
        stop=41.0,
        target=46.0,
        risk={"slot": 1, "dollars": 200.0, "equity": 100_000.0},
        created_ts=datetime(2026, 7, 1, 13, 30, 5, tzinfo=timezone.utc),
    )


def _sample_intent():
    return importlib.import_module("trader.contracts.intents").Intent(
        algo_id="breakout",
        ts=datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc),
        action="open",
        side="long",
        signal_symbol="SNDK",
        instrument="SNXX",
        entry="market_next_open",
        stop=41.0,
        target=46.0,
        confidence=0.8,
        reason="price cleared the opening range",
        meta={"setup": "opening_range"},
    )


def test_broker_is_a_protocol_with_exact_interface() -> None:
    broker_type = _broker_type()
    orders = importlib.import_module("trader.contracts.orders")
    market_data_type = importlib.import_module("trader.contracts.market").MarketData

    assert broker_type._is_protocol is True
    with pytest.raises(TypeError, match="Protocols cannot be instantiated"):
        broker_type()
    assert get_type_hints(broker_type.submit) == {
        "ticket": orders.OrderTicket,
        "return": type(None),
    }
    assert get_type_hints(broker_type.on_bar) == {
        "asof": datetime,
        "data": market_data_type,
        "return": list[orders.Fill],
    }
    assert get_type_hints(broker_type.cancel_open) == {
        "reason": str,
        "return": type(None),
    }


def test_broker_protocol_can_be_implemented_and_used() -> None:
    broker_type = _broker_type()
    ticket = _sample_ticket()

    class RecordingBroker(broker_type):
        def __init__(self) -> None:
            self.submitted = []
            self.cancel_reason = None

        def submit(self, ticket) -> None:
            self.submitted.append(ticket)

        def on_bar(self, asof: datetime, data: object) -> list:
            return []

        def cancel_open(self, reason: str) -> None:
            self.cancel_reason = reason

    broker = RecordingBroker()
    broker.submit(ticket)
    broker.cancel_open("session close")

    assert broker.submitted == [ticket]
    assert broker.on_bar(ticket.created_ts, object()) == []
    assert broker.cancel_reason == "session close"


def test_risk_engine_is_a_protocol_with_exact_interface() -> None:
    risk_engine_type = _risk_engine_type()
    intent_type = importlib.import_module("trader.contracts.intents").Intent
    orders = importlib.import_module("trader.contracts.orders")
    market_data_type = importlib.import_module("trader.contracts.market").MarketData

    assert risk_engine_type._is_protocol is True
    with pytest.raises(TypeError, match="Protocols cannot be instantiated"):
        risk_engine_type()
    assert get_type_hints(risk_engine_type.check_and_size) == {
        "intent": intent_type,
        "portfolio": orders.PortfolioState,
        "data": market_data_type,
        "return": orders.OrderTicket | orders.Rejection,
    }


def test_risk_engine_protocol_can_be_implemented_and_used() -> None:
    risk_engine_type = _risk_engine_type()
    orders = importlib.import_module("trader.contracts.orders")
    intent = _sample_intent()
    portfolio = orders.PortfolioState(
        cash=100_000.0,
        equity=100_000.0,
        positions=[],
        entries_today=3,
        realized_r_today=0.0,
        muted_until=None,
    )
    rejection = orders.Rejection(
        intent=intent,
        rule="daily_entry_cap",
        detail="three entries already submitted today",
    )

    class RejectingRiskEngine(risk_engine_type):
        def check_and_size(self, intent, portfolio, data):
            return rejection

    result = RejectingRiskEngine().check_and_size(intent, portfolio, object())

    assert result == rejection
