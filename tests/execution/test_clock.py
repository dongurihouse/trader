"""Tests for production execution clocks."""

from datetime import datetime, timedelta, timezone

import pytest

from trader.contracts import Clock
from trader.execution.clock import BacktestClock, LiveClock


START = datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)


def test_backtest_clock_starts_non_live_and_advances_by_exact_delta() -> None:
    clock = BacktestClock(START)

    assert clock.live is False
    assert clock.now() == START

    assert clock.advance(timedelta(seconds=90)) is None
    assert clock.now() == datetime(2026, 7, 1, 13, 31, 30, tzinfo=timezone.utc)


def test_backtest_clock_sleep_until_moves_to_later_time_exactly() -> None:
    clock = BacktestClock(START)
    deadline = datetime(2026, 7, 1, 13, 35, tzinfo=timezone.utc)

    assert clock.sleep_until(deadline) is None
    assert clock.now() == deadline


def test_backtest_clock_rejects_moving_backwards_without_changing_time() -> None:
    clock = BacktestClock(START)
    deadline = datetime(2026, 7, 1, 13, 35, tzinfo=timezone.utc)
    clock.sleep_until(deadline)

    with pytest.raises(ValueError):
        clock.sleep_until(deadline - timedelta(microseconds=1))

    assert clock.now() == deadline


def test_live_clock_sleeps_for_future_deadline_using_injected_functions() -> None:
    sleep_calls: list[float] = []
    clock = LiveClock(sleep=sleep_calls.append, now_fn=lambda: START)
    deadline = datetime(2026, 7, 1, 13, 31, 30, tzinfo=timezone.utc)

    assert clock.live is True
    assert clock.now() == START
    assert clock.sleep_until(deadline) is None
    assert sleep_calls == [90.0]


@pytest.mark.parametrize("offset", [timedelta(0), -timedelta(seconds=1)])
def test_live_clock_does_not_sleep_when_deadline_is_not_in_future(
    offset: timedelta,
) -> None:
    sleep_calls: list[float] = []
    clock = LiveClock(sleep=sleep_calls.append, now_fn=lambda: START)

    assert clock.sleep_until(START + offset) is None
    assert sleep_calls == []


def test_live_clock_defaults_are_constructible_and_now_is_aware_utc() -> None:
    clock = LiveClock()

    current = clock.now()

    assert isinstance(current, datetime)
    assert current.tzinfo is not None
    assert current.utcoffset() == timedelta(0)


def test_clocks_structurally_satisfy_clock_protocol() -> None:
    clocks: list[Clock] = [
        BacktestClock(START),
        LiveClock(sleep=lambda _seconds: None, now_fn=lambda: START),
    ]

    for clock in clocks:
        current = clock.now()
        assert isinstance(clock.live, bool)
        assert isinstance(current, datetime)
        assert clock.sleep_until(current) is None
