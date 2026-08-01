"""Contract tests for clock and market protocols."""

from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
import importlib
import inspect
from typing import get_type_hints

import pandas as pd
import pytest


def _clock_type():
    return importlib.import_module("trader.contracts.clock").Clock


def _market_types():
    module = importlib.import_module("trader.contracts.market")
    return module.MarketCalendar, module.MarketData


def test_clock_is_a_protocol_with_exact_interface() -> None:
    clock_type = _clock_type()

    assert clock_type._is_protocol is True
    with pytest.raises(TypeError, match="Protocols cannot be instantiated"):
        clock_type()
    assert isinstance(clock_type.live, property)
    assert get_type_hints(clock_type.live.fget) == {"return": bool}
    assert get_type_hints(clock_type.now) == {"return": datetime}
    assert get_type_hints(clock_type.sleep_until) == {
        "when": datetime,
        "return": type(None),
    }


def test_clock_protocol_can_be_implemented_and_used() -> None:
    clock_type = _clock_type()
    start = datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)

    class FixedClock(clock_type):
        def __init__(self, current: datetime) -> None:
            self.current = current

        @property
        def live(self) -> bool:
            return False

        def now(self) -> datetime:
            return self.current

        def sleep_until(self, when: datetime) -> None:
            self.current = when

    clock = FixedClock(start)
    next_minute = start + timedelta(minutes=1)
    clock.sleep_until(next_minute)

    assert clock.live is False
    assert clock.now() == next_minute


def test_market_calendar_is_a_protocol_with_exact_interface() -> None:
    calendar_type, _ = _market_types()

    assert calendar_type._is_protocol is True
    with pytest.raises(TypeError, match="Protocols cannot be instantiated"):
        calendar_type()
    assert get_type_hints(calendar_type.is_session) == {
        "day": date,
        "return": bool,
    }
    assert get_type_hints(calendar_type.session_close) == {
        "day": date,
        "return": datetime,
    }
    assert get_type_hints(calendar_type.prev_session) == {
        "day": date,
        "return": date | None,
    }


def test_market_data_is_a_protocol_with_exact_interface() -> None:
    calendar_type, market_data_type = _market_types()

    assert market_data_type._is_protocol is True
    with pytest.raises(TypeError, match="Protocols cannot be instantiated"):
        market_data_type()
    assert get_type_hints(market_data_type.bars_1m) == {
        "symbol": str,
        "asof": datetime,
        "lookback_minutes": int | None,
        "return": pd.DataFrame,
    }
    assert get_type_hints(market_data_type.bars_1d) == {
        "symbol": str,
        "asof": date,
        "lookback_days": int,
        "return": pd.DataFrame,
    }
    assert get_type_hints(market_data_type.signal) == {
        "name": str,
        "asof": datetime,
        "params": Mapping[str, object] | None,
        "return": float,
    }
    assert get_type_hints(market_data_type.event) == {
        "kind": str,
        "asof": datetime,
        "return": dict | None,
    }
    assert get_type_hints(market_data_type.calendar) == {"return": calendar_type}

    for method_name in ("bars_1m", "bars_1d", "signal", "event"):
        signature = inspect.signature(getattr(market_data_type, method_name))
        assert signature.parameters["asof"].kind is inspect.Parameter.KEYWORD_ONLY


def test_market_protocols_can_be_implemented_and_used() -> None:
    calendar_type, market_data_type = _market_types()
    session_day = date(2026, 7, 1)
    asof = datetime(2026, 7, 1, 13, 31, tzinfo=timezone.utc)
    frame = pd.DataFrame(
        [{"o": 42.0, "h": 43.5, "l": 41.5, "c": 43.0, "v": 12_345.0}],
        index=pd.DatetimeIndex([asof - timedelta(minutes=1)], name="ts"),
    )

    class SimpleCalendar(calendar_type):
        def is_session(self, day: date) -> bool:
            return day == session_day

        def session_close(self, day: date) -> datetime:
            return datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc)

        def prev_session(self, day: date) -> date | None:
            return date(2026, 6, 30)

    calendar = SimpleCalendar()

    class SimpleMarketData(market_data_type):
        def bars_1m(
            self,
            symbol: str,
            *,
            asof: datetime,
            lookback_minutes: int | None = None,
        ) -> pd.DataFrame:
            return frame

        def bars_1d(
            self, symbol: str, *, asof: date, lookback_days: int
        ) -> pd.DataFrame:
            return frame

        def signal(
            self,
            name: str,
            *,
            asof: datetime,
            params: Mapping[str, object] | None = None,
        ) -> float:
            return 0.75

        def event(self, kind: str, *, asof: datetime) -> dict | None:
            return {"kind": kind}

        def calendar(self):
            return calendar

    data = SimpleMarketData()

    assert calendar.is_session(session_day) is True
    assert calendar.prev_session(session_day) == date(2026, 6, 30)
    assert data.bars_1m("SNDK", asof=asof).equals(frame)
    assert data.bars_1d("SNDK", asof=session_day, lookback_days=1).equals(frame)
    assert data.signal("momentum", asof=asof) == 0.75
    assert data.event("earnings_proximity", asof=asof) == {
        "kind": "earnings_proximity"
    }
    assert data.calendar() is calendar
