"""Behavioral tests for clocks, telemetry, and broker test doubles."""

from datetime import datetime, timedelta, timezone
import time

import pandas as pd
import pytest

from trader.contracts.orders import Fill, OrderTicket
from trader.contracts.testing import (
    CollectingTelemetry,
    FakeBroker,
    FakeClock,
    FakeMarketData,
)


_FIRST_BAR = datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)


def _frame(opens: list[float]) -> pd.DataFrame:
    index = pd.date_range(_FIRST_BAR, periods=len(opens), freq="min", name="ts")
    return pd.DataFrame(
        {
            "o": opens,
            "h": [price + 0.50 for price in opens],
            "l": [price - 0.50 for price in opens],
            "c": [price + 0.25 for price in opens],
            "v": [1_000 + position for position in range(len(opens))],
        },
        index=index,
    )


def _ticket(
    ticket_id: str,
    instrument: str,
    shares: int,
) -> OrderTicket:
    return OrderTicket(
        ticket_id=ticket_id,
        algo_id="breakout",
        intent_ts=_FIRST_BAR - timedelta(minutes=1),
        instrument=instrument,
        side="long",
        shares=shares,
        entry="market_next_open",
        stop=9.0,
        target=12.0,
        risk={"slot": 1, "dollars": 100.0, "equity": 100_000.0},
        created_ts=_FIRST_BAR,
    )


def test_fake_clock_is_non_live_and_moves_forward_without_real_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)
    clock = FakeClock(start)

    def fail_if_called(_seconds: float) -> None:
        raise AssertionError("FakeClock must not sleep on wall-clock time")

    monkeypatch.setattr(time, "sleep", fail_if_called)

    assert clock.live is False
    assert clock.now() == start
    clock.sleep_until(start)
    assert clock.now() == start

    later = start + timedelta(minutes=5)
    assert clock.sleep_until(later) is None
    assert clock.now() == later

    with pytest.raises(ValueError):
        clock.sleep_until(later - timedelta(microseconds=1))
    assert clock.now() == later


def test_fake_clock_advance_moves_by_exact_delta() -> None:
    start = datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)
    clock = FakeClock(start)

    assert clock.advance(timedelta(seconds=90)) is None
    assert clock.now() == start + timedelta(seconds=90)


def test_collecting_telemetry_preserves_every_record_in_call_order() -> None:
    telemetry = CollectingTelemetry()
    records = [
        {"ev": "session_start", "session": "fixture"},
        {"ev": "tick", "bar_ts": "2026-07-01T13:30:00Z"},
        {"ev": "session_end", "bars_processed": 1},
    ]

    assert telemetry.records == []
    for record in records:
        assert telemetry.emit(record) is None

    assert telemetry.records == records


def test_fake_broker_waits_for_a_completed_bar_then_fills_once_at_raw_open() -> None:
    data = FakeMarketData({"AAA": _frame([10.0, 10.5, 11.0])})
    broker = FakeBroker()
    ticket = _ticket("ticket-1", "AAA", shares=37)
    broker.submit(ticket)

    assert broker.on_bar(_FIRST_BAR, data) == []
    assert broker.on_bar(_FIRST_BAR + timedelta(minutes=1), data) == [
        Fill(
            ticket_id="ticket-1",
            ts=_FIRST_BAR + timedelta(minutes=1),
            price=10.0,
            shares=37,
            kind="entry",
            book="real",
            price_basis="real",
        )
    ]
    assert broker.on_bar(_FIRST_BAR + timedelta(minutes=2), data) == []


def test_fake_broker_fills_all_pending_tickets_with_instrument_specific_opens() -> None:
    data = FakeMarketData(
        {
            "AAA": _frame([10.0, 10.5]),
            "BBB": _frame([25.0, 25.5]),
        }
    )
    broker = FakeBroker()
    broker.submit(_ticket("ticket-a", "AAA", shares=10))
    broker.submit(_ticket("ticket-b", "BBB", shares=23))

    assert broker.on_bar(_FIRST_BAR, data) == []
    assert broker.on_bar(_FIRST_BAR + timedelta(minutes=1), data) == [
        Fill(
            ticket_id="ticket-a",
            ts=_FIRST_BAR + timedelta(minutes=1),
            price=10.0,
            shares=10,
            kind="entry",
            book="real",
            price_basis="real",
        ),
        Fill(
            ticket_id="ticket-b",
            ts=_FIRST_BAR + timedelta(minutes=1),
            price=25.0,
            shares=23,
            kind="entry",
            book="real",
            price_basis="real",
        ),
    ]


def test_fake_broker_cancel_open_discards_pending_tickets_without_fills() -> None:
    data = FakeMarketData({"AAA": _frame([10.0, 10.5])})
    broker = FakeBroker()
    broker.submit(_ticket("cancelled", "AAA", shares=5))

    broker.cancel_open("session ended")

    assert broker.on_bar(_FIRST_BAR + timedelta(minutes=1), data) == []
