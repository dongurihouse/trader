"""Point-in-time market-data test fake."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from trader.contracts.errors import LookaheadError
from trader.contracts.market import MarketCalendar


_EASTERN = ZoneInfo("America/New_York")


class _SyntheticCalendar:
    def __init__(self, session_days: set[date]) -> None:
        self._session_days = tuple(sorted(session_days))

    def is_session(self, day: date) -> bool:
        return day in self._session_days

    def session_close(self, day: date) -> datetime:
        eastern_close = datetime.combine(day, time(16), tzinfo=_EASTERN)
        return eastern_close.astimezone(timezone.utc)

    def prev_session(self, day: date) -> date | None:
        prior_days = [session_day for session_day in self._session_days if session_day < day]
        return prior_days[-1] if prior_days else None


class FakeMarketData:
    """Serve fixed OHLCV frames and signal values with real PIT enforcement."""

    def __init__(
        self,
        frames: Mapping[str, pd.DataFrame],
        signals: Mapping[str, float] | None = None,
    ) -> None:
        self._frames = {
            symbol: frame.copy(deep=True).sort_index()
            for symbol, frame in frames.items()
        }
        self._signals = dict(signals or {})
        session_days = {
            timestamp.date()
            for frame in self._frames.values()
            for timestamp in frame.index
        }
        self._calendar = _SyntheticCalendar(session_days)

    def bars_1m(
        self,
        symbol: str,
        *,
        asof: datetime,
        lookback_minutes: int | None = None,
    ) -> pd.DataFrame:
        frame = self._frames[symbol]
        last_completion = frame.index.max() + timedelta(minutes=1)
        if asof > last_completion:
            raise LookaheadError(
                f"{symbol} minute data is known only through {last_completion}"
            )

        visible = frame.loc[frame.index + timedelta(minutes=1) <= asof]
        if lookback_minutes is not None:
            visible = visible.tail(lookback_minutes)
        return visible.copy(deep=True)

    def bars_1d(
        self,
        symbol: str,
        *,
        asof: date,
        lookback_days: int,
    ) -> pd.DataFrame:
        frame = self._frames[symbol]
        frame_days = frame.index.date
        last_known_day = max(frame_days)
        if asof > last_known_day + timedelta(days=1):
            raise LookaheadError(
                f"{symbol} daily data is known only through {last_known_day}"
            )

        daily = frame.groupby(frame_days, sort=True).agg(
            {"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"}
        )
        eligible = daily.loc[[day < asof for day in daily.index]]
        return eligible.tail(lookback_days).copy(deep=True)

    def signal(
        self,
        name: str,
        *,
        asof: datetime,
        params: Mapping[str, object] | None = None,
    ) -> float:
        return self._signals[name]

    def event(self, kind: str, *, asof: datetime) -> dict | None:
        """Return None because this fake has no event data source."""
        return None

    def calendar(self) -> MarketCalendar:
        return self._calendar


__all__ = ["FakeMarketData"]
