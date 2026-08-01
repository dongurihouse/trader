"""Contract tests for order, fill, rejection, and portfolio records."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import importlib
from typing import Literal, get_args, get_type_hints

import pytest


def _orders_module():
    return importlib.import_module("trader.contracts.orders")


def _sample_intent():
    intent_type = importlib.import_module("trader.contracts.intents").Intent
    return intent_type(
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


def _sample_ticket():
    return _orders_module().OrderTicket(
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


def test_order_ticket_constructs_with_exact_fields_and_values() -> None:
    ticket = _sample_ticket()

    assert vars(ticket) == {
        "ticket_id": "ticket-001",
        "algo_id": "breakout",
        "intent_ts": datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc),
        "instrument": "SNXX",
        "side": "long",
        "shares": 100,
        "entry": "market_next_open",
        "stop": 41.0,
        "target": 46.0,
        "risk": {"slot": 1, "dollars": 200.0, "equity": 100_000.0},
        "created_ts": datetime(2026, 7, 1, 13, 30, 5, tzinfo=timezone.utc),
    }


def test_order_ticket_has_exact_field_types_and_is_frozen() -> None:
    ticket_type = _orders_module().OrderTicket
    side_type = importlib.import_module("trader.contracts.types").Side

    assert get_type_hints(ticket_type) == {
        "ticket_id": str,
        "algo_id": str,
        "intent_ts": datetime,
        "instrument": str,
        "side": side_type,
        "shares": int,
        "entry": Literal["market_next_open"],
        "stop": float | None,
        "target": float | None,
        "risk": dict,
        "created_ts": datetime,
    }
    assert get_args(get_type_hints(ticket_type)["entry"]) == ("market_next_open",)

    with pytest.raises(FrozenInstanceError):
        _sample_ticket().shares = 101


def test_fill_constructs_with_exact_fields_types_and_literal_values() -> None:
    fill_type = _orders_module().Fill
    fill = fill_type(
        ticket_id="ticket-001",
        ts=datetime(2026, 7, 1, 13, 31, tzinfo=timezone.utc),
        price=43.25,
        shares=100,
        kind="entry",
        book="real",
    )

    assert vars(fill) == {
        "ticket_id": "ticket-001",
        "ts": datetime(2026, 7, 1, 13, 31, tzinfo=timezone.utc),
        "price": 43.25,
        "shares": 100,
        "kind": "entry",
        "book": "real",
    }
    hints = get_type_hints(fill_type)
    assert hints == {
        "ticket_id": str,
        "ts": datetime,
        "price": float,
        "shares": int,
        "kind": Literal["entry", "stop", "target", "reversal", "eod"],
        "book": Literal["real", "shadow"],
    }
    assert get_args(hints["kind"]) == (
        "entry",
        "stop",
        "target",
        "reversal",
        "eod",
    )
    assert get_args(hints["book"]) == ("real", "shadow")


def test_fill_is_frozen() -> None:
    fill = _orders_module().Fill(
        ticket_id="ticket-001",
        ts=datetime(2026, 7, 1, 13, 31, tzinfo=timezone.utc),
        price=43.25,
        shares=100,
        kind="entry",
        book="real",
    )

    with pytest.raises(FrozenInstanceError):
        fill.price = 44.0


def test_rejection_constructs_with_exact_fields_types_and_is_frozen() -> None:
    rejection_type = _orders_module().Rejection
    intent_type = importlib.import_module("trader.contracts.intents").Intent
    intent = _sample_intent()
    rejection = rejection_type(
        intent=intent,
        rule="daily_entry_cap",
        detail="three entries already submitted today",
    )

    assert vars(rejection) == {
        "intent": intent,
        "rule": "daily_entry_cap",
        "detail": "three entries already submitted today",
    }
    assert get_type_hints(rejection_type) == {
        "intent": intent_type,
        "rule": str,
        "detail": str,
    }

    with pytest.raises(FrozenInstanceError):
        rejection.rule = "muted"


def test_position_state_constructs_with_exact_fields_types_and_is_mutable() -> None:
    position_type = _orders_module().PositionState
    side_type = importlib.import_module("trader.contracts.types").Side
    position = position_type(
        instrument="SNXX",
        side="long",
        shares=100,
        entry_price=43.25,
        entry_ts=datetime(2026, 7, 1, 13, 31, tzinfo=timezone.utc),
        stop=41.0,
        target=46.0,
        algo_id="breakout",
    )

    assert vars(position) == {
        "instrument": "SNXX",
        "side": "long",
        "shares": 100,
        "entry_price": 43.25,
        "entry_ts": datetime(2026, 7, 1, 13, 31, tzinfo=timezone.utc),
        "stop": 41.0,
        "target": 46.0,
        "algo_id": "breakout",
    }
    assert get_type_hints(position_type) == {
        "instrument": str,
        "side": side_type,
        "shares": int,
        "entry_price": float,
        "entry_ts": datetime,
        "stop": float | None,
        "target": float | None,
        "algo_id": str,
    }

    position.shares = 50
    assert position.shares == 50


def test_portfolio_state_constructs_with_exact_fields_types_and_is_mutable() -> None:
    orders = _orders_module()
    position = orders.PositionState(
        instrument="SNXX",
        side="long",
        shares=100,
        entry_price=43.25,
        entry_ts=datetime(2026, 7, 1, 13, 31, tzinfo=timezone.utc),
        stop=41.0,
        target=46.0,
        algo_id="breakout",
    )
    muted_until = datetime(2026, 7, 1, 15, 0, tzinfo=timezone.utc)
    portfolio = orders.PortfolioState(
        cash=95_675.0,
        equity=100_000.0,
        positions=[position],
        entries_today=1,
        realized_r_today=-0.5,
        muted_until=muted_until,
    )

    assert vars(portfolio) == {
        "cash": 95_675.0,
        "equity": 100_000.0,
        "positions": [position],
        "entries_today": 1,
        "realized_r_today": -0.5,
        "muted_until": muted_until,
    }
    assert get_type_hints(orders.PortfolioState) == {
        "cash": float,
        "equity": float,
        "positions": list[orders.PositionState],
        "entries_today": int,
        "realized_r_today": float,
        "muted_until": datetime | None,
    }

    portfolio.cash = 96_000.0
    assert portfolio.cash == 96_000.0
