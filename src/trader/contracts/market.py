"""Point-in-time market calendar and data boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Protocol

import pandas as pd


class MarketCalendar(Protocol):
    def is_session(self, day: date) -> bool: ...

    def session_close(self, day: date) -> datetime: ...

    def prev_session(self, day: date) -> date | None: ...


class MarketData(Protocol):
    def bars_1m(
        self,
        symbol: str,
        *,
        asof: datetime,
        lookback_minutes: int | None = None,
    ) -> pd.DataFrame: ...

    def bars_1d(
        self, symbol: str, *, asof: date, lookback_days: int
    ) -> pd.DataFrame: ...

    def signal(
        self,
        name: str,
        *,
        asof: datetime,
        params: Mapping[str, object] | None = None,
    ) -> float: ...

    def event(self, kind: str, *, asof: datetime) -> dict | None: ...

    def calendar(self) -> MarketCalendar: ...


__all__ = ["MarketCalendar", "MarketData"]
